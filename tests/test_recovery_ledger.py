from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stagemesh.audit import record_audit
from stagemesh.persistence import Store, StoreValidationError
from stagemesh.postgres_store import postgres_schema_contract

TASK = "TASK-LEDGER"


def _store(path: Path) -> Store:
    store = Store(path)
    store.migrate()
    store.upsert_task("recovery ledger", source_id=TASK)
    return store


def test_budget_epochs_are_stage_scoped_and_preserve_cumulative_history(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3")
    first = store.add_task_remediation(TASK, "REVIEW", "a" * 40)
    second = store.add_task_remediation(TASK, "REVIEW", "b" * 40)
    validate = store.add_task_remediation(TASK, "VALIDATE", "c" * 40)
    assert store.clear_task_remediations(TASK, "REVIEW") == 2
    third = store.add_task_remediation(TASK, "REVIEW", "d" * 40)

    assert [store.task_remediation(item)["budget_epoch"] for item in (first, second, third)] == [0, 0, 1]
    assert store.task_remediation(validate)["budget_epoch"] == 0
    assert store.task_remediation_count(TASK, "REVIEW") == 1
    assert store.task_remediation_count(TASK, "VALIDATE") == 1
    summary = store.task_remediation_summary(TASK)
    assert summary["current_attempts"] == 2 and summary["cumulative_attempts"] == 4
    review, validation = summary["stages"]
    assert (review["stage"], review["current_epoch"], review["epoch_count"]) == ("REVIEW", 1, 2)
    assert (review["current_attempts"], review["cleared_attempts"], review["cumulative_attempts"]) == (1, 2, 3)
    assert (validation["stage"], validation["current_epoch"], validation["epoch_count"]) == ("VALIDATE", 0, 1)
    store.close()


def test_remediation_row_binds_only_to_typed_retry_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path / "state.sqlite3")
    remediation_id = store.add_task_remediation(TASK, "REVIEW", "a" * 40)
    with pytest.raises(StoreValidationError, match="typed retry evidence"):
        store.bind_task_remediation_retry(remediation_id, "missing")

    retry_event_id = record_audit(
        store,
        "task.remediation_queued",
        {"task_id": TASK, "remediation_id": remediation_id, "budget_epoch": 0},
    )
    store.bind_task_remediation_retry(remediation_id, retry_event_id)
    latest = store.task_remediation_summary(TASK)["stages"][0]["latest"]
    assert latest["retry_event_id"] == retry_event_id
    assert latest["retry_state"] == "QUEUED"
    assert latest["claim_id"] is latest["execution_id"] is None
    with pytest.raises(StoreValidationError, match="already bound"):
        store.bind_task_remediation_retry(remediation_id, retry_event_id)
    store.close()


def test_version_eight_migrates_legacy_rows_without_erasing_history(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = _store(path)
    old = store.add_task_remediation(TASK, "REVIEW", "a" * 40)
    store.close()

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        ALTER TABLE task_remediations RENAME TO task_remediations_v8;
        CREATE TABLE task_remediations (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            stage TEXT NOT NULL,
            candidate_sha TEXT NOT NULL,
            cleared INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        INSERT INTO task_remediations(id, task_id, stage, candidate_sha, cleared, created_at)
            SELECT id, task_id, stage, candidate_sha, cleared, created_at FROM task_remediations_v8;
        DROP TABLE task_remediations_v8;
        DELETE FROM schema_migrations WHERE version>=8;
        DELETE FROM migration_audit WHERE version>=8;
        """
    )
    conn.commit()
    conn.close()

    upgraded = Store(path)
    upgraded.migrate()
    columns = {row["name"] for row in upgraded.conn.execute("PRAGMA table_info(task_remediations)")}
    assert {"budget_epoch", "retry_event_id"} <= columns
    assert upgraded.schema_version() >= 8
    assert upgraded.task_remediation(old)["budget_epoch"] == 0
    assert upgraded.task_remediation_summary(TASK)["cumulative_attempts"] == 1
    upgraded.close()


def test_postgres_contract_declares_recovery_ledger_parity() -> None:
    contract = postgres_schema_contract()
    assert "task_remediations" in contract["tables"]
    assert set(contract["tables"]) == set(contract["declared_tables"])
    schema = str(contract["schema_sql"])
    assert "budget_epoch INTEGER NOT NULL DEFAULT 0" in schema
    assert "retry_event_id TEXT" in schema
