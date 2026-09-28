from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .capacity import CapacityKind, CapacityRegistry
from .domain import ExecutionStatus
from .execution import ExecutionResult
from .persistence import Store


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
    adapters: list[RuntimeCommandAdapter] = []
    for name, env_name, fallback in [
        ("codex", "STAGEMESH_CODEX_CMD", "codex"),
        ("claude", "STAGEMESH_CLAUDE_CMD", "claude"),
        ("grok", "STAGEMESH_GROK_CMD", "grok"),
    ]:
        command = tuple((os.environ.get(env_name) or fallback).split())
        adapters.append(RuntimeCommandAdapter(name=name, command=command))
    return adapters


def record_provider_capacity(registry: CapacityRegistry, adapters: list[ProviderAdapter]) -> None:
    for adapter in adapters:
        registry.record(adapter.name, adapter.check_capacity())
