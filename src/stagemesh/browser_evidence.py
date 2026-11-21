"""No-window browser evidence bound to one retained task candidate.

The browser is an explicitly headless local process.  StageMesh never opens a
desktop window, never navigates away from a loopback origin, and never installs
a browser or project dependency.  Captures are evidence, not approval: a
configured accountable approver must separately record PASS or FAIL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from .audit import record_audit
from .config import load_config
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .execution import kill_process_tree, popen_session_kwargs
from .git import GitWorkspace
from .persistence import Store
from .redaction import redact_command_secrets, sanitize_provider_output
from .remediation import finding_identity
from .spec_planning import PROVIDER_PROCESS_ENV
from .workspaces import task_workspace

CAPTURE_EVENT = "task.browser_evidence_captured"
DECISION_EVENT = "task.browser_evidence_decided"
_BROWSER_REQUIREMENT_MARKERS = (
    "browser",
    "responsive",
    "viewport",
    "360-pixel",
    "360px",
    "desktop navigation",
)
_CAPTURE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,79})=(?P<width>[1-9][0-9]{1,3})x(?P<height>[1-9][0-9]{1,3}):(?P<route>/[^\r\n]*)$"
)
_COMMON_BROWSERS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


class BrowserEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureRequest:
    name: str
    width: int
    height: int
    route: str


def browser_evidence_required(reason: str, requirements: tuple[str, ...] | list[str]) -> bool:
    text = " ".join((reason, *requirements)).lower()
    return any(marker in text for marker in _BROWSER_REQUIREMENT_MARKERS)


def parse_capture_request(value: str) -> CaptureRequest:
    match = _CAPTURE_RE.fullmatch(value if isinstance(value, str) else "")
    if match is None:
        raise BrowserEvidenceError("capture must be NAME=WIDTHxHEIGHT:/relative-route")
    width, height = int(match["width"]), int(match["height"])
    if not (240 <= width <= 3840 and 240 <= height <= 4320):
        raise BrowserEvidenceError("capture viewport must be between 240x240 and 3840x4320")
    route = match["route"]
    parsed = urlparse(route)
    if parsed.scheme or parsed.netloc or route.startswith("//") or "\\" in route:
        raise BrowserEvidenceError("capture route must stay on the configured loopback origin")
    return CaptureRequest(match["name"], width, height, route)


def parse_server_command(value: str) -> tuple[str, ...]:
    try:
        command = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BrowserEvidenceError("server command must be a JSON array of argv strings") from exc
    if not isinstance(command, list) or not command or len(command) > 30:
        raise BrowserEvidenceError("server command must contain 1 to 30 argv strings")
    if any(not isinstance(part, str) or not part or len(part) > 1000 for part in command):
        raise BrowserEvidenceError("server command argv entries must be non-empty bounded strings")
    return tuple(command)


def _loopback_base_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        _port = parsed.port
    except ValueError as exc:
        raise BrowserEvidenceError("browser evidence base URL has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise BrowserEvidenceError(
            "browser evidence base URL must be an unauthenticated loopback http URL"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise BrowserEvidenceError("browser evidence base URL must name only the loopback origin")
    return value.rstrip("/") + "/"


def _browser_path(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).resolve()
        if candidate.is_file():
            return candidate
        raise BrowserEvidenceError(f"headless browser executable does not exist: {candidate}")
    for command in ("msedge", "microsoft-edge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(command)
        if found:
            return Path(found).resolve()
    for candidate in _COMMON_BROWSERS:
        if candidate.is_file():
            return candidate.resolve()
    raise BrowserEvidenceError(
        "no supported Edge, Chrome, or Chromium executable was found; StageMesh will not install one"
    )


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise BrowserEvidenceError(f"headless browser did not produce a valid PNG: {path.name}")
    return struct.unpack(">II", header[16:24])


def _safe_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
        raise BrowserEvidenceError("browser evidence requires a bounded task identifier")
    if task_id in {".", ".."} or any(character in task_id for character in ("/", "\\", "\x00")):
        raise BrowserEvidenceError("task identifier cannot contain path separators")
    return task_id


def _verify_capture_artifacts(project: Path, capture: dict[str, object]) -> None:
    runtime = (project / ".stagemesh" / "evidence").resolve()
    artifacts = capture.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BrowserEvidenceError("browser evidence capture has no recorded artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise BrowserEvidenceError("browser evidence artifact record is invalid")
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        viewport = artifact.get("viewport")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(viewport, str)
        ):
            raise BrowserEvidenceError("browser evidence artifact metadata is incomplete")
        path = (project / relative).resolve()
        if runtime not in path.parents or not path.is_file():
            raise BrowserEvidenceError(
                "browser evidence artifact is missing or outside the project evidence area"
            )
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual_digest, digest):
            raise BrowserEvidenceError(f"browser evidence artifact hash changed: {path.name}")
        width, height = _png_dimensions(path)
        if viewport != f"{width}x{height}":
            raise BrowserEvidenceError(f"browser evidence artifact dimensions changed: {path.name}")


def _wait_ready(url: str, process: subprocess.Popen[str], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserEvidenceError("local evidence server exited before it became ready")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.25)
    raise BrowserEvidenceError("local evidence server did not become ready before the timeout")


def _headless_command(
    executable: Path,
    profile: Path,
    target: Path,
    capture: CaptureRequest,
    target_url: str,
) -> list[str]:
    return [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--metrics-recording-only",
        "--mute-audio",
        f"--user-data-dir={profile}",
        f"--window-size={capture.width},{capture.height}",
        f"--screenshot={target}",
        "--virtual-time-budget=2500",
        target_url,
    ]


def _run_headless(command: list[str], workspace: Path) -> tuple[int, str, str]:
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen_session_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        raise BrowserEvidenceError(
            "headless browser capture timed out and its process tree was terminated"
        ) from exc
    return process.returncode, stdout, stderr


def _capture_one(
    executable: Path,
    profile: Path,
    root: Path,
    workspace: Path,
    origin: str,
    capture: CaptureRequest,
) -> dict[str, object]:
    target = root / f"{capture.name}.png"
    target_url = urljoin(origin, capture.route.lstrip("/"))
    returncode, browser_stdout, browser_stderr = _run_headless(
        _headless_command(executable, profile, target, capture, target_url), workspace
    )
    if returncode == 0 and not target.is_file():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not target.is_file():
            time.sleep(0.1)
    if returncode != 0 or not target.is_file():
        detail = sanitize_provider_output(browser_stderr or browser_stdout, 500)
        raise BrowserEvidenceError(
            f"headless capture {capture.name} failed: {detail or 'no PNG was produced'}"
        )
    width, height = _png_dimensions(target)
    if (width, height) != (capture.width, capture.height):
        raise BrowserEvidenceError(
            f"capture {capture.name} has unexpected dimensions {width}x{height}"
        )
    return {
        "name": capture.name,
        "route": capture.route,
        "viewport": f"{width}x{height}",
        "path": target,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
    }


def _remove_profile(profile: Path, root: Path) -> None:
    if not profile.is_dir() or root not in profile.parents:
        return
    for attempt in range(20):
        try:
            shutil.rmtree(profile)
            return
        except OSError:
            if attempt < 19:
                time.sleep(0.1)


def _capture_session(
    *,
    root: Path,
    workspace: Path,
    server_command: tuple[str, ...],
    environment: dict[str, str],
    origin: str,
    executable: Path,
    captures: tuple[CaptureRequest, ...],
    startup_timeout_seconds: float,
) -> list[dict[str, object]]:
    profile = root / "browser-profile"
    server: subprocess.Popen[str] | None = None
    try:
        with (
            (root / "server.stdout.log").open("w", encoding="utf-8") as stdout,
            (root / "server.stderr.log").open("w", encoding="utf-8") as stderr,
        ):
            try:
                server = subprocess.Popen(
                    list(server_command),
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    **popen_session_kwargs(),
                )
            except OSError as exc:
                raise BrowserEvidenceError(
                    f"could not start the explicit local evidence server: {type(exc).__name__}"
                ) from exc
            _wait_ready(origin, server, startup_timeout_seconds)
            return [
                _capture_one(executable, profile, root, workspace, origin, capture)
                for capture in captures
            ]
    finally:
        if server is not None and server.poll() is None:
            kill_process_tree(server)
        _remove_profile(profile, root)


def capture_browser_evidence(
    store: Store,
    project: Path,
    task_id: str,
    *,
    server_command: tuple[str, ...],
    base_url: str,
    captures: tuple[CaptureRequest, ...],
    browser: str | None = None,
    startup_timeout_seconds: float = 30.0,
) -> dict[str, object]:
    """Capture exact-candidate screenshots without ever creating a visible browser window."""
    if os.environ.get(PROVIDER_PROCESS_ENV):
        raise BrowserEvidenceError(
            "browser evidence capture is an operator action and cannot run inside a provider process"
        )
    task_id = _safe_task_id(task_id)
    task = store.get_task(task_id)
    candidate = store.latest_candidate(task_id)
    if task is None or candidate is None:
        raise BrowserEvidenceError(f"task {task_id} has no retained candidate to capture")
    candidate_sha = str(candidate["sha"])
    workspace = task_workspace(project, task_id).resolve()
    if not workspace.is_dir() or GitWorkspace(workspace).head() != candidate_sha:
        raise BrowserEvidenceError(
            "the retained task worktree is missing or is not at the recorded candidate"
        )
    if not captures or len(captures) > 20:
        raise BrowserEvidenceError("browser evidence requires 1 to 20 captures")
    if len({capture.name for capture in captures}) != len(captures):
        raise BrowserEvidenceError("browser evidence capture names must be unique")
    origin = _loopback_base_url(base_url)
    executable = _browser_path(browser)
    capture_id = f"browser-{uuid.uuid4().hex}"
    root = (project / ".stagemesh" / "evidence" / task_id / candidate_sha / capture_id).resolve()
    runtime = (project / ".stagemesh").resolve()
    if runtime not in root.parents:
        raise BrowserEvidenceError("browser evidence path escaped the project runtime")
    root.mkdir(parents=True, exist_ok=False)
    environment = {**os.environ, "CI": "1", "BROWSER": "none", "STAGEMESH_HEADLESS_EVIDENCE": "1"}
    artifacts = _capture_session(
        root=root,
        workspace=workspace,
        server_command=server_command,
        environment=environment,
        origin=origin,
        executable=executable,
        captures=captures,
        startup_timeout_seconds=startup_timeout_seconds,
    )
    for artifact in artifacts:
        artifact_path = artifact["path"]
        if not isinstance(artifact_path, Path):
            raise BrowserEvidenceError("internal browser evidence artifact path is invalid")
        artifact["path"] = artifact_path.relative_to(project).as_posix()
    payload: dict[str, object] = {
        "task_id": task_id,
        "candidate_sha": candidate_sha,
        "capture_id": capture_id,
        "stage": str(task["stage"]),
        "browser": executable.name,
        "headless": True,
        "base_origin": origin,
        "server_command": redact_command_secrets(shlex.join(server_command)),
        "artifacts": artifacts,
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["manifest"] = manifest.relative_to(project).as_posix()
    payload["event_id"] = record_audit(store, CAPTURE_EVENT, payload)
    return payload


def _event_payloads(store: Store, event_type: str, task_id: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in store.task_audit_events(event_type, task_id):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and all(isinstance(key, str) for key in payload):
            result.append({str(key): value for key, value in payload.items()})
    return result


def latest_browser_capture(
    store: Store, task_id: str, candidate_sha: str | None = None
) -> dict[str, object] | None:
    return next(
        (
            payload
            for payload in reversed(_event_payloads(store, CAPTURE_EVENT, task_id))
            if candidate_sha is None or payload.get("candidate_sha") == candidate_sha
        ),
        None,
    )


def latest_browser_decision(
    store: Store, task_id: str, candidate_sha: str | None = None
) -> dict[str, object] | None:
    return next(
        (
            payload
            for payload in reversed(_event_payloads(store, DECISION_EVENT, task_id))
            if candidate_sha is None or payload.get("candidate_sha") == candidate_sha
        ),
        None,
    )


def decide_browser_evidence(
    store: Store, project: Path, task_id: str, decision: str, reason: str
) -> dict[str, object]:
    """Record the configured human authority's exact-candidate visual decision; FAIL routes back to implementation."""
    if os.environ.get(PROVIDER_PROCESS_ENV):
        raise BrowserEvidenceError(
            "browser evidence acceptance is an operator decision and cannot run inside a provider process"
        )
    task_id = _safe_task_id(task_id)
    normalized = decision.upper()
    if normalized not in {"PASS", "FAIL"}:
        raise BrowserEvidenceError("browser evidence decision must be PASS or FAIL")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 2000:
        raise BrowserEvidenceError(
            "browser evidence decision requires a reason of 1 to 2000 characters"
        )
    config = load_config(project)
    authority = config.approval_authority
    if authority is None:
        raise BrowserEvidenceError(
            "accountable browser evidence requires planning_gate.approval_authority"
        )
    task, candidate = store.get_task(task_id), store.latest_candidate(task_id)
    if task is None or candidate is None:
        raise BrowserEvidenceError(f"task {task_id} has no retained candidate")
    candidate_sha = str(candidate["sha"])
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if capture is None:
        raise BrowserEvidenceError("no browser evidence capture exists for the current candidate")
    _verify_capture_artifacts(project, capture)
    payload: dict[str, object] = {
        "task_id": task_id,
        "candidate_sha": candidate_sha,
        "capture_id": capture["capture_id"],
        "decision": normalized,
        "reason": reason,
        "authority_id": authority.authority_id,
        "approver_id": authority.approver_id,
        "authority_hash": authority.digest,
        "artifacts": capture.get("artifacts", []),
    }
    payload["event_id"] = record_audit(store, DECISION_EVENT, payload)
    if normalized == "FAIL":
        message = f"Headless responsive review failed for candidate {candidate_sha[:12]}: {reason}"
        finding_id = finding_identity(candidate_sha, message)
        store.upsert_finding(finding_id, task_id, candidate_sha, "error", message)
        binding = store.contract_binding(task_id, candidate_sha)
        contract_hash = str(binding["digest"]) if binding is not None else None
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.REVIEW,
            EvidenceStatus.FAILED,
            {
                "contract_hash": contract_hash,
                "browser_evidence": payload,
                "findings": [
                    {"severity": "error", "message": message, "remediation": "code_change"}
                ],
            },
        )
        remediation = ensure_browser_evidence_remediation(store, task_id, candidate_sha)
        if remediation is None:  # pragma: no cover - persistence invariant
            raise BrowserEvidenceError("browser evidence remediation was not persisted")
        store.advance_task(task_id, Stage.IMPLEMENT)
        record_audit(
            store,
            "task.browser_evidence_remediation_queued",
            {
                "task_id": task_id,
                "candidate_sha": candidate_sha,
                "finding_id": finding_id,
                "capture_id": capture["capture_id"],
                "remediation_id": str(remediation["id"]),
                "retry_event_id": remediation["retry_event_id"],
            },
        )
        payload["next_stage"] = str(Stage.IMPLEMENT)
        payload["status"] = str(TaskStatus.OPEN)
    return payload


def ensure_browser_evidence_remediation(
    store: Store, task_id: str, candidate_sha: str
):
    """Persist or recover the exact REVIEW remediation context for a failed browser decision."""
    latest = store.latest_task_remediation(task_id)
    if (
        latest is not None
        and str(latest["stage"]) == str(Stage.REVIEW)
        and str(latest["candidate_sha"]) == candidate_sha
    ):
        return latest
    decision = latest_browser_decision(store, task_id, candidate_sha)
    if decision is None or decision.get("decision") != "FAIL":
        return None
    findings = [
        row
        for row in store.open_findings_for_candidate(task_id, candidate_sha)
        if str(row["message"]).startswith("Headless responsive review failed for candidate ")
    ]
    if not findings:
        return None
    remediation_id = store.add_task_remediation(task_id, str(Stage.REVIEW), candidate_sha)
    remediation = store.task_remediation(remediation_id)
    if remediation is None:  # pragma: no cover - persistence invariant
        return None
    retry_event_id = record_audit(
        store,
        "task.remediation_queued",
        {
            "task_id": task_id,
            "candidate_sha": candidate_sha,
            "stage": str(Stage.REVIEW),
            "finding_count": len(findings),
            "remediation_id": remediation_id,
            "budget_epoch": int(remediation["budget_epoch"]),
            "source": "accountable_browser_evidence",
        },
    )
    store.bind_task_remediation_retry(remediation_id, retry_event_id)
    for finding in findings:
        store.add_remediation_attempt(
            str(finding["id"]),
            "QUEUED",
            {
                "candidate_sha": candidate_sha,
                "failed_stage": str(Stage.REVIEW),
                "next_stage": str(Stage.IMPLEMENT),
            },
        )
    return store.task_remediation(remediation_id)


def browser_evidence_context(
    store: Store, project: Path, task_id: str, candidate_sha: str
) -> dict[str, object] | None:
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if capture is None:
        return None
    try:
        _verify_capture_artifacts(project, capture)
        integrity_valid = True
        integrity_error = None
    except BrowserEvidenceError as exc:
        integrity_valid = False
        integrity_error = str(exc)
    decision = latest_browser_decision(store, task_id, candidate_sha)
    if decision is not None and decision.get("capture_id") != capture.get("capture_id"):
        decision = None
    return {
        "capture_id": capture.get("capture_id"),
        "candidate_sha": candidate_sha,
        "headless": capture.get("headless") is True,
        "artifacts": capture.get("artifacts", []),
        "decision": decision if integrity_valid else None,
        "integrity_valid": integrity_valid,
        "integrity_error": integrity_error,
    }


def browser_evidence_accepted(
    store: Store, project: Path, task_id: str, candidate_sha: str
) -> bool:
    decision = latest_browser_decision(store, task_id, candidate_sha)
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if not decision or decision.get("decision") != "PASS" or capture is None:
        return False
    try:
        _verify_capture_artifacts(project, capture)
    except BrowserEvidenceError:
        return False
    return decision.get("capture_id") == capture.get("capture_id")
