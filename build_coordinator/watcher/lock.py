"""Watcher lock and process metadata (SDD-001 section 4.3).

A coordinator-owned `BuildWatcherRecord` row is the single active-watcher
lock for one repository authorization scope (keyed by the deterministic,
repo-scoped Task Scheduler task name). This lock is separate from
`BuildTaskClaim` -- worker claims remain the authoritative task-ownership
mechanism; this only prevents two watcher processes from running
unattended against the same authorized repository at once.

If the recorded metadata points to a dead PID on the same host, startup
recovers the stale lock and continues. If it points to a live PID, startup
returns the existing status instead of launching a second watcher.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from build_coordinator.models import BuildWatcherRecord, new_uuid


STALE_HEARTBEAT_SECONDS = 30.0


class WatcherLockHeld(RuntimeError):
    """Raised when a live watcher already holds the lock for this scope."""

    def __init__(self, message: str, *, owner_health: dict | None = None) -> None:
        super().__init__(message)
        self.owner_health = owner_health or {}


def _now():
    return datetime.now(UTC)


def _host_name() -> str:
    return socket.gethostname()


def query_pid_liveness(pid: int) -> bool | None:
    """Return Windows PID liveness, or None when the probe is inconclusive.

    The unattended watcher can run under a different Windows logon SID than an
    observing shell. In that case `tasklist` may fail with access/visibility
    errors even while the watcher is alive and heartbeating. Callers that need
    hard exclusion semantics can treat None as not alive; status reporting can
    surface it honestly as unknown.
    """
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.lstrip().startswith('"') and f',"{pid}",' in result.stdout


def is_pid_alive(pid: int) -> bool:
    """Windows-only liveness check for lock recovery."""
    return query_pid_liveness(pid) is True


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _heartbeat_age_seconds(record: BuildWatcherRecord, *, now: datetime) -> float | None:
    heartbeat_at = _coerce_utc(record.heartbeat_at)
    if heartbeat_at is None:
        return None
    return max(0.0, (now - heartbeat_at).total_seconds())


def _lease_expired(record: BuildWatcherRecord, *, now: datetime, stale_after_seconds: float) -> bool:
    age = _heartbeat_age_seconds(record, now=now)
    return age is None or age > stale_after_seconds


def describe_owner_health(
    record: BuildWatcherRecord,
    *,
    current_instance_id: str | None,
    current_pid: int | None,
    stale_after_seconds: float = STALE_HEARTBEAT_SECONDS,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    owner_process_alive = None
    if record.process_id is not None and record.host_name == _host_name():
        owner_process_alive = is_pid_alive(record.process_id)
    same_instance = bool(
        current_instance_id
        and record.watcher_id == current_instance_id
        and current_pid is not None
        and record.process_id == current_pid
        and record.host_name == _host_name()
    )
    lease_expired = _lease_expired(record, now=now, stale_after_seconds=stale_after_seconds)
    return {
        "current_instance_id": current_instance_id,
        "recorded_owner_instance_id": record.watcher_id,
        "current_pid": current_pid,
        "recorded_owner_pid": record.process_id,
        "recorded_owner_host": record.host_name,
        "owner_process_alive": owner_process_alive,
        "owner_started_at": record.started_at,
        "heartbeat_age_seconds": _heartbeat_age_seconds(record, now=now),
        "stale_after_seconds": stale_after_seconds,
        "lease_expired": lease_expired,
        "same_instance": same_instance,
    }


@dataclass(frozen=True)
class LockAcquisition:
    record: BuildWatcherRecord
    recovered_stale: bool
    recovery_reason: str | None = None


def acquire_lock(
    session: Session,
    *,
    task_name: str,
    control_repo_root: str,
    repository_slug: str,
    pid: int | None = None,
    instance_id: str | None = None,
    stale_after_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> LockAcquisition:
    """Acquire (or recover) the watcher lock for `task_name`. Raises
    `WatcherLockHeld` if a live watcher on this host already holds it."""
    pid = pid if pid is not None else os.getpid()
    instance_id = instance_id or new_uuid()
    record = session.get(BuildWatcherRecord, task_name)
    now = _now()
    if record is None:
        record = BuildWatcherRecord(
            task_name=task_name,
            watcher_id=instance_id,
            control_repo_root=control_repo_root,
            repository_slug=repository_slug,
            host_name=_host_name(),
            process_id=pid,
            started_at=now,
            heartbeat_at=now,
            stop_requested=False,
        )
        session.add(record)
        session.flush()
        return LockAcquisition(record=record, recovered_stale=False)

    recovered_stale = False
    recovery_reason = None
    if record.process_id is not None and record.host_name == _host_name():
        owner_health = describe_owner_health(
            record,
            current_instance_id=instance_id,
            current_pid=pid,
            stale_after_seconds=stale_after_seconds,
            now=now,
        )
        if owner_health["same_instance"]:
            record.control_repo_root = control_repo_root
            record.repository_slug = repository_slug
            record.heartbeat_at = now
            record.stop_requested = False
            session.flush()
            return LockAcquisition(record=record, recovered_stale=False)
        if owner_health["owner_process_alive"] is True and owner_health["lease_expired"] is False:
            raise WatcherLockHeld(
                f"watcher {task_name!r} is already running as PID {record.process_id} "
                f"on {record.host_name!r}",
                owner_health={**owner_health, "reclaim_decision": "held", "reason": "owner_alive_and_lease_fresh"},
            )
        recovered_stale = True
        recovery_reason = (
            "owner_process_dead"
            if owner_health["owner_process_alive"] is False
            else "owner_lease_expired"
        )
    elif record.process_id is not None and record.host_name != _host_name():
        owner_health = describe_owner_health(
            record,
            current_instance_id=instance_id,
            current_pid=pid,
            stale_after_seconds=stale_after_seconds,
            now=now,
        )
        if owner_health["lease_expired"]:
            recovered_stale = True
            recovery_reason = "cross_host_owner_lease_expired"
        else:
            # Cross-host liveness cannot be checked from here; a fresh
            # cross-host lease is still authoritative and must fail closed.
            raise WatcherLockHeld(
                f"watcher {task_name!r} is recorded as running on a different host "
                f"({record.host_name!r}); refusing to take over the fresh lock from here",
                owner_health={**owner_health, "reclaim_decision": "held", "reason": "cross_host_lease_fresh"},
            )
    elif record.process_id is None:
        recovered_stale = True
        recovery_reason = "released_lock"

    record.control_repo_root = control_repo_root
    record.repository_slug = repository_slug
    record.watcher_id = instance_id
    record.host_name = _host_name()
    record.process_id = pid
    record.started_at = now
    record.heartbeat_at = now
    record.restart_count = (record.restart_count or 0) + (1 if recovered_stale else 0)
    record.stop_requested = False
    session.flush()
    return LockAcquisition(record=record, recovered_stale=recovered_stale, recovery_reason=recovery_reason)


def heartbeat(session: Session, task_name: str) -> BuildWatcherRecord | None:
    record = session.get(BuildWatcherRecord, task_name)
    if record is None:
        return None
    record.heartbeat_at = _now()
    session.flush()
    return record


def record_cycle(
    session: Session,
    task_name: str,
    *,
    summary: dict,
    error_type: str | None = None,
    error_message_redacted: str | None = None,
    backoff_until: datetime | None = None,
) -> BuildWatcherRecord | None:
    """Record the outcome of one watcher cycle. A cycle with no
    `error_type` is a success and resets `consecutive_failure_count` to 0;
    otherwise the counter increments so repeated failures of the same or a
    different class keep climbing the caller's exponential backoff curve
    instead of plateauing after the second failure."""
    record = session.get(BuildWatcherRecord, task_name)
    if record is None:
        return None
    record.last_cycle_at = _now()
    record.last_cycle_summary = summary
    record.last_error_type = error_type
    record.last_error_message_redacted = error_message_redacted
    record.backoff_until = backoff_until
    record.consecutive_failure_count = (record.consecutive_failure_count or 0) + 1 if error_type else 0
    session.flush()
    return record


def request_stop(session: Session, task_name: str) -> BuildWatcherRecord | None:
    record = session.get(BuildWatcherRecord, task_name)
    if record is None:
        return None
    record.stop_requested = True
    session.flush()
    return record


def release_lock(session: Session, task_name: str) -> None:
    record = session.get(BuildWatcherRecord, task_name)
    if record is None:
        return
    record.process_id = None
    session.flush()
