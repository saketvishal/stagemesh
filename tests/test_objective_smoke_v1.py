"""First real objective smoke test (item 18 of the
AUTONOMOUS-OBJECTIVE-LIFECYCLE-V1 ticket).

HONESTY NOTE: this environment has no real, configured autonomous coding
agent to act as builder/reviewer (the runner's `subprocess` adapter needs a
real external agent command -- none is wired here). This smoke test proves
the CONTROLLER's automatic lifecycle wiring end-to-end -- planning, parallel
builder dispatch, automatic review, automatic follow-up and unrelated-
finding task creation, and stopping at REMOTE_MAIN_PUSH_APPROVAL_REQUIRED --
using the same `fake` executor adapter the rest of the coordinator test
suite uses. It does NOT prove a real LLM-driven agent can carry out the
work; it proves the operator never has to carry prompts, findings, or
follow-ups between roles by hand once the objective is submitted.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import delete, select

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
from build_coordinator.objectives import objective_tasks, open_gates, create_objective
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.types import ObjectiveSpec, PlannedChildTask


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    q_dir = tmp_path / "q-records"
    q_dir.mkdir()
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_Q_RECORDS_DIR", str(q_dir))
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


def _config():
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("builder-b", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        auto_push_allowed=False,  # remote main stays human-gated, per the ticket
        q_records_dir=os.getenv("BUILD_COORDINATOR_Q_RECORDS_DIR"),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def test_one_objective_two_parallel_builders_followups_and_human_gate_stop():
    objective_id = "SMOKE-DOCINTEL-V1"
    with SessionLocal() as session:
        create_objective(
            session,
            ObjectiveSpec(
                objective_id=objective_id,
                goal=(
                    "Evaluate the best document-intelligence stack for Caventra using our "
                    "benchmark. Use available builders in parallel."
                ),
                child_tasks=(
                    PlannedChildTask(
                        task_id="SMOKE-EVAL-CANDIDATE-A",
                        title="Evaluate candidate stack A against the benchmark",
                        description="Run candidate A through the document-intelligence benchmark",
                        parallel_safe=True,
                    ),
                    PlannedChildTask(
                        task_id="SMOKE-EVAL-CANDIDATE-B",
                        title="Evaluate candidate stack B against the benchmark",
                        description="Run candidate B through the document-intelligence benchmark",
                        parallel_safe=True,
                    ),
                ),
            ),
        )
        session.commit()

    # -- ONE high-level objective -> at least TWO child tasks -------------------
    with SessionLocal() as session:
        tasks = objective_tasks(session, objective_id)
        assert len(tasks) == 2
        assert {t.task_id for t in tasks} == {"SMOKE-EVAL-CANDIDATE-A", "SMOKE-EVAL-CANDIDATE-B"}

    executors = {
        "builder-a": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "objective_signal": {
                            "task_outcome": "SUCCESS",
                            "follow_up_tasks": [
                                {
                                    "title": "Tighten confidence threshold for candidate A",
                                    "description": "small related follow-up found while benchmarking A",
                                    "reason": "benchmark run surfaced a low-confidence edge case",
                                    "risk_level": "LOW",
                                }
                            ],
                            "unrelated_findings": [
                                {
                                    "title": "Starlette HTTP 413 constant bug",
                                    "description": "unrelated upload-size bug noticed during benchmarking",
                                    "reason": "noticed while running the benchmark harness",
                                    "risk_level": "LOW",
                                    "task_id": "API-DOCUMENT-UPLOAD-413-STATUS-FIX-V1",
                                }
                            ],
                        }
                    },
                )
            ]
        ),
        "builder-b": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
                ),
            ]
        ),
        "integration-1": FakeExecutor(
            [ExecutionObservation("SUCCEEDED"), ExecutionObservation("SUCCEEDED")]
        ),
    }

    def run():
        return BuildRunner(SessionLocal, _config(), executors=executors, git=FakeGit()).run_once()

    # -- dispatch: two builders used when capacity permits -----------------------
    result = run()
    assert len(result.launched) == 2
    with SessionLocal() as session:
        worker_ids = {
            row.worker_id
            for row in session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.role == "BUILDER")
            ).all()
        }
        assert worker_ids == {"builder-a", "builder-b"}, "expected both builders used, not chosen by a human"

    # -- builder execution + independent review automatically dispatched ---------
    run()
    with SessionLocal() as session:
        review_count = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.role == "REVIEWER")
        )
        assert review_count is not None, "review must be dispatched automatically, no human copied a prompt"

    # -- GREEN observed -> integration dispatched automatically; objective ------
    #    reconciliation (same run_once cycle) creates the follow-up and the
    #    unrelated-finding task from builder-a's structured result, with no
    #    human relaying that information between tasks.
    run()
    with SessionLocal() as session:
        tasks = objective_tasks(session, objective_id)
        reasons = {t.reason_created for t in tasks}
        assert "FOLLOW_UP" in reasons, "expected an automatic follow-up task"
        assert "UNRELATED_FINDING" in reasons, "expected an automatic unrelated-finding task"
        follow_up = next(t for t in tasks if t.reason_created == "FOLLOW_UP")
        unrelated = next(t for t in tasks if t.reason_created == "UNRELATED_FINDING")
        assert follow_up.parent_task_id == "SMOKE-EVAL-CANDIDATE-A"
        assert unrelated.parent_task_id is None  # does not contaminate the source task's scope
        assert unrelated.task_id == "API-DOCUMENT-UPLOAD-413-STATUS-FIX-V1"
        source_task = next(t for t in tasks if t.task_id == "SMOKE-EVAL-CANDIDATE-A")
        assert source_task.state not in {"BLOCKED", "FAILED"}  # source task continues unchanged

    # -- integration preparation happened; remote main push is human-gated -----
    result = run()
    with SessionLocal() as session:
        integration_executions = session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "INTEGRATION")
        ).all()
        assert len(integration_executions) >= 1, "expected integration preparation to run automatically"
        blocked_tasks = [
            t for t in objective_tasks(session, objective_id) if t.state == "BLOCKED"
        ]
        assert blocked_tasks, "expected the runner to stop for human approval before touching main"

    # -- the objective-level reconciliation bridges that into a typed gate ------
    run()
    with SessionLocal() as session:
        gates = open_gates(session, objective_id)
        assert any(g.gate_type == "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED" for g in gates), (
            "expected the objective to stop specifically at "
            "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED, not a generic escalation"
        )
        objective = session.get(BuildObjective, objective_id)
        assert objective.state == "HUMAN_GATE"
