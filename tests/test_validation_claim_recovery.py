"""Regression tests for Problem 2: validation-restart recovery must not send
a completed implementation back to a builder.

Root cause (diagnosed via isolated synthetic repro against this checkout):
validation executions are launched against the *same* claim_id as the
BUILDER (or REMEDIATION) claim that produced the feature SHA under
validation -- there is no dedicated VALIDATION claim_type in the schema
(claim_type is one of IMPLEMENTATION / REVIEW / INTEGRATION). When that
claim's lease expires (e.g. the coordinator or the validation executor was
down long enough to miss a heartbeat), `recover_expired()`'s generic
fallback used to unconditionally flip the task to STALE -- a claimable
state -- even though the task was sitting in VALIDATING with an already
-successful implementation. A fresh builder could then claim the task and
redo the implementation from scratch.

The fix makes `recover_expired()` leave a VALIDATING task in VALIDATING
instead of downgrading it to STALE. The very same runner cycle's
`_dispatch_validation()` sweep then observes no active validation execution
for the task and relaunches validation against the correct, already
-recorded feature SHA -- so validation is safely recovered/rerun without
ever redispatching the implementation.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    recover_expired,
    upsert_task,
    utcnow,
)
from sqlalchemy import delete


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
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="Validation-claim-recovery regression test task",
        acceptance_criteria=["passes"],
        review_policy="INDEPENDENT",
        required_validation=[f"{sys.executable} -c \"print('ok')\""],
        migration_allowed=False,
    )


def _config() -> RunnerConfig:
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        result_dir=None,
    )


def _runner(executors=None) -> BuildRunner:
    import os

    config = _config()
    config = RunnerConfig(**{**config.__dict__, "result_dir": os.getenv("BUILD_COORDINATOR_RESULT_DIR")})
    return BuildRunner(SessionLocal, config, executors=executors, git=FakeGit())


def _seed_builder_succeeded_validation_in_flight(task_id: str, *, worker_id="builder-a"):
    """Build the exact durable state of 'builder succeeded, validation
    launched, then the shared claim's lease expired' -- without touching any
    operational database; this is a disposable per-test sqlite DB."""
    with SessionLocal() as session:
        task = upsert_task(session, _task(task_id))
        claim = claim_task(session, ClaimRequest(task_id, worker_id=worker_id))
        task.state = "VALIDATING"
        session.add(
            BuildRunnerExecution(
                execution_id=f"{task_id}-builder-done",
                task_id=task_id,
                role="BUILDER",
                worker_id=worker_id,
                provider="local",
                adapter="fake",
                claim_id=claim.claim_id,
                status="SUCCEEDED",
                worktree_path=str(Path.cwd()),
                result_data={"feature_sha": "feature-sha-original"},
                completed_at=utcnow(),
            )
        )
        session.add(
            BuildRunnerExecution(
                execution_id=f"{task_id}-validation-in-flight",
                task_id=task_id,
                role="BUILDER",
                worker_id="runner-validation",
                provider="runner",
                adapter="validation",
                claim_id=claim.claim_id,
                status="LAUNCHED",
                worktree_path=str(Path.cwd()),
                reviewed_feature_sha="feature-sha-original",
                result_data={
                    "commands": [f"{sys.executable} -c \"print('ok')\""],
                    "workspace": str(Path.cwd()),
                    "source_execution_id": f"{task_id}-builder-done",
                    "feature_sha": "feature-sha-original",
                    "validated_sha": "feature-sha-original",
                    "next_state": "REVIEW_READY",
                },
            )
        )
        # Simulate the coordinator having been down long enough that the
        # shared IMPLEMENTATION claim's lease expired while validation was
        # still outstanding.
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
        return claim.claim_id


def test_expired_claim_during_validation_stays_validating_not_stale():
    """Direct unit-level exercise of recover_expired(): the historical bug
    was `task.state = "STALE"` for *any* expired claim whose claim_type
    wasn't REVIEW/INTEGRATION, regardless of the task's actual lifecycle
    stage. A task correctly sitting in VALIDATING (implementation already
    succeeded) must not become claimable again."""
    claim_id = _seed_builder_succeeded_validation_in_flight("VCR-1")

    with SessionLocal() as session:
        recovered = recover_expired(session)
        task = session.get(BuildTask, "VCR-1")
        claim = session.get(BuildTaskClaim, claim_id)
        stuck_validation = session.get(BuildRunnerExecution, "VCR-1-validation-in-flight")
        builder_row = session.get(BuildRunnerExecution, "VCR-1-builder-done")

        assert task.task_id in {t.task_id for t in recovered}
        # The historical defect: this used to be "STALE" (claimable).
        assert task.state == "VALIDATING"
        assert claim.status == "EXPIRED"
        assert task.current_claim_id is None
        # The stuck validation execution under the expired claim is
        # terminated so a fresh one can be dispatched; the successful
        # builder execution is untouched (it is not LAUNCHED/RUNNING).
        assert stuck_validation.status == "TERMINATED"
        assert builder_row.status == "SUCCEEDED"


def test_runner_cycle_relaunches_validation_not_implementation_after_claim_expiry():
    """Full-cycle exercise: after recover_expired() leaves the task in
    VALIDATING, the same cycle's _dispatch_validation() sweep must relaunch
    validation against the original feature SHA, and no new BUILDER
    execution must ever be created for the already-implemented task."""
    _seed_builder_succeeded_validation_in_flight("VCR-2")

    result = _runner(executors={"builder-a": FakeExecutor()}).run_once()

    assert "VCR-2" in result.recovered

    with SessionLocal() as session:
        task = session.get(BuildTask, "VCR-2")
        builder_rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "VCR-2")
            .where(BuildRunnerExecution.adapter == "fake")
        ).all()
        validation_rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "VCR-2")
            .where(BuildRunnerExecution.adapter == "validation")
        ).all()
        live_validations = [
            row for row in validation_rows if row.status in {"LAUNCHED", "RUNNING"}
        ]

        # Implementation was never redispatched: still exactly the one,
        # original, successful builder execution.
        assert len(builder_rows) == 1
        assert builder_rows[0].status == "SUCCEEDED"
        assert builder_rows[0].execution_id == "VCR-2-builder-done"

        # The stuck validation was terminated and a fresh one relaunched,
        # tied to the same original feature SHA -- not a new/different one.
        assert session.get(BuildRunnerExecution, "VCR-2-validation-in-flight").status == "TERMINATED"
        assert len(live_validations) == 1
        assert live_validations[0].reviewed_feature_sha == "feature-sha-original"
        assert (live_validations[0].result_data or {}).get("validated_sha") == "feature-sha-original"

        # Task never became claimable in the process.
        assert task.state == "VALIDATING"

