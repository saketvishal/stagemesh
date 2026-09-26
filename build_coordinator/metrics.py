"""Durable coordinator metrics derived from persisted state."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import mean, median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from build_coordinator.claims import CLAIMABLE_STATES, utcnow
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskClaim

LIVE_EXECUTION_STATUSES = frozenset({"LAUNCHED", "RUNNING"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _duration_seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (_as_utc(end) - _as_utc(start)).total_seconds())


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "avg_seconds": None,
            "p50_seconds": None,
            "max_seconds": None,
        }
    return {
        "count": len(values),
        "avg_seconds": mean(values),
        "p50_seconds": median(values),
        "max_seconds": max(values),
    }


def _queue_depth(session: Session) -> dict[str, Any]:
    by_state = dict(
        session.execute(
            select(BuildTask.state, func.count())
            .where(BuildTask.state.in_(tuple(CLAIMABLE_STATES)))
            .group_by(BuildTask.state)
        ).all()
    )
    by_state = {state: int(by_state.get(state, 0)) for state in sorted(CLAIMABLE_STATES)}
    return {
        "total": sum(by_state.values()),
        "by_state": by_state,
    }


def _claim_latency(session: Session) -> dict[str, Any]:
    task_rows = {
        task_id: created_at
        for task_id, created_at in session.execute(
            select(BuildTask.task_id, BuildTask.created_at)
        ).all()
    }
    first_claims: dict[tuple[str, str], datetime] = {}
    for task_id, claim_type, claimed_at in session.execute(
        select(BuildTaskClaim.task_id, BuildTaskClaim.claim_type, BuildTaskClaim.claimed_at)
        .order_by(BuildTaskClaim.claimed_at.asc())
    ).all():
        first_claims.setdefault((task_id, claim_type), claimed_at)

    by_claim_type: dict[str, dict[str, float | int | None]] = {}
    for claim_type in sorted({claim_type for _task_id, claim_type in first_claims}):
        latencies = [
            _duration_seconds(task_rows[task_id], claimed_at)
            for (task_id, row_claim_type), claimed_at in first_claims.items()
            if row_claim_type == claim_type and task_id in task_rows
        ]
        by_claim_type[claim_type] = _latency_summary(latencies)

    implementation_latencies = [
        _duration_seconds(task_rows[task_id], claimed_at)
        for (task_id, claim_type), claimed_at in first_claims.items()
        if claim_type == "IMPLEMENTATION" and task_id in task_rows
    ]
    return {
        "implementation": _latency_summary(implementation_latencies),
        "by_claim_type": by_claim_type,
    }


def _execution_outcomes(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(
            BuildRunnerExecution.status,
            BuildRunnerExecution.role,
            func.count(),
        ).group_by(BuildRunnerExecution.status, BuildRunnerExecution.role)
    ).all()
    by_status: Counter[str] = Counter()
    by_role: dict[str, Counter[str]] = defaultdict(Counter)
    total = 0
    for status, role, count in rows:
        amount = int(count)
        total += amount
        by_status[status] += amount
        by_role[role][status] += amount
    return {
        "total": total,
        "by_status": dict(sorted(by_status.items())),
        "by_role": {
            role: dict(sorted(statuses.items()))
            for role, statuses in sorted(by_role.items())
        },
    }


def _worker_utilisation(session: Session, now: datetime) -> dict[str, Any]:
    workers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "active_claims": 0,
            "active_executions": 0,
            "total_executions": 0,
            "completed_executions": 0,
            "total_busy_seconds": 0.0,
            "outcomes": {},
        }
    )
    active_claim_rows = session.execute(
        select(BuildTaskClaim.worker_id, func.count())
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
        .group_by(BuildTaskClaim.worker_id)
    ).all()
    for worker_id, count in active_claim_rows:
        workers[worker_id]["active_claims"] = int(count)

    outcome_counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in session.scalars(select(BuildRunnerExecution)).all():
        metrics = workers[row.worker_id]
        metrics["total_executions"] += 1
        outcome_counters[row.worker_id][row.status] += 1
        if row.status in LIVE_EXECUTION_STATUSES:
            metrics["active_executions"] += 1
            metrics["total_busy_seconds"] += _duration_seconds(row.launched_at, now)
        elif row.completed_at is not None:
            metrics["completed_executions"] += 1
            metrics["total_busy_seconds"] += _duration_seconds(row.launched_at, row.completed_at)

    payload = {}
    for worker_id, metrics in sorted(workers.items()):
        active_units = max(metrics["active_claims"], metrics["active_executions"])
        payload[worker_id] = {
            **metrics,
            "active_units": active_units,
            "outcomes": dict(sorted(outcome_counters[worker_id].items())),
        }
    return payload


def coordinator_metrics(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Return metrics computed from durable coordinator rows."""

    observed_at = now or utcnow()
    return {
        "observed_at": observed_at.isoformat(),
        "queue_depth": _queue_depth(session),
        "claim_latency": _claim_latency(session),
        "execution_outcomes": _execution_outcomes(session),
        "worker_utilisation": _worker_utilisation(session, observed_at),
    }
