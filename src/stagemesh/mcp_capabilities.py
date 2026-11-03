"""Bounded, project-local MCP configuration for provider invocations.

The first adapter deliberately supports only Codex stdio servers. Codex can
ignore ambient user configuration, require one server, and enforce an exact
tool allowlist for a single invocation. Other providers remain fail-closed
until they can provide the same isolation and evidence properties.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class McpCapabilityError(RuntimeError):
    """An MCP manifest cannot be executed within StageMesh policy."""


_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ALLOWED_FIELDS = frozenset(
    {
        "format_version",
        "transport",
        "server_name",
        "command",
        "args",
        "cwd",
        "allowed_tools",
        "startup_timeout_seconds",
        "tool_timeout_seconds",
    }
)


@dataclass(frozen=True)
class McpServerConfig:
    server_name: str
    command: str
    args: tuple[str, ...]
    cwd: Path
    allowed_tools: tuple[str, ...]
    startup_timeout_seconds: int
    tool_timeout_seconds: int

    def private_payload(self) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "command": self.command,
            "args": list(self.args),
            "cwd": str(self.cwd),
            "allowed_tools": list(self.allowed_tools),
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
        }


def load_mcp_manifest(path: Path, project: Path) -> McpServerConfig:
    """Validate a no-secrets, no-network, read-only stdio server manifest."""
    root = Path(project).resolve()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpCapabilityError(f"cannot load MCP manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise McpCapabilityError("MCP manifest root must be an object")
    if set(payload) - _ALLOWED_FIELDS:
        raise McpCapabilityError("MCP manifest contains unsupported fields")
    if payload.get("format_version") != 1:
        raise McpCapabilityError("MCP manifest format_version must be 1")
    if payload.get("transport") != "stdio":
        raise McpCapabilityError("MCP manifest transport must be stdio")
    server_name = payload.get("server_name")
    if not isinstance(server_name, str) or not _NAME.fullmatch(server_name):
        raise McpCapabilityError("MCP server_name must be a lowercase portable identifier")
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise McpCapabilityError("MCP command must be a non-empty executable name or path")
    command = command.strip()
    if len(command) > 1000:
        raise McpCapabilityError("MCP command must be 1000 characters or fewer")
    args = _string_list(payload.get("args", []), "MCP args", maximum=64, portable=False)
    tools = _string_list(payload.get("allowed_tools"), "MCP allowed_tools", maximum=64)
    if not tools or any(not _TOOL.fullmatch(tool) for tool in tools):
        raise McpCapabilityError("MCP allowed_tools must contain portable tool names")
    cwd_value = payload.get("cwd", ".")
    if not isinstance(cwd_value, str) or not cwd_value.strip():
        raise McpCapabilityError("MCP cwd must be a project-relative directory")
    cwd = _inside_project(root, cwd_value.strip())
    if not cwd.is_dir():
        raise McpCapabilityError("MCP cwd must be an installed project directory")
    startup = _seconds(payload.get("startup_timeout_seconds", 10), "startup_timeout_seconds")
    tool_timeout = _seconds(payload.get("tool_timeout_seconds", 60), "tool_timeout_seconds")
    return McpServerConfig(
        server_name=server_name,
        command=command,
        args=args,
        cwd=cwd,
        allowed_tools=tools,
        startup_timeout_seconds=startup,
        tool_timeout_seconds=tool_timeout,
    )


def configure_codex_mcp_command(
    command: Iterable[str], attachments: Iterable[dict[str, object]]
) -> tuple[list[str], frozenset[str]]:
    """Add validated MCP config to one Codex invocation without persistent state."""
    args = list(command)
    executable = Path(args[0]).stem.casefold() if args else ""
    configs = [item.get("_mcp") for item in attachments if item.get("kind") == "mcp"]
    configs = [item for item in configs if isinstance(item, dict)]
    if not configs:
        return args, frozenset()
    if executable != "codex":
        raise McpCapabilityError("MCP runtime adapter currently supports only the codex executable")
    if "--ignore-user-config" not in args:
        args.append("--ignore-user-config")
    permitted: set[str] = set()
    server_names: set[str] = set()
    for config in configs:
        name = str(config["server_name"])
        if name in server_names:
            raise McpCapabilityError(f"duplicate MCP server_name: {name}")
        server_names.add(name)
        values: dict[str, object] = {
            "command": str(config["command"]),
            "args": list(config["args"]),
            "cwd": str(config["cwd"]),
            "enabled": True,
            "required": True,
            "enabled_tools": list(config["allowed_tools"]),
            "default_tools_approval_mode": "auto",
            "startup_timeout_sec": int(config["startup_timeout_seconds"]),
            "tool_timeout_sec": int(config["tool_timeout_seconds"]),
        }
        for field, value in values.items():
            key = f"mcp_servers.{name}.{field}"
            args.extend(("-c", f"{key}={_toml_value(value)}"))
            permitted.add(key)
    return args, frozenset(permitted)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _seconds(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 600:
        raise McpCapabilityError(f"MCP {field} must be an integer from 1 to 600")
    return value


def _string_list(value: object, field: str, *, maximum: int, portable: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise McpCapabilityError(f"{field} must be a list with at most {maximum} entries")
    if not all(isinstance(item, str) and item.strip() and "\x00" not in item for item in value):
        raise McpCapabilityError(f"{field} must contain non-empty strings")
    result = tuple(item.strip() if portable else item for item in value)
    if len(result) != len(set(result)):
        raise McpCapabilityError(f"{field} must not contain duplicates")
    if any(len(item) > 1000 for item in result):
        raise McpCapabilityError(f"{field} entries must be 1000 characters or fewer")
    return result


def _inside_project(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise McpCapabilityError("MCP cwd must stay inside the project")
    target = (root / relative).resolve()
    try:
        inside = target.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside:
        raise McpCapabilityError("MCP cwd must stay inside the project")
    return target
