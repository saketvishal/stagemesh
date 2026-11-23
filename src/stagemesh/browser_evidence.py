"""No-window browser evidence bound to one retained task candidate.

The browser is an explicitly headless local process.  StageMesh never opens a
desktop window, never navigates away from a loopback origin, and never installs
a browser or project dependency. Captures are evidence, not approval:
independent candidate review must bind the exact capture receipt. A human
decision is required only when the governing specification leaves an
owner-decision gate unresolved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Self, cast
from urllib.parse import urljoin, urlparse

from .audit import record_audit
from .concurrency import IntegrationLock
from .config import load_config
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .execution import kill_process_tree, popen_session_kwargs
from .git import GitWorkspace
from .persistence import Store
from .redaction import REDACTED, redact_command_secrets
from .remediation import finding_identity
from .spec_planning import PROVIDER_PROCESS_ENV
from .workspaces import task_workspace

CAPTURE_EVENT = "task.browser_evidence_captured"
DECISION_EVENT = "task.browser_evidence_decided"
INVALIDATED_EVENT = "task.browser_evidence_invalidated"
AUTOMATIC_RECAPTURE_EVENT = "task.browser_evidence_recaptured"
AUTOMATIC_EXPANSION_EVENT = "task.browser_evidence_expanded"
AUTOMATIC_EXPANSION_FAILED_EVENT = "task.browser_evidence_expansion_failed"
CAPTURE_PROTOCOL = "cdp-device-metrics-v1"
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
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


class BrowserEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureRequest:
    name: str
    width: int
    height: int
    route: str
    dialog_trigger: str | None = None


@dataclass(frozen=True)
class BrowserRecaptureRecipe:
    """A replay-safe recipe recovered from one invalidated exact-candidate capture."""

    server_command: tuple[str, ...]
    base_url: str
    captures: tuple[CaptureRequest, ...]


@dataclass(frozen=True)
class BrowserExpansionRecipe:
    """A bounded same-origin expansion of one verified exact-candidate capture."""

    source_capture_id: str
    finding_digest: str
    server_command: tuple[str, ...]
    base_url: str
    captures: tuple[CaptureRequest, ...]
    findings: tuple[str, ...]


_AUTOMATIC_RECAPTURE_LOCK = threading.Lock()


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
    for command in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
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
    proof_error = _capture_viewport_proof_error(capture)
    if proof_error is not None:
        raise BrowserEvidenceError(proof_error)
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
        requested_viewport = artifact.get("requested_viewport")
        css_viewport = artifact.get("css_viewport")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(viewport, str)
            or not isinstance(requested_viewport, str)
            or not isinstance(css_viewport, str)
        ):
            raise BrowserEvidenceError("browser evidence artifact metadata is incomplete")
        if requested_viewport != css_viewport:
            raise BrowserEvidenceError(
                f"browser evidence CSS viewport was {css_viewport}, not {requested_viewport}"
            )
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
        if viewport != requested_viewport:
            raise BrowserEvidenceError(
                f"browser evidence bitmap was {viewport}, not {requested_viewport}"
            )


def _capture_viewport_proof_error(capture: dict[str, object]) -> str | None:
    if capture.get("capture_protocol") != CAPTURE_PROTOCOL:
        return "browser capture predates verified CSS viewport evidence; recapture is required"
    artifacts = capture.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return "browser evidence capture has no recorded artifacts"
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return "browser evidence artifact record is invalid"
        requested = artifact.get("requested_viewport")
        actual = artifact.get("css_viewport")
        if not isinstance(requested, str) or not isinstance(actual, str):
            return "browser capture lacks measured CSS viewport evidence; recapture is required"
        if requested != actual:
            return f"browser capture measured CSS viewport {actual}, not requested {requested}"
    return None


def _wait_ready(url: str, process: subprocess.Popen[str], timeout_seconds: float) -> None:
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserEvidenceError("local evidence server exited before it became ready")
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read(64 * 1024)
            if response.status < 500:
                return
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.25)
    raise BrowserEvidenceError("local evidence server did not become ready before the timeout")


def _headless_command(
    executable: Path,
    profile: Path,
    debug_port: int,
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
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={debug_port}",
        "about:blank",
    ]


def _available_debug_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _DebuggerClient:
    """Minimal loopback-only Chrome DevTools Protocol WebSocket client."""

    def __init__(self, websocket_url: str, timeout_seconds: float = 15.0):
        parsed = urlparse(websocket_url)
        if (
            parsed.scheme != "ws"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.username
            or parsed.password
        ):
            raise BrowserEvidenceError("browser debugger endpoint is not a loopback WebSocket")
        self._socket = socket.create_connection(("127.0.0.1", parsed.port), timeout_seconds)
        self._socket.settimeout(timeout_seconds)
        self._buffer = b""
        self._events: list[dict[str, object]] = []
        self._next_id = 0
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._receive_headers()
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            self.close()
            raise BrowserEvidenceError("browser debugger rejected its loopback WebSocket")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        )
        headers = {
            name.strip().lower(): value.strip()
            for line in response.split(b"\r\n")[1:]
            if b":" in line
            for name, value in (line.split(b":", 1),)
        }
        if not hmac.compare_digest(headers.get(b"sec-websocket-accept", b""), expected):
            self.close()
            raise BrowserEvidenceError("browser debugger WebSocket handshake was invalid")

    def _receive_headers(self) -> bytes:
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise BrowserEvidenceError("browser debugger closed during WebSocket handshake")
            response += chunk
            if len(response) > 65536:
                raise BrowserEvidenceError("browser debugger returned oversized headers")
        headers, self._buffer = response.split(b"\r\n\r\n", 1)
        return headers

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._socket.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise BrowserEvidenceError("browser debugger closed unexpectedly")
            self._buffer += chunk
        result, self._buffer = self._buffer[:size], self._buffer[size:]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        size = len(payload)
        header = bytearray((0x80 | opcode, 0x80 | min(126, size)))
        if size >= 65536:
            header[1] = 0x80 | 127
            header.extend(struct.pack(">Q", size))
        elif size >= 126:
            header.extend(struct.pack(">H", size))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def _receive_message(self) -> dict[str, object]:
        assembled = bytearray()
        started = False
        while True:
            first, second = self._recv_exact(2)
            final, opcode = bool(first & 0x80), first & 0x0F
            if second & 0x80:
                raise BrowserEvidenceError("browser debugger sent a masked server frame")
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._recv_exact(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._recv_exact(8))[0]
            if size > 32 * 1024 * 1024:
                raise BrowserEvidenceError("browser debugger response exceeded 32 MiB")
            payload = self._recv_exact(size)
            if opcode == 0x8:
                raise BrowserEvidenceError("browser debugger closed unexpectedly")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                assembled = bytearray(payload)
                started = True
            elif opcode == 0x0 and started:
                assembled.extend(payload)
            else:
                raise BrowserEvidenceError("browser debugger sent an unsupported WebSocket frame")
            if final:
                break
        try:
            message = json.loads(assembled.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserEvidenceError("browser debugger returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise BrowserEvidenceError("browser debugger returned a non-object message")
        return {str(key): value for key, value in message.items()}

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send_frame(0x1, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        while True:
            try:
                message = self._receive_message()
            except TimeoutError as exc:
                raise BrowserEvidenceError(f"browser debugger timed out during {method}") from exc
            if message.get("id") == request_id:
                if "error" in message:
                    raise BrowserEvidenceError(f"browser debugger rejected {method}")
                result = message.get("result", {})
                return result if isinstance(result, dict) else {}
            if isinstance(message.get("method"), str):
                self._events.append(message)

    def wait_event(self, method: str, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for index, event in enumerate(self._events):
                if event.get("method") == method:
                    self._events.pop(index)
                    return
            self._socket.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                message = self._receive_message()
            except TimeoutError:
                break
            if message.get("method") == method:
                return
            if isinstance(message.get("method"), str):
                self._events.append(message)
        raise BrowserEvidenceError(f"browser debugger did not emit {method} before timeout")

    def close(self) -> None:
        if getattr(self, "_socket", None) is not None:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self._socket.close()
            self._socket = None  # type: ignore[assignment]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _debugger_target(port: int, timeout_seconds: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            targets = _debugger_json(port, "/json/list")
        except BrowserEvidenceError:
            time.sleep(0.1)
            continue
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, dict) and target.get("type") == "page":
                    endpoint = target.get("webSocketDebuggerUrl")
                    if isinstance(endpoint, str):
                        parsed = urlparse(endpoint)
                        if (
                            parsed.scheme == "ws"
                            and parsed.hostname in {"127.0.0.1", "localhost"}
                            and parsed.port == port
                            and not parsed.username
                            and not parsed.password
                        ):
                            return endpoint
        time.sleep(0.1)
    raise BrowserEvidenceError("headless browser debugger did not become ready before timeout")


def _debugger_json(port: int, path: str) -> object:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise BrowserEvidenceError("headless browser debugger returned a non-success status")
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise BrowserEvidenceError("headless browser debugger response exceeded 2 MiB")
        return json.loads(body)
    except (OSError, json.JSONDecodeError) as exc:
        raise BrowserEvidenceError("headless browser debugger returned invalid JSON") from exc
    finally:
        connection.close()


def _browser_debugger_endpoint(port: int) -> str:
    version = _debugger_json(port, "/json/version")
    endpoint = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if not isinstance(endpoint, str):
        raise BrowserEvidenceError("headless browser root debugger endpoint is missing")
    parsed = urlparse(endpoint)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != port:
        raise BrowserEvidenceError("headless browser root debugger is not loopback-bound")
    return endpoint


def _close_debug_browser(port: int) -> None:
    try:
        endpoint = _browser_debugger_endpoint(port)
        with _DebuggerClient(endpoint) as client:
            try:
                client.call("Browser.close")
            except BrowserEvidenceError:
                pass  # A successful close may drop the socket before returning a response.
    except (BrowserEvidenceError, OSError):
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _debugger_json(port, "/json/version")
        except BrowserEvidenceError:
            return
        time.sleep(0.1)


def _remote_value(response: dict[str, object]) -> object:
    remote = response.get("result")
    return remote.get("value") if isinstance(remote, dict) else None


def _navigate_capture(
    client: _DebuggerClient,
    origin: str,
    capture: CaptureRequest,
) -> str:
    """Navigate one bounded same-origin GET route at its exact CSS viewport."""
    target_url = urljoin(origin, capture.route.lstrip("/"))
    requested = f"{capture.width}x{capture.height}"
    client.call("Page.enable")
    client.call(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": capture.width,
            "height": capture.height,
            "deviceScaleFactor": 1,
            "mobile": False,
            "screenWidth": capture.width,
            "screenHeight": capture.height,
            "positionX": 0,
            "positionY": 0,
        },
    )
    navigation = client.call("Page.navigate", {"url": target_url})
    if navigation.get("errorText"):
        raise BrowserEvidenceError(
            f"headless capture {capture.name} navigation failed: {navigation['errorText']}"
        )
    client.wait_event("Page.loadEventFired")
    return requested


def _open_declared_dialog(
    client: _DebuggerClient,
    capture: CaptureRequest,
) -> None:
    """Click only an element that explicitly declares a dialog popup."""
    if capture.dialog_trigger is None:
        return
    wanted = json.dumps(" ".join(capture.dialog_trigger.split()).casefold())
    response = client.call(
        "Runtime.evaluate",
        {
            "expression": f"""(async () => {{
const norm = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const wanted = {wanted};
const nodes = [...document.querySelectorAll('[aria-haspopup=\"dialog\"]')];
const target = nodes.find(node => norm(node.getAttribute('aria-label') || node.textContent) === wanted);
if (!target) return {{clicked:false, dialog:false}};
target.click();
for (let attempt = 0; attempt < 30; attempt += 1) {{
  if (document.querySelector('[role=\"dialog\"]')) return {{clicked:true, dialog:true}};
  await new Promise(resolve => setTimeout(resolve, 50));
}}
return {{clicked:true, dialog:false}};
}})()""",
            "awaitPromise": True,
            "returnByValue": True,
        },
    )
    result = _remote_value(response)
    if not isinstance(result, dict) or result.get("clicked") is not True:
        raise BrowserEvidenceError(
            f"capture {capture.name} could not find its declared dialog trigger"
        )
    if result.get("dialog") is not True:
        raise BrowserEvidenceError(
            f"capture {capture.name} trigger did not open a declared dialog"
        )


def _capture_one(
    client: _DebuggerClient,
    root: Path,
    origin: str,
    capture: CaptureRequest,
) -> dict[str, object]:
    target = root / f"{capture.name}.png"
    requested = _navigate_capture(client, origin, capture)
    _open_declared_dialog(client, capture)
    measured = client.call(
        "Runtime.evaluate",
        {
            "expression": "({width:window.innerWidth,height:window.innerHeight,deviceScaleFactor:window.devicePixelRatio})",
            "returnByValue": True,
        },
    )
    value = _remote_value(measured)
    if not isinstance(value, dict):
        raise BrowserEvidenceError(f"capture {capture.name} could not measure its CSS viewport")
    css_width, css_height = value.get("width"), value.get("height")
    if not isinstance(css_width, int) or not isinstance(css_height, int):
        raise BrowserEvidenceError(f"capture {capture.name} returned an invalid CSS viewport")
    css_viewport = f"{css_width}x{css_height}"
    if css_viewport != requested:
        raise BrowserEvidenceError(
            f"capture {capture.name} measured CSS viewport {css_viewport}, not requested {requested}"
        )
    screenshot = client.call(
        "Page.captureScreenshot",
        {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
    )
    encoded = screenshot.get("data")
    if not isinstance(encoded, str):
        raise BrowserEvidenceError(f"headless capture {capture.name} produced no PNG data")
    try:
        target.write_bytes(base64.b64decode(encoded, validate=True))
    except (OSError, ValueError) as exc:
        raise BrowserEvidenceError(f"headless capture {capture.name} produced invalid PNG data") from exc
    width, height = _png_dimensions(target)
    if (width, height) != (capture.width, capture.height):
        raise BrowserEvidenceError(
            f"capture {capture.name} has unexpected dimensions {width}x{height}"
        )
    artifact: dict[str, object] = {
        "name": capture.name,
        "route": capture.route,
        "requested_viewport": requested,
        "css_viewport": css_viewport,
        "device_scale_factor": value.get("deviceScaleFactor"),
        "viewport": f"{width}x{height}",
        "path": target,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
    }
    if capture.dialog_trigger is not None:
        artifact["interaction"] = {
            "kind": "declared_dialog_trigger",
            "accessible_name": capture.dialog_trigger,
        }
    return artifact


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


_EVIDENCE_STOP_WORDS = frozenset(
    {
        "browser",
        "capture",
        "desktop",
        "evidence",
        "internal",
        "missing",
        "mobile",
        "record",
        "required",
        "responsive",
        "review",
        "supplied",
        "verified",
        "viewport",
    }
)


def _evidence_tokens(findings: tuple[str, ...]) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}", " ".join(findings))
        if token.casefold() not in _EVIDENCE_STOP_WORDS
    }


def _probe_page_states(
    client: _DebuggerClient,
    origin: str,
    route: str,
    width: int,
    height: int,
) -> dict[str, list[dict[str, str]] | list[str]]:
    """Read bounded same-origin links and explicitly declared dialog triggers."""
    probe = CaptureRequest("probe", width, height, route)
    _navigate_capture(client, origin, probe)
    response = client.call(
        "Runtime.evaluate",
        {
            "expression": """(() => {
const norm = value => (value || '').replace(/\\s+/g, ' ').trim();
const links = [...document.querySelectorAll('a[href]')].slice(0, 100).flatMap(node => {
  try {
    const url = new URL(node.getAttribute('href'), location.href);
    if (url.origin !== location.origin) return [];
    return [{route:url.pathname + url.search + url.hash, text:norm(node.getAttribute('aria-label') || node.textContent)}];
  } catch (_) { return []; }
});
const dialogs = [...document.querySelectorAll('[aria-haspopup=\"dialog\"]')].slice(0, 20)
  .map(node => norm(node.getAttribute('aria-label') || node.textContent)).filter(Boolean);
return {links, dialogs};
})()""",
            "returnByValue": True,
        },
    )
    value = _remote_value(response)
    if not isinstance(value, dict):
        raise BrowserEvidenceError("headless evidence expansion could not inspect page states")
    raw_links, raw_dialogs = value.get("links"), value.get("dialogs")
    links: list[dict[str, str]] = []
    for item in raw_links if isinstance(raw_links, list) else []:
        if not isinstance(item, dict):
            continue
        route_value, text_value = item.get("route"), item.get("text")
        if not isinstance(route_value, str) or not isinstance(text_value, str):
            continue
        try:
            parsed = parse_capture_request(f"probe={width}x{height}:{route_value}")
        except BrowserEvidenceError:
            continue
        links.append({"route": parsed.route, "text": " ".join(text_value.split())[:500]})
    dialog_values = raw_dialogs if isinstance(raw_dialogs, list) else []
    dialogs = [
        " ".join(item.split())[:200]
        for item in dialog_values if isinstance(item, str) and item.strip()
    ]
    return {"links": links, "dialogs": dialogs}


def _expanded_capture_requests(
    client: _DebuggerClient,
    origin: str,
    captures: tuple[CaptureRequest, ...],
    findings: tuple[str, ...],
) -> tuple[CaptureRequest, ...]:
    """Expand a route-only recipe with relevant same-origin GET and dialog states."""
    if not findings or any(capture.dialog_trigger is not None for capture in captures):
        return captures
    viewports = tuple(dict.fromkeys((capture.width, capture.height) for capture in captures))
    if not viewports:
        return captures
    probe_width, probe_height = min(viewports, key=lambda item: item[0] * item[1])
    tokens = _evidence_tokens(findings)
    existing_routes = {capture.route for capture in captures}
    candidates: dict[str, tuple[int, str]] = {}
    for seed in captures:
        first_segment = seed.route.split("/", 2)[1] if seed.route.startswith("/") and seed.route != "/" else ""
        if not first_segment:
            continue
        states = _probe_page_states(client, origin, seed.route, probe_width, probe_height)
        for item in states["links"]:
            assert isinstance(item, dict)
            route, text = item["route"], item["text"]
            if route in existing_routes or not route.startswith(f"/{first_segment}/"):
                continue
            haystack = f"{route} {text}".casefold()
            score = sum(1 for token in tokens if token in haystack)
            if score > 0 and score > candidates.get(route, (0, ""))[0]:
                candidates[route] = (score, text)
    selected = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))[:3]
    if not selected:
        raise BrowserEvidenceError(
            "verified browser evidence has no relevant bounded same-origin route to expand"
        )
    result = list(captures)
    wants_dialog = any(
        marker in " ".join(findings).casefold()
        for marker in ("dialog", "enlarg", "modal", "preview", "open larger")
    )
    for route_index, (route, _rank) in enumerate(selected, start=1):
        states = _probe_page_states(client, origin, route, probe_width, probe_height)
        dialog_names = states["dialogs"] if wants_dialog else []
        dialog_name = str(dialog_names[0]) if len(dialog_names) == 1 else None
        for viewport_index, (width, height) in enumerate(viewports, start=1):
            result.append(
                CaptureRequest(
                    f"expanded-{route_index}-{viewport_index}", width, height, route
                )
            )
            if dialog_name is not None:
                result.append(
                    CaptureRequest(
                        f"expanded-{route_index}-{viewport_index}-dialog",
                        width,
                        height,
                        route,
                        dialog_name,
                    )
                )
    if len(result) > 20:
        raise BrowserEvidenceError("bounded browser evidence expansion exceeds 20 captures")
    return tuple(result)


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
    expand_findings: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    profile = root / "browser-profile"
    server: subprocess.Popen[str] | None = None
    browser: subprocess.Popen[str] | None = None
    debug_port: int | None = None
    try:
        with (
            (root / "server.stdout.log").open("w", encoding="utf-8") as stdout,
            (root / "server.stderr.log").open("w", encoding="utf-8") as stderr,
            (root / "browser.stdout.log").open("w", encoding="utf-8") as browser_stdout,
            (root / "browser.stderr.log").open("w", encoding="utf-8") as browser_stderr,
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
            profile.mkdir(parents=True, exist_ok=True)
            debug_port = _available_debug_port()
            try:
                browser = subprocess.Popen(
                    _headless_command(executable, profile, debug_port),
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=browser_stdout,
                    stderr=browser_stderr,
                    text=True,
                    **popen_session_kwargs(),
                )
            except OSError as exc:
                raise BrowserEvidenceError(
                    f"could not start the installed headless browser: {type(exc).__name__}"
                ) from exc
            endpoint = _debugger_target(debug_port)
            with _DebuggerClient(endpoint) as client:
                requested = (
                    _expanded_capture_requests(client, origin, captures, expand_findings)
                    if expand_findings
                    else captures
                )
                return [_capture_one(client, root, origin, capture) for capture in requested]
    finally:
        if debug_port is not None:
            _close_debug_browser(debug_port)
        if browser is not None and browser.poll() is None:
            kill_process_tree(browser)
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
    expand_findings: tuple[str, ...] = (),
    expanded_from_capture: str | None = None,
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
        expand_findings=expand_findings,
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
        "capture_protocol": CAPTURE_PROTOCOL,
        "base_origin": origin,
        "server_command": redact_command_secrets(shlex.join(server_command)),
        "artifacts": artifacts,
    }
    if expand_findings:
        payload["expanded_from_capture"] = expanded_from_capture
        payload["expansion_finding_digest"] = hashlib.sha256(
            json.dumps(sorted(expand_findings), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
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


def retire_unverified_browser_failure(
    store: Store, task_id: str, candidate_sha: str
) -> bool:
    """Retire a FAIL derived from legacy captures that never proved the CSS viewport."""
    capture = latest_browser_capture(store, task_id, candidate_sha)
    decision = latest_browser_decision(store, task_id, candidate_sha)
    if (
        capture is None
        or decision is None
        or decision.get("decision") != "FAIL"
        or decision.get("capture_id") != capture.get("capture_id")
        or _capture_viewport_proof_error(capture) is None
    ):
        return False
    if any(
        event.get("candidate_sha") == candidate_sha
        and event.get("capture_id") == capture.get("capture_id")
        for event in _event_payloads(store, INVALIDATED_EVENT, task_id)
    ):
        return False
    finding_ids: list[str] = []
    for finding in store.open_findings_for_candidate(task_id, candidate_sha):
        if str(finding["message"]).startswith(
            "Headless responsive review failed for candidate "
        ):
            finding_ids.append(str(finding["id"]))
            store.close_finding(str(finding["id"]))
    latest_remediation = store.latest_task_remediation(task_id)
    cleared = bool(
        latest_remediation is not None
        and str(latest_remediation["stage"]) == str(Stage.REVIEW)
        and str(latest_remediation["candidate_sha"]) == candidate_sha
        and store.clear_task_remediation(str(latest_remediation["id"]))
    )
    store.advance_task(task_id, Stage.REVIEW)
    from .block_kinds import BlockKind, record_block

    reason = (
        "the prior browser FAIL is invalid because its capture did not verify the actual CSS "
        "viewport; preserve the candidate and recapture before any implementation retry"
    )
    record_block(
        store,
        task_id,
        BlockKind.EXTERNAL_DEPENDENCY,
        reason,
        stage=str(Stage.REVIEW),
        candidate_sha=candidate_sha,
        detail={
            "failure_code": "browser_viewport_proof_required",
            "requirements": [
                "verified browser capture whose measured CSS viewport matches the requested viewport"
            ],
        },
    )
    record_audit(
        store,
        INVALIDATED_EVENT,
        {
            "task_id": task_id,
            "candidate_sha": candidate_sha,
            "capture_id": capture.get("capture_id"),
            "reason": _capture_viewport_proof_error(capture),
            "resolved_finding_ids": finding_ids,
            "cleared_remediation": cleared,
            "next_stage": str(Stage.REVIEW),
        },
    )
    return True


def browser_evidence_expansion_recipe(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str,
    findings: tuple[str, ...] | list[str],
) -> BrowserExpansionRecipe | None:
    """Return one safe route/dialog expansion for unresolved browser coverage."""
    messages = tuple(str(item).strip() for item in findings if str(item).strip())
    if not messages or not any(browser_evidence_required(item, [item]) for item in messages):
        return None
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if capture is None:
        return None
    try:
        _verify_capture_artifacts(project, capture)
        command_text = capture.get("server_command")
        if not isinstance(command_text, str) or not command_text.strip() or REDACTED in command_text:
            return None
        command = parse_server_command(json.dumps(shlex.split(command_text)))
        base_url = _loopback_base_url(str(capture.get("base_origin", "")))
    except (BrowserEvidenceError, ValueError):
        return None
    artifacts = capture.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    captures: list[CaptureRequest] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        name, route = artifact.get("name"), artifact.get("route")
        viewport = artifact.get("requested_viewport") or artifact.get("viewport")
        if not isinstance(name, str) or not isinstance(route, str) or not isinstance(viewport, str):
            return None
        try:
            captures.append(parse_capture_request(f"{name}={viewport}:{route}"))
        except BrowserEvidenceError:
            return None
    source_capture_id = capture.get("capture_id")
    if not isinstance(source_capture_id, str):
        return None
    finding_digest = hashlib.sha256(
        json.dumps(sorted(messages), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if capture.get("expansion_finding_digest") == finding_digest:
        return None
    for event_type in (AUTOMATIC_EXPANSION_EVENT, AUTOMATIC_EXPANSION_FAILED_EVENT):
        for event in _event_payloads(store, event_type, task_id):
            if (
                event.get("candidate_sha") == candidate_sha
                and event.get("source_capture_id") == source_capture_id
                and event.get("finding_digest") == finding_digest
            ):
                return None
    return BrowserExpansionRecipe(
        source_capture_id,
        finding_digest,
        command,
        base_url,
        tuple(captures),
        messages,
    )


def expand_browser_evidence(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str,
    expected_recipe: BrowserExpansionRecipe,
) -> dict[str, object]:
    """Capture relevant same-origin routes and declared dialogs without app edits."""
    try:
        with _AUTOMATIC_RECAPTURE_LOCK:
            lock_path = project / ".stagemesh" / "locks" / f"browser-evidence-{_safe_task_id(task_id)}.lock"
            with IntegrationLock(lock_path, timeout_seconds=60.0).hold(
                f"browser-expand:{task_id}"
            ):
                recipe = browser_evidence_expansion_recipe(
                    store,
                    project,
                    task_id,
                    candidate_sha,
                    expected_recipe.findings,
                )
                if recipe != expected_recipe:
                    raise BrowserEvidenceError("browser evidence expansion state changed while waiting")
                report = capture_browser_evidence(
                    store,
                    project,
                    task_id,
                    server_command=recipe.server_command,
                    base_url=recipe.base_url,
                    captures=recipe.captures,
                    expand_findings=recipe.findings,
                    expanded_from_capture=recipe.source_capture_id,
                )
                record_audit(
                    store,
                    AUTOMATIC_EXPANSION_EVENT,
                    {
                        "task_id": task_id,
                        "candidate_sha": candidate_sha,
                        "source_capture_id": recipe.source_capture_id,
                        "capture_id": report["capture_id"],
                        "finding_digest": recipe.finding_digest,
                        "artifact_count": len(cast(list[object], report["artifacts"])),
                    },
                )
                return report
    except (BrowserEvidenceError, OSError) as exc:
        record_audit(
            store,
            AUTOMATIC_EXPANSION_FAILED_EVENT,
            {
                "task_id": task_id,
                "candidate_sha": candidate_sha,
                "source_capture_id": expected_recipe.source_capture_id,
                "finding_digest": expected_recipe.finding_digest,
                "reason": str(exc)[:500],
            },
        )
        if isinstance(exc, BrowserEvidenceError):
            raise
        raise BrowserEvidenceError(
            f"automatic browser evidence expansion failed: {type(exc).__name__}"
        ) from exc


def invalidated_browser_recapture_recipe(
    store: Store, task_id: str, candidate_sha: str
) -> BrowserRecaptureRecipe | None:
    """Recover a bounded recipe only when the current legacy capture was explicitly invalidated.

    The prior command was already operator supplied and is stored in redacted form.  A
    redacted command cannot be replayed because guessing its missing value would expand
    authority.  Routes and viewports are reconstructed only from the invalidated
    candidate-bound artifacts.
    """
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if capture is None or _capture_viewport_proof_error(capture) is None:
        return None
    capture_id = capture.get("capture_id")
    decision = latest_browser_decision(store, task_id, candidate_sha)
    if (
        decision is None
        or decision.get("decision") != "FAIL"
        or decision.get("capture_id") != capture_id
    ):
        return None
    invalidated = any(
        event.get("candidate_sha") == candidate_sha
        and event.get("capture_id") == capture_id
        for event in _event_payloads(store, INVALIDATED_EVENT, task_id)
    )
    if not invalidated:
        return None
    command_text = capture.get("server_command")
    if (
        not isinstance(command_text, str)
        or not command_text.strip()
        or REDACTED in command_text
    ):
        return None
    try:
        command = parse_server_command(json.dumps(shlex.split(command_text)))
        base_url = _loopback_base_url(str(capture.get("base_origin", "")))
    except (BrowserEvidenceError, ValueError):
        return None
    artifacts = capture.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    requests: list[CaptureRequest] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        name, route = artifact.get("name"), artifact.get("route")
        viewport = artifact.get("requested_viewport") or artifact.get("viewport")
        if (
            not isinstance(name, str)
            or not isinstance(route, str)
            or not isinstance(viewport, str)
        ):
            return None
        try:
            requests.append(parse_capture_request(f"{name}={viewport}:{route}"))
        except BrowserEvidenceError:
            return None
    if len({request.name for request in requests}) != len(requests):
        return None
    return BrowserRecaptureRecipe(command, base_url, tuple(requests))


def recapture_invalidated_browser_evidence(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str,
    expected_recipe: BrowserRecaptureRecipe | None = None,
) -> dict[str, object] | None:
    """Replay an invalidated legacy capture without provider or operator intervention."""
    try:
        with _AUTOMATIC_RECAPTURE_LOCK:
            lock_path = project / ".stagemesh" / "locks" / f"browser-evidence-{_safe_task_id(task_id)}.lock"
            with IntegrationLock(lock_path, timeout_seconds=60.0).hold(
                f"browser-recapture:{task_id}"
            ):
                # Re-read under the process lock. Another StageMesh command may have
                # completed the capture while this process waited.
                recipe = invalidated_browser_recapture_recipe(store, task_id, candidate_sha)
                if recipe is None or (
                    expected_recipe is not None and recipe != expected_recipe
                ):
                    return None
                report = capture_browser_evidence(
                    store,
                    project,
                    task_id,
                    server_command=recipe.server_command,
                    base_url=recipe.base_url,
                    captures=recipe.captures,
                )
                record_audit(
                    store,
                    AUTOMATIC_RECAPTURE_EVENT,
                    {
                        "task_id": task_id,
                        "candidate_sha": candidate_sha,
                        "capture_id": report["capture_id"],
                        "source": "invalidated_exact_candidate_recipe",
                        "artifact_count": len(recipe.captures),
                    },
                )
    except OSError as exc:
        raise BrowserEvidenceError(
            f"automatic browser recapture could not create its local evidence: {type(exc).__name__}"
        ) from exc
    return report


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
) -> sqlite3.Row | None:
    """Persist or recover the exact REVIEW remediation context for a failed browser decision."""
    latest = store.latest_task_remediation(task_id)
    if (
        latest is not None
        and str(latest["stage"]) == str(Stage.REVIEW)
        and str(latest["candidate_sha"]) == candidate_sha
    ):
        return cast(sqlite3.Row, latest)
    capture = latest_browser_capture(store, task_id, candidate_sha)
    if capture is None or _capture_viewport_proof_error(capture) is not None:
        return None
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
    return cast(sqlite3.Row | None, store.task_remediation(remediation_id))


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


def browser_review_requirement(
    store: Store, project: Path, task_id: str, candidate_sha: str
) -> dict[str, object] | None:
    """Return the exact verified capture receipt an independent reviewer must bind.

    An explicit operator decision remains authoritative when one exists.  Without
    one, technical visual acceptance belongs to independent review rather than a
    second mandatory operator approval.
    """
    context = browser_evidence_context(store, project, task_id, candidate_sha)
    if (
        context is None
        or context.get("integrity_valid") is not True
        or context.get("decision") is not None
    ):
        return None
    artifacts = context.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    receipts: list[dict[str, object]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        required = {
            "name": artifact.get("name"),
            "path": artifact.get("path"),
            "sha256": artifact.get("sha256"),
            "route": artifact.get("route"),
            "requested_viewport": artifact.get("requested_viewport"),
            "css_viewport": artifact.get("css_viewport"),
        }
        if not all(isinstance(value, str) and value for value in required.values()):
            return None
        receipts.append(required)
    return {
        "capture_id": context["capture_id"],
        "candidate_sha": candidate_sha,
        "artifacts": receipts,
    }


def browser_evidence_recheck_requirement(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str,
    findings: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> dict[str, object] | None:
    """Authorize one review when verified browser evidence post-dates browser-only findings.

    This is deliberately narrower than a generic review retry.  At least one open
    finding must require browser evidence, every open finding must be satisfiable
    by that browser review (including the acceptance record the review creates),
    and the exact verified capture must be newer than every finding.  A terminal
    review receipt for the capture consumes the authorization, while provider
    outages and malformed answers do not.
    """
    from .review_remediation import EXTERNAL_EVIDENCE, classify_review_finding

    requirement = browser_review_requirement(store, project, task_id, candidate_sha)
    if requirement is None or not findings:
        return None
    messages = [str(item.get("message") or "") for item in findings]
    browser_flags = [browser_evidence_required(message, [message]) for message in messages]
    satisfiable = [
        classify_review_finding(message) == EXTERNAL_EVIDENCE
        and (browser_flag or "internal acceptance" in message.casefold())
        for message, browser_flag in zip(messages, browser_flags, strict=True)
    ]
    if not any(browser_flags) or not all(satisfiable):
        return None

    capture_id = requirement.get("capture_id")
    capture_at: float | None = None
    for row in store.task_audit_events(CAPTURE_EVENT, task_id):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("candidate_sha") == candidate_sha
            and payload.get("capture_id") == capture_id
        ):
            capture_at = float(row["created_at"])
    finding_times = [float(item.get("_at") or 0.0) for item in findings]
    if capture_at is None or capture_at <= max(finding_times, default=0.0):
        return None

    expected = sorted(
        str(artifact["sha256"])
        for artifact in requirement["artifacts"]
        if isinstance(artifact, dict) and isinstance(artifact.get("sha256"), str)
    )
    reviews = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='REVIEW' "
        "AND status IN ('PASSED', 'FAILED') AND created_at>=? ORDER BY created_at, rowid",
        (task_id, candidate_sha, capture_at),
    ).fetchall()
    for row in reviews:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        receipt = payload.get("browser_evidence_review") if isinstance(payload, dict) else None
        supplied = receipt.get("artifact_sha256s") if isinstance(receipt, dict) else None
        if (
            isinstance(receipt, dict)
            and receipt.get("capture_id") == capture_id
            and isinstance(supplied, list)
            and sorted(str(item) for item in supplied) == expected
        ):
            return None
    return requirement


def materialize_browser_review_bundle(
    store: Store,
    project: Path,
    review_path: Path,
    task_id: str,
    candidate_sha: str,
) -> dict[str, object] | None:
    """Copy verified PNGs into a disposable reviewer checkout with bounded paths."""
    requirement = browser_review_requirement(store, project, task_id, candidate_sha)
    if requirement is None:
        return None
    root = (review_path / ".stagemesh-review-evidence" / "browser").resolve()
    checkout = review_path.resolve()
    if checkout not in root.parents:
        raise BrowserEvidenceError("browser review bundle escaped the disposable checkout")
    root.mkdir(parents=True, exist_ok=False)
    bundled: list[dict[str, object]] = []
    for index, artifact in enumerate(cast(list[dict[str, object]], requirement["artifacts"])):
        name = str(artifact["name"])
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name) is None:
            raise BrowserEvidenceError("browser review artifact name is unsafe")
        source = (project / str(artifact["path"])).resolve()
        target = root / f"{index + 1:02d}-{name}.png"
        shutil.copyfile(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, str(artifact["sha256"])):
            raise BrowserEvidenceError("browser review bundle changed during materialization")
        bundled.append(
            {
                **artifact,
                "path": target.relative_to(review_path).as_posix(),
            }
        )
    return {
        "capture_id": requirement["capture_id"],
        "candidate_sha": candidate_sha,
        "artifacts": bundled,
    }


def verify_browser_review_bundle(review_path: Path, bundle: dict[str, object]) -> None:
    """Fail closed if a reviewer mutated its disposable evidence bundle."""
    root = (review_path / ".stagemesh-review-evidence" / "browser").resolve()
    artifacts = bundle.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BrowserEvidenceError("browser review bundle has no artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise BrowserEvidenceError("browser review bundle metadata is invalid")
        path = (review_path / str(artifact.get("path", ""))).resolve()
        if root not in path.parents or not path.is_file():
            raise BrowserEvidenceError("browser review artifact is missing or outside its bundle")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, str(artifact.get("sha256", ""))):
            raise BrowserEvidenceError("browser review artifact changed during review")
        width, height = _png_dimensions(path)
        if f"{width}x{height}" != artifact.get("requested_viewport"):
            raise BrowserEvidenceError("browser review artifact dimensions changed during review")


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
