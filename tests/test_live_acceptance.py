from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from scripts.live_acceptance import live_acceptance_checks, provider_execution_status
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


def test_live_acceptance_reports_github_transport_failure_as_unknown(
    tmp_path: Path, monkeypatch,
) -> None:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps({"github": {"owner": "example", "repo": "project"}}),
        encoding="utf-8",
    )

    def unavailable(_client) -> None:
        raise urllib.error.URLError("SENSITIVE_MARKER")

    monkeypatch.setattr("scripts.live_acceptance.GitHubClient.list_open_issues", unavailable)
    monkeypatch.setattr("scripts.live_acceptance.adapters_from_config", lambda _config: [])

    assert live_acceptance_checks(tmp_path) == [
        {"name": "github", "status": "UNKNOWN"},
        {"name": "github:sync", "status": "NOT_PROVEN"},
    ]
