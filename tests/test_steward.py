from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
    BuildWorkerLease,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, StewardConfig, WorkerConfig
from build_coordinator.runner.steward import run_steward_cycle, select_steward_worker
from build_coordinator.service import ClaimRequest, TaskSpec, claim_task, upsert_task, utcnow


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildWorkerLease,
            BuildRunnerExecution,
            BuildTaskEvent,
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
        description="steward test task",
        acceptance_criteria=["passes"],
    )


def _steward(**overrides) -> WorkerConfig:
    data = {
        "worker_id": "steward-1",
        "role": "STEWARD",
        "capabilities": ("MAINTENANCE",),
        "stages": ("maintenance",),
        "permissions": ("COORDINATOR_MAINTENANCE",),
    }
    data.update(overrides)
    return WorkerConfig(**data)


def test_steward_worker_requires_explicit_capability_and_permission():
    workers = (
        WorkerConfig(
            worker_id="missing-cap",
            role="CUSTOM",
            stages=("maintenance",),
            permissions=("COORDINATOR_MAINTENANCE",),
        ),
        WorkerConfig(
            worker_id="missing-permission",
            role="CUSTOM",
            stages=("maintenance",),
            capabilities=("MAINTENANCE",),
        ),
        _steward(worker_id="eligible", preference=5),
    )

    selected = select_steward_worker(workers)

    assert selected is not None
    assert selected.worker_id == "eligible"


def test_steward_dry_run_surfaces_candidates_without_mutating_claims():
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("STALE-CLAIM"))
        claim = claim_task(session, ClaimRequest("STALE-CLAIM", worker_id="builder-a"))
        claim.lease_expires_at = now - timedelta(minutes=5)
        session.add(
            BuildRunnerExecution(
                execution_id="exec-stale",
                task_id="STALE-CLAIM",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                claim_id=claim.claim_id,
                status="RUNNING",
            )
        )
        session.commit()

    with SessionLocal() as session:
        result = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(enabled=True, apply=False),
            now=now,
        )
        claim = session.get(BuildTaskClaim, claim.claim_id)
        execution = session.get(BuildRunnerExecution, "exec-stale")

        assert result.applied is False
        assert {item.kind for item in result.cleanup_candidates} >= {"stale_claim", "stale_execution"}
        assert claim is not None and claim.status == "ACTIVE"
        assert execution is not None and execution.status == "RUNNING"


def test_steward_dry_run_respects_configured_responsibilities():
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("HEALTH-ONLY-STALE"))
        stale_claim = claim_task(session, ClaimRequest("HEALTH-ONLY-STALE", worker_id="builder-a"))
        stale_claim.lease_expires_at = now - timedelta(minutes=5)
        session.add(
            BuildRunnerExecution(
                execution_id="exec-health-only-stale",
                task_id="HEALTH-ONLY-STALE",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                claim_id=stale_claim.claim_id,
                status="RUNNING",
            )
        )

        upsert_task(session, _task("HEALTH-ONLY-LOST"))
        lost_claim = claim_task(session, ClaimRequest("HEALTH-ONLY-LOST", worker_id="builder-b"))
        session.add(
            BuildRunnerExecution(
                execution_id="exec-health-only-lost",
                task_id="HEALTH-ONLY-LOST",
                role="BUILDER",
                worker_id="builder-b",
                adapter="fake",
                claim_id=lost_claim.claim_id,
                status="LOST",
                completed_at=now - timedelta(minutes=1),
            )
        )

        session.add(
            BuildWorkerLease(
                lease_id="lease-health-only-expired",
                worker_id="builder-c",
                task_id="HEALTH-ONLY-STALE",
                lease_expires_at=now - timedelta(minutes=1),
                status="ACTIVE",
            )
        )
        session.commit()

    with SessionLocal() as session:
        result = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(
                enabled=True,
                apply=False,
                responsibilities=("health_audits",),
            ),
            now=now,
        )

        assert result.applied is False
        assert result.cleanup_candidates == []
        assert result.audits == ["health:active_leases=1:live_executions=1"]


def test_steward_config_preserves_explicit_empty_responsibilities():
    config = StewardConfig.from_mapping({"enabled": True, "apply": True, "responsibilities": []})

    assert config.enabled is True
    assert config.apply is True
    assert config.responsibilities == ()


def test_steward_config_defaults_responsibilities_when_omitted_or_null():
    omitted = StewardConfig.from_mapping({"enabled": True})
    explicit_null = StewardConfig.from_mapping({"enabled": True, "responsibilities": None})

    assert omitted.responsibilities == StewardConfig().responsibilities
    assert explicit_null.responsibilities == StewardConfig().responsibilities


def test_steward_apply_recovers_stale_claims_idempotently():
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("APPLY-STALE"))
        claim = claim_task(session, ClaimRequest("APPLY-STALE", worker_id="builder-a"))
        claim.lease_expires_at = now - timedelta(minutes=5)
        session.add(
            BuildRunnerExecution(
                execution_id="exec-apply",
                task_id="APPLY-STALE",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                claim_id=claim.claim_id,
                status="RUNNING",
            )
        )
        session.commit()

    with SessionLocal() as session:
        first = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(enabled=True, apply=True, interval_seconds=0),
            now=now,
        )
        second = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(enabled=True, apply=True, interval_seconds=0),
            now=now,
        )
        task = session.get(BuildTask, "APPLY-STALE")
        claim = session.scalar(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "APPLY-STALE"))
        execution = session.get(BuildRunnerExecution, "exec-apply")

        assert first.recovered_tasks == ["APPLY-STALE"]
        assert second.recovered_tasks == []
        assert task is not None and task.state == "STALE"
        assert claim is not None and claim.status == "EXPIRED"
        assert execution is not None and execution.status == "TERMINATED"


def test_steward_apply_expires_active_worker_lease_with_missing_execution():
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("MISSING-EXEC-LEASE"))
        session.add(
            BuildWorkerLease(
                lease_id="lease-missing-execution",
                worker_id="builder-missing-exec",
                task_id="MISSING-EXEC-LEASE",
                execution_id="exec-does-not-exist",
                lease_expires_at=now + timedelta(minutes=5),
                status="ACTIVE",
            )
        )
        session.commit()

    with SessionLocal() as session:
        first = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(
                enabled=True,
                apply=True,
                interval_seconds=0,
                responsibilities=("worker_leases",),
            ),
            now=now,
        )
        second = run_steward_cycle(
            session,
            workers=(_steward(),),
            config=StewardConfig(
                enabled=True,
                apply=True,
                interval_seconds=0,
                responsibilities=("worker_leases",),
            ),
            now=now,
        )
        lease = session.get(BuildWorkerLease, "lease-missing-execution")

        assert first.released_worker_leases == ["lease-missing-execution"]
        assert second.released_worker_leases == []
        assert lease is not None
        assert lease.status == "EXPIRED"
        assert lease.heartbeat_at == now


def test_runner_steward_hook_does_not_dispatch_feature_work_without_normal_task_lifecycle():
    config = RunnerConfig(
        workers=(_steward(),),
        steward=StewardConfig(enabled=True, apply=False),
    )

    result = BuildRunner(SessionLocal, config, executors={}, git=FakeGit()).run_once()

    assert result.launched == []
    assert result.steward["worker_id"] == "steward-1"
    assert result.steward["applied"] is False
