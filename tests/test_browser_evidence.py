from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

import pytest
from test_bounded_execution import TASK, _setup

import stagemesh.cli as cli_module
from stagemesh.audit import record_audit
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.browser_evidence import (
    CAPTURE_EVENT,
    BrowserEvidenceError,
    CaptureRequest,
    _headless_command,
    _loopback_base_url,
    _safe_task_id,
    browser_evidence_accepted,
    browser_evidence_context,
    decide_browser_evidence,
    parse_capture_request,
    parse_server_command,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.persistence import Store

SHA = "a" * 40


def _authority(project: Path) -> None:
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "planning_gate": {
                    "approval_authority": {
                        "version": 1,
                        "authority_id": "project-owner:test/project",
                        "approver_id": "github:test-approver",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _blocked_capture(tmp_path: Path) -> tuple[Path, Store]:
    project, store = _setup(tmp_path)
    _authority(project)
    store.add_candidate(TASK, SHA, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    record_block(
        store,
        TASK,
        BlockKind.EXTERNAL_DEPENDENCY,
        "browser review and accountable acceptance are required",
        stage=str(Stage.REVIEW),
        candidate_sha=SHA,
        detail={
            "failure_code": "external_evidence_required",
            "requirements": ["360px and desktop browser review"],
        },
    )
    artifact = (
        project / ".stagemesh" / "evidence" / TASK / SHA / "browser-test" / "sources-mobile.png"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 360, 800)
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    record_audit(
        store,
        CAPTURE_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": SHA,
            "capture_id": "browser-test",
            "headless": True,
            "artifacts": [
                {
                    "name": "sources-mobile",
                    "route": "/sources",
                    "viewport": "360x800",
                    "path": artifact.relative_to(project).as_posix(),
                    "sha256": digest,
                    "bytes": artifact.stat().st_size,
                }
            ],
        },
    )
    return project, store


def test_headless_capture_inputs_are_bounded_and_loopback_only() -> None:
    capture = parse_capture_request("sources-mobile=360x800:/sources")
    assert capture == CaptureRequest("sources-mobile", 360, 800, "/sources")
    assert parse_server_command('["npm","run","dev"]') == ("npm", "run", "dev")

    with pytest.raises(BrowserEvidenceError, match="relative-route"):
        parse_capture_request("mobile=360x800:https://example.com")
    with pytest.raises(BrowserEvidenceError, match="JSON array"):
        parse_server_command("npm run dev")
    with pytest.raises(BrowserEvidenceError, match="loopback"):
        _loopback_base_url("https://example.com:443")
    with pytest.raises(BrowserEvidenceError, match="port"):
        _loopback_base_url("http://127.0.0.1:not-a-port")
    with pytest.raises(BrowserEvidenceError, match="path separators"):
        _safe_task_id("../escape")


def test_browser_command_is_always_headless_and_uses_an_isolated_profile(tmp_path: Path) -> None:
    command = _headless_command(
        Path("browser.exe"),
        tmp_path / "profile",
        tmp_path / "capture.png",
        CaptureRequest("mobile", 360, 800, "/sources"),
        "http://127.0.0.1:4179/sources",
    )

    assert "--headless=new" in command
    assert "--disable-background-networking" in command and "--disable-extensions" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert command[-1] == "http://127.0.0.1:4179/sources"


def test_failed_browser_decision_routes_exact_candidate_back_to_implementation(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)

    report = decide_browser_evidence(
        store,
        project,
        TASK,
        "FAIL",
        "The 360px captures have horizontal overflow and clipped content; desktop is intact.",
    )

    task = store.get_task(TASK)
    assert report["decision"] == "FAIL" and report["next_stage"] == "IMPLEMENT"
    assert (task["stage"], task["status"]) == (Stage.IMPLEMENT, TaskStatus.OPEN)
    findings = store.open_findings_for_candidate(TASK, SHA)
    assert len(findings) == 1 and "horizontal overflow" in findings[0]["message"]
    failed = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (TASK, SHA, EvidenceKind.REVIEW, EvidenceStatus.FAILED),
    ).fetchone()
    assert (
        failed is not None
        and json.loads(failed["payload"])["browser_evidence"]["capture_id"] == "browser-test"
    )


def test_passed_browser_decision_is_candidate_bound_and_still_requires_retry(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)

    report = decide_browser_evidence(
        store,
        project,
        TASK,
        "PASS",
        "Reviewed mobile and desktop captures against the approved design.",
    )

    assert report["approver_id"] == "github:test-approver"
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert browser_evidence_accepted(store, project, TASK, SHA)
    context = browser_evidence_context(store, project, TASK, SHA)
    assert context is not None and context["decision"]["decision"] == "PASS"
    assert context["integrity_valid"] is True


def test_changed_artifact_invalidates_pass_and_review_context(tmp_path: Path) -> None:
    project, store = _blocked_capture(tmp_path)
    decide_browser_evidence(store, project, TASK, "PASS", "Reviewed the exact-candidate captures.")
    capture = browser_evidence_context(store, project, TASK, SHA)
    assert capture is not None
    artifact = project / capture["artifacts"][0]["path"]
    artifact.write_bytes(artifact.read_bytes() + b"changed")

    assert browser_evidence_accepted(store, project, TASK, SHA) is False
    context = browser_evidence_context(store, project, TASK, SHA)
    assert context is not None and context["integrity_valid"] is False
    assert context["decision"] is None

    with pytest.raises(BrowserEvidenceError, match="hash changed"):
        decide_browser_evidence(
            store, project, TASK, "PASS", "Attempted to approve changed evidence."
        )


def test_new_capture_invalidates_older_pass_everywhere(tmp_path: Path) -> None:
    project, store = _blocked_capture(tmp_path)
    decide_browser_evidence(store, project, TASK, "PASS", "Reviewed the first exact capture.")
    prior = browser_evidence_context(store, project, TASK, SHA)
    assert prior is not None
    record_audit(
        store,
        CAPTURE_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": SHA,
            "capture_id": "browser-newer",
            "headless": True,
            "artifacts": prior["artifacts"],
        },
    )

    assert browser_evidence_accepted(store, project, TASK, SHA) is False
    current = browser_evidence_context(store, project, TASK, SHA)
    assert current is not None and current["capture_id"] == "browser-newer"
    assert current["decision"] is None


def test_retry_refuses_external_evidence_block_until_accountable_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, store = _blocked_capture(tmp_path)
    store.close()
    monkeypatch.setattr(cli_module, "_sync_all_sources", lambda *_args, **_kwargs: None)
    args = argparse.Namespace(project=str(project), task=TASK, json=False)

    assert cli_module.command_retry_task(args) == 2
    assert "still lacks accountable browser evidence" in capsys.readouterr().err

    store = cli_module.Store(cli_module.db_path(project))
    decide_browser_evidence(
        store, project, TASK, "PASS", "Reviewed the exact-candidate captures and approved them."
    )
    store.close()
    assert cli_module.command_retry_task(args) == 0
    assert "task TASK-1 unblocked" in capsys.readouterr().out


def test_provider_process_cannot_record_human_browser_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _blocked_capture(tmp_path)
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    with pytest.raises(BrowserEvidenceError, match="operator decision"):
        decide_browser_evidence(store, project, TASK, "PASS", "provider tried to self-approve")
