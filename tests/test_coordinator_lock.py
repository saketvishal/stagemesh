"""Single-coordinator-per-project lock tests (GH-101)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from build_coordinator import coordinator_lock
from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildCoordinatorLock


@pytest.fixture(autouse=True)
def clean_coordinator_lock():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        session.execute(delete(BuildCoordinatorLock))
        session.commit()
    yield


def test_first_acquisition_succeeds():
    with SessionLocal() as session:
        acquisition = coordinator_lock.acquire_coordinator_lock(
            session, pid=12345, instance_id="owner-1"
        )
        session.commit()
        assert not acquisition.recovered_stale
        assert acquisition.record.process_id == 12345


def test_live_owner_prevents_second_coordinator(monkeypatch):
    with SessionLocal() as session:
        coordinator_lock.acquire_coordinator_lock(session, pid=os.getpid(), instance_id="owner-1")
        session.commit()

    with SessionLocal() as session:
        monkeypatch.setattr(coordinator_lock, "is_pid_alive", lambda pid: True)
        with pytest.raises(coordinator_lock.CoordinatorLockHeld):
            coordinator_lock.acquire_coordinator_lock(
                session, pid=os.getpid() + 1, instance_id="owner-2"
            )


def test_same_instance_can_reacquire_for_next_cycle():
    with SessionLocal() as session:
        first = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="owner-1"
        )
        session.commit()

    with SessionLocal() as session:
        second = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="owner-1"
        )
        session.commit()
        assert not second.recovered_stale
        assert second.record.process_id == os.getpid()


def test_dead_pid_lock_is_reclaimed(monkeypatch):
    monkeypatch.setattr(coordinator_lock, "is_pid_alive", lambda pid: False)
    with SessionLocal() as session:
        coordinator_lock.acquire_coordinator_lock(session, pid=999_999, instance_id="old-owner")
        session.commit()

    with SessionLocal() as session:
        acquisition = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="new-owner"
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.recovery_reason == "owner_process_dead"
        assert acquisition.record.instance_id == "new-owner"


def test_live_pid_with_stale_heartbeat_is_reclaimed(monkeypatch):
    monkeypatch.setattr(coordinator_lock, "is_pid_alive", lambda pid: True)
    with SessionLocal() as session:
        coordinator_lock.acquire_coordinator_lock(session, pid=50784, instance_id="old-owner")
        record = session.get(BuildCoordinatorLock, 1)
        record.heartbeat_at = datetime.now(UTC) - timedelta(
            seconds=coordinator_lock.STALE_HEARTBEAT_SECONDS + 5
        )
        session.commit()

    with SessionLocal() as session:
        acquisition = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="new-owner"
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.recovery_reason == "owner_lease_expired"


def test_release_then_reacquire_by_other_instance_succeeds():
    with SessionLocal() as session:
        coordinator_lock.acquire_coordinator_lock(session, pid=os.getpid(), instance_id="owner-1")
        session.commit()

    with SessionLocal() as session:
        coordinator_lock.release_coordinator_lock(session, instance_id="owner-1")
        session.commit()

    with SessionLocal() as session:
        acquisition = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="owner-2"
        )
        session.commit()
        assert acquisition.record.instance_id == "owner-2"


def test_heartbeat_refreshes_lease():
    with SessionLocal() as session:
        acquisition = coordinator_lock.acquire_coordinator_lock(
            session, pid=os.getpid(), instance_id="owner-1"
        )
        first_heartbeat = acquisition.record.heartbeat_at
        session.commit()

    with SessionLocal() as session:
        coordinator_lock.heartbeat_coordinator_lock(session, instance_id="owner-1")
        session.commit()
        record = session.get(BuildCoordinatorLock, 1)
        assert record.heartbeat_at >= first_heartbeat
