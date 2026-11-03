"""Small stdio MCP policy proxy used by provider adapters.

The proxy exposes only the tools named by a validated StageMesh manifest.  It
does not claim to be an operating-system sandbox: the child server still runs
as the StageMesh user inside the task workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .mcp_capabilities import McpCapabilityError, McpServerConfig, load_mcp_manifest

_POLICY_ERROR = -32001
_METHOD_NOT_ALLOWED = -32601


class McpPolicyProxy:
    """Relay one stdio MCP server while filtering discovery and calls."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        client_in: TextIO,
        client_out: TextIO,
        diagnostics: TextIO,
    ) -> None:
        self.config = config
        self.client_in = client_in
        self.client_out = client_out
        self.diagnostics = diagnostics
        self.allowed = frozenset(config.allowed_tools)
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._list_requests: set[str] = set()
        self._timers: dict[str, threading.Timer] = {}
        self._expired: set[str] = set()
        self._timed_out = False
        self.process: subprocess.Popen[str] | None = None

    def run(self) -> int:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            if os.name == "nt"
            else 0
        )
        self.process = subprocess.Popen(
            (self.config.command, *self.config.args),
            cwd=self.config.cwd,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        server = threading.Thread(target=self._server_to_client, daemon=True)
        stderr = threading.Thread(target=self._copy_diagnostics, daemon=True)
        server.start()
        stderr.start()
        try:
            self._client_to_server()
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        code = self.process.wait()
        server.join(timeout=2)
        stderr.join(timeout=2)
        self._cancel_timers()
        return 124 if self._timed_out else code

    def _client_to_server(self) -> None:
        assert self.process is not None and self.process.stdin is not None
        for line in self.client_in:
            message = _message(line)
            if message is None:
                self._diagnostic("StageMesh MCP proxy rejected malformed client JSON")
                self._terminate()
                return
            method = message.get("method")
            request_id = _request_key(message.get("id")) if "id" in message else None
            if method == "tools/call":
                name = _tool_call_name(message)
                if name not in self.allowed:
                    if request_id is not None:
                        self._send_error(
                            message.get("id"),
                            _METHOD_NOT_ALLOWED,
                            "tool is not allowed by StageMesh capability policy",
                        )
                    continue
                if request_id is not None:
                    self._start_timer(
                        request_id,
                        message.get("id"),
                        self.config.tool_timeout_seconds,
                        "MCP tool call exceeded StageMesh timeout",
                    )
            elif method == "tools/list" and request_id is not None:
                with self._state_lock:
                    self._list_requests.add(request_id)
            elif isinstance(method, str) and method.startswith(("resources/", "prompts/")):
                if request_id is not None:
                    self._send_error(
                        message.get("id"),
                        _METHOD_NOT_ALLOWED,
                        "MCP resources and prompts are not enabled by this capability",
                    )
                continue
            elif method == "initialize" and request_id is not None:
                self._start_timer(
                    request_id,
                    message.get("id"),
                    self.config.startup_timeout_seconds,
                    "MCP server initialization exceeded StageMesh timeout",
                )
            self.process.stdin.write(_encode(message))
            self.process.stdin.flush()

    def _server_to_client(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            message = _message(line)
            if message is None:
                self._diagnostic("StageMesh MCP proxy rejected malformed server JSON")
                self._terminate()
                return
            method = message.get("method")
            if isinstance(method, str) and method.startswith(("notifications/resources/", "notifications/prompts/")):
                continue
            request_id = _request_key(message.get("id")) if "id" in message else None
            if request_id is not None:
                with self._state_lock:
                    if request_id in self._expired:
                        continue
                    timer = self._timers.pop(request_id, None)
                    is_list = request_id in self._list_requests
                    self._list_requests.discard(request_id)
                if timer is not None:
                    timer.cancel()
                if is_list:
                    _filter_tools(message, self.allowed)
            self._send(message)

    def _copy_diagnostics(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self.diagnostics.write(line)
            self.diagnostics.flush()

    def _start_timer(self, key: str, raw_id: object, seconds: int, message: str) -> None:
        def expire() -> None:
            with self._state_lock:
                if key not in self._timers:
                    return
                self._timers.pop(key, None)
                self._list_requests.discard(key)
                self._expired.add(key)
                self._timed_out = True
            self._send_error(raw_id, _POLICY_ERROR, message)
            self._terminate()

        timer = threading.Timer(seconds, expire)
        timer.daemon = True
        with self._state_lock:
            previous = self._timers.pop(key, None)
            self._timers[key] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    def _send_error(self, request_id: object, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self.client_out.write(_encode(message))
            self.client_out.flush()

    def _diagnostic(self, message: str) -> None:
        self.diagnostics.write(f"{message}\n")
        self.diagnostics.flush()

    def _terminate(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _cancel_timers(self) -> None:
        with self._state_lock:
            timers = tuple(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()


def _filter_tools(message: dict[str, Any], allowed: frozenset[str]) -> None:
    result = message.get("result")
    if not isinstance(result, dict):
        return
    tools = result.get("tools")
    if not isinstance(tools, list):
        return
    result["tools"] = [
        tool for tool in tools if isinstance(tool, dict) and tool.get("name") in allowed
    ]


def _tool_call_name(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) else None


def _message(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) and value.get("jsonrpc") == "2.0" else None


def _request_key(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StageMesh bounded stdio MCP proxy")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--project", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    project = Path(options.project).resolve()
    manifest = Path(options.manifest).resolve()
    try:
        if not manifest.is_relative_to(project):
            raise McpCapabilityError("MCP manifest must stay inside the project")
        config = load_mcp_manifest(manifest, project)
    except McpCapabilityError as exc:
        print(f"StageMesh MCP proxy refused configuration: {exc}", file=sys.stderr)
        return 2
    try:
        return McpPolicyProxy(
            config,
            client_in=sys.stdin,
            client_out=sys.stdout,
            diagnostics=sys.stderr,
        ).run()
    except (OSError, UnicodeError) as exc:
        print(f"StageMesh MCP proxy failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
