"""Provider adapter catalog and side-effect-free local discovery.

The catalog combines bundled adapters with independently installed entry-point
plugins. It is not the project provider registry: ``stagemesh init`` merges
locally available entries into ``.stagemesh/config.json`` and runtime routing
uses that project registry. Discovery never launches, authenticates, or
authorizes a provider.
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from typing import Any

READ_ONLY_GIT_TOOLS = (
    "Bash(git status*),Bash(git diff*),Bash(git log*),"
    "Bash(git show*),Bash(git rev-parse*)"
)


@dataclass(frozen=True)
class ProviderDefinition:
    """A supported adapter definition, separate from project enablement."""

    name: str
    executable: str
    command: str
    legacy_command: str
    command_env: str
    implement_rank: int
    review_rank: int
    capabilities: tuple[str, ...] = ("IMPLEMENT", "REVIEW")


PROVIDER_CATALOG = (
    ProviderDefinition(
        "codex", "codex",
        "codex exec --sandbox workspace-write --ignore-user-config --ephemeral",
        "codex exec",
        "STAGEMESH_CODEX_CMD", 0, 3,
    ),
    ProviderDefinition(
        "claude", "claude",
        (
            "claude -p --permission-mode acceptEdits --permission-prompts none "
            "--safe-mode --no-chrome --no-session-persistence --disable-slash-commands "
            "--strict-mcp-config "
            f"--allowedTools 'Edit,Write,MultiEdit,{READ_ONLY_GIT_TOOLS},"
            "Bash(python *),Bash(pytest *),Bash(ruff *)'"
        ),
        "claude -p", "STAGEMESH_CLAUDE_CMD", 2, 0,
    ),
    ProviderDefinition(
        "grok", "grok",
        (
            "grok --permission-mode acceptEdits --disable-web-search --no-subagents "
            "--tools read_file,list_dir,grep,run_terminal_command "
            "--allow Edit --allow Write --allow MultiEdit "
            "--allow 'Bash(git status*)' --allow 'Bash(git diff*)' --allow 'Bash(git log*)' "
            "--allow 'Bash(git show*)' --allow 'Bash(git rev-parse*)' "
            "--allow 'Bash(python *)' --allow 'Bash(pytest *)' --allow 'Bash(ruff *)'"
        ),
        "grok", "STAGEMESH_GROK_CMD", 1, 1,
    ),
    ProviderDefinition(
        "agy", "agy", "agy --mode accept-edits --sandbox --disable-slash-commands",
        "agy", "STAGEMESH_AGY_CMD", 3, 2,
    ),
)

PROVIDER_PLUGIN_GROUP = "stagemesh.providers"


@lru_cache(maxsize=1)
def _provider_plugins() -> tuple[ProviderDefinition, ...]:
    """Load independently installed provider adapter plugins through Python entry points."""
    try:
        points = metadata.entry_points()
        selected = (
            points.select(group=PROVIDER_PLUGIN_GROUP)
            if hasattr(points, "select")
            else points.get(PROVIDER_PLUGIN_GROUP, ())
        )
    except (OSError, ValueError):
        return ()
    definitions: list[ProviderDefinition] = []
    for point in sorted(selected, key=lambda item: item.name):
        try:
            raw: Any = point.load()
            raw = raw() if callable(raw) else raw
            if isinstance(raw, ProviderDefinition):
                definition = raw
            elif isinstance(raw, dict):
                command = str(raw["command"])
                definition = ProviderDefinition(
                    name=str(raw.get("name") or point.name),
                    executable=str(raw["executable"]),
                    command=command,
                    legacy_command=str(raw.get("legacy_command") or command),
                    command_env=str(raw.get("command_env") or ""),
                    implement_rank=int(raw.get("implement_rank", 100)),
                    review_rank=int(raw.get("review_rank", 100)),
                    capabilities=tuple(
                        str(item)
                        for item in raw.get("capabilities", ("IMPLEMENT", "REVIEW"))
                    ),
                )
            else:
                continue
        except (ImportError, AttributeError, KeyError, TypeError, ValueError):
            continue
        if definition.name and definition.executable and definition.command:
            definitions.append(definition)
    return tuple(definitions)


def catalog_definitions() -> tuple[ProviderDefinition, ...]:
    """Bundled adapters plus provider plugins, with bundled names protected from replacement."""
    definitions = list(PROVIDER_CATALOG)
    known = {item.name for item in definitions}
    for item in _provider_plugins():
        if item.name not in known:
            definitions.append(item)
            known.add(item.name)
    return tuple(definitions)


def catalog_names() -> tuple[str, ...]:
    return tuple(item.name for item in catalog_definitions())


def catalog_commands() -> dict[str, str]:
    return {
        item.name: (os.environ.get(item.command_env) if item.command_env else None) or item.command
        for item in catalog_definitions()
    }


def discovered_provider_entries(
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, dict[str, object]]:
    """Return available catalog providers as config entries, without execution.

    An explicit command environment override also counts as local availability,
    but its value is never persisted because it may contain sensitive flags.
    """

    entries: dict[str, dict[str, object]] = {}
    for item in catalog_definitions():
        override = os.environ.get(item.command_env) if item.command_env else None
        if override is None and which(item.executable) is None:
            continue
        entries[item.name] = {
            "command": item.command,
            "capabilities": list(item.capabilities),
        }
    return entries


def write_capable_command(name: str, command: str) -> str:
    item = next((item for item in catalog_definitions() if item.name == name), None)
    if item is not None and shlex.split(command) == shlex.split(item.legacy_command):
        return item.command
    return command


def balanced_provider_pools(names: set[str] | tuple[str, ...] | list[str]) -> dict[str, tuple[str, ...]]:
    """Build balanced pools from the project's provider registry."""

    definitions = {item.name: item for item in catalog_definitions()}

    def rank(name: str, stage: str) -> tuple[int, str]:
        item = definitions.get(name)
        if item is None:
            return (len(definitions), name)
        return (item.implement_rank if stage == "IMPLEMENT" else item.review_rank, name)

    unique = set(names)
    return {
        stage: tuple(sorted(unique, key=lambda name: rank(name, stage)))
        for stage in ("IMPLEMENT", "REVIEW")
    }
