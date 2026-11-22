from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import http.server
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from test_bounded_execution import TASK, _setup

import stagemesh.cli as cli_module
from stagemesh.audit import record_audit
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.browser_evidence import (
    CAPTURE_EVENT,
    CAPTURE_PROTOCOL,
    DECISION_EVENT,
    INVALIDATED_EVENT,
    BrowserEvidenceError,
    CaptureRequest,
    _available_debug_port,
    _browser_path,
    _capture_one,
    _close_debug_browser,
    _debugger_target,
    _DebuggerClient,
    _headless_command,
    _loopback_base_url,
    _remove_profile,
    _safe_task_id,
    browser_evidence_accepted,
    browser_evidence_context,
    decide_browser_evidence,
    parse_capture_request,
    parse_server_command,
    retire_unverified_browser_failure,
)
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import kill_process_tree, popen_session_kwargs
from stagemesh.persistence import Store
from stagemesh.providers import _remediation_prompt
from stagemesh.remediation import remediation_context

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
            "capture_protocol": CAPTURE_PROTOCOL,
            "artifacts": [
                {
                    "name": "sources-mobile",
                    "route": "/sources",
                    "requested_viewport": "360x800",
                    "css_viewport": "360x800",
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
        39777,
    )

    assert "--headless=new" in command
    assert "--disable-background-networking" in command and "--disable-extensions" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=39777" in command
    assert not any(part.startswith("--screenshot=") for part in command)
    assert command[-1] == "about:blank"


class _FakeDebugger:
    def __init__(self, *, width: int = 360, height: int = 800):
        self.width, self.height = width, height
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "value": {
                        "width": self.width,
                        "height": self.height,
                        "deviceScaleFactor": 1,
                    }
                }
            }
        if method == "Page.captureScreenshot":
            png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(
                ">II", 360, 800
            )
            return {"data": base64.b64encode(png).decode("ascii")}
        return {}

    def wait_event(self, method: str, timeout_seconds: float = 15.0) -> None:
        assert method == "Page.loadEventFired"


def test_capture_overrides_and_measures_css_viewport_before_screenshot(tmp_path: Path) -> None:
    client = _FakeDebugger()

    artifact = _capture_one(
        client,  # type: ignore[arg-type]
        tmp_path,
        "http://127.0.0.1:4179/",
        CaptureRequest("mobile", 360, 800, "/sources"),
    )

    methods = [method for method, _params in client.calls]
    assert methods == [
        "Page.enable",
        "Emulation.setDeviceMetricsOverride",
        "Page.navigate",
        "Runtime.evaluate",
        "Page.captureScreenshot",
    ]
    metrics = client.calls[1][1]
    assert metrics is not None and metrics["width"] == 360 and metrics["height"] == 800
    assert artifact["requested_viewport"] == artifact["css_viewport"] == "360x800"
    assert artifact["viewport"] == "360x800"


def test_capture_fails_closed_when_browser_uses_a_wider_css_viewport(tmp_path: Path) -> None:
    client = _FakeDebugger(width=492)

    with pytest.raises(BrowserEvidenceError, match="492x800, not requested 360x800"):
        _capture_one(
            client,  # type: ignore[arg-type]
            tmp_path,
            "http://127.0.0.1:4179/",
            CaptureRequest("mobile", 360, 800, "/sources"),
        )

    assert "Page.captureScreenshot" not in [method for method, _params in client.calls]


def test_live_browser_capture_uses_exact_css_viewport() -> None:
    if os.environ.get("STAGEMESH_LIVE_BROWSER_TEST") != "1":
        pytest.skip("set STAGEMESH_LIVE_BROWSER_TEST=1 for installed-browser acceptance")

    live_root = Path(tempfile.mkdtemp(prefix="stagemesh-browser-live-", dir=Path.cwd()))

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

    (live_root / "index.html").write_text(
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<script>document.body.textContent=innerWidth+'x'+innerHeight</script>",
        encoding="utf-8",
    )
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietHandler, directory=str(live_root)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile, root = live_root / "profile", live_root / "captures"
    profile.mkdir()
    root.mkdir()
    debug_port = _available_debug_port()
    browser = subprocess.Popen(
        _headless_command(
            _browser_path(os.environ.get("STAGEMESH_TEST_BROWSER")), profile, debug_port
        ),
        cwd=live_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_session_kwargs(),
    )
    try:
        try:
            endpoint = _debugger_target(debug_port)
        except BrowserEvidenceError as exc:
            _stdout, stderr = browser.communicate(timeout=5)
            pytest.fail(f"{exc}: {stderr}")
        with _DebuggerClient(endpoint) as client:
            artifact = _capture_one(
                client,
                root,
                f"http://127.0.0.1:{server.server_port}/",
                CaptureRequest("probe", 360, 800, "/index.html"),
            )
        assert artifact["requested_viewport"] == artifact["css_viewport"] == "360x800"
        assert artifact["viewport"] == "360x800"
    finally:
        _close_debug_browser(debug_port)
        if browser.poll() is None:
            kill_process_tree(browser)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _remove_profile(profile, live_root)
        shutil.rmtree(live_root, ignore_errors=True)


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


def test_failed_browser_decision_supplies_exact_finding_to_next_provider_prompt(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)
    reason = "The 360px Sources view clips the right edge while desktop remains intact."

    decide_browser_evidence(store, project, TASK, "FAIL", reason)

    remediation = store.latest_task_remediation(TASK)
    assert remediation is not None
    assert (remediation["stage"], remediation["candidate_sha"]) == (Stage.REVIEW, SHA)
    assert remediation["retry_event_id"] is not None

    context = remediation_context(store, TASK, project)
    assert context is not None
    prompt = _remediation_prompt(context)
    assert reason in prompt
    assert f"Previous candidate {SHA} failed REVIEW." in prompt
    assert "fix exactly these" in prompt


def test_legacy_failed_browser_decision_recovers_provider_context_before_dispatch(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)
    reason = "The legacy 360px Sources capture clips the right edge."
    decide_browser_evidence(store, project, TASK, "FAIL", reason)

    remediation = store.latest_task_remediation(TASK)
    assert remediation is not None
    retry_event_id = str(remediation["retry_event_id"])
    with store.conn:
        store.conn.execute("DELETE FROM task_remediations WHERE task_id=?", (TASK,))
        store.conn.execute(
            "DELETE FROM retry_attempts WHERE retry_event_id=?", (retry_event_id,)
        )
        store.conn.execute("DELETE FROM audit_events WHERE id=?", (retry_event_id,))
    assert store.latest_task_remediation(TASK) is None

    routed = Coordinator(store, project)._reroute_evidence_remediation(TASK)

    assert routed is False  # code-change remediation proceeds to implementation
    recovered = store.latest_task_remediation(TASK)
    assert recovered is not None and recovered["retry_event_id"] is not None
    context = remediation_context(store, TASK, project)
    assert context is not None
    assert reason in _remediation_prompt(context)

    assert Coordinator(store, project)._reroute_evidence_remediation(TASK) is False
    assert store.task_remediation_count(TASK, Stage.REVIEW) == 1


def test_legacy_unmeasured_browser_fail_is_retired_instead_of_reimplemented(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)
    capture = json.loads(list(store.task_audit_events(CAPTURE_EVENT, TASK))[-1]["payload"])
    capture.pop("capture_protocol")
    for artifact in capture["artifacts"]:
        artifact.pop("requested_viewport")
        artifact.pop("css_viewport")
    with store.conn:
        store.conn.execute("DELETE FROM audit_events WHERE event_type=?", (CAPTURE_EVENT,))
    record_audit(store, CAPTURE_EVENT, capture)
    reason = "The old cropped screenshot appeared to clip the right edge."
    message = f"Headless responsive review failed for candidate {SHA[:12]}: {reason}"
    finding_id = hashlib.sha256(f"{SHA}\n{message}".encode()).hexdigest()
    record_audit(
        store,
        DECISION_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": SHA,
            "capture_id": "browser-test",
            "decision": "FAIL",
            "reason": reason,
        },
    )
    store.upsert_finding(finding_id, TASK, SHA, "error", message)
    store.add_task_remediation(TASK, str(Stage.REVIEW), SHA)
    store.advance_task(TASK, Stage.IMPLEMENT)

    assert retire_unverified_browser_failure(store, TASK, SHA) is True

    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.BLOCKED)
    assert store.get_finding(finding_id)["status"] == "RESOLVED"
    assert store.latest_task_remediation(TASK) is None
    events = list(store.task_audit_events(INVALIDATED_EVENT, TASK))
    assert len(events) == 1
    assert "predates verified CSS viewport" in json.loads(events[0]["payload"])["reason"]

    # Re-running recovery is idempotent and cannot reopen implementation.
    assert retire_unverified_browser_failure(store, TASK, SHA) is False
    assert Coordinator(store, project)._reroute_evidence_remediation(TASK) is False


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
