"""Bounded SQLite lock-contention retry/backoff tests (GH-101)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from build_coordinator.db import DatabaseBusyError, with_sqlite_retry


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

    with pytest.raises(DatabaseBusyError):
        with_sqlite_retry(fn, attempts=3, base_delay=0.0)


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
