from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _message(request_id: int, method: str, params: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload, separators=(",", ":"))


def test_stdio_proxy_filters_discovery_and_blocks_unlisted_calls(tmp_path: Path) -> None:
    marker = tmp_path / "dangerous-called"
    server = tmp_path / "fake_mcp.py"
    server.write_text(
        """from __future__ import annotations
import json
import pathlib
import sys

marker = pathlib.Path(sys.argv[1])
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "safe", "inputSchema": {"type": "object"}}, {"name": "dangerous", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        name = message["params"]["name"]
        if name == "dangerous":
            marker.write_text("called", encoding="utf-8")
        result = {"content": [{"type": "text", "text": name}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "server.json"
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "transport": "stdio",
                "server_name": "fake",
                "command": sys.executable,
                "args": [str(server), str(marker)],
                "cwd": ".",
                "allowed_tools": ["safe"],
                "startup_timeout_seconds": 5,
                "tool_timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(source_root), env.get("PYTHONPATH", "")) if value
    )
    client = subprocess.run(
        [
            sys.executable,
            "-m",
            "stagemesh.mcp_proxy",
            "--manifest",
            str(manifest),
            "--project",
            str(tmp_path),
        ],
        input="\n".join(
            (
                _message(1, "initialize", {}),
                _message(2, "tools/list", {}),
                _message(3, "tools/call", {"name": "dangerous", "arguments": {}}),
                _message(4, "tools/call", {"name": "safe", "arguments": {}}),
                _message(5, "resources/list", {}),
                _message(6, "prompts/get", {"name": "ambient"}),
                "",
            )
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )

    assert client.returncode == 0, client.stderr
    responses = {item["id"]: item for item in map(json.loads, client.stdout.splitlines())}
    assert [tool["name"] for tool in responses[2]["result"]["tools"]] == ["safe"]
    assert responses[3]["error"]["code"] == -32601
    assert responses[4]["result"]["content"][0]["text"] == "safe"
    assert responses[5]["error"]["code"] == -32601
    assert responses[6]["error"]["code"] == -32601
    assert not marker.exists()


def test_stdio_proxy_terminates_a_tool_call_at_the_manifest_timeout(tmp_path: Path) -> None:
    server = tmp_path / "slow_mcp.py"
    server.write_text(
        """from __future__ import annotations
import json
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "slow", "version": "1"}}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif message.get("method") == "tools/call":
        time.sleep(10)
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "server.json"
    manifest.write_text(
        json.dumps(
            {
                "format_version": 1,
                "transport": "stdio",
                "server_name": "slow",
                "command": sys.executable,
                "args": [str(server)],
                "cwd": ".",
                "allowed_tools": ["safe"],
                "startup_timeout_seconds": 5,
                "tool_timeout_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(source_root), env.get("PYTHONPATH", "")) if value
    )

    client = subprocess.run(
        [
            sys.executable,
            "-m",
            "stagemesh.mcp_proxy",
            "--manifest",
            str(manifest),
            "--project",
            str(tmp_path),
        ],
        input="\n".join(
            (
                _message(1, "initialize", {}),
                _message(2, "tools/call", {"name": "safe", "arguments": {}}),
                "",
            )
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )

    assert client.returncode == 124
    responses = {item["id"]: item for item in map(json.loads, client.stdout.splitlines())}
    assert responses[2]["error"] == {
        "code": -32001,
        "message": "MCP tool call exceeded StageMesh timeout",
    }
