"""Provider adapter catalog and side-effect-free local discovery.

The catalog describes adapters StageMesh knows how to drive. It is not the
project provider registry: ``stagemesh init`` snapshots locally available
entries into ``.stagemesh/config.json`` and runtime routing uses that project
registry. Discovery never launches, authenticates, or authorizes a provider.
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass

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


PROVIDER_CATALOG = (
    ProviderDefinition(
        "codex", "codex", "codex exec --sandbox workspace-write", "codex exec",
        "STAGEMESH_CODEX_CMD", 0, 3,
    ),
    ProviderDefinition(
        "claude", "claude",
        (
            "claude -p --permission-mode acceptEdits --permission-prompts none "
            f"--allowedTools 'Edit,Write,MultiEdit,{READ_ONLY_GIT_TOOLS},"
            "Bash(python *),Bash(pytest *),Bash(ruff *)'"
        ),
        "claude -p", "STAGEMESH_CLAUDE_CMD", 2, 0,
    ),
    ProviderDefinition(
        "grok", "grok",
        (
            "grok --permission-mode acceptEdits --always-approve "
            "--allow Edit --allow Write --allow MultiEdit "
            "--allow 'Bash(git status*)' --allow 'Bash(git diff*)' --allow 'Bash(git log*)' "
            "--allow 'Bash(git show*)' --allow 'Bash(git rev-parse*)' "
            "--allow 'Bash(python *)' --allow 'Bash(pytest *)' --allow 'Bash(ruff *)'"
        ),
        "grok", "STAGEMESH_GROK_CMD", 1, 1,
    ),
    ProviderDefinition(
        "agy", "agy", "agy --mode accept-edits", "agy", "STAGEMESH_AGY_CMD", 3, 2,
    ),
)


def catalog_names() -> tuple[str, ...]:
    return tuple(item.name for item in PROVIDER_CATALOG)


def catalog_commands() -> dict[str, str]:
    return {item.name: os.environ.get(item.command_env) or item.command for item in PROVIDER_CATALOG}


def discovered_provider_entries(
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, dict[str, object]]:
    """Return available catalog providers as config entries, without execution.

    An explicit command environment override also counts as local availability,
    but its value is never persisted because it may contain sensitive flags.
    """

    entries: dict[str, dict[str, object]] = {}
    for item in PROVIDER_CATALOG:
        override = os.environ.get(item.command_env)
        if override is None and which(item.executable) is None:
            continue
        entries[item.name] = {
            "command": item.command,
            "capabilities": ["IMPLEMENT", "REVIEW"],
        }
    return entries


def write_capable_command(name: str, command: str) -> str:
    item = next((item for item in PROVIDER_CATALOG if item.name == name), None)
    if item is not None and shlex.split(command) == shlex.split(item.legacy_command):
        return item.command
    return command


def balanced_provider_pools(names: set[str] | tuple[str, ...] | list[str]) -> dict[str, tuple[str, ...]]:
    """Build balanced pools from the project's provider registry."""

    definitions = {item.name: item for item in PROVIDER_CATALOG}

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
