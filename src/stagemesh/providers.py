from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .attribution import attribution_for_worker
from .capacity import CapacityKind, CapacityRegistry
from .config import StageMeshConfig
from .domain import ExecutionKind, ExecutionStatus
from .execution import ExecutionResult, classify_failure, _capture_baseline_sha
from .git import GitWorkspace
from .persistence import Store
from .process_identity import popen_identity


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
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True, failure_reason="provider_unavailable")
        task = store.get_task(task_id)
        task_prompt = _build_task_prompt(task_id, task)
        from .governance import (
            capture_agent_result_tree,
            canonicalize_and_record_candidate,
            prepare_task_worktree,
            recover_pending_canonical_candidate,
            resolve_or_capture_baseline,
        )
        baseline = resolve_or_capture_baseline(store, project, task_id)
        task_project = prepare_task_worktree(project, task_id, base_sha=baseline.commit_sha)
        workspace = GitWorkspace(task_project)
        workspace.init_if_needed()

        recovery_result = recover_pending_canonical_candidate(
            store=store,
            workspace=workspace,
            task_id=task_id,
            baseline=baseline,
            provider=self.name,
            claim_id=claim_id,
        )
        if recovery_result is not None:
            return recovery_result

        executable = self.command[0] if self.command else None
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            executable=executable,
        )
        store.record_baseline(
            task_id=task_id,
            execution_id=execution_id,
            commit_sha=baseline.commit_sha,
            tree_sha=baseline.tree_sha,
            branch=baseline.branch,
            repo_path=baseline.repo_path,
        )

        try:
            proc = subprocess.Popen(
                list(self.command),
                cwd=task_project,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            is_cap, reason = classify_failure(1, exc=exc)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=is_cap, failure_reason=reason)
        identity = popen_identity(proc)
        store.attach_execution_process(
            execution_id=execution_id,
            pid=identity.pid,
            process_create_time=identity.create_time,
            boot_id=identity.boot_id,
            executable=identity.executable,
        )
        stdout, stderr = proc.communicate(input=task_prompt)
        if proc.returncode != 0:
            is_cap, reason = classify_failure(proc.returncode, stdout, stderr)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=is_cap, failure_reason=reason)
        try:
            agent_tree = capture_agent_result_tree(workspace)
            sha = canonicalize_and_record_candidate(
                store=store,
                workspace=workspace,
                task_id=task_id,
                execution_id=execution_id,
                claim_id=claim_id,
                provider=self.name,
                baseline=baseline,
                agent_result_tree=agent_tree,
                durable_handoff=True,
            )
            store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
            return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
        except Exception as exc:
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=False,
                failure_reason=f"governance_canonicalization_failed: {exc}",
            )

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return self.execute(store, task_id, claim_id, project)


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
    if not isinstance(commands, dict):
        raise ProviderValidationError("provider commands must be an object")
    adapters: list[RuntimeCommandAdapter] = []
    for name, command in sorted(commands.items()):
        if not isinstance(command, str) or not command.strip():
            raise ProviderValidationError(f"provider command for {name} must be a non-empty string")
        if len(command) > 2000:
            raise ProviderValidationError(f"provider command for {name} must be 2000 characters or fewer")
        try:
            parts = tuple(shlex.split(command))
        except ValueError as exc:
            raise ProviderValidationError(f"provider command for {name} must be valid shell-style syntax") from exc
        adapters.append(RuntimeCommandAdapter(name=name, command=parts))
    return adapters


def record_provider_capacity(registry: CapacityRegistry, adapters: list[ProviderAdapter]) -> None:
    for adapter in adapters:
        registry.record(adapter.name, adapter.check_capacity())


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProviderValidationError("provider adapter name must be a non-empty string")
    normalized = name.strip()
    if len(normalized) > 100:
        raise ProviderValidationError("provider adapter name must be 100 characters or fewer")
    if any(char.isspace() for char in normalized):
        raise ProviderValidationError("provider adapter name must not contain whitespace")
    return normalized


def _validate_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command or any(not isinstance(part, str) or not part.strip() for part in command):
        raise ProviderValidationError("provider adapter command must contain non-empty arguments")
    normalized = tuple(part.strip() for part in command)
    if len(normalized) > 100:
        raise ProviderValidationError("provider adapter command must contain 100 arguments or fewer")
    if any(len(part) > 1000 for part in normalized):
        raise ProviderValidationError("provider adapter command arguments must be 1000 characters or fewer")
    return normalized


def _validate_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    if not capabilities:
        raise ProviderValidationError("provider adapter must declare at least one capability")
    normalized = frozenset(capability.strip() for capability in capabilities if isinstance(capability, str) and capability.strip())
    if len(normalized) != len(capabilities):
        raise ProviderValidationError("provider adapter capabilities must be non-empty strings")
    return normalized


def _build_task_prompt(task_id: str, task: object) -> str:
    """Build the prompt string sent via stdin to a provider CLI.

    The prompt gives the agent its task title and a reminder to commit any
    changes via git so StageMesh can capture the resulting SHA for evidence.
    """
    import sqlite3 as _sqlite3

    title = task["title"] if isinstance(task, _sqlite3.Row) and "title" in task.keys() else str(task_id)
    return (
        f"StageMesh task: {title}\n\n"
        "Please implement the changes described above. "
        "When you are done, commit all changes to git with a descriptive commit message "
        "so StageMesh can record the resulting commit SHA as the implementation candidate.\n"
    )
