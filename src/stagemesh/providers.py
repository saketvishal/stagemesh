from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .capacity import CapacityKind, CapacityRegistry
from .config import StageMeshConfig
from .domain import ExecutionStatus
from .execution import ExecutionResult
from .persistence import Store


class ProviderValidationError(ValueError):
    pass


class ProviderAdapter(Protocol):
    name: str
    capabilities: frozenset[str]

    def check_capacity(self) -> str:
        ...

    def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        ...


@dataclass(frozen=True)
class RuntimeCommandAdapter:
    """SDK adapter for command-line coding agents such as Codex, Claude, or Grok."""

    name: str
    command: tuple[str, ...]
    capabilities: frozenset[str] = frozenset({"code", "review", "validate"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_name(self.name))
        object.__setattr__(self, "command", _validate_command(self.command))
        object.__setattr__(self, "capabilities", _validate_capabilities(self.capabilities))

    def check_capacity(self) -> str:
        executable = self.command[0] if self.command else ""
        return CapacityKind.AVAILABLE if executable and shutil.which(executable) else CapacityKind.UNKNOWN

    def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        if self.check_capacity() != CapacityKind.AVAILABLE:
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)
        result = subprocess.run(list(self.command), cwd=project, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            return ExecutionResult(ExecutionStatus.FAILED)
        return ExecutionResult(ExecutionStatus.SUCCEEDED)


def approved_default_adapters() -> list[RuntimeCommandAdapter]:
    commands: dict[str, str] = {}
    for name, env_name, fallback in [
        ("codex", "STAGEMESH_CODEX_CMD", "codex"),
        ("claude", "STAGEMESH_CLAUDE_CMD", "claude"),
        ("grok", "STAGEMESH_GROK_CMD", "grok"),
    ]:
        commands[name] = os.environ.get(env_name) or fallback
    return adapters_from_commands(commands)


def adapters_from_config(config: StageMeshConfig) -> list[RuntimeCommandAdapter]:
    commands = {adapter.name: shlex.join(adapter.command) for adapter in approved_default_adapters()}
    commands.update(config.provider_commands)
    return adapters_from_commands(commands)


def adapters_from_commands(commands: dict[str, str]) -> list[RuntimeCommandAdapter]:
    return [
        RuntimeCommandAdapter(name=name, command=tuple(shlex.split(command)))
        for name, command in sorted(commands.items())
    ]


def record_provider_capacity(registry: CapacityRegistry, adapters: list[ProviderAdapter]) -> None:
    for adapter in adapters:
        registry.record(adapter.name, adapter.check_capacity())


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProviderValidationError("provider adapter name must be a non-empty string")
    return name.strip()


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ProviderValidationError("provider adapter command must contain non-empty arguments")
    return tuple(part.strip() for part in command)


def _validate_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    if not capabilities:
        raise ProviderValidationError("provider adapter must declare at least one capability")
    normalized = frozenset(capability.strip() for capability in capabilities if isinstance(capability, str) and capability.strip())
    if len(normalized) != len(capabilities):
        raise ProviderValidationError("provider adapter capabilities must be non-empty strings")
    return normalized
