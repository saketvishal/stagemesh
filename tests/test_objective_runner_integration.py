"""End-to-end coverage of the objective lifecycle through the REAL
BuildRunner (not direct objectives.py calls): proves that an
objective-tagged task flows through the exact same, already-tested
claim/review/remediation/integration machinery as any other task -- no
second orchestrator, no separate review lifecycle -- while objective
reconciliation (follow-ups, gates, completion) happens automatically inside
the same `run_once()` cycle the operator already runs.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select

from _state_isolation import configure_isolated_test_state

configure_isolated_test_state()

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
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
from build_coordinator.objectives import create_objective, objective_tasks, open_gates
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.types import ObjectiveSpec, PlannedChildTask


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
    from build_coordinator.types import PlannedChildTask

    return PlannedChildTask(task_id=task_id, title=f"Task {task_id}", description="objective child task", **kwargs)


def _objective_spec(objective_id, child_tasks):
    return ObjectiveSpec(objective_id=objective_id, goal="Smoke-test goal", child_tasks=tuple(child_tasks))


def _config(*, builder_worktrees=None, remediation_cycles=2, auto_push=False):
    builder_worktrees = builder_worktrees or {}
    return RunnerConfig(
        workers=(
            WorkerConfig(
                "builder-a", "BUILDER", adapter="fake", worktree_path=builder_worktrees.get("builder-a")
            ),
            WorkerConfig(
                "builder-b", "BUILDER", adapter="fake", worktree_path=builder_worktrees.get("builder-b")
            ),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        max_remediation_cycles=remediation_cycles,
        auto_push_allowed=auto_push,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(config=None, executors=None):
    return BuildRunner(SessionLocal, config or _config(), executors=executors, git=FakeGit())


# 5. two parallel builders --------------------------------------------------------


def test_two_objective_tasks_dispatch_to_both_builders_up_to_capacity():
    with SessionLocal() as session:
        create_objective(
            session, _objective_spec("OBJ-PAR", [_child("OBJ-PAR-A", parallel_safe=True), _child("OBJ-PAR-B", parallel_safe=True)])
        )
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 2
    with SessionLocal() as session:
        worker_ids = {
            row.worker_id
            for row in session.scalars(select(BuildRunnerExecution).where(BuildRunnerExecution.role == "BUILDER")).all()
        }
        assert worker_ids == {"builder-a", "builder-b"}


# 6. automatic worktree assignment -------------------------------------------------


def test_objective_task_worktree_is_assigned_automatically_from_worker_config(tmp_path):
    worktree_a = tmp_path / "builder-a-worktree"
    worktree_a.mkdir()
    with SessionLocal() as session:
        create_objective(session, _objective_spec("OBJ-WT", [_child("OBJ-WT-A")]))
        session.commit()

    _runner(config=_config(builder_worktrees={"builder-a": str(worktree_a)})).run_once()

    with SessionLocal() as session:
        claim = session.scalar(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "OBJ-WT-A"))
        # The operator never named a worktree for this task -- the runner
        # selected it from the configured worker, automatically.
        assert claim.worktree_path == str(worktree_a)


# 7 / 10. builder completion -> automatic review -> GREEN -> integration ----------


def test_builder_success_triggers_automatic_review_and_green_triggers_integration():
    executors = {
        "builder-a": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
                )
            ]
        ),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        create_objective(session, _objective_spec("OBJ-FLOW", [_child("OBJ-FLOW-A")]))
        session.commit()

    _runner(executors=executors).run_once()  # dispatch builder
    with SessionLocal() as session:
        assert session.get(BuildTask, "OBJ-FLOW-A").state == "CLAIMED"

    _runner(executors=executors).run_once()  # builder observed SUCCEEDED -> review_ready + review dispatched
    with SessionLocal() as session:
        review_execution = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "REVIEWER")
        )
        assert review_execution is not None  # no human copied a reviewer prompt anywhere

    result = _runner(executors=executors).run_once()  # review GREEN observed -> integration dispatched
    with SessionLocal() as session:
        integration_execution = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "INTEGRATION")
        )
        assert integration_execution is not None

    # No auto-push: remote main stays human-gated (auto_push_allowed=False by default).
    result = _runner(executors=executors).run_once()
    assert any("REMOTE_PUSH_APPROVAL_REQUIRED" in e for e in result.escalations)
    with SessionLocal() as session:
        assert session.get(BuildTask, "OBJ-FLOW-A").state == "BLOCKED"


# 8 / 9. REMEDIATION_REQUIRED -> automatic remediation -> automatic rereview -------


def test_remediation_required_routes_to_builder_and_rereview_automatically():
    reviewer_calls = FakeExecutor(
        [
            ExecutionObservation(
                "SUCCEEDED",
                result_data={
                    "review": {
                        "verdict": "REMEDIATION_REQUIRED",
                        "findings": ["missing edge case"],
                        "required_remediation": ["add edge-case test"],
                    }
                },
            ),
            ExecutionObservation(
                "SUCCEEDED",
                result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
            ),
        ]
    )
    executors = {
        "builder-a": FakeExecutor(
            [ExecutionObservation("SUCCEEDED"), ExecutionObservation("SUCCEEDED")]
        ),
        "reviewer-1": reviewer_calls,
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        create_objective(session, _objective_spec("OBJ-REWORK", [_child("OBJ-REWORK-A")]))
        session.commit()

    _runner(executors=executors).run_once()  # dispatch builder
    _runner(executors=executors).run_once()  # builder succeeds -> review dispatched
    _runner(executors=executors).run_once()  # review REMEDIATION_REQUIRED observed

    with SessionLocal() as session:
        task = session.get(BuildTask, "OBJ-REWORK-A")
        assert task.state == "CLAIMED"  # routed back to builder automatically
        remediation = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "REMEDIATION")
        )
        assert remediation is not None
        # still the same feature branch/task -- no separate task created for remediation
        assert remediation.task_id == "OBJ-REWORK-A"

    _runner(executors=executors).run_once()  # remediation succeeds -> back to review
    _runner(executors=executors).run_once()  # rereview GREEN observed

    with SessionLocal() as session:
        reviews = session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "REVIEWER")
        ).all()
        assert len(reviews) == 2  # original review + automatic rereview, no human re-triggered it


# Objective reconciliation runs automatically inside run_once() -------------------


def test_objective_reconciliation_happens_automatically_inside_run_once():
    with SessionLocal() as session:
        create_objective(session, _objective_spec("OBJ-AUTO", [_child("OBJ-AUTO-A")]))
        session.add(
            BuildRunnerExecution(
                task_id="OBJ-AUTO-A",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                status="SUCCEEDED",
                result_data={
                    "objective_signal": {
                        "follow_up_tasks": [
                            {
                                "title": "auto follow up",
                                "description": "d",
                                "reason": "r",
                                "risk_level": "LOW",
                            }
                        ]
                    }
                },
            )
        )
        session.commit()

    result = _runner().run_once()

    assert "OBJ-AUTO" in result.objectives_reconciled
    assert len(result.objective_follow_ups_created) == 1
    with SessionLocal() as session:
        tasks = objective_tasks(session, "OBJ-AUTO")
        assert any(t.reason_created == "FOLLOW_UP" for t in tasks)
