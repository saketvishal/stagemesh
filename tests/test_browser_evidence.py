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
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from test_bounded_execution import TASK, _setup

import stagemesh.browser_evidence as browser_evidence_module
import stagemesh.cli as cli_module
import stagemesh.coordinator as coordinator_module
from stagemesh.audit import record_audit
from stagemesh.block_kinds import BlockKind, classify_block, record_block
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
    browser_review_requirement,
    capture_browser_evidence,
    decide_browser_evidence,
    invalidated_browser_recapture_recipe,
    materialize_browser_review_bundle,
    parse_capture_request,
    parse_server_command,
    recapture_invalidated_browser_evidence,
    retire_unverified_browser_failure,
    verify_browser_review_bundle,
)
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.execution import kill_process_tree, popen_session_kwargs
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter, _remediation_prompt
from stagemesh.remediation import remediation_context
from stagemesh.review import Reviewer
from stagemesh.review_disposition import (
    FINDING_DISPOSITION,
    unresolved_adverse_review_findings,
)
from stagemesh.workspaces import task_workspace

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


def _record_verified_capture(
    project: Path,
    store: Store,
    task_id: str,
    candidate_sha: str,
    *,
    capture_id: str = "browser-test",
) -> str:
    artifact = (
        project
        / ".stagemesh"
        / "evidence"
        / task_id
        / candidate_sha
        / capture_id
        / "sources-mobile.png"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 360, 800)
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    record_audit(
        store,
        CAPTURE_EVENT,
        {
            "task_id": task_id,
            "candidate_sha": candidate_sha,
            "capture_id": capture_id,
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
    return digest


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
    _record_verified_capture(project, store, TASK, SHA)
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


def test_live_invalidated_recipe_is_replayed_without_visible_browser(tmp_path: Path) -> None:
    if os.environ.get("STAGEMESH_LIVE_BROWSER_TEST") != "1":
        pytest.skip("set STAGEMESH_LIVE_BROWSER_TEST=1 for installed-browser acceptance")
    project, store = _setup(tmp_path)
    sha = GitWorkspace(project).head()
    workspace = task_workspace(project, TASK)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    GitWorkspace(project).run("worktree", "add", "--detach", str(workspace), sha)
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    port = _available_debug_port()
    command = (
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
    )
    first = capture_browser_evidence(
        store,
        project,
        TASK,
        server_command=command,
        base_url=f"http://127.0.0.1:{port}/",
        captures=(CaptureRequest("mobile", 360, 800, "/docs/a.md"),),
    )
    legacy = json.loads(list(store.task_audit_events(CAPTURE_EVENT, TASK))[-1]["payload"])
    assert legacy["capture_id"] == first["capture_id"]
    legacy.pop("capture_protocol")
    for artifact in legacy["artifacts"]:
        artifact.pop("requested_viewport")
        artifact.pop("css_viewport")
    with store.conn:
        store.conn.execute("DELETE FROM audit_events WHERE event_type=?", (CAPTURE_EVENT,))
    record_audit(store, CAPTURE_EVENT, legacy)
    record_audit(
        store,
        INVALIDATED_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": sha,
            "capture_id": first["capture_id"],
            "reason": "legacy capture did not measure the CSS viewport",
        },
    )
    record_audit(
        store,
        DECISION_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": sha,
            "capture_id": first["capture_id"],
            "decision": "FAIL",
            "reason": "legacy screenshot appeared clipped",
        },
    )

    recipe = invalidated_browser_recapture_recipe(store, TASK, sha)
    assert recipe is not None and recipe.server_command == command
    report = recapture_invalidated_browser_evidence(
        store, project, TASK, sha, recipe
    )

    assert report is not None and report["capture_protocol"] == CAPTURE_PROTOCOL
    artifacts = report["artifacts"]
    assert isinstance(artifacts, list)
    artifact = artifacts[0]
    assert artifact["requested_viewport"] == artifact["css_viewport"] == "360x800"
    assert artifact["viewport"] == "360x800"


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
    capture["base_origin"] = "http://127.0.0.1:4179/"
    capture["server_command"] = "'C:\\Program Files\\nodejs\\npm.cmd' run dev -- --port 4179"
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

    recipe = invalidated_browser_recapture_recipe(store, TASK, SHA)
    assert recipe is not None
    assert recipe.server_command == (
        r"C:\Program Files\nodejs\npm.cmd",
        "run",
        "dev",
        "--",
        "--port",
        "4179",
    )
    assert recipe.base_url == "http://127.0.0.1:4179/"
    assert recipe.captures == (CaptureRequest("sources-mobile", 360, 800, "/sources"),)
    block = classify_block(store, TASK)
    assert block is not None and block.kind is BlockKind.RECOVERABLE_OPERATIONAL

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


def test_invalidated_capture_with_redacted_command_is_not_replayed(tmp_path: Path) -> None:
    project, store = _blocked_capture(tmp_path)
    capture = json.loads(list(store.task_audit_events(CAPTURE_EVENT, TASK))[-1]["payload"])
    capture.pop("capture_protocol")
    for artifact in capture["artifacts"]:
        artifact.pop("requested_viewport")
        artifact.pop("css_viewport")
    capture["base_origin"] = "http://127.0.0.1:4179/"
    capture["server_command"] = "npm run dev -- --token ***REDACTED***"
    with store.conn:
        store.conn.execute("DELETE FROM audit_events WHERE event_type=?", (CAPTURE_EVENT,))
    record_audit(store, CAPTURE_EVENT, capture)
    record_audit(
        store,
        DECISION_EVENT,
        {
            "task_id": TASK,
            "candidate_sha": SHA,
            "capture_id": "browser-test",
            "decision": "FAIL",
        },
    )

    assert retire_unverified_browser_failure(store, TASK, SHA) is True
    assert invalidated_browser_recapture_recipe(store, TASK, SHA) is None
    block = classify_block(store, TASK)
    assert block is not None and block.kind is BlockKind.EXTERNAL_DEPENDENCY
    assert project.is_dir()


def test_review_stage_automatically_recaptures_then_continues_to_independent_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _setup(tmp_path)
    (project / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    GitWorkspace(project).commit_all("ignore runtime")
    (project / "docs" / "a.md").write_text("candidate\n", encoding="utf-8")
    sha = GitWorkspace(project).commit_all("candidate")
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    order: list[str] = []
    digest: list[str] = []

    class ExactBrowserReviewer:
        name = "codex"

        def review(self, prompt: str) -> str:
            assert "A separate operator approval is not required" in prompt
            return json.dumps(
                {
                    "decision": "PASS",
                    "browser_evidence": {
                        "capture_id": "browser-verified",
                        "artifact_sha256s": digest,
                    },
                }
            )

    def recapture(*_args: object) -> dict[str, str]:
        order.append("recapture")
        digest.append(
            _record_verified_capture(
                project,
                store,
                TASK,
                sha,
                capture_id="browser-verified",
            )
        )
        return {"capture_id": "browser-verified"}

    monkeypatch.setattr(
        coordinator_module,
        "invalidated_browser_recapture_recipe",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        Coordinator,
        "_workspace_intact",
        lambda *_args, **_kwargs: order.append("workspace") or True,
    )
    monkeypatch.setattr(
        coordinator_module,
        "recapture_invalidated_browser_evidence",
        recapture,
    )

    coordinator = Coordinator(
        store,
        project,
        reviewer=Reviewer(adapter=ExactBrowserReviewer(), require_independent=True),
    )
    assert coordinator.tick() == 1
    assert order[:2] == ["workspace", "recapture"]
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE, [
        (row["kind"], row["status"], row["payload"])
        for row in store.conn.execute("SELECT kind, status, payload FROM evidence")
    ]
    assert store.has_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert classify_block(store, TASK) is None


def test_verified_capture_recorded_after_persisted_browser_findings_gets_one_bound_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the real upgrade state: an old failed review exists before the verified capture."""
    project, store = _setup(tmp_path)
    (project / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    GitWorkspace(project).commit_all("ignore runtime")
    (project / "docs" / "a.md").write_text("candidate\n", encoding="utf-8")
    sha = GitWorkspace(project).commit_all("candidate")
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)

    class MissingBrowserReviewer:
        name = "codex"

        def review(self, _prompt: str) -> str:
            return json.dumps(
                {
                    "decision": "FAIL_WITH_FINDINGS",
                    "findings": [
                        {
                            "severity": "error",
                            "message": "360-pixel and desktop browser review is missing",
                            "remediation": "external_evidence",
                        },
                        {
                            "severity": "error",
                            "message": "internal acceptance record is missing",
                            "remediation": "external_evidence",
                        },
                    ],
                }
            )

    assert Reviewer(
        adapter=MissingBrowserReviewer(), require_independent=True
    ).review(store, TASK, sha, project) == EvidenceStatus.FAILED
    assert len(unresolved_adverse_review_findings(store, TASK, sha)) == 2
    digest = _record_verified_capture(
        project, store, TASK, sha, capture_id="browser-after-findings"
    )

    class ExactBrowserReviewer:
        name = "codex"

        def __init__(self) -> None:
            self.calls = 0

        def review(self, prompt: str) -> str:
            self.calls += 1
            assert "browser-after-findings" in prompt
            return json.dumps(
                {
                    "decision": "PASS",
                    "browser_evidence": {
                        "capture_id": "browser-after-findings",
                        "artifact_sha256s": [digest],
                    },
                }
            )

    adapter = ExactBrowserReviewer()
    monkeypatch.setattr(Coordinator, "_workspace_intact", lambda *_args, **_kwargs: True)
    coordinator = Coordinator(
        store,
        project,
        reviewer=Reviewer(adapter=adapter, require_independent=True),
    )

    assert coordinator.tick() == 1
    assert adapter.calls == 1
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE
    assert unresolved_adverse_review_findings(store, TASK, sha) == []
    dispositions = list(store.task_audit_events(FINDING_DISPOSITION, TASK))
    assert len(dispositions) == 2
    assert all(
        json.loads(row["payload"])["disposition"] == "withdrawn_by_reviewer"
        for row in dispositions
    )


def test_verified_capture_does_not_recheck_unrelated_persisted_code_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _setup(tmp_path)
    sha = GitWorkspace(project).head()
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)

    class CodeFindingReviewer:
        name = "codex"

        def review(self, _prompt: str) -> str:
            return json.dumps(
                {
                    "decision": "FAIL_WITH_FINDINGS",
                    "findings": [
                        {
                            "severity": "error",
                            "message": "docs/a.md omits required behavior",
                            "path": "docs/a.md",
                            "remediation": "code_change",
                        }
                    ],
                }
            )

    assert Reviewer(
        adapter=CodeFindingReviewer(), require_independent=True
    ).review(store, TASK, sha, project) == EvidenceStatus.FAILED
    _record_verified_capture(project, store, TASK, sha, capture_id="unrelated-capture")

    class MustNotRun:
        name = "codex"

        def __init__(self) -> None:
            self.calls = 0

        def review(self, _prompt: str) -> str:
            self.calls += 1
            return '{"decision":"PASS"}'

    adapter = MustNotRun()
    monkeypatch.setattr(Coordinator, "_workspace_intact", lambda *_args, **_kwargs: True)
    coordinator = Coordinator(
        store,
        project,
        reviewer=Reviewer(adapter=adapter, require_independent=True),
    )

    assert coordinator.tick() == 1
    assert adapter.calls == 0
    assert unresolved_adverse_review_findings(store, TASK, sha)


def test_review_stage_does_not_recapture_when_candidate_integrity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _setup(tmp_path)
    sha = GitWorkspace(project).head()
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    monkeypatch.setattr(
        coordinator_module,
        "invalidated_browser_recapture_recipe",
        lambda *_args: object(),
    )
    monkeypatch.setattr(Coordinator, "_workspace_intact", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        coordinator_module,
        "recapture_invalidated_browser_evidence",
        lambda *_args: pytest.fail("recapture must not run before workspace integrity"),
    )

    assert Coordinator(store, project).tick() == 1


def test_unexpected_browser_failure_becomes_recoverable_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _setup(tmp_path)
    sha = GitWorkspace(project).head()
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    monkeypatch.setattr(
        coordinator_module,
        "invalidated_browser_recapture_recipe",
        lambda *_args: object(),
    )
    monkeypatch.setattr(Coordinator, "_workspace_intact", lambda *_args, **_kwargs: True)

    def fail_recapture(*_args: object) -> None:
        raise RuntimeError("unexpected debugger transport failure")

    monkeypatch.setattr(
        coordinator_module, "recapture_invalidated_browser_evidence", fail_recapture
    )

    assert Coordinator(store, project).tick() == 1
    block = classify_block(store, TASK)
    assert block is not None and block.kind is BlockKind.RECOVERABLE_OPERATIONAL
    assert block.failure_code == "browser_recapture_failed"


def test_automatic_recapture_uses_project_cross_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _setup(tmp_path)
    recipe = browser_evidence_module.BrowserRecaptureRecipe(
        ("python", "-m", "http.server"),
        "http://127.0.0.1:4179/",
        (CaptureRequest("mobile", 360, 800, "/"),),
    )
    held: list[tuple[Path, str]] = []

    class FakeLock:
        def __init__(self, path: Path, timeout_seconds: float):
            assert timeout_seconds == 60.0
            self.path = path

        def hold(self, owner: str):
            class Held:
                def __enter__(inner_self) -> None:
                    held.append((self.path, owner))

                def __exit__(inner_self, *_args: object) -> None:
                    return None

            return Held()

    monkeypatch.setattr(browser_evidence_module, "IntegrationLock", FakeLock)
    monkeypatch.setattr(
        browser_evidence_module,
        "invalidated_browser_recapture_recipe",
        lambda *_args: recipe,
    )
    monkeypatch.setattr(
        browser_evidence_module,
        "capture_browser_evidence",
        lambda *_args, **_kwargs: {"capture_id": "verified"},
    )

    report = recapture_invalidated_browser_evidence(
        store, project, TASK, SHA, recipe
    )

    assert report == {"capture_id": "verified"}
    assert held == [
        (
            project / ".stagemesh" / "locks" / f"browser-evidence-{TASK}.lock",
            f"browser-recapture:{TASK}",
        )
    ]


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


def test_verified_capture_is_materialized_and_reverified_in_disposable_review_checkout(
    tmp_path: Path,
) -> None:
    project, store = _blocked_capture(tmp_path)
    review_path = project / "disposable-review"
    review_path.mkdir()

    requirement = browser_review_requirement(store, project, TASK, SHA)
    bundle = materialize_browser_review_bundle(
        store, project, review_path, TASK, SHA
    )

    assert requirement is not None and bundle is not None
    assert bundle["capture_id"] == requirement["capture_id"] == "browser-test"
    bundled = bundle["artifacts"][0]
    assert bundled["path"] == ".stagemesh-review-evidence/browser/01-sources-mobile.png"
    verify_browser_review_bundle(review_path, bundle)

    (review_path / bundled["path"]).write_bytes(b"changed")
    with pytest.raises(BrowserEvidenceError, match="changed during review"):
        verify_browser_review_bundle(review_path, bundle)


def test_runtime_reviewer_receives_exact_verified_png_bundle(
    tmp_path: Path,
) -> None:
    project, store = _setup(tmp_path)
    (project / "docs" / "a.md").write_text("candidate\n", encoding="utf-8")
    sha = GitWorkspace(project).commit_all("candidate")
    store.add_candidate(TASK, sha, "claude", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    digest = _record_verified_capture(project, store, TASK, sha)
    observed = tmp_path / "review-observed.json"
    script = tmp_path / "reviewer.py"
    provider_response = json.dumps(
        {
            "decision": "PASS",
            "browser_evidence": {
                "capture_id": "browser-test",
                "artifact_sha256s": [digest],
            },
        }
    )
    script.write_text(
        "import hashlib, json, pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "artifact = pathlib.Path('.stagemesh-review-evidence/browser/01-sources-mobile.png')\n"
        f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{'exists': artifact.is_file(), "
        "'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'prompt': prompt}), encoding='utf-8')\n"
        f"print({provider_response!r})\n",
        encoding="utf-8",
    )

    response = RuntimeCommandAdapter("test", (sys.executable, str(script))).review_candidate(
        "Review candidate",
        project,
        sha,
        store=store,
        task_id=TASK,
    )

    assert json.loads(response)["decision"] == "PASS", response
    receipt = json.loads(observed.read_text(encoding="utf-8"))
    assert receipt["exists"] is True and receipt["sha256"] == digest
    assert ".stagemesh-review-evidence/browser/01-sources-mobile.png" in receipt["prompt"]


def test_browser_evidence_command_lists_verified_paths_without_requiring_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, store = _blocked_capture(tmp_path)
    store.close()

    result = cli_module.command_evidence(
        argparse.Namespace(
            project=str(project), evidence_command="browser", task=TASK, json=False
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "integrity: verified" in output
    assert "sources-mobile.png" in output
    assert "operator decision: not required; independent review is pending" in output


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


def test_retry_allows_verified_capture_to_continue_to_independent_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, store = _blocked_capture(tmp_path)
    store.close()
    monkeypatch.setattr(cli_module, "_sync_all_sources", lambda *_args, **_kwargs: None)
    args = argparse.Namespace(project=str(project), task=TASK, json=False)

    assert cli_module.command_retry_task(args) == 0
    assert "unblocked" in capsys.readouterr().out

    store = cli_module.Store(cli_module.db_path(project))
    decide_browser_evidence(
        store, project, TASK, "PASS", "Reviewed the exact-candidate captures and approved them."
    )
    store.close()
    assert cli_module.command_retry_task(args) == 2
    assert "task is not blocked" in capsys.readouterr().err


def test_provider_process_cannot_record_human_browser_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _blocked_capture(tmp_path)
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    with pytest.raises(BrowserEvidenceError, match="operator decision"):
        decide_browser_evidence(store, project, TASK, "PASS", "provider tried to self-approve")
