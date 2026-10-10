from __future__ import annotations

from pathlib import Path

from scripts.live_acceptance import provider_execution_status
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.persistence import Store


def _store(root: Path) -> Store:
    runtime = root / ".stagemesh"
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    task_id = store.upsert_task("live proof", source_id="T-1")
    return store


def test_provider_execution_status_is_not_proven_without_store(tmp_path: Path) -> None:
    assert provider_execution_status(tmp_path, "codex") == "NOT_PROVEN"


def test_provider_execution_status_uses_completed_provider_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        execution_id = store.start_execution(
            task_id="T-1",
            claim_id=None,
            kind=ExecutionKind.IMPLEMENTATION,
            actor="codex",
        )
        store.finish_execution(execution_id, ExecutionStatus.FAILED, result="provider_timeout")
    finally:
        store.close()

    assert provider_execution_status(tmp_path, "codex") == "PROVEN"


def test_provider_execution_status_ignores_non_provider_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        execution_id = store.start_execution(
            task_id="T-1",
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            actor="codex",
        )
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    finally:
        store.close()

    assert provider_execution_status(tmp_path, "codex") == "NOT_PROVEN"
