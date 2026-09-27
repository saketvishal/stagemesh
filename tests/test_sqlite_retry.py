"""Bounded SQLite lock-contention retry/backoff tests (GH-101)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from build_coordinator.db import DatabaseBusyError, commit_or_busy, with_sqlite_retry


class _FakeDBAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def _operational_error(message: str) -> OperationalError:
    return OperationalError(message, None, _FakeDBAPIError(message))


def test_succeeds_immediately_when_no_error():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert with_sqlite_retry(fn, base_delay=0.0) == "ok"
    assert len(calls) == 1


def test_retries_transient_lock_error_then_succeeds():
    attempts = {"count": 0}

    def fn():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _operational_error("database is locked")
        return "recovered"

    result = with_sqlite_retry(fn, attempts=5, base_delay=0.0)
    assert result == "recovered"
    assert attempts["count"] == 3


def test_raises_database_busy_error_after_exhausting_attempts():
    def fn():
        raise _operational_error("database is locked")

    with pytest.raises(DatabaseBusyError, match="STAGEMESH_SQLITE_BUSY") as exc_info:
        with_sqlite_retry(fn, attempts=3, base_delay=0.0)
    assert str(exc_info.value).startswith("STAGEMESH_SQLITE_BUSY:")


def test_cli_reports_database_busy_as_typed_diagnostic(monkeypatch):
    import sys

    import build_coordinator.cli as cli
    import build_coordinator.project.commands as project_commands

    def busy(_args):
        raise DatabaseBusyError("SQLite write contention persisted after 1 attempts")

    monkeypatch.setattr(project_commands, "handle_continue", busy)
    monkeypatch.setattr(sys, "argv", ["stagemesh", "continue", "fixture"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert str(exc_info.value).startswith("STAGEMESH_SQLITE_BUSY:")


def test_non_lock_operational_error_propagates_without_retry():
    calls = []

    def fn():
        calls.append(1)
        raise _operational_error("no such table: foo")

    with pytest.raises(OperationalError):
        with_sqlite_retry(fn, attempts=5, base_delay=0.0)
    assert len(calls) == 1


def test_non_operational_error_propagates_immediately():
    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        with_sqlite_retry(fn, attempts=5, base_delay=0.0)


class _FakeSession:
    def __init__(self, commit_error: Exception | None) -> None:
        self._commit_error = commit_error
        self.committed = False
        self.rolled_back = False

    def commit(self):
        if self._commit_error is not None:
            raise self._commit_error
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_commit_or_busy_succeeds_when_no_contention():
    session = _FakeSession(commit_error=None)
    commit_or_busy(session)
    assert session.committed is True
    assert session.rolled_back is False


def test_commit_or_busy_converts_lock_contention_to_typed_error():
    session = _FakeSession(commit_error=_operational_error("database is locked"))
    with pytest.raises(DatabaseBusyError, match="STAGEMESH_SQLITE_BUSY"):
        commit_or_busy(session)
    assert session.rolled_back is True


def test_commit_or_busy_propagates_non_lock_operational_error():
    session = _FakeSession(commit_error=_operational_error("no such table: foo"))
    with pytest.raises(OperationalError):
        commit_or_busy(session)
    assert session.rolled_back is True
