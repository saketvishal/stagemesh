"""Single-coordinator-per-project ownership lock (GH-101).

`BuildCoordinatorLock` (see `build_coordinator.models`) holds exactly one
row per database, recording the process that currently owns the
`stagemesh continue` orchestration loop. A second `stagemesh continue`
against the same SQLite file fails fast with `CoordinatorLockHeld` instead
of racing the first process's writes; a lock left behind by a process that
died is reclaimed automatically.

This intentionally does not attempt any cross-process/cross-thread
concurrency model beyond "one coordinator loop, one database" -- that is
the supported concurrency model referenced by the task. Anything else
(two coordinators cooperating on one project) is out of scope here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from build_coordinator.db import _is_transient_sqlite_lock_error
from build_coordinator.models import BuildCoordinatorLock, new_uuid
from build_coordinator.watcher.lock import _host_name, is_pid_alive

STALE_HEARTBEAT_SECONDS = 30.0


class CoordinatorLockHeld(RuntimeError):
    """Raised when a live coordinator already owns this database."""

    def __init__(self, message: str, *, owner: dict | None = None) -> None:
        super().__init__(message)
        self.owner = owner or {}


def _now() -> datetime:
    return datetime.now(UTC)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _heartbeat_age_seconds(record: BuildCoordinatorLock, *, now: datetime) -> float | None:
    heartbeat_at = _coerce_utc(record.heartbeat_at)
    if heartbeat_at is None:
        return None
    return max(0.0, (now - heartbeat_at).total_seconds())


@dataclass(frozen=True)
class CoordinatorLockAcquisition:
    record: BuildCoordinatorLock
    recovered_stale: bool
    recovery_reason: str | None = None


def acquire_coordinator_lock(
    session: Session,
    *,
    pid: int | None = None,
    instance_id: str | None = None,
    stale_after_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> CoordinatorLockAcquisition:
    """Acquire (or reclaim) the coordinator lock for this database.

    Raises `CoordinatorLockHeld` if a live process on this host already
    holds a fresh lease. A lease recorded on a different host is treated as
    live unless it has already gone stale, since liveness cannot be probed
    remotely -- StageMesh's supported model is one SQLite file per project
    accessed from one host at a time.
    """
    pid = pid if pid is not None else os.getpid()
    instance_id = instance_id or new_uuid()
    now = _now()
    record = session.get(BuildCoordinatorLock, 1)
    if record is None:
        record = BuildCoordinatorLock(
            singleton_id=1,
            instance_id=instance_id,
            host_name=_host_name(),
            process_id=pid,
            started_at=now,
            heartbeat_at=now,
        )
        session.add(record)
        try:
            session.flush()
        except OperationalError as exc:
            session.rollback()
            if _is_transient_sqlite_lock_error(exc):
                # A short-lived SQLite writer lock, not a real ownership
                # conflict -- propagate so `with_sqlite_retry` can retry the
                # whole acquisition rather than misclassifying it as a held
                # coordinator lock.
                raise
            try:
                winner = session.get(BuildCoordinatorLock, 1)
            except OperationalError:
                winner = None
            if winner is not None:
                _raise_lock_held(winner, now=_now(), stale_after_seconds=stale_after_seconds)
            raise CoordinatorLockHeld(
                "another coordinator acquired this project while this process was starting; "
                "refusing to start a second coordinator against the same database"
            ) from exc
        except IntegrityError as exc:
            session.rollback()
            try:
                winner = session.get(BuildCoordinatorLock, 1)
            except OperationalError:
                winner = None
            if winner is not None:
                _raise_lock_held(winner, now=_now(), stale_after_seconds=stale_after_seconds)
            raise CoordinatorLockHeld(
                "another coordinator acquired this project while this process was starting; "
                "refusing to start a second coordinator against the same database"
            ) from exc
        return CoordinatorLockAcquisition(record=record, recovered_stale=False)

    same_instance = record.instance_id == instance_id and record.process_id == pid and record.host_name == _host_name()
    if same_instance:
        record.heartbeat_at = now
        session.flush()
        return CoordinatorLockAcquisition(record=record, recovered_stale=False)

    age = _heartbeat_age_seconds(record, now=now)
    lease_expired = age is None or age > stale_after_seconds
    if record.process_id is not None and record.host_name == _host_name():
        owner_alive = is_pid_alive(record.process_id)
        if owner_alive:
            raise CoordinatorLockHeld(
                f"a coordinator is already running against this project as PID "
                f"{record.process_id} on {record.host_name!r} "
                f"(heartbeat {age:.1f}s ago); refusing to start a second "
                "`stagemesh continue` against the same database",
                owner={
                    "process_id": record.process_id,
                    "host_name": record.host_name,
                    "heartbeat_age_seconds": age,
                },
            )
        recovery_reason = "owner_process_dead"
    elif record.process_id is not None and not lease_expired:
        raise CoordinatorLockHeld(
            f"a coordinator is recorded as running on a different host "
            f"({record.host_name!r}); refusing to take over the fresh lock from here",
            owner={"process_id": record.process_id, "host_name": record.host_name, "heartbeat_age_seconds": age},
        )
    elif record.process_id is not None:
        recovery_reason = "cross_host_lease_expired"
    else:
        recovery_reason = None

    record.instance_id = instance_id
    record.host_name = _host_name()
    record.process_id = pid
    record.started_at = now
    record.heartbeat_at = now
    session.flush()
    return CoordinatorLockAcquisition(
        record=record,
        recovered_stale=recovery_reason is not None,
        recovery_reason=recovery_reason,
    )


def _raise_lock_held(record: BuildCoordinatorLock, *, now: datetime, stale_after_seconds: float) -> None:
    age = _heartbeat_age_seconds(record, now=now)
    if record.process_id is not None and record.host_name == _host_name():
        if is_pid_alive(record.process_id):
            raise CoordinatorLockHeld(
                f"a coordinator is already running against this project as PID "
                f"{record.process_id} on {record.host_name!r} "
                f"(heartbeat {age:.1f}s ago); refusing to start a second "
                "`stagemesh continue` against the same database",
                owner={
                    "process_id": record.process_id,
                    "host_name": record.host_name,
                    "heartbeat_age_seconds": age,
                },
            )
    raise CoordinatorLockHeld(
        "another coordinator owns this project database; refusing to start a second coordinator",
        owner={"process_id": record.process_id, "host_name": record.host_name, "heartbeat_age_seconds": age},
    )


def heartbeat_coordinator_lock(session: Session, *, instance_id: str) -> None:
    record = session.get(BuildCoordinatorLock, 1)
    if record is None or record.instance_id != instance_id:
        return
    record.heartbeat_at = _now()
    session.flush()


def release_coordinator_lock(session: Session, *, instance_id: str) -> None:
    record = session.get(BuildCoordinatorLock, 1)
    if record is None or record.instance_id != instance_id:
        return
    record.process_id = None
    session.flush()
