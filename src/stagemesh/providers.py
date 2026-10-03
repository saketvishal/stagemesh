from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .attribution import attribution_for_worker
from .capacity import CapacityKind, CapacityRegistry
from .config import StageMeshConfig
from .contract_binding import ContractRejected, bind_task_contract
from .domain import ExecutionKind, ExecutionStatus
from .execution import ExecutionResult, classify_failure
from .persistence import Store
from .process_identity import popen_identity
from .workspaces import (
    NO_IMPLEMENTATION_CHANGE,
    commit_implementation_candidate,
    prepare_task_workspace,
    record_task_baseline,
)


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
        run_path = prepare_task_workspace(project, task_id)
        baseline_sha = record_task_baseline(store, task_id, run_path)
        try:
            bound = bind_task_contract(store, project, task_id, baseline_sha)
        except ContractRejected as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"{exc.reason}: {exc}")
        task = store.get_task(task_id)
        task_prompt = _build_task_prompt(task_id, task, run_path, contract=bound.contract)
        try:
            proc = subprocess.Popen(
                list(self.command),
                cwd=run_path,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            is_cap, reason = classify_failure(1, exc=exc)
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=is_cap, failure_reason=reason)
        identity = popen_identity(proc)
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
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
        sha = commit_implementation_candidate(
            store,
            task_id,
            run_path,
            baseline_sha,
            f"StageMesh implementation for {task_id}",
            attribution=attribution_for_worker("local-worker", self.name),
        )
        if sha is None:
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=NO_IMPLEMENTATION_CHANGE)
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return self.execute(store, task_id, claim_id, project)

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        if self.check_capacity() != CapacityKind.AVAILABLE:
            return _review_failure("provider_unavailable")
        with tempfile.TemporaryDirectory(prefix="stagemesh-review-") as temp_dir:
            review_path = Path(temp_dir) / "candidate"
            clone = subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", str(Path(project).resolve()), str(review_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            if clone.returncode != 0:
                return _review_failure("review workspace clone failed")
            checkout = subprocess.run(
                ["git", "checkout", "--quiet", "--detach", candidate_sha],
                cwd=review_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if checkout.returncode != 0:
                return _review_failure("review candidate checkout failed")
            before_head = _git_output(review_path, "rev-parse", "HEAD")
            if before_head != candidate_sha:
                return _review_failure("review workspace did not checkout exact candidate")
            try:
                proc = subprocess.Popen(
                    list(self.command),
                    cwd=review_path,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                return _review_failure("provider_unavailable")
            stdout, stderr = proc.communicate(input=prompt)
            after_head = _git_output(review_path, "rev-parse", "HEAD")
            tracked_dirty = _tracked_content_changed(review_path)
            if after_head != before_head or tracked_dirty:
                return _review_failure("review execution mutated candidate workspace")
            if proc.returncode != 0:
                _, reason = classify_failure(proc.returncode, stdout, stderr)
                return _review_failure(reason)
            return stdout.strip()


@dataclass(frozen=True)
class RuntimeReviewAdapter:
    runtime: RuntimeCommandAdapter
    project: Path
    candidate_sha: str

    @property
    def name(self) -> str:
        return self.runtime.name

    def review(self, prompt: str) -> str:
        return self.runtime.review_candidate(prompt, self.project, self.candidate_sha)


def approved_default_adapters() -> list[RuntimeCommandAdapter]:
    commands: dict[str, str] = {}
    for name, env_name, fallback in [
        ("codex", "STAGEMESH_CODEX_CMD", "codex exec"),
        ("claude", "STAGEMESH_CLAUDE_CMD", "claude -p"),
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


def _build_task_prompt(task_id: str, task: object, project: Path | None = None, contract: object = None) -> str:
    """Build the prompt string sent via stdin to a provider CLI.

    The prompt gives the agent its task title and a reminder to commit any
    changes via git so StageMesh can capture the resulting SHA for evidence.
    """
    import sqlite3 as _sqlite3

    from .contracts import ContractError, contract_prompt, load_contract

    row_keys = set(task.keys()) if isinstance(task, _sqlite3.Row) else set()
    title = task["title"] if "title" in row_keys else str(task_id)
    contract_text = ""
    if contract is not None:
        contract_text = "\n\n" + contract_prompt(contract) + "\n"
    elif project is not None:
        try:
            contract_text = "\n\n" + contract_prompt(load_contract(project, task_id)) + "\n"
        except ContractError as exc:
            contract_text = f"\n\nChange contract is invalid and must be fixed before coding: {exc}\n"
    return (
        f"StageMesh task: {title}\n\n"
        f"{contract_text}"
        "Please implement the changes described above. "
        "When you are done, commit all changes to git with a descriptive commit message "
        "so StageMesh can record the resulting commit SHA as the implementation candidate.\n"
    )


def _git_output(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _tracked_content_changed(path: Path) -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=path, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path, check=False)
    return unstaged.returncode != 0 or staged.returncode != 0


def _review_failure(message: str) -> str:
    import json

    return json.dumps({"decision": "FAIL", "findings": [{"severity": "error", "message": message}]})
