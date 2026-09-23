from __future__ import annotations

import os
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
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    recover_expired,
    set_mode,
    transition_task,
    upsert_task,
    utcnow,
)


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


def _task(task_id: str, *, review_policy="INDEPENDENT", migration_allowed=False):
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="Runner test task",
        acceptance_criteria=["passes"],
        review_policy=review_policy,
        required_validation=["pytest tooling/build_coordinator/tests"],
        migration_allowed=migration_allowed,
    )


def _config(*, auto_push=False, remediation_cycles=2):
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("builder-b", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        max_remediation_cycles=remediation_cycles,
        auto_push_allowed=auto_push,
        q_records_dir=os.getenv("BUILD_COORDINATOR_Q_RECORDS_DIR"),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(config=None, executors=None, git=None):
    return BuildRunner(
        SessionLocal,
        config or _config(),
        executors=executors,
        git=git if git is not None else FakeGit(),
    )


def test_ready_task_dispatches_one_builder():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-READY"))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-READY")
        execution = session.scalar(select(BuildRunnerExecution))
        assert task.state == "CLAIMED"
        assert execution.role == "BUILDER"
        assert execution.prompt_hash


def test_worker_saturation_is_backpressure_not_configuration_escalation():
    with SessionLocal() as session:
        for task_id in ("SAT-1", "SAT-2", "SAT-3", "SAT-4", "SAT-5"):
            upsert_task(session, _task(task_id))
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-only", "BUILDER", adapter="fake"),),
        q_records_dir=os.getenv("BUILD_COORDINATOR_Q_RECORDS_DIR"),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    result = _runner(config=config).run_once()
    assert len(result.launched) == 1
    assert result.capacity_full is True
    assert not any("EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED" in item for item in result.escalations)
    with SessionLocal() as session:
        states = {row.task_id: row.state for row in session.scalars(select(BuildTask))}
        assert list(states.values()).count("CLAIMED") == 1
        assert list(states.values()).count("READY") == 4


def test_two_ready_tasks_dispatch_to_capacity_and_third_waits():
    with SessionLocal() as session:
        for task_id in ("RUN-A", "RUN-B", "RUN-C"):
            upsert_task(session, _task(task_id))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 2
    assert result.capacity_full is True
    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-C").state == "READY"


def test_migration_lane_rule_remains_coordinator_enforced():
    with SessionLocal() as session:
        upsert_task(session, _task("MIG-A", migration_allowed=True))
        upsert_task(session, _task("MIG-B", migration_allowed=True))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        states = {task.task_id: task.state for task in session.scalars(select(BuildTask))}
        assert sorted(states.values()) == ["CLAIMED", "READY"]


def test_paused_and_draining_launch_no_new_builder():
    for mode in ("PAUSED", "DRAINING"):
        with SessionLocal() as session:
            upsert_task(session, _task(f"RUN-{mode}"))
            set_mode(session, mode)
            session.commit()

        result = _runner().run_once()

        assert result.launched == []
        with SessionLocal() as session:
            set_mode(session, "RUNNING")
            session.commit()


def test_review_ready_dispatches_independent_reviewer_not_builder():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-REVIEW"))
        claim_task(session, ClaimRequest("RUN-REVIEW", worker_id="builder-a"))
        transition_task(session, "RUN-REVIEW", "IN_PROGRESS")
        transition_task(session, "RUN-REVIEW", "VALIDATING")
        transition_task(session, "RUN-REVIEW", "REVIEW_READY")
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        execution = session.scalar(select(BuildRunnerExecution))
        assert execution.role == "REVIEWER"
        assert execution.worker_id == "reviewer-1"


def test_green_review_dispatches_integration_and_push_policy_blocks_resumably():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "feature_sha": "abc123",
                        "review": {"verdict": "GREEN", "ready_for_integration": True},
                    },
                )
            ]
        ),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-GREEN"))
        claim_task(session, ClaimRequest("RUN-GREEN", worker_id="builder-a"))
        transition_task(session, "RUN-GREEN", "IN_PROGRESS")
        transition_task(session, "RUN-GREEN", "VALIDATING")
        transition_task(session, "RUN-GREEN", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()
    result = _runner(executors=executors).run_once()

    assert "RUN-GREEN:REMOTE_PUSH_APPROVAL_REQUIRED" in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-GREEN").state == "BLOCKED"


def test_green_with_notes_without_remediation_is_integration_eligible():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN_WITH_NOTES",
                            "findings": ["minor note"],
                            "required_remediation": [],
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        )
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-NOTES"))
        claim_task(session, ClaimRequest("RUN-NOTES", worker_id="builder-a"))
        transition_task(session, "RUN-NOTES", "IN_PROGRESS")
        transition_task(session, "RUN-NOTES", "VALIDATING")
        transition_task(session, "RUN-NOTES", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        assert session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "INTEGRATION")
        )


def test_remediation_required_routes_to_rework_and_findings_are_checkpointed():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["fix test gap"],
                            "required_remediation": ["add regression test"],
                        }
                    },
                )
            ]
        )
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-REWORK"))
        claim_task(session, ClaimRequest("RUN-REWORK", worker_id="builder-a"))
        transition_task(session, "RUN-REWORK", "IN_PROGRESS")
        transition_task(session, "RUN-REWORK", "VALIDATING")
        transition_task(session, "RUN-REWORK", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-REWORK").state == "CLAIMED"
        checkpoint = session.scalar(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "RUN-REWORK")
        )
        assert checkpoint.remaining_work == ["add regression test"]
        remediation = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "REMEDIATION")
        )
        assert remediation is not None


def test_rework_loop_count_is_bounded():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LIMIT"))
        session.add(
            BuildRunnerExecution(
                execution_id="old-remediation",
                task_id="RUN-LIMIT",
                role="REMEDIATION",
                worker_id="builder-a",
                adapter="fake",
                status="SUCCEEDED",
            )
        )
        claim_task(session, ClaimRequest("RUN-LIMIT", worker_id="builder-a"))
        transition_task(session, "RUN-LIMIT", "IN_PROGRESS")
        transition_task(session, "RUN-LIMIT", "VALIDATING")
        transition_task(session, "RUN-LIMIT", "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "REMEDIATION_REQUIRED"}},
                )
            ]
        )
    }
    _runner(config=_config(remediation_cycles=1), executors=executors).run_once()
    result = _runner(config=_config(remediation_cycles=1), executors=executors).run_once()

    assert "RUN-LIMIT:REMEDIATION_LIMIT_REACHED" in result.escalations


def test_stale_builder_can_be_recovered_and_resumed_without_duplicate_launch():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-STALE"))
        claim = claim_task(session, ClaimRequest("RUN-STALE", worker_id="builder-a"))
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    result = _runner().run_once()

    assert "RUN-STALE" in result.recovered
    with SessionLocal() as session:
        recover_expired(session)
        task = session.get(BuildTask, "RUN-STALE")
        assert task.state in {"STALE", "CLAIMED"}


def test_failed_process_does_not_mark_task_done():
    executors = {"builder-a": FakeExecutor([ExecutionObservation("FAILED", exit_code=2)])}
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-FAIL"))
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-FAIL").state == "CLAIMED"


def test_lost_execution_releases_active_claim_to_stale_with_checkpoint():
    executors = {"builder-a": FakeExecutor([ExecutionObservation("LOST")])}
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST"))
        session.commit()

    _runner(executors=executors).run_once()
    result = _runner(executors=executors).run_once()

    assert "RUN-LOST" in result.recovered
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST")
        claims = session.scalars(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST")).all()
        execution = session.scalar(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "RUN-LOST"))
        checkpoint_row = session.scalar(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "RUN-LOST")
        )
        assert task.state == "CLAIMED"
        assert [claim.status for claim in claims].count("EXPIRED") == 1
        assert [claim.status for claim in claims].count("ACTIVE") == 1
        assert task.current_claim_id in {claim.claim_id for claim in claims if claim.status == "ACTIVE"}
        assert execution.status == "LOST"
        assert checkpoint_row.current_step == "builder execution lost"


def test_lost_execution_without_checkpoint_is_idempotently_recoverable_after_restart():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST-RESTART"))
        claim = claim_task(session, ClaimRequest("RUN-LOST-RESTART", worker_id="builder-a"))
        session.add(
            BuildRunnerExecution(
                execution_id="lost-after-restart",
                task_id="RUN-LOST-RESTART",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                claim_id=str(claim.claim_id),
                status="LOST",
                completed_at=utcnow(),
                result_data={"reconciliation_state": "LOST"},
            )
        )
        session.commit()

    first = _runner().run_once()
    second = _runner().run_once()

    assert first.recovered == ["RUN-LOST-RESTART"]
    assert second.recovered == []
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST-RESTART")
        claims = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST-RESTART")
        ).all()
        active_claims = [claim for claim in claims if claim.status == "ACTIVE"]
        assert task.state == "CLAIMED"
        assert len(active_claims) == 1
        assert len([claim for claim in claims if claim.status == "EXPIRED"]) == 1


def test_lost_execution_with_live_successor_does_not_reclaim_active_claim():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST-LIVE"))
        claim = claim_task(session, ClaimRequest("RUN-LOST-LIVE", worker_id="builder-a"))
        session.add_all(
            [
                BuildRunnerExecution(
                    execution_id="lost-old",
                    task_id="RUN-LOST-LIVE",
                    role="BUILDER",
                    worker_id="builder-a",
                    adapter="fake",
                    claim_id=str(claim.claim_id),
                    status="LOST",
                    completed_at=utcnow(),
                ),
                BuildRunnerExecution(
                    execution_id="live-new",
                    task_id="RUN-LOST-LIVE",
                    role="BUILDER",
                    worker_id="builder-a",
                    adapter="fake",
                    claim_id=str(claim.claim_id),
                    status="RUNNING",
                ),
            ]
        )
        session.commit()

    result = _runner().run_once()

    assert result.recovered == []
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST-LIVE")
        claim = session.scalar(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST-LIVE"))
        assert task.state == "CLAIMED"
        assert claim.status == "ACTIVE"


def test_restart_does_not_duplicate_existing_claims_or_launches():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-RESTART"))
        session.commit()

    _runner().run_once()
    result = _runner().run_once()

    assert result.launched == []
    with SessionLocal() as session:
        assert session.scalar(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "RUN-RESTART"))


def test_auto_push_allowed_completes_after_integration():
    executors = {
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
        upsert_task(session, _task("RUN-DONE"))
        claim_task(session, ClaimRequest("RUN-DONE", worker_id="builder-a"))
        transition_task(session, "RUN-DONE", "IN_PROGRESS")
        transition_task(session, "RUN-DONE", "VALIDATING")
        transition_task(session, "RUN-DONE", "REVIEW_READY")
        session.commit()

    runner = _runner(config=_config(auto_push=True), executors=executors)
    runner.run_once()
    runner.run_once()
    runner.run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-DONE").state == "DONE"
