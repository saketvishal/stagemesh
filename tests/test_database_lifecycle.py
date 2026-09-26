from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text

from build_coordinator.db import (
    CURRENT_SCHEMA_VERSION,
    DatabaseLifecycle,
    DatabaseSchemaError,
    DatabaseUnconfiguredError,
    SCHEMA_VERSION_TABLE,
    TestStateIsolationError,
    engine_from_url,
    get_process_database,
    reset_process_database,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_import_does_not_bind_a_process_database():
    script = (
        "from build_coordinator import db; "
        "assert db.get_process_database() is None\n"
    )
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env.pop("BUILD_COORDINATOR_DATABASE_URL", None)
    env.pop("BUILD_COORDINATOR_DATA_DIR", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_independent_lifecycles_in_one_process(tmp_path: Path):
    first = tmp_path / "one.sqlite3"
    second = tmp_path / "two.sqlite3"
    a = DatabaseLifecycle(f"sqlite:///{first.as_posix()}", data_dir=tmp_path)
    b = DatabaseLifecycle(f"sqlite:///{second.as_posix()}", data_dir=tmp_path)
    a.initialize_schema()
    b.initialize_schema()
    with a.session() as session:
        session.execute(text("CREATE TABLE IF NOT EXISTS marker_a (id INTEGER PRIMARY KEY)"))
        session.execute(text("INSERT INTO marker_a (id) VALUES (1)"))
        session.commit()
    with b.session() as session:
        tables = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        names = {row[0] for row in tables}
        assert "marker_a" not in names
    a.dispose()
    b.dispose()


def test_fresh_database_records_schema_version_and_reopens(tmp_path: Path):
    db_path = tmp_path / "coordinator.sqlite3"
    lifecycle = DatabaseLifecycle(f"sqlite:///{db_path.as_posix()}", data_dir=tmp_path)
    lifecycle.initialize_schema()
    with lifecycle.session() as session:
        version = session.execute(
            text(f"SELECT version FROM {SCHEMA_VERSION_TABLE} WHERE singleton_id = 1")
        ).scalar_one()
        assert version == CURRENT_SCHEMA_VERSION
    lifecycle.dispose()

    reopened = DatabaseLifecycle(f"sqlite:///{db_path.as_posix()}", data_dir=tmp_path)
    reopened.initialize_schema()
    with reopened.session() as session:
        tables = inspect(session.get_bind()).get_table_names()
        assert "build_tasks" in tables
        assert SCHEMA_VERSION_TABLE in tables
    reopened.dispose()


def test_stale_coordinator_database_without_schema_version_fails_closed(tmp_path: Path):
    db_path = tmp_path / "stale.sqlite3"
    lifecycle = DatabaseLifecycle(f"sqlite:///{db_path.as_posix()}", data_dir=tmp_path)
    with lifecycle.engine.begin() as connection:
        connection.execute(text("CREATE TABLE build_tasks (task_id TEXT PRIMARY KEY)"))
    try:
        lifecycle.initialize_schema()
        raise AssertionError("expected DatabaseSchemaError")
    except DatabaseSchemaError as exc:
        assert "no build_coordinator_schema_version metadata" in str(exc)
    lifecycle.dispose()


def test_sqlite_and_postgresql_urls_select_matching_dialects():
    sqlite = engine_from_url("sqlite:///:memory:")
    assert sqlite.dialect.name == "sqlite"
    sqlite.dispose()

    try:
        import psycopg  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("psycopg is not installed")

    postgres = engine_from_url("postgresql+psycopg://user:pass@localhost:5432/coord")
    assert postgres.dialect.name == "postgresql"
    postgres.dispose()


def test_process_default_requires_explicit_configure_without_env(monkeypatch):
    import os

    url = os.environ["BUILD_COORDINATOR_DATABASE_URL"]
    data_dir = os.environ["BUILD_COORDINATOR_DATA_DIR"]
    monkeypatch.delenv("BUILD_COORDINATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("BUILD_COORDINATOR_DATA_DIR", raising=False)
    reset_process_database()
    try:
        assert get_process_database() is None
        try:
            from build_coordinator.db import initialize_schema

            initialize_schema()
            raise AssertionError("expected DatabaseUnconfiguredError")
        except DatabaseUnconfiguredError:
            pass
    finally:
        from build_coordinator.db import configure_process_database

        configure_process_database(database_url=url, data_dir=data_dir)


def test_test_guard_refuses_active_project_durable_state(monkeypatch):
    active_state_dir = REPO_ROOT / ".build-coordinator"
    monkeypatch.setenv("STAGEMESH_TEST_STATE_GUARD", "1")

    try:
        DatabaseLifecycle(
            f"sqlite:///{(active_state_dir / 'coordinator.sqlite3').as_posix()}",
            data_dir=active_state_dir,
        )
        raise AssertionError("expected TestStateIsolationError")
    except TestStateIsolationError as exc:
        assert "active project durable state" in str(exc)


def test_test_guard_refuses_relative_sqlite_url_to_active_project_state(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("STAGEMESH_TEST_STATE_GUARD", "1")

    try:
        DatabaseLifecycle("sqlite:///.build-coordinator/coordinator.sqlite3")
        raise AssertionError("expected TestStateIsolationError")
    except TestStateIsolationError as exc:
        assert "active project durable state" in str(exc)
