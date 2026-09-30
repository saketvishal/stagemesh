from __future__ import annotations

import os
import time
from pathlib import Path
import pytest

from stagemesh.watcher import WatcherLock, WatcherLockError, WatcherLoop
from stagemesh.persistence import Store


def test_watcher_lock_acquisition_and_single_instance(tmp_path: Path):
    lock_file = tmp_path / "stagemesh.lock"

    lock1 = WatcherLock(lock_file)
    lock1.acquire()
    assert lock_file.exists()

    # Second instance attempting to acquire lock with live PID must fail
    lock2 = WatcherLock(lock_file)
    with pytest.raises(WatcherLockError, match="Daemon lock already held"):
        lock2.acquire()

    lock1.release()
    assert not lock_file.exists()


def test_watcher_lock_stale_pid_recovery(tmp_path: Path):
    lock_file = tmp_path / "stagemesh.lock"

    # Write stale lock with non-existent PID
    stale_pid = 999999
    lock_file.write_text(
        f'{{"pid": {stale_pid}, "boot_id": "dead-boot", "created_at": {time.time() - 3600}, "heartbeat_at": {time.time() - 3600}}}',
        encoding="utf-8",
    )

    # New lock instance recovers dead PID lock safely
    lock = WatcherLock(lock_file)
    lock.acquire()
    assert lock_file.exists()
    assert lock.metadata["pid"] == os.getpid()
    lock.release()


def test_watcher_loop_continuous_execution_and_clean_shutdown(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Watcher Loop Task", source_id="T-WATCH-1")

    ticks_run = 0

    def mock_tick():
        nonlocal ticks_run
        ticks_run += 1
        if ticks_run >= 3:
            loop.stop()

    loop = WatcherLoop(store=store, interval_seconds=0.01, tick_fn=mock_tick)
    loop.run()

    assert ticks_run == 3
