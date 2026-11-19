from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from scripts.live_acceptance import live_acceptance_checks, provider_execution_status
from stagemesh.ci import default_gate_commands
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.e2e_acceptance import end_to_end_acceptance
from stagemesh.persistence import Store
from stagemesh.qualification import qualification_commands


def _store(root: Path) -> Store:
    runtime = root / ".stagemesh"
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("live proof", source_id="T-1")
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
    monkeypatch.setenv("STAGEMESH_GITHUB_TOKEN", "synthetic-test-token")
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


def test_release_qualification_has_one_authoritative_gate_manifest() -> None:
    assert default_gate_commands(False) == qualification_commands(False)
    assert default_gate_commands(True) == qualification_commands(True)
    names = [name for name, _command in qualification_commands(True)]
    assert len(names) == len(set(names))
    assert names[-3:] == [
        "acceptance",
        "project_local_acceptance",
        "development_journey_acceptance",
    ]


def test_end_to_end_proof_requires_a_current_clean_executable_receipt() -> None:
    missing = end_to_end_acceptance()
    assert missing["status"] == "INCOMPLETE" and missing["proven"] == 0
    steps = [
        {"step": row["step"], "status": "PASS", "evidence": f"step {row['step']}"}
        for row in missing["steps"]
    ]
    receipt = {
        "schema_version": 1,
        "candidate_sha": "abc1234",
        "source_dirty": False,
        "status": "PASS",
        "steps": steps,
    }
    complete = end_to_end_acceptance(receipt, "abc1234")
    assert complete["status"] == "COMPLETE" and complete["proven"] == complete["total"] == 18
    assert end_to_end_acceptance(receipt, "def5678")["status"] == "INCOMPLETE"
    assert end_to_end_acceptance({**receipt, "source_dirty": True}, "abc1234")["status"] == "INCOMPLETE"
    assert end_to_end_acceptance({**receipt, "status": "FAIL"}, "abc1234")["status"] == "INCOMPLETE"
    no_evidence = {**receipt, "steps": [{**steps[0], "evidence": ""}, *steps[1:]]}
    assert end_to_end_acceptance(no_evidence, "abc1234")["status"] == "INCOMPLETE"
