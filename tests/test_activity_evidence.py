from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stagemesh.activity import ActivityQuery, ActivityQueryError, query_activity
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store

BASE = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "activity.sqlite3")
    value.migrate()
    yield value
    value.close()


def _task(store: Store) -> str:
    task = store.upsert_task("evidence activity", source="github", source_id="573", project="team/alpha")
    store.cache_source("github", "573", {"state": "OPEN"}, "OPEN")
    store.conn.execute(
        "UPDATE tasks SET stage=?, updated_at=? WHERE id=?",
        (Stage.REVIEW.value, BASE.timestamp(), task),
    )
    store.conn.commit()
    return task


def _at(store: Store, evidence_id: str, minutes: int) -> None:
    store.conn.execute(
        "UPDATE evidence SET created_at=? WHERE id=?",
        ((BASE + timedelta(minutes=minutes)).timestamp(), evidence_id),
    )
    store.conn.commit()


def test_typed_evidence_filters_counts_and_identity_are_exact(store: Store) -> None:
    task = _task(store)
    candidate = "a" * 40
    validation = store.add_evidence(
        task, candidate, EvidenceKind.VALIDATION, EvidenceStatus.PASSED,
        {"token": "must-never-appear", "findings": [{"message": "private provider output"}]},
    )
    review = store.add_evidence(
        task, candidate, EvidenceKind.REVIEW, EvidenceStatus.FAILED,
        {"review_provider": "codex", "command": "secret command"},
    )
    _at(store, validation, 10)
    _at(store, review, 20)

    page = query_activity(
        store,
        ActivityQuery(
            include_evidence_history=True,
            evidence_kind="REVIEW",
            evidence_status="FAILED",
            task_id=task,
            project="team/alpha",
            source="github",
        ),
    )

    assert [entry.id for entry in page.entries] == [review]
    entry = page.entries[0]
    assert entry.kind == "evidence" and entry.state == "closed"
    assert (entry.evidence_kind, entry.evidence_status, entry.status) == ("REVIEW", "FAILED", "FAILED")
    assert entry.candidate_sha == candidate
    assert entry.current_task_status == "OPEN" and entry.source_status == "OPEN"
    assert entry.stage == "REVIEW" and entry.project == "team/alpha"
    assert page.status_counts["evidence"]["REVIEW"] == 1
    assert page.status_counts["evidence_outcome"]["FAILED"] == 1
    assert "private provider output" not in repr(entry) and "secret command" not in repr(entry)


def test_evidence_history_is_opt_in_half_open_and_restart_safe(store: Store) -> None:
    task = _task(store)
    first = store.add_evidence(task, "b" * 40, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    boundary = store.add_evidence(task, "c" * 40, EvidenceKind.INTEGRATION, EvidenceStatus.UNKNOWN)
    _at(store, first, 10)
    _at(store, boundary, 20)
    before = store.db_path.read_bytes()

    plain = query_activity(store, ActivityQuery(task_id=task))
    assert not [entry for entry in plain.entries if entry.kind == "evidence"]
    ranged = query_activity(
        store,
        ActivityQuery(
            task_id=task,
            include_evidence_history=True,
            since=BASE + timedelta(minutes=10),
            until=BASE + timedelta(minutes=20),
        ),
    )
    assert [entry.id for entry in ranged.entries if entry.kind == "evidence"] == [first]
    assert store.db_path.read_bytes() == before

    path = store.db_path
    store.close()
    reopened = Store(path)
    try:
        page = query_activity(
            reopened,
            ActivityQuery(task_id=task, include_evidence_history=True, evidence_kind="INTEGRATION"),
        )
        assert [entry.id for entry in page.entries] == [boundary]
    finally:
        reopened.close()


def test_malformed_candidate_and_unallowlisted_rows_fail_closed(store: Store) -> None:
    task = _task(store)
    malformed = store.add_evidence(task, "valid-at-write", EvidenceKind.REVIEW, EvidenceStatus.CAPACITY)
    _at(store, malformed, 1)
    store.conn.execute("UPDATE evidence SET candidate_sha=? WHERE id=?", ("token-secret", malformed))
    store.conn.execute(
        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("forged-kind", task, "d" * 40, "HANDOFF", "PASSED", "{}", BASE.timestamp()),
    )
    store.conn.execute(
        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("forged-status", task, "e" * 40, "REVIEW", "MAYBE", "{}", BASE.timestamp()),
    )
    store.conn.commit()

    page = query_activity(store, ActivityQuery(task_id=task, include_evidence_history=True))
    evidence = [entry for entry in page.entries if entry.kind == "evidence"]
    assert [entry.id for entry in evidence] == [malformed]
    assert evidence[0].candidate_sha is None


@pytest.mark.parametrize(
    "query",
    [
        ActivityQuery(evidence_kind="REVIEW"),
        ActivityQuery(evidence_status="FAILED"),
        ActivityQuery(include_evidence_history=True, evidence_kind="HANDOFF"),
        ActivityQuery(include_evidence_history=True, evidence_status="MAYBE"),
        ActivityQuery(include_evidence_history=True, evidence_kind="REVIEW", task_event="status_changed", include_task_history=True),
        ActivityQuery(include_evidence_history=True, evidence_status="FAILED", retry_state="FAILED", include_retry_history=True),
    ],
)
def test_invalid_or_ambiguous_evidence_queries_are_rejected(store: Store, query: ActivityQuery) -> None:
    with pytest.raises(ActivityQueryError):
        query_activity(store, query)


def test_read_only_sqlite_connection_can_project_evidence(tmp_path: Path) -> None:
    path = tmp_path / "readonly.sqlite3"
    writer = Store(path)
    writer.migrate()
    task = _task(writer)
    writer.add_evidence(task, "f" * 40, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)
    writer.close()
    before = path.read_bytes()

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        page = query_activity(
            type("ReadOnly", (), {"conn": connection})(),
            ActivityQuery(task_id=task, include_evidence_history=True, evidence_kind="INTEGRATION"),
        )
    finally:
        connection.close()
    assert len(page.entries) == 1 and page.entries[0].evidence_status == "PASSED"
    assert path.read_bytes() == before
