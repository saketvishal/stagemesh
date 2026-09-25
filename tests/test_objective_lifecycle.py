"""Direct coverage of the objective lifecycle service (`objectives.py`):
creation, plan persistence, dependency/depth/count/cycle limits, duplicate
finding suppression, typed human gates, gate resume, pause/resume, and
completion criteria.

These exercise `objectives.py` functions directly against the coordinator
DB (not through the CLI or the full runner loop) so the reconciliation
logic itself is pinned down precisely; `test_objective_runner_integration.py`
covers the same lifecycle end-to-end through the real BuildRunner.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
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
    ObjectiveError,
    create_objective,
    is_planner_task,
    objective_tasks,
    objective_work_tasks,
    open_gates,
    pause_objective,
    planner_status,
    reconcile_objective,
    resolve_gate,
    resume_objective,
)
from build_coordinator.planner import planner_task_id
from build_coordinator.events import record_event
from build_coordinator.types import (
    EventInput,
    FindingSpec,
    ObjectiveSpec,
    PlannedChildTask,
    StructuredContractError,
)


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    engine.dispose()
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
    engine.dispose()


def _child(task_id, *, parent=None, dependencies=(), risk="MEDIUM", reason="OBJECTIVE_PLAN"):
    return PlannedChildTask(
        task_id=task_id,
        title=f"Task {task_id}",
        description="test child task",
        dependencies=tuple(dependencies),
        parent_task_id=parent,
        risk_level=risk,
        reason_created=reason,
    )


def _spec(
    objective_id="OBJ-1",
    *,
    child_tasks=(),
    completion_criteria=(),
    max_auto=20,
    max_depth=4,
    requested_human_gates=(),
):
    return ObjectiveSpec(
        objective_id=objective_id,
        goal="Evaluate the best document-intelligence stack using our benchmark.",
        completion_criteria=tuple(completion_criteria),
        child_tasks=tuple(child_tasks),
        requested_human_gates=tuple(requested_human_gates),
        max_auto_created_tasks=max_auto,
        max_child_depth=max_depth,
    )


def _succeeded_execution(task_id, *, result_data, role="BUILDER"):
    # `result_data` here is the objective-signal payload (follow_up_tasks,
    # human_gate, ...) -- the real persisted shape nests it under
    # "objective_signal" (see execution/results.py::parse_executor_result),
    # so wrap it the same way rather than passing it at the top level.
    return BuildRunnerExecution(
        task_id=task_id,
        role=role,
        worker_id="builder-a",
        adapter="fake",
        status="SUCCEEDED",
        result_data={"objective_signal": result_data},
    )


# 1. objective creation ------------------------------------------------------


def test_objective_creation_with_explicit_plan_creates_objective_and_tasks():
    with SessionLocal() as session:
        objective = create_objective(
            session, _spec(child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-B")])
        )
        session.commit()
        assert objective.state == "ACTIVE"

    with SessionLocal() as session:
        tasks = objective_tasks(session, "OBJ-1")
        assert {t.task_id for t in tasks} == {"OBJ-1-A", "OBJ-1-B"}
        assert all(t.objective_id == "OBJ-1" for t in tasks)


def test_bare_goal_with_no_plan_stays_planning_and_does_not_invent_work():
    with SessionLocal() as session:
        objective = create_objective(session, _spec())
        session.commit()
        assert objective.state == "PLANNING"
        assert planner_status(session, objective) == "PENDING"

    with SessionLocal() as session:
        assert open_gates(session, "OBJ-1") == []
        assert objective_work_tasks(session, "OBJ-1") == []
        tasks = objective_tasks(session, "OBJ-1")
        assert len(tasks) == 1
        assert is_planner_task(tasks[0])
        assert tasks[0].task_id == planner_task_id("OBJ-1")


# 2. plan persistence ---------------------------------------------------------


def test_plan_is_persisted_with_source_and_version():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.commit()

    with SessionLocal() as session:
        plan = session.scalar(select(BuildObjectivePlan).where(BuildObjectivePlan.objective_id == "OBJ-1"))
        assert plan is not None
        assert plan.version == 1
        assert plan.source == "EXPLICIT_INPUT"
        assert plan.plan_data[0]["task_id"] == "OBJ-1-A"


# 4. dependency handling -------------------------------------------------------


def test_child_task_dependencies_are_retained_on_the_task():
    with SessionLocal() as session:
        create_objective(
            session,
            _spec(child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-B", dependencies=["OBJ-1-A"])]),
        )
        session.commit()

    with SessionLocal() as session:
        task_b = session.get(BuildTask, "OBJ-1-B")
        assert task_b.dependencies == ["OBJ-1-A"]


# 16. cycle prevention ----------------------------------------------------------


def test_plan_with_dependency_cycle_is_rejected():
    with SessionLocal() as session:
        with pytest.raises(StructuredContractError, match="cycle"):
            create_objective(
                session,
                _spec(
                    child_tasks=[
                        _child("OBJ-1-A", dependencies=["OBJ-1-B"]),
                        _child("OBJ-1-B", dependencies=["OBJ-1-A"]),
                    ]
                ),
            )


def test_plan_with_duplicate_task_id_is_rejected():
    with SessionLocal() as session:
        with pytest.raises(StructuredContractError, match="duplicate"):
            create_objective(
                session,
                _spec(child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-A")]),
            )


# 11 / 12. follow-up and unrelated finding automation ---------------------------


def test_related_follow_up_is_auto_created_from_structured_result():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(
            _succeeded_execution(
                "OBJ-1-A",
                result_data={
                    "task_outcome": "SUCCESS",
                    "follow_up_tasks": [
                        {
                            "title": "Tighten validation on adjacent field",
                            "description": "small related cleanup",
                            "reason": "found while implementing OBJ-1-A",
                            "risk_level": "LOW",
                        }
                    ],
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert len(summary.follow_ups_created) == 1

    with SessionLocal() as session:
        tasks = objective_tasks(session, "OBJ-1")
        follow_up = next(t for t in tasks if t.reason_created == "FOLLOW_UP")
        assert follow_up.parent_task_id == "OBJ-1-A"


def test_unrelated_finding_creates_separate_task_not_parented_to_source():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("PROVENANCE-INVESTIGATION")]))
        session.add(
            _succeeded_execution(
                "PROVENANCE-INVESTIGATION",
                result_data={
                    "task_outcome": "SUCCESS",
                    "unrelated_findings": [
                        {
                            "title": "Starlette HTTP 413 constant bug",
                            "description": "unrelated upload-size bug found during investigation",
                            "reason": "noticed while tracing provenance",
                            "risk_level": "LOW",
                            "task_id": "API-DOCUMENT-UPLOAD-413-STATUS-FIX-V1",
                        }
                    ],
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert summary.unrelated_tasks_created == ["API-DOCUMENT-UPLOAD-413-STATUS-FIX-V1"]

    with SessionLocal() as session:
        source_task = session.get(BuildTask, "PROVENANCE-INVESTIGATION")
        finding_task = session.get(BuildTask, "API-DOCUMENT-UPLOAD-413-STATUS-FIX-V1")
        assert source_task.state != "BLOCKED"  # the source task continues unchanged
        assert finding_task.parent_task_id is None
        assert finding_task.reason_created == "UNRELATED_FINDING"


# 13. duplicate finding suppression ---------------------------------------------


def test_duplicate_finding_is_not_recreated_on_second_reconcile():
    result_data = {
        "task_outcome": "SUCCESS",
        "follow_up_tasks": [
            {"title": "Same finding", "description": "x", "reason": "y", "risk_level": "LOW"}
        ],
    }
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(_succeeded_execution("OBJ-1-A", result_data=result_data))
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()

    with SessionLocal() as session:
        first_count = len(objective_tasks(session, "OBJ-1"))

    # Reconcile again (simulating a restart / repeated cycle) without a new execution.
    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()

    with SessionLocal() as session:
        second_count = len(objective_tasks(session, "OBJ-1"))
        assert second_count == first_count


# 17 / 19. restart safety: reconciling twice must not double-process ------------


def test_restart_reconcile_does_not_reprocess_the_same_execution_twice():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(
            _succeeded_execution(
                "OBJ-1-A",
                result_data={
                    "task_outcome": "SUCCESS",
                    "follow_up_tasks": [
                        {"title": "F1", "description": "d", "reason": "r", "risk_level": "LOW"}
                    ],
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        events_after_first = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.event_type == "objective.execution_processed"
            )
        ).all()
        assert len(events_after_first) == 1

    # Simulate a fresh process (new session/state) reconciling again.
    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        events_after_second = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.event_type == "objective.execution_processed"
            )
        ).all()
        assert len(events_after_second) == 1  # not duplicated


# 14. child-depth limit -----------------------------------------------------------


def test_child_depth_limit_raises_gate_instead_of_creating_deeper_task():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")], max_depth=1))
        session.add(
            _succeeded_execution(
                "OBJ-1-A",
                result_data={
                    "follow_up_tasks": [
                        {"title": "Depth 1", "description": "d", "reason": "r", "risk_level": "LOW"}
                    ]
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        depth_one_task = next(t for t in objective_tasks(session, "OBJ-1") if t.reason_created == "FOLLOW_UP")

    with SessionLocal() as session:
        session.add(
            _succeeded_execution(
                depth_one_task.task_id,
                result_data={
                    "follow_up_tasks": [
                        {"title": "Depth 2", "description": "d", "reason": "r", "risk_level": "LOW"}
                    ]
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert summary.follow_ups_created == []
        gates = open_gates(session, "OBJ-1")
        assert any(g.gate_type == "MAJOR_SCOPE_EXPANSION_REQUIRED" for g in gates)


# 15. task-count limit -------------------------------------------------------------


def test_max_auto_created_tasks_limit_raises_gate():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")], max_auto=0))
        session.add(
            _succeeded_execution(
                "OBJ-1-A",
                result_data={
                    "follow_up_tasks": [
                        {"title": "Over limit", "description": "d", "reason": "r", "risk_level": "LOW"}
                    ]
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert summary.follow_ups_created == []
        gates = open_gates(session, "OBJ-1")
        assert any(g.gate_type == "MAJOR_SCOPE_EXPANSION_REQUIRED" for g in gates)


# 20-23. typed human gates from structured results -------------------------------


@pytest.mark.parametrize(
    "gate_type",
    [
        "ARCHITECTURE_DECISION_REQUIRED",
        "SECURITY_DECISION_REQUIRED",
        "PRIVACY_DECISION_REQUIRED",
        "EXTERNAL_COST_APPROVAL_REQUIRED",
        "CREDENTIAL_REQUIRED",
        "DESTRUCTIVE_ACTION_APPROVAL_REQUIRED",
    ],
)
def test_structured_human_gate_field_raises_the_specific_typed_gate(gate_type):
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(_succeeded_execution("OBJ-1-A", result_data={"human_gate": gate_type}))
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        assert objective.state == "HUMAN_GATE"
        gates = open_gates(session, "OBJ-1")
        assert len(gates) == 1
        assert gates[0].gate_type == gate_type


def test_unknown_human_gate_value_fails_closed_as_unresolvable_conflict():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(_succeeded_execution("OBJ-1-A", result_data={"human_gate": "NOT_A_REAL_GATE"}))
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        gates = open_gates(session, "OBJ-1")
        assert len(gates) == 1
        assert gates[0].gate_type == "UNRESOLVABLE_CONFLICT"


# 24. remote-main push -> human gate (bridged from the existing runner escalation) --


def test_remote_main_push_escalation_is_bridged_to_objective_gate():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        task = session.get(BuildTask, "OBJ-1-A")
        task.state = "BLOCKED"
        record_event(
            session,
            EventInput(
                task_id="OBJ-1-A",
                event_type="task.transitioned",
                from_state="INTEGRATING",
                to_state="BLOCKED",
                event_data={"reason": "REMOTE_PUSH_APPROVAL_REQUIRED"},
            ),
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        gates = open_gates(session, "OBJ-1")
        assert len(gates) == 1
        assert gates[0].gate_type == "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED"


def test_human_gate_still_bridges_later_remote_push_blocks():
    from build_coordinator.objectives import run_objective_cycle

    with SessionLocal() as session:
        create_objective(
            session,
            _spec(
                child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-B")],
                requested_human_gates=["ARCHITECTURE_DECISION_REQUIRED"],
            ),
        )
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "HUMAN_GATE"
        task = session.get(BuildTask, "OBJ-1-A")
        task.state = "BLOCKED"
        record_event(
            session,
            EventInput(
                task_id="OBJ-1-A",
                event_type="task.transitioned",
                from_state="INTEGRATING",
                to_state="BLOCKED",
                event_data={"reason": "REMOTE_PUSH_APPROVAL_REQUIRED"},
            ),
        )
        session.commit()

    with SessionLocal() as session:
        summaries = run_objective_cycle(session)
        session.commit()
        assert summaries
        gates = {gate.gate_type for gate in open_gates(session, "OBJ-1")}
        assert "ARCHITECTURE_DECISION_REQUIRED" in gates
        assert "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED" in gates


# 25. human gate resume -----------------------------------------------------------


def test_gate_resolution_resumes_objective_when_no_gates_remain():
    with SessionLocal() as session:
        create_objective(
            session,
            _spec(
                child_tasks=[_child("OBJ-1-A")],
                requested_human_gates=["ARCHITECTURE_DECISION_REQUIRED"],
            ),
        )
        session.commit()
        gate_id = open_gates(session, "OBJ-1")[0].gate_id

    with SessionLocal() as session:
        resolve_gate(session, gate_id, resolved_by="alice", resolution_note="approved manual plan")
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        assert objective.state == "ACTIVE"
        assert open_gates(session, "OBJ-1") == []


def test_resolving_an_already_resolved_gate_is_rejected():
    with SessionLocal() as session:
        create_objective(
            session,
            _spec(
                child_tasks=[_child("OBJ-1-A")],
                requested_human_gates=["ARCHITECTURE_DECISION_REQUIRED"],
            ),
        )
        session.commit()
        gate_id = open_gates(session, "OBJ-1")[0].gate_id

    with SessionLocal() as session:
        resolve_gate(session, gate_id, resolved_by="alice")
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(ObjectiveError):
            resolve_gate(session, gate_id, resolved_by="bob")


def test_gate_resolved_before_plan_applied_resumes_to_planning():
    from build_coordinator.objectives import record_planner_failed

    with SessionLocal() as session:
        create_objective(session, _spec())  # bare goal: stays PLANNING, no child tasks yet
        objective = session.get(BuildObjective, "OBJ-1")
        assert objective.state == "PLANNING"
        record_planner_failed(session, objective, reason="planner produced an invalid plan")
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "HUMAN_GATE"
        gate_id = open_gates(session, "OBJ-1")[0].gate_id

    with SessionLocal() as session:
        resolve_gate(session, gate_id, resolved_by="alice", resolution_note="retry planning")
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        assert objective.state == "PLANNING"
        assert open_gates(session, "OBJ-1") == []


def test_gate_resolved_after_plan_already_applied_resumes_to_active_not_planning():
    # Same trigger as test_gate_resolution_resumes_objective_when_no_gates_remain,
    # but pins down that a gate captured while still PLANNING does not strand the
    # objective back in PLANNING once real work tasks exist by the time it's resolved.
    with SessionLocal() as session:
        create_objective(
            session,
            _spec(
                child_tasks=[_child("OBJ-1-A")],
                requested_human_gates=["ARCHITECTURE_DECISION_REQUIRED"],
            ),
        )
        session.commit()
        gate_id = open_gates(session, "OBJ-1")[0].gate_id

    with SessionLocal() as session:
        resolve_gate(session, gate_id, resolved_by="alice")
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildObjective, "OBJ-1").state == "ACTIVE"


def test_recurring_gate_after_resolution_raises_a_new_gate_rather_than_stranding_the_task():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        task = session.get(BuildTask, "OBJ-1-A")
        task.state = "BLOCKED"
        record_event(
            session,
            EventInput(
                task_id="OBJ-1-A",
                event_type="task.transitioned",
                from_state="INTEGRATING",
                to_state="BLOCKED",
                event_data={"reason": "REMOTE_PUSH_APPROVAL_REQUIRED"},
            ),
        )
        session.commit()
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        first_gate_id = open_gates(session, "OBJ-1")[0].gate_id

    with SessionLocal() as session:
        resolve_gate(session, first_gate_id, resolved_by="alice")
        session.commit()
        assert open_gates(session, "OBJ-1") == []

    with SessionLocal() as session:
        # The same task hits the same blocked reason again (e.g. a second
        # remote push needs approval). This must open a fresh gate, not
        # silently no-op against the now-resolved one.
        record_event(
            session,
            EventInput(
                task_id="OBJ-1-A",
                event_type="task.transitioned",
                from_state="INTEGRATING",
                to_state="BLOCKED",
                event_data={"reason": "REMOTE_PUSH_APPROVAL_REQUIRED"},
            ),
        )
        session.commit()
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        gates = open_gates(session, "OBJ-1")
        assert len(gates) == 1
        assert gates[0].gate_id != first_gate_id
        assert session.get(BuildObjective, "OBJ-1").state == "HUMAN_GATE"


def test_stale_gate_auto_reconciles_once_the_blocked_task_clears():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        task = session.get(BuildTask, "OBJ-1-A")
        task.state = "BLOCKED"
        record_event(
            session,
            EventInput(
                task_id="OBJ-1-A",
                event_type="task.transitioned",
                from_state="INTEGRATING",
                to_state="BLOCKED",
                event_data={"reason": "MERGE_CONFLICT"},
            ),
        )
        session.commit()
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        assert open_gates(session, "OBJ-1")
        assert session.get(BuildObjective, "OBJ-1").state == "HUMAN_GATE"

    with SessionLocal() as session:
        # The underlying condition clears on its own (e.g. an automatic
        # rebase resolved the conflict) before a human actions the gate.
        task = session.get(BuildTask, "OBJ-1-A")
        task.state = "DONE"
        session.commit()
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        assert open_gates(session, "OBJ-1") == []
        assert session.get(BuildObjective, "OBJ-1").state != "HUMAN_GATE"
        resolved = session.scalar(select(BuildObjectiveGate).where(BuildObjectiveGate.objective_id == "OBJ-1"))
        assert resolved.status == "RESOLVED"
        assert resolved.resolved_by == "system:auto-reconciled"


def test_stale_gate_reconciliation_does_not_resolve_gates_raised_directly_from_human_gate_field():
    # A gate type that overlaps with BLOCKED_REASON_TO_GATE_TYPE's values but
    # was raised straight from a structured `human_gate` field (not from a
    # BLOCKED task) must never be auto-reconciled just because its source
    # task isn't BLOCKED -- it was never BLOCKED to begin with.
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(_succeeded_execution("OBJ-1-A", result_data={"human_gate": "ARCHITECTURE_DECISION_REQUIRED"}))
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()
        gates = open_gates(session, "OBJ-1")
        assert len(gates) == 1
        assert session.get(BuildObjective, "OBJ-1").state == "HUMAN_GATE"


# 26. objective pause/resume -------------------------------------------------------


def test_objective_pause_and_resume_round_trip():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.commit()

    with SessionLocal() as session:
        pause_objective(session, "OBJ-1")
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "PAUSED"

    with SessionLocal() as session:
        resume_objective(session, "OBJ-1")
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "ACTIVE"


def test_paused_objective_is_excluded_from_the_reconcile_cycle():
    from build_coordinator.objectives import run_objective_cycle

    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        pause_objective(session, "OBJ-1")
        session.commit()

    with SessionLocal() as session:
        summaries = run_objective_cycle(session)
        session.commit()
        assert summaries == []


def test_objective_paused_while_still_planning_resumes_to_planning():
    with SessionLocal() as session:
        create_objective(session, _spec())  # bare goal: no plan applied yet
        objective = session.get(BuildObjective, "OBJ-1")
        assert objective.state == "PLANNING"
        pause_objective(session, "OBJ-1")
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "PAUSED"

    with SessionLocal() as session:
        resume_objective(session, "OBJ-1")
        session.commit()
        assert session.get(BuildObjective, "OBJ-1").state == "PLANNING"


# 27. objective completion criteria -------------------------------------------------


def test_objective_completes_only_when_all_tasks_done_and_criteria_met():
    with SessionLocal() as session:
        create_objective(
            session,
            _spec(child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-B")], completion_criteria=["OBJ-1-A", "OBJ-1-B"]),
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert not summary.completed
        assert objective.state == "WAITING_ON_TASKS"

    with SessionLocal() as session:
        session.get(BuildTask, "OBJ-1-A").state = "DONE"
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert not summary.completed  # OBJ-1-B still pending

    with SessionLocal() as session:
        session.get(BuildTask, "OBJ-1-B").state = "DONE"
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert summary.completed
        assert objective.state == "COMPLETED"


def test_objective_does_not_complete_just_because_the_first_task_finished():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A"), _child("OBJ-1-B")]))
        session.commit()

    with SessionLocal() as session:
        session.get(BuildTask, "OBJ-1-A").state = "DONE"
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        summary = reconcile_objective(session, objective)
        session.commit()
        assert not summary.completed
        assert objective.state == "WAITING_ON_TASKS"


# 30. no chain-of-thought persistence -----------------------------------------------


def test_no_chain_of_thought_persisted_in_objective_events():
    with SessionLocal() as session:
        create_objective(session, _spec(child_tasks=[_child("OBJ-1-A")]))
        session.add(
            _succeeded_execution(
                "OBJ-1-A",
                result_data={
                    "follow_up_tasks": [
                        {"title": "F", "description": "d", "reason": "r", "risk_level": "LOW"}
                    ]
                },
            )
        )
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "OBJ-1")
        reconcile_objective(session, objective)
        session.commit()

    forbidden_keys = {"reasoning", "chain_of_thought", "thinking", "scratchpad", "raw_prompt"}
    with SessionLocal() as session:
        events = session.scalars(select(BuildObjectiveEvent)).all()
        assert events, "expected at least one objective event"
        for event in events:
            serialized = json.dumps(event.event_data)
            assert set(event.event_data.keys()).isdisjoint(forbidden_keys)
            # every value must itself be a short structured field, not an
            # essay-length reasoning dump
            assert len(serialized) < 2000


# 29. no product dependency ----------------------------------------------------------


def test_objectives_module_imports_no_product_runtime_package():
    import build_coordinator.objectives as objectives_module

    module_source_names = set(dir(objectives_module))
    assert "app" not in module_source_names
    for name, value in vars(objectives_module).items():
        module = getattr(value, "__module__", "")
        assert not module.startswith("app."), f"{name} imports from product runtime package: {module}"
