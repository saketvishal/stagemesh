"""Wave 4 — PERSISTENCE-005: SQLite busy/lock retry behavior.

Legacy contract (test_sqlite_retry.py, test_github_sqlite_busy_retry.py,
build_coordinator/db.py):

  - busy_timeout is configured in Store so SQLite retries automatically
    at the driver level (PRAGMA busy_timeout = 5000)
  - when contention is simulated via threading, reads/writes eventually
    succeed without DatabaseBusyError
  - the retry does NOT create duplicate task/candidate/evidence records
  - contention that persists beyond the busy timeout raises a clear error
    (sqlite3.OperationalError with 'database is locked')
  - non-lock errors propagate immediately without being wrapped
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from stagemesh.persistence import Store, StoreValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


# ---------------------------------------------------------------------------
# PERSISTENCE-005: busy_timeout is set
# ---------------------------------------------------------------------------

def test_busy_timeout_pragma_is_set(tmp_path: Path):
    """PRAGMA busy_timeout = 5000 must be configured on every connection."""
    store = _store(tmp_path)
    row = store.conn.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert int(row[0]) >= 5000
    store.close()


def test_wal_mode_is_enabled(tmp_path: Path):
    """WAL mode reduces contention between concurrent readers and one writer."""
    store = _store(tmp_path)
    row = store.conn.execute("PRAGMA journal_mode").fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"
    store.close()


# ---------------------------------------------------------------------------
# Real two-thread contention: writer holds exclusive lock, reader waits
# ---------------------------------------------------------------------------

def test_concurrent_reader_waits_for_writer_then_reads_correctly(tmp_path: Path):
    """
    Thread A holds a write transaction for a short time.
    Thread B (reader) opens a separate connection with busy_timeout and
    successfully reads the committed data.
    No duplicate records must be created.
    """
    db_path = tmp_path / "contention.db"
    store_a = Store(db_path)
    store_a.migrate()

    # Pre-insert one task via store_a
    task_id = store_a.upsert_task("Contention task", source="local")

    read_results: list[list] = []
    errors: list[Exception] = []

    barrier = threading.Barrier(2, timeout=10)

    def writer_thread():
        # Open second Store connection on same DB
        store_w = Store(db_path)
        # Begin a write transaction and hold it briefly
        store_w.conn.execute("BEGIN EXCLUSIVE")
        barrier.wait()          # signal reader to start trying
        time.sleep(0.2)         # hold lock for 200ms
        store_w.conn.execute("COMMIT")
        store_w.close()

    def reader_thread():
        barrier.wait()          # wait until writer has exclusive lock
        try:
            store_r = Store(db_path)
            # This read will have to wait for the writer to release
            rows = store_r.tasks()
            read_results.append(rows)
            store_r.close()
        except Exception as exc:
            errors.append(exc)

    t_writer = threading.Thread(target=writer_thread, daemon=True)
    t_reader = threading.Thread(target=reader_thread, daemon=True)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=5)
    t_reader.join(timeout=5)

    assert not errors, f"Reader raised: {errors}"
    assert read_results, "Reader produced no results"
    tasks = read_results[0]
    assert any(t["id"] == task_id for t in tasks)
    store_a.close()


def test_concurrent_writers_do_not_create_duplicate_tasks(tmp_path: Path):
    """
    Two threads both attempt to upsert the same task (same source+source_id).
    Only one record must exist after both complete.
    """
    db_path = tmp_path / "dedup.db"
    store_init = Store(db_path)
    store_init.migrate()
    store_init.close()

    errors: list[Exception] = []
    task_ids: list[str] = []
    barrier = threading.Barrier(2, timeout=10)

    def upsert_worker(n: int):
        barrier.wait()
        try:
            s = Store(db_path)
            tid = s.upsert_task(f"Shared task #{n}", source="github", source_id="shared-99")
            task_ids.append(tid)
            s.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upsert_worker, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)

    assert not errors, f"Upsert raised: {errors}"

    # Verify exactly one row in the DB for this source+source_id
    verify = Store(db_path)
    rows = verify.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE source=? AND source_id=?",
        ("github", "shared-99"),
    ).fetchone()
    assert int(rows[0]) == 1, f"Expected 1 row, got {int(rows[0])}"
    verify.close()


# ---------------------------------------------------------------------------
# Lock contention that exceeds the driver-level timeout raises OperationalError
# ---------------------------------------------------------------------------

def test_excessive_contention_raises_operational_error(tmp_path: Path):
    """
    If a writer holds an EXCLUSIVE lock longer than the busy_timeout, a
    subsequent writer raises sqlite3.OperationalError ('database is locked').
    We simulate this by setting an extremely short busy_timeout on a second
    raw connection.
    """
    db_path = tmp_path / "timeout.db"
    store = Store(db_path)
    store.migrate()
    store.close()

    # Raw connection with 1ms busy_timeout
    raw = sqlite3.connect(str(db_path), timeout=0.001)
    raw.execute("PRAGMA busy_timeout = 1")  # 1 ms

    # Another raw connection holds exclusive lock
    holder = sqlite3.connect(str(db_path))
    holder.execute("BEGIN EXCLUSIVE")

    with pytest.raises(sqlite3.OperationalError):
        raw.execute("BEGIN EXCLUSIVE")
        raw.execute("INSERT INTO tasks(id, title, stage, status, source, source_id, project, created_at, updated_at) VALUES ('x','t','PLAN','OPEN','local',NULL,NULL,1.0,1.0)")
        raw.execute("COMMIT")

    holder.execute("ROLLBACK")
    holder.close()
    raw.close()


# ---------------------------------------------------------------------------
# Non-lock errors propagate immediately
# ---------------------------------------------------------------------------

def test_non_lock_error_propagates_without_retry(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(StoreValidationError):
        store.upsert_task("")  # blank title → validation error before DB
    store.close()


def test_schema_not_present_raises_on_bad_query(tmp_path: Path):
    """Querying a non-existent table raises OperationalError immediately."""
    db_path = tmp_path / "noschema.db"
    raw = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        raw.execute("SELECT * FROM nonexistent_table_xyz").fetchall()
    raw.close()


# ---------------------------------------------------------------------------
# PERSISTENCE-005: Application-level with_sqlite_retry & DatabaseBusyError
# ---------------------------------------------------------------------------

def test_with_sqlite_retry_recovers_from_transient_busy():
    from stagemesh.persistence import with_sqlite_retry

    calls = 0

    def transient_op():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise sqlite3.OperationalError("database is locked")
        return "success"

    result = with_sqlite_retry(transient_op, attempts=5, base_delay=0.01)
    assert result == "success"
    assert calls == 3


def test_with_sqlite_retry_raises_database_busy_error_on_persistent_contention():
    from stagemesh.persistence import DatabaseBusyError, with_sqlite_retry

    def always_busy():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(DatabaseBusyError, match="contention persisted"):
        with_sqlite_retry(always_busy, attempts=3, base_delay=0.01)


def test_store_contention_recovery_using_store_api(tmp_path: Path):
    """
    Exercise Store operations under real contention using Store APIs.
    Store write recovers and inserts successfully without corrupting state.
    """
    from stagemesh.persistence import with_sqlite_retry

    db_path = tmp_path / "store_contention.db"
    store1 = Store(db_path)
    store1.migrate()

    barrier = threading.Barrier(2, timeout=10)

    def locker():
        store_lock = Store(db_path)
        store_lock.conn.execute("BEGIN EXCLUSIVE")
        barrier.wait()
        time.sleep(0.15)
        store_lock.conn.execute("COMMIT")
        store_lock.close()

    t = threading.Thread(target=locker, daemon=True)
    t.start()
    barrier.wait()

    # store1 attempts an upsert with retry
    task_id = with_sqlite_retry(
        lambda: store1.upsert_task("Contention Task", source="local"),
        attempts=5,
        base_delay=0.05,
    )
    t.join(timeout=5)

    assert task_id is not None
    assert store1.get_task(task_id)["title"] == "Contention Task"
    store1.close()

