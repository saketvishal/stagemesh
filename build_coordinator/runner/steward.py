"""Explicit steward maintenance operations for coordinator housekeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.events import record_event
from build_coordinator.models import (
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
    BuildWorkerLease,
)
from build_coordinator.runner.models import StewardConfig, WorkerConfig
from build_coordinator.runner.routing import CAP_MAINTENANCE
from build_coordinator.runner.scheduling import active_worktrees, normalize_worktree_path
from build_coordinator.service import (
    recover_expired,
    recover_lost_execution_claims,
    reconcile_stale_executions,
    utcnow,
)
from build_coordinator.types import EventInput

STEWARD_PERMISSION = "COORDINATOR_MAINTENANCE"


@dataclass(frozen=True)
class MaintenanceCandidate:
    kind: str
    identifier: str
    reason: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identifier": self.identifier,
            "reason": self.reason,
            "data": dict(self.data),
        }


@dataclass
class StewardRunResult:
    worker_id: str | None = None
    applied: bool = False
    skipped_reason: str | None = None
    recovered_tasks: list[str] = field(default_factory=list)
    reconciled_executions: list[str] = field(default_factory=list)
    released_worker_leases: list[str] = field(default_factory=list)
    cleanup_candidates: list[MaintenanceCandidate] = field(default_factory=list)
    audits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "applied": self.applied,
            "skipped_reason": self.skipped_reason,
            "recovered_tasks": list(self.recovered_tasks),
            "reconciled_executions": list(self.reconciled_executions),
            "released_worker_leases": list(self.released_worker_leases),
            "cleanup_candidates": [item.to_dict() for item in self.cleanup_candidates],
            "audits": list(self.audits),
        }


def select_steward_worker(workers: Iterable[WorkerConfig]) -> WorkerConfig | None:
    eligible = [
        worker
        for worker in workers
        if worker.enabled
        and "maintenance" in worker.stage_names()
        and CAP_MAINTENANCE in worker.capability_names()
        and STEWARD_PERMISSION in worker.permissions
    ]
    return sorted(eligible, key=lambda item: (item.preference, item.worker_id))[0] if eligible else None


def run_steward_cycle(
    session: Session,
    *,
    workers: Iterable[WorkerConfig],
    config: StewardConfig,
    now: datetime | None = None,
) -> StewardRunResult:
    result = StewardRunResult(applied=config.apply)
    if not config.enabled:
        result.skipped_reason = "DISABLED"
        return result
    worker = select_steward_worker(workers)
    if worker is None:
        result.skipped_reason = "NO_ELIGIBLE_STEWARD_WORKER"
        _record_steward_event(session, result)
        return result
    result.worker_id = worker.worker_id
    now = now or utcnow()
    if _within_interval(session, now, config.interval_seconds):
        result.skipped_reason = "INTERVAL_WAIT"
        return result
    responsibilities = set(config.responsibilities)

    if config.apply:
        if "stale_claims" in responsibilities:
            result.recovered_tasks.extend(
                task.task_id for task in recover_expired(session, actor=worker.worker_id)
            )
        if "stale_executions" in responsibilities:
            result.reconciled_executions.extend(
                row.execution_id for row in reconcile_stale_executions(session)
            )
        if "lost_execution_claims" in responsibilities:
            for task in recover_lost_execution_claims(session, actor=worker.worker_id):
                if task.task_id not in result.recovered_tasks:
                    result.recovered_tasks.append(task.task_id)
        if "worker_leases" in responsibilities:
            result.released_worker_leases.extend(_expire_orphan_worker_leases(session, now))
    else:
        if "stale_claims" in responsibilities:
            result.cleanup_candidates.extend(_stale_claim_candidates(session, now))
        if "stale_executions" in responsibilities:
            result.cleanup_candidates.extend(_stale_execution_candidates(session, now))
        if "lost_execution_claims" in responsibilities:
            result.cleanup_candidates.extend(_lost_execution_claim_candidates(session))
        if "worker_leases" in responsibilities:
            result.cleanup_candidates.extend(_orphan_worker_lease_candidates(session, now))

    if "orphaned_worktrees" in responsibilities:
        result.cleanup_candidates.extend(_orphaned_worktree_candidates(session, now))
    if "waiting_conditions" in responsibilities:
        result.audits.extend(_waiting_condition_audits(session))
    if "health_audits" in responsibilities:
        result.audits.extend(_health_audits(session))
    if "evidence_retention" in responsibilities:
        result.audits.extend(_evidence_retention_audits(session))
    if "hygiene" in responsibilities:
        result.audits.extend(_hygiene_audits(session))
    if "resumption_checks" in responsibilities:
        result.audits.extend(_resumption_audits(session))

    _record_steward_event(session, result)
    return result


def _expire_orphan_worker_leases(session: Session, now: datetime) -> list[str]:
    released: list[str] = []
    leases = session.scalars(
        select(BuildWorkerLease)
        .where(BuildWorkerLease.status == "ACTIVE")
        .where(BuildWorkerLease.lease_expires_at <= now)
    ).all()
    for lease in leases:
        lease.status = "EXPIRED"
        lease.heartbeat_at = now
        released.append(lease.lease_id)
    return released


def _stale_claim_candidates(session: Session, now: datetime) -> list[MaintenanceCandidate]:
    return [
        MaintenanceCandidate(
            "stale_claim",
            claim.claim_id,
            "active claim lease has expired",
            {
                "task_id": claim.task_id,
                "worker_id": claim.worker_id,
                "lease_expires_at": claim.lease_expires_at.isoformat(),
            },
        )
        for claim in session.scalars(
            select(BuildTaskClaim)
            .where(BuildTaskClaim.status == "ACTIVE")
            .where(BuildTaskClaim.lease_expires_at <= now)
        ).all()
    ]


def _stale_execution_candidates(session: Session, now: datetime) -> list[MaintenanceCandidate]:
    candidates: list[MaintenanceCandidate] = []
    live = session.scalars(
        select(BuildRunnerExecution).where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
    ).all()
    for execution in live:
        if not execution.claim_id:
            reason = "live execution has no claim"
        else:
            claim = session.get(BuildTaskClaim, execution.claim_id)
            if claim is not None and claim.status == "ACTIVE" and _as_utc(claim.lease_expires_at) > now:
                continue
            reason = "live execution claim is stale"
        candidates.append(
            MaintenanceCandidate(
                "stale_execution",
                execution.execution_id,
                reason,
                {
                    "task_id": execution.task_id,
                    "worker_id": execution.worker_id,
                    "claim_id": execution.claim_id,
                },
            )
        )
    return candidates


def _lost_execution_claim_candidates(session: Session) -> list[MaintenanceCandidate]:
    candidates: list[MaintenanceCandidate] = []
    terminal = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.status.in_(("LOST", "TERMINATED")))
        .where(BuildRunnerExecution.claim_id.is_not(None))
        .where(BuildRunnerExecution.completed_at.is_not(None))
    ).all()
    for execution in terminal:
        claim = session.get(BuildTaskClaim, execution.claim_id)
        if claim is not None and claim.status == "ACTIVE":
            candidates.append(
                MaintenanceCandidate(
                    "lost_execution_claim",
                    execution.execution_id,
                    "terminal execution still has an active claim",
                    {"task_id": execution.task_id, "claim_id": execution.claim_id},
                )
            )
    return candidates


def _orphan_worker_lease_candidates(session: Session, now: datetime) -> list[MaintenanceCandidate]:
    candidates: list[MaintenanceCandidate] = []
    for lease in session.scalars(select(BuildWorkerLease).where(BuildWorkerLease.status == "ACTIVE")).all():
        if _as_utc(lease.lease_expires_at) <= now:
            candidates.append(
                MaintenanceCandidate(
                    "worker_lease",
                    lease.lease_id,
                    "active worker lease has expired",
                    {"worker_id": lease.worker_id, "execution_id": lease.execution_id},
                )
            )
            continue
        if lease.execution_id and session.get(BuildRunnerExecution, lease.execution_id) is None:
            candidates.append(
                MaintenanceCandidate(
                    "worker_lease",
                    lease.lease_id,
                    "active worker lease references a missing execution",
                    {"worker_id": lease.worker_id, "execution_id": lease.execution_id},
                )
            )
    return candidates


def _orphaned_worktree_candidates(session: Session, now: datetime) -> list[MaintenanceCandidate]:
    busy = active_worktrees(session, now)
    candidates: list[MaintenanceCandidate] = []
    tasks = session.scalars(select(BuildTask).where(BuildTask.worktree_path.is_not(None))).all()
    for task in tasks:
        normalized = normalize_worktree_path(task.worktree_path or "")
        if not normalized or normalized in busy:
            continue
        path_exists = Path(task.worktree_path or "").exists()
        if task.state in {"DONE", "FAILED", "STALE", "BLOCKED"} or not path_exists:
            candidates.append(
                MaintenanceCandidate(
                    "managed_worktree",
                    task.worktree_path or "",
                    "worktree is not actively leased; cleanup requires explicit destructive authorization",
                    {"task_id": task.task_id, "state": task.state, "path_exists": path_exists},
                )
            )
    return candidates


def _waiting_condition_audits(session: Session) -> list[str]:
    tasks = session.scalars(select(BuildTask).where(BuildTask.state == "WAITING_FOR_INPUT")).all()
    return [f"waiting_for_input:{task.task_id}" for task in tasks]


def _health_audits(session: Session) -> list[str]:
    active_leases = len(session.scalars(select(BuildWorkerLease).where(BuildWorkerLease.status == "ACTIVE")).all())
    live_executions = len(
        session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
        ).all()
    )
    return [f"health:active_leases={active_leases}:live_executions={live_executions}"]


def _evidence_retention_audits(session: Session) -> list[str]:
    terminal = len(
        session.scalars(
            select(BuildRunnerExecution).where(
                BuildRunnerExecution.status.in_(
                    (
                        "SUCCEEDED",
                        "FAILED",
                        "HUMAN_ACTION_REQUIRED",
                        "WAITING_FOR_INPUT",
                        "TERMINATED",
                        "LOST",
                    )
                )
            )
        ).all()
    )
    return [f"evidence_retention:terminal_executions={terminal}:compaction_candidates=0"]


def _hygiene_audits(session: Session) -> list[str]:
    task_count = len(session.scalars(select(BuildTask)).all())
    return [f"hygiene:tasks={task_count}"]


def _resumption_audits(session: Session) -> list[str]:
    resumable = session.scalars(select(BuildTask).where(BuildTask.state.in_(("STALE", "RESUMABLE")))).all()
    return [f"resumption_candidate:{task.task_id}:{task.state}" for task in resumable]


def _record_steward_event(session: Session, result: StewardRunResult) -> None:
    record_event(
        session,
        EventInput(
            task_id=None,
            event_type="steward.cycle",
            actor=result.worker_id or "steward",
            event_data=result.to_dict(),
        ),
    )


def _within_interval(session: Session, now: datetime, interval_seconds: float) -> bool:
    if interval_seconds <= 0:
        return False
    latest = session.scalar(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id.is_(None))
        .where(BuildTaskEvent.event_type == "steward.cycle")
        .order_by(BuildTaskEvent.created_at.desc())
        .limit(1)
    )
    if latest is None:
        return False
    return (now - _as_utc(latest.created_at)).total_seconds() < interval_seconds


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
