"""Free-text objective planner: existing executor role, validated plan, no
second orchestrator. Covers schema/policy fail-closed, human-gate
enforcement, idempotency, no chain-of-thought persistence, and
planner-unavailable resume.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.execution.results import (
    parse_executor_result,
    result_file_contract_for_role,
    sanitize_result_mapping,
)
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildObjective,
    BuildObjectiveEvent,
    BuildObjectiveGate,
    BuildObjectivePlan,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.objectives import (
    apply_validated_plan,
    create_objective,
    objective_work_tasks,
    open_gates,
    planner_status,
)
from build_coordinator.planner import parse_planner_plan, planner_task_id
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.prompts import PlannerPromptBuilder
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import ProviderConfig
from build_coordinator.types import (
    ObjectivePlan,
    ObjectiveSpec,
    PlannedChildTask,
    StructuredContractError,
)


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildObjectiveEvent,
            BuildObjectiveGate,
            BuildObjectivePlan,
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildObjective,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _child(task_id, **kwargs):
    return PlannedChildTask(
        task_id=task_id,
        title=kwargs.pop("title", f"Task {task_id}"),
        description=kwargs.pop("description", "planned work"),
        goal=kwargs.pop("goal", f"Goal for {task_id}"),
        **kwargs,
    )


def _plan(*task_ids, **kwargs):
    return ObjectivePlan(tasks=tuple(_child(task_id) for task_id in task_ids), **kwargs)


def _spec(objective_id="OBJ-PLAN", *, child_tasks=()):
    return ObjectiveSpec(
        objective_id=objective_id,
        goal="Evaluate the best document-intelligence stack for the product using our benchmark.",
        child_tasks=tuple(child_tasks),
    )


def _config(*, auto_push=False, planner_adapter="fake"):
    return RunnerConfig(
        workers=(
            WorkerConfig("planner-1", "PLANNER", adapter=planner_adapter),
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("builder-b", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        auto_push_allowed=auto_push,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(config=None, executors=None):
    return BuildRunner(SessionLocal, config or _config(), executors=executors, git=FakeGit())


def _valid_plan_payload():
    return {
        "tasks": [
            {
                "task_id": "OBJ-PLAN-A",
                "title": "Evaluate candidate A",
                "goal": "Score candidate A on the Labs benchmark",
                "description": "Run the document-intelligence benchmark against candidate A",
                "scope": ["docs/evaluations/**"],
                "prohibited_scope": ["apps/api/**"],
                "dependencies": [],
                "parallel_safe": True,
                "risk_level": "LOW",
                "reason_created": "OBJECTIVE_PLAN",
            },
            {
                "task_id": "OBJ-PLAN-B",
                "title": "Evaluate candidate B",
                "goal": "Score candidate B on the Labs benchmark",
                "description": "Run the document-intelligence benchmark against candidate B",
                "scope": ["docs/evaluations/**"],
                "parallel_safe": True,
                "risk_level": "LOW",
                "reason_created": "OBJECTIVE_PLAN",
            },
        ]
    }


def _planner_success_observation(plan=None):
    return ExecutionObservation("SUCCEEDED", result_data={"plan": plan or _valid_plan_payload()})


def test_planner_contract_requires_full_executor_envelope():
    contract = result_file_contract_for_role("PLANNER")

    assert contract["required_top_level_fields"] == [
        "schema_version",
        "execution_id",
        "task_id",
        "role",
        "status",
        "plan",
    ]
    assert "full executor-result envelope" in contract["plan"]["instructions"]
    assert "FULL executor-result JSON object" in PlannerPromptBuilder.role_policy
    assert "Do NOT write a bare ObjectivePlan object" in PlannerPromptBuilder.role_policy


# -- free-text invokes planner ---------------------------------------------------


def test_free_text_objective_invokes_planner_via_existing_executor():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    executors = {"planner-1": FakeExecutor([_planner_success_observation()])}
    runner = _runner(executors=executors)
    launched = runner.run_once()
    assert launched.launched

    observed = runner.run_once()
    assert observed.observed

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-PLAN")
        assert objective.state in {"ACTIVE", "WAITING_ON_TASKS"}
        work = objective_work_tasks(session, "OBJ-PLAN")
        assert {task.task_id for task in work} == {"OBJ-PLAN-A", "OBJ-PLAN-B"}
        plan = session.scalar(select(BuildObjectivePlan).where(BuildObjectivePlan.objective_id == "OBJ-PLAN"))
        assert plan.source == "PLANNER"
        execution = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "PLANNER")
        )
        assert execution is not None
        assert execution.adapter == "fake"
        assert planner_status(session, objective) == "APPLIED"


def test_valid_planner_plan_creates_child_tasks():
    with SessionLocal() as session:
        objective = create_objective(session, _spec())
        plan = parse_planner_plan(_valid_plan_payload())
        apply_validated_plan(session, objective, plan)
        session.commit()

    with SessionLocal() as session:
        work = objective_work_tasks(session, "OBJ-PLAN")
        assert len(work) == 2
        assert all(task.reason_created == "OBJECTIVE_PLAN" for task in work)


def test_structured_plan_input_still_works_without_planner():
    with SessionLocal() as session:
        objective = create_objective(
            session,
            _spec(child_tasks=[_child("OBJ-PLAN-A"), _child("OBJ-PLAN-B")]),
        )
        session.commit()
        assert objective.state == "ACTIVE"

    with SessionLocal() as session:
        work = objective_work_tasks(session, "OBJ-PLAN")
        assert {task.task_id for task in work} == {"OBJ-PLAN-A", "OBJ-PLAN-B"}
        assert session.get(BuildTask, planner_task_id("OBJ-PLAN")) is None


# -- fail closed -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, match",
    [
        ({"tasks": [{"title": "missing id"}]}, "task_id"),
        ({"tasks": [_valid_plan_payload()["tasks"][0]], "worktree_path": "C:/evil"}, "unknown"),
        (
            {
                "tasks": [
                    {
                        **_valid_plan_payload()["tasks"][0],
                        "auto_push_allowed": True,
                    }
                ]
            },
            "unknown",
        ),
        ({"tasks": "not-a-list"}, "must be a list"),
        ({"child_tasks": [], "tasks": []}, "both tasks and child_tasks"),
        ({"tasks": []}, "at least one"),
    ],
)
def test_malformed_planner_output_fails_closed(payload, match):
    with pytest.raises(StructuredContractError, match=match):
        parse_planner_plan(payload)


def test_unknown_task_field_fails_closed():
    payload = {
        "tasks": [
            {
                **_valid_plan_payload()["tasks"][0],
                "worktree": "C:/example-dev-a",
            }
        ]
    }
    with pytest.raises(StructuredContractError, match="unknown planned task field"):
        parse_planner_plan(payload)


def test_planner_cannot_choose_arbitrary_worktree():
    payload = {"tasks": _valid_plan_payload()["tasks"], "worktree_path": "C:/not-allowed"}
    with pytest.raises(StructuredContractError, match="unknown objective plan field"):
        parse_planner_plan(payload)


def test_planner_human_gate_objects_fail_closed():
    payload = {
        "tasks": _valid_plan_payload()["tasks"],
        "requested_human_gates": [{"gate_type": "DECISION", "title": "pick a stack"}],
    }
    with pytest.raises(StructuredContractError, match="typed gate strings"):
        parse_planner_plan(payload)


def test_planner_cannot_authorize_remote_main_push():
    payload = {"tasks": _valid_plan_payload()["tasks"], "auto_push_allowed": True}
    with pytest.raises(StructuredContractError, match="unknown objective plan field"):
        parse_planner_plan(payload)
    payload = {"tasks": _valid_plan_payload()["tasks"], "main_push_policy": "AUTO"}
    with pytest.raises(StructuredContractError, match="unknown objective plan field"):
        parse_planner_plan(payload)


def test_planner_cannot_weaken_review_policy_to_bypass_human_gates():
    task = dict(_valid_plan_payload()["tasks"][0])
    task["review_policy"] = "NONE"
    with pytest.raises(StructuredContractError, match="review_policy"):
        parse_planner_plan({"tasks": [task, _valid_plan_payload()["tasks"][1]]})


def test_planner_requested_human_gate_opens_gate_and_does_not_authorize():
    payload = {
        "tasks": _valid_plan_payload()["tasks"],
        "requested_human_gates": ["REMOTE_MAIN_PUSH_APPROVAL_REQUIRED"],
    }
    plan = parse_planner_plan(payload)
    with SessionLocal() as session:
        objective = create_objective(session, _spec())
        apply_validated_plan(session, objective, plan)
        session.commit()
        gates = open_gates(session, "OBJ-PLAN")
        assert [gate.gate_type for gate in gates] == ["REMOTE_MAIN_PUSH_APPROVAL_REQUIRED"]
        assert session.get(BuildObjective, "OBJ-PLAN").state == "HUMAN_GATE"
        assert objective_work_tasks(session, "OBJ-PLAN")


def test_planner_cannot_bypass_architecture_gate_by_omitting_it():
    # Creating tasks is allowed; authorizing architecture is not a planner power.
    # A planner that tries to set an unknown authorization field fails closed.
    payload = {
        "tasks": _valid_plan_payload()["tasks"],
        "authorize_architecture": True,
    }
    with pytest.raises(StructuredContractError, match="unknown objective plan field"):
        parse_planner_plan(payload)


# -- idempotency -----------------------------------------------------------------


def test_duplicate_planner_apply_does_not_duplicate_tasks():
    with SessionLocal() as session:
        objective = create_objective(session, _spec())
        plan = parse_planner_plan(_valid_plan_payload())
        apply_validated_plan(session, objective, plan)
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-PLAN")
        apply_validated_plan(session, objective, parse_planner_plan(_valid_plan_payload()))
        session.commit()

    with SessionLocal() as session:
        assert len(objective_work_tasks(session, "OBJ-PLAN")) == 2
        plans = session.scalars(select(BuildObjectivePlan).where(BuildObjectivePlan.objective_id == "OBJ-PLAN")).all()
        assert len(plans) == 1


def test_restart_after_plan_generation_is_idempotent():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    executors = {"planner-1": FakeExecutor([_planner_success_observation()])}
    runner = _runner(executors=executors)
    runner.run_once()
    runner.run_once()

    with SessionLocal() as session:
        first_ids = {task.task_id for task in objective_work_tasks(session, "OBJ-PLAN")}
        first_count = len(session.scalars(select(BuildObjectivePlan)).all())

    create_objective  # restart: create is a no-op
    with SessionLocal() as session:
        again = create_objective(session, _spec())
        session.commit()
        assert again.objective_id == "OBJ-PLAN"

    runner.run_once()
    with SessionLocal() as session:
        assert {task.task_id for task in objective_work_tasks(session, "OBJ-PLAN")} == first_ids
        assert len(session.scalars(select(BuildObjectivePlan)).all()) == first_count


def test_duplicate_planner_execution_does_not_duplicate_tasks():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    executors = {
        "planner-1": FakeExecutor(
            [_planner_success_observation(), _planner_success_observation()]
        )
    }
    runner = _runner(executors=executors)
    runner.run_once()
    runner.run_once()
    runner.run_once()

    with SessionLocal() as session:
        assert len(objective_work_tasks(session, "OBJ-PLAN")) == 2
        planner_execs = list(
            session.scalars(select(BuildRunnerExecution).where(BuildRunnerExecution.role == "PLANNER"))
        )
        succeeded = [row for row in planner_execs if row.status == "SUCCEEDED"]
        assert len(succeeded) == 1


# -- no chain-of-thought ---------------------------------------------------------


def test_no_chain_of_thought_persisted_from_planner_result():
    raw = {
        "schema_version": 1,
        "execution_id": "exec-1",
        "task_id": "OBJ-PLAN-PLANNER",
        "role": "PLANNER",
        "status": "SUCCEEDED",
        "chain_of_thought": "secret scratchpad",
        "thinking": "hidden",
        "plan": _valid_plan_payload(),
    }
    cleaned = sanitize_result_mapping(raw)
    assert "chain_of_thought" not in cleaned
    assert "thinking" not in cleaned
    parsed = parse_executor_result(
        raw,
        execution_id="exec-1",
        task_id="OBJ-PLAN-PLANNER",
        role="PLANNER",
        require_identity=True,
    )
    assert "chain_of_thought" not in parsed.persisted
    assert "thinking" not in parsed.persisted
    assert parsed.plan is not None


def test_malformed_planner_execution_fails_closed_without_creating_work():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    bad = ExecutionObservation(
        "SUCCEEDED",
        result_data={"plan": {"tasks": [{"title": "no id", "worktree": "x"}]}},
    )
    runner = _runner(executors={"planner-1": FakeExecutor([bad])})
    runner.run_once()
    runner.run_once()

    with SessionLocal() as session:
        assert objective_work_tasks(session, "OBJ-PLAN") == []
        gates = open_gates(session, "OBJ-PLAN")
        assert any(gate.gate_type == "UNRESOLVABLE_CONFLICT" for gate in gates)
        planner_task = session.get(BuildTask, planner_task_id("OBJ-PLAN"))
        assert planner_task is not None
        assert planner_task.state == "BLOCKED"
        exec_count_before = len(
            list(
                session.scalars(
                    select(BuildRunnerExecution).where(
                        BuildRunnerExecution.role == "PLANNER"
                    )
                )
            )
        )

    # An unchanged malformed planner prompt stays suppressed instead of
    # repeatedly consuming provider quota.
    runner.run_once()
    with SessionLocal() as session:
        exec_count_after = len(
            list(
                session.scalars(
                    select(BuildRunnerExecution).where(
                        BuildRunnerExecution.role == "PLANNER"
                    )
                )
            )
        )
        assert exec_count_after == exec_count_before
        planner_task = session.get(BuildTask, planner_task_id("OBJ-PLAN"))
        assert planner_task is not None
        assert planner_task.state == "BLOCKED"


def test_changed_planner_contract_recovers_blocked_malformed_planner(monkeypatch):
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    bad = ExecutionObservation(
        "SUCCEEDED",
        result_data={"plan": {"tasks": [{"title": "no id", "worktree": "x"}]}},
    )
    good = _planner_success_observation()
    executor = FakeExecutor([bad, good])
    runner = _runner(executors={"planner-1": executor})

    runner.run_once()  # launch bad planner
    runner.run_once()  # observe malformed result and block it

    with SessionLocal() as session:
        planner_task = session.get(BuildTask, planner_task_id("OBJ-PLAN"))
        assert planner_task is not None
        assert planner_task.state == "BLOCKED"

    monkeypatch.setattr(
        PlannerPromptBuilder,
        "role_policy",
        PlannerPromptBuilder.role_policy + " Contract revision for retry.",
    )

    recovered = runner.run_once()
    assert planner_task_id("OBJ-PLAN") in recovered.recovered
    assert recovered.launched

    runner.run_once()  # observe the new-contract successful planner result
    with SessionLocal() as session:
        assert {task.task_id for task in objective_work_tasks(session, "OBJ-PLAN")} == {
            "OBJ-PLAN-A",
            "OBJ-PLAN-B",
        }


# -- planner unavailable ---------------------------------------------------------


def test_temporarily_unavailable_planner_provider_is_not_misreported_as_unconfigured():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    config = _config()
    config = RunnerConfig(
        **{
            **config.__dict__,
            "providers": {
                "local": ProviderConfig(
                    "local",
                    availability="QUOTA_EXHAUSTED",
                    consumption_mode="FALLBACK",
                )
            },
        }
    )
    runner = _runner(config=config)
    result = runner.run_once()

    assert not any(
        "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED" in item
        for item in result.escalations
    )
    assert result.launched == []

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-PLAN")
        assert objective.state == "PLANNING"
        assert planner_status(session, objective) == "UNAVAILABLE"


def test_planner_unavailable_is_explicit_and_resumable():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    runner = _runner(config=_config(planner_adapter="unconfigured"))
    result = runner.run_once()
    assert any("EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED" in item for item in result.escalations)

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-PLAN")
        assert objective.state == "PLANNING"
        assert planner_status(session, objective) == "UNAVAILABLE"
        assert objective_work_tasks(session, "OBJ-PLAN") == []
        events = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.event_type == "objective.planner_unavailable"
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_data.get("resumable") is True

    # Configuring a planner later resumes without rewriting the objective.
    runner = _runner(executors={"planner-1": FakeExecutor([_planner_success_observation()])})
    runner.run_once()
    runner.run_once()
    with SessionLocal() as session:
        assert {task.task_id for task in objective_work_tasks(session, "OBJ-PLAN")} == {
            "OBJ-PLAN-A",
            "OBJ-PLAN-B",
        }


def test_free_text_plan_dispatches_two_builders_when_capacity_permits():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    builder_a = FakeExecutor([ExecutionObservation("RUNNING")])
    builder_b = FakeExecutor([ExecutionObservation("RUNNING")])
    runner = _runner(
        executors={
            "planner-1": FakeExecutor([_planner_success_observation()]),
            "builder-a": builder_a,
            "builder-b": builder_b,
        }
    )
    runner.run_once()
    dispatched = runner.run_once()
    if len(dispatched.launched) < 2:
        dispatched = runner.run_once()
    assert len(dispatched.launched) == 2
    with SessionLocal() as session:
        workers = {
            row.worker_id
            for row in session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.role == "BUILDER")
            )
        }
        assert workers == {"builder-a", "builder-b"}


def test_planner_does_not_dispatch_as_a_builder():
    with SessionLocal() as session:
        create_objective(session, _spec())
        session.commit()

    builder = FakeExecutor()
    runner = _runner(
        executors={
            "planner-1": FakeExecutor([ExecutionObservation("RUNNING")]),
            "builder-a": builder,
            "builder-b": FakeExecutor(),
        }
    )
    runner.run_once()
    assert builder.launches == []
