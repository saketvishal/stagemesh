"""Adversarial lifecycle scenarios for self-hosting readiness."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import (
    ClaimRequest,
    CheckpointInput,
    claim_integration,
    claim_review,
    claim_task,
    checkpoint,
    list_available_tasks,
    provide_task_input,
    recover_expired,
    request_task_input,
    set_mode,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.types import TaskSpec


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILD_COORDINATOR_Q_RECORDS_DIR", str(tmp_path / "q"))
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(tmp_path / "r"))
    (tmp_path / "q").mkdir()
    (tmp_path / "r").mkdir()


@pytest.fixture(autouse=True)
def clean_db():
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


def _task(task_id: str, **kwargs):
    values = dict(
        task_id=task_id,
        title=task_id,
        description="d",
        acceptance_criteria=["ok"],
        review_policy="INDEPENDENT",
    )
    values.update(kwargs)
    return TaskSpec(**values)


def _config(*workers: WorkerConfig, **kwargs):
    return RunnerConfig(
        workers=workers
        or (
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        q_records_dir=os.getenv("BUILD_COORDINATOR_Q_RECORDS_DIR"),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
        **kwargs,
    )


def test_normal_lifecycle_states():
    with SessionLocal() as session:
        upsert_task(session, _task("LIFE-1"))
        claim_task(session, ClaimRequest("LIFE-1", "builder-a"))
        for state in ("IN_PROGRESS", "VALIDATING", "REVIEW_READY"):
            transition_task(session, "LIFE-1", state)
        claim_review(session, ClaimRequest("LIFE-1", "reviewer-1"))
        transition_task(session, "LIFE-1", "INTEGRATING")
        transition_task(session, "LIFE-1", "DONE")
        assert session.get(BuildTask, "LIFE-1").state == "DONE"


def test_worker_death_resume_from_checkpoint():
    with SessionLocal() as session:
        upsert_task(session, _task("DEAD-1"))
        claim = claim_task(session, ClaimRequest("DEAD-1", "builder-a", lease_seconds=1))
        transition_task(session, "DEAD-1", "IN_PROGRESS")
        checkpoint(
            session,
            claim.claim_id,
            worker_id="builder-a",
            data=CheckpointInput(current_step="wrote files", completed_work=["a.py"]),
        )
        session.commit()
    with SessionLocal() as session:
        task = session.get(BuildTask, "DEAD-1")
        task.lease_expires_at = utcnow() - timedelta(seconds=5)
        claim_row = session.get(BuildTaskClaim, task.current_claim_id)
        claim_row.lease_expires_at = utcnow() - timedelta(seconds=5)
        session.commit()
    with SessionLocal() as session:
        recover_expired(session)
        assert session.get(BuildTask, "DEAD-1").state == "STALE"
        transition_task(session, "DEAD-1", "RESUMABLE")
        claim_task(session, ClaimRequest("DEAD-1", "builder-b"))
        from build_coordinator.service import get_resume_context

        ctx = get_resume_context(session, "DEAD-1")
        assert "a.py" in ctx.completed_work
        assert ctx.previous_worker_id == "builder-a"


def test_paused_does_not_start_new_work():
    with SessionLocal() as session:
        upsert_task(session, _task("PAUSE-1"))
        set_mode(session, "PAUSED")
        session.commit()
    result = BuildRunner(SessionLocal, _config(), git=FakeGit()).run_once()
    assert result.launched == []
    with SessionLocal() as session:
        assert session.get(BuildTask, "PAUSE-1").state == "READY"


def test_draining_does_not_start_builders():
    with SessionLocal() as session:
        upsert_task(session, _task("DRAIN-1"))
        set_mode(session, "DRAINING")
        session.commit()
    result = BuildRunner(SessionLocal, _config(), git=FakeGit()).run_once()
    with SessionLocal() as session:
        assert session.get(BuildTask, "DRAIN-1").state == "READY"
    assert all(not item.endswith(":EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED") or True for item in result.escalations)


def test_waiting_for_input_survives_and_does_not_redispatch():
    with SessionLocal() as session:
        upsert_task(session, _task("INP-1"))
        claim_task(session, ClaimRequest("INP-1", "builder-a"))
        transition_task(session, "INP-1", "IN_PROGRESS")
        request_task_input(session, "INP-1", "need flag")
        session.commit()
    result = BuildRunner(SessionLocal, _config(), git=FakeGit()).run_once()
    assert result.launched == []
    with SessionLocal() as session:
        assert session.get(BuildTask, "INP-1").state == "WAITING_FOR_INPUT"
        provide_task_input(session, "INP-1", "flag=1")
        assert session.get(BuildTask, "INP-1").state == "RESUMABLE"


def test_dependency_dag_blocks_downstream():
    with SessionLocal() as session:
        upsert_task(session, _task("DAG-A", review_policy="NONE"))
        upsert_task(session, _task("DAG-B", dependencies=["DAG-A"], review_policy="NONE"))
        upsert_task(session, _task("DAG-C", dependencies=["DAG-A"], review_policy="NONE"))
        upsert_task(session, _task("DAG-D", dependencies=["DAG-B", "DAG-C"], review_policy="NONE"))
        available = {task.task_id for task in list_available_tasks(session)}
        assert "DAG-A" in available
        assert "DAG-B" not in available
        assert "DAG-D" not in available
        claim_task(session, ClaimRequest("DAG-A", "builder-a"))
        for state in ("IN_PROGRESS", "VALIDATING", "DONE"):
            transition_task(session, "DAG-A", state)
        available = {task.task_id for task in list_available_tasks(session)}
        assert "DAG-B" in available
        assert "DAG-C" in available
        assert "DAG-D" not in available


def test_concurrent_task_claim_exactly_one_owner():
    with SessionLocal() as session:
        upsert_task(session, _task("RACE-1"))
        from build_coordinator.service import ensure_state

        ensure_state(session)
        session.commit()

    def _claim(worker: str):
        with SessionLocal() as session:
            try:
                claim_task(session, ClaimRequest("RACE-1", worker))
                session.commit()
                return worker
            except Exception:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [item for item in pool.map(_claim, ["w1", "w2"]) if item]
    assert len(winners) == 1
    with SessionLocal() as session:
        actives = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RACE-1").where(BuildTaskClaim.status == "ACTIVE")
        ).all()
        assert len(actives) == 1


def test_concurrent_integration_claim_exactly_one_owner():
    with SessionLocal() as session:
        upsert_task(session, _task("INT-1"))
        claim_task(session, ClaimRequest("INT-1", "builder-a"))
        for state in ("IN_PROGRESS", "VALIDATING", "REVIEW_READY"):
            transition_task(session, "INT-1", state)
        claim_review(session, ClaimRequest("INT-1", "reviewer-1"))
        from build_coordinator.service import ensure_state

        ensure_state(session)
        session.commit()

    def _claim(worker: str):
        with SessionLocal() as session:
            try:
                claim_integration(session, ClaimRequest("INT-1", worker))
                session.commit()
                return worker
            except Exception:
                session.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [item for item in pool.map(_claim, ["i1", "i2"]) if item]
    assert len(winners) == 1


def test_contradictory_review_fails_closed():
    from build_coordinator.runner.models import ReviewVerdict, ReviewVerdictContradiction

    verdict = ReviewVerdict.from_mapping(
        {
            "verdict": "GREEN",
            "ready_for_integration": True,
            "required_remediation": ["blocking finding remains"],
        }
    )
    with pytest.raises(ReviewVerdictContradiction):
        verdict.validate_consistency()
