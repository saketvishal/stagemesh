"""Bounded, project-local MCP configuration for provider invocations."""

from __future__ import annotations

import json
import re
import sys
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


def configure_claude_mcp_command(
    command: Iterable[str], attachments: Iterable[dict[str, object]]
) -> tuple[list[str], frozenset[str]]:
    """Attach allowlist-filtered stdio servers to one isolated Claude invocation."""
    args = list(command)
    executable = Path(args[0]).stem.casefold() if args else ""
    selected = [item for item in attachments if item.get("kind") == "mcp"]
    if not selected:
        return args, frozenset()
    if executable != "claude":
        raise McpCapabilityError("Claude MCP runtime adapter requires the claude executable")
    if _has_option(args, "--mcp-config"):
        raise McpCapabilityError(
            "Claude MCP runtime adapter refuses a provider command with preconfigured MCP servers"
        )

    servers: dict[str, dict[str, object]] = {}
    permitted_tools: list[str] = []
    for attachment in selected:
        config = attachment.get("_mcp")
        manifest = attachment.get("_path")
        project = attachment.get("_project")
        if not isinstance(config, dict) or not isinstance(manifest, str) or not manifest:
            raise McpCapabilityError("selected MCP capability is missing private runtime metadata")
        if not isinstance(project, str) or not project:
            raise McpCapabilityError("selected MCP capability is missing its project boundary")
        name = str(config.get("server_name") or "")
        if not _NAME.fullmatch(name):
            raise McpCapabilityError("selected MCP capability has an invalid server_name")
        if name in servers:
            raise McpCapabilityError(f"duplicate MCP server_name: {name}")
        tools = config.get("allowed_tools")
        if not isinstance(tools, list) or not tools:
            raise McpCapabilityError(f"MCP server {name} has no allowed tools")
        servers[name] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-m",
                "stagemesh.mcp_proxy",
                "--manifest",
                manifest,
                "--project",
                project,
            ],
        }
        permitted_tools.extend(f"mcp__{name}__{tool}" for tool in tools)

    args = _set_option(args, "--permission-prompts", "none")
    args = _merge_allowed_tools(args, permitted_tools)
    if "--strict-mcp-config" not in args:
        args.append("--strict-mcp-config")
    args.extend(
        (
            "--mcp-config",
            json.dumps({"mcpServers": servers}, ensure_ascii=False, separators=(",", ":")),
        )
    )
    return args, frozenset()


def configure_mcp_command(
    command: Iterable[str], attachments: Iterable[dict[str, object]]
) -> tuple[list[str], frozenset[str]]:
    """Dispatch one MCP attachment set to a provider-specific, fail-closed adapter."""
    args = list(command)
    selected = list(attachments)
    if not any(item.get("kind") == "mcp" for item in selected):
        return args, frozenset()
    executable = Path(args[0]).stem.casefold() if args else ""
    if executable == "codex":
        return configure_codex_mcp_command(args, selected)
    if executable == "claude":
        return configure_claude_mcp_command(args, selected)
    raise McpCapabilityError(
        f"MCP runtime adapter is unavailable for provider executable: {executable or '(missing)'}"
    )


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _has_option(args: list[str], option: str) -> bool:
    return any(token == option or token.startswith(f"{option}=") for token in args)


def _set_option(args: list[str], option: str, value: str) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == option:
            index += 2
            continue
        if token.startswith(f"{option}="):
            index += 1
            continue
        result.append(token)
        index += 1
    return [*result, option, value]


def _merge_allowed_tools(args: list[str], additions: Iterable[str]) -> list[str]:
    """Canonicalize Claude's variadic allowed-tools option without widening it."""
    result: list[str] = []
    existing: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--allowedTools", "--allowed-tools"}:
            index += 1
            while index < len(args) and not args[index].startswith("-"):
                existing.extend(part for part in args[index].split(",") if part)
                index += 1
            continue
        if token.startswith(("--allowedTools=", "--allowed-tools=")):
            existing.extend(part for part in token.split("=", 1)[1].split(",") if part)
            index += 1
            continue
        result.append(token)
        index += 1
    merged = list(dict.fromkeys([*existing, *additions]))
    return [*result, "--allowedTools", ",".join(merged)]


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
