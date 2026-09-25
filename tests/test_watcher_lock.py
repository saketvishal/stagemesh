"""Watcher lock tests (SDD-001 section 9.4)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildWatcherRecord
from build_coordinator.watcher import lock as watcher_lock


@pytest.fixture(autouse=True)
def clean_watcher_records():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        session.execute(delete(BuildWatcherRecord))
        session.commit()
    yield


def test_first_acquisition_succeeds():
    with SessionLocal() as session:
        acquisition = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test1",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=12345,
            instance_id="owner-1",
        )
        session.commit()
        assert not acquisition.recovered_stale
        assert acquisition.record.process_id == 12345


def test_live_same_scope_watcher_prevents_duplicate_start(monkeypatch):
    with SessionLocal() as session:
        # Use this test process's own PID -- guaranteed alive -- to
        # exercise the "live watcher" branch deterministically.
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test2",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
        )
        session.commit()

    with SessionLocal() as session:
        monkeypatch.setattr(watcher_lock, "is_pid_alive", lambda pid: True)
        with pytest.raises(watcher_lock.WatcherLockHeld):
            watcher_lock.acquire_lock(
                session,
                task_name="BuildCoordinator-test2",
                control_repo_root="C:/repo",
                repository_slug="saketvishal/stagemesh-orchestrator",
                pid=os.getpid() + 1,
                instance_id="owner-2",
            )


def test_same_process_can_refresh_existing_lock_for_next_cycle():
    with SessionLocal() as session:
        first = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test-reentrant",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="owner-1",
        )
        first_heartbeat = first.record.heartbeat_at
        session.commit()

    with SessionLocal() as session:
        second = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test-reentrant",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="owner-1",
        )
        session.commit()
        assert not second.recovered_stale
        assert second.record.process_id == os.getpid()
        assert second.record.heartbeat_at is not None


def test_stale_same_host_pid_is_recoverable():
    # A PID this unlikely to be alive stands in for a crashed watcher.
    dead_pid = 999_999
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test3",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=dead_pid,
            instance_id="owner-1",
        )
        session.commit()

    with SessionLocal() as session:
        acquisition = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test3",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="owner-2",
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.record.process_id == os.getpid()
        assert acquisition.record.restart_count == 1
        assert acquisition.record.watcher_id == "owner-2"


def test_dead_pid_with_fresh_heartbeat_is_recoverable(monkeypatch):
    monkeypatch.setattr(watcher_lock, "is_pid_alive", lambda pid: False)
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-dead-fresh",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=50784,
            instance_id="old-owner",
        )
        session.commit()

    with SessionLocal() as session:
        acquisition = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-dead-fresh",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="new-owner",
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.recovery_reason == "owner_process_dead"
        assert acquisition.record.watcher_id == "new-owner"


def test_live_pid_with_stale_heartbeat_is_recoverable(monkeypatch):
    monkeypatch.setattr(watcher_lock, "is_pid_alive", lambda pid: True)
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-live-stale",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=50784,
            instance_id="old-owner",
        )
        record = session.get(BuildWatcherRecord, "BuildCoordinator-live-stale")
        record.heartbeat_at = datetime.now(UTC) - timedelta(
            seconds=watcher_lock.STALE_HEARTBEAT_SECONDS + 5
        )
        session.commit()

    with SessionLocal() as session:
        acquisition = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-live-stale",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="new-owner",
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.recovery_reason == "owner_lease_expired"


def test_stale_db_state_from_pid_50784_shape_recovers(monkeypatch):
    monkeypatch.setattr(watcher_lock, "is_pid_alive", lambda pid: True)
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-f252c95ffef225a1",
            control_repo_root="C:/stagemesh-orchestrator",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=50784,
            instance_id="ffdc04c2-b381-4f15-bb73-a22151552513",
        )
        record = session.get(BuildWatcherRecord, "BuildCoordinator-f252c95ffef225a1")
        record.heartbeat_at = datetime(2026, 9, 13, 23, 17, 58, 884875, tzinfo=UTC)
        record.last_cycle_at = datetime(2026, 9, 13, 23, 17, 58, 888303, tzinfo=UTC)
        session.commit()

    with SessionLocal() as session:
        acquisition = watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-f252c95ffef225a1",
            control_repo_root="C:/stagemesh-orchestrator",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
            instance_id="new-owner",
        )
        session.commit()
        assert acquisition.recovered_stale
        assert acquisition.recovery_reason == "owner_lease_expired"
        assert acquisition.record.process_id == os.getpid()
        assert acquisition.record.watcher_id == "new-owner"


def test_stop_request_is_durable():
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test4",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
        )
        session.commit()

    with SessionLocal() as session:
        watcher_lock.request_stop(session, "BuildCoordinator-test4")
        session.commit()

    with SessionLocal() as session:
        record = session.get(BuildWatcherRecord, "BuildCoordinator-test4")
        assert record.stop_requested is True


def test_record_cycle_climbs_consecutive_failure_count_and_resets_on_success():
    with SessionLocal() as session:
        watcher_lock.acquire_lock(
            session,
            task_name="BuildCoordinator-test5",
            control_repo_root="C:/repo",
            repository_slug="saketvishal/stagemesh-orchestrator",
            pid=os.getpid(),
        )
        session.commit()

    with SessionLocal() as session:
        for _ in range(3):
            watcher_lock.record_cycle(
                session, "BuildCoordinator-test5", summary={}, error_type="TRANSIENT_GITHUB_FAILURE"
            )
        session.commit()
        record = session.get(BuildWatcherRecord, "BuildCoordinator-test5")
        assert record.consecutive_failure_count == 3

    with SessionLocal() as session:
        watcher_lock.record_cycle(session, "BuildCoordinator-test5", summary={"mode": "RUNNING"})
        session.commit()
        record = session.get(BuildWatcherRecord, "BuildCoordinator-test5")
        assert record.consecutive_failure_count == 0
