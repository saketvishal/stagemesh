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
from .execution import (
    PROVIDER_TIMEOUT,
    ExecutionResult,
    capacity_evidence,
    classify_failure,
    communicate_bounded,
    popen_session_kwargs,
    provider_timeout_seconds,
)
from .persistence import Store
from .process_identity import popen_identity
from .remediation import remediation_context
from .workspace_guard import (
    EXTERNAL_WORKSPACE_MUTATION,
    WorkspaceLease,
    WorkspaceMutation,
    owned_workspace,
)
from .workspaces import (
    NO_IMPLEMENTATION_CHANGE,
    commit_implementation_candidate,
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
    timeout_seconds: float | None = None

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
        try:
            with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
                return self._execute_owned(store, task_id, claim_id, project, lease)
        except WorkspaceMutation:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)

    def _execute_owned(self, store: Store, task_id: str, claim_id: str | None, project: Path, lease: WorkspaceLease) -> ExecutionResult:
        run_path = lease.path
        baseline_sha = record_task_baseline(store, task_id, run_path)
        try:
            bound = bind_task_contract(store, project, task_id, baseline_sha)
        except ContractRejected as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"{exc.reason}: {exc}")
        task = store.get_task(task_id)
        task_prompt = _build_task_prompt(
            task_id, task, run_path, contract=bound.contract, remediation=remediation_context(store, task_id)
        )
        lease.check("before_agent")
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
                **popen_session_kwargs(),
            )
        except FileNotFoundError as exc:
            is_cap, reason = classify_failure(1, exc=exc)
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=is_cap, failure_reason=reason)
        identity = popen_identity(proc)
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            actor=self.name,
            pid=identity.pid,
            process_create_time=identity.create_time,
            boot_id=identity.boot_id,
            executable=identity.executable,
        )
        lease.bind_execution(execution_id)
        try:
            stdout, stderr, timed_out = communicate_bounded(proc, task_prompt, provider_timeout_seconds(self.timeout_seconds))
            lease.after_agent()
            if timed_out:
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=PROVIDER_TIMEOUT)
                return ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
            if proc.returncode != 0:
                is_cap, reason = classify_failure(proc.returncode, stdout, stderr)
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=reason)
                output, retry_after = capacity_evidence(stdout, stderr) if is_cap else (None, None)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=is_cap,
                    failure_reason=reason,
                    provider_output=output,
                    retry_after=retry_after,
                )
            sha = commit_implementation_candidate(
                store,
                task_id,
                run_path,
                baseline_sha,
                f"StageMesh implementation for {task_id}",
                attribution=attribution_for_worker("local-worker", self.name),
            )
            if sha is None:
                output, _ = capacity_evidence(stdout, stderr)
                store.finish_execution(execution_id, ExecutionStatus.FAILED, result=NO_IMPLEMENTATION_CHANGE)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    failure_reason=NO_IMPLEMENTATION_CHANGE,
                    provider_output=output or None,
                )
            store.add_candidate(task_id, sha, self.name, durable_handoff=True)
            lease.seal(sha)
            store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
            return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
        except Exception as exc:
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=f"{type(exc).__name__}: {exc}"[:300])
            raise

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return self.execute(store, task_id, claim_id, project)

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        if self.check_capacity() != CapacityKind.AVAILABLE:
            return _review_infrastructure_failure("provider_unavailable")
        with tempfile.TemporaryDirectory(prefix="stagemesh-review-") as temp_dir:
            review_path = Path(temp_dir) / "candidate"
            clone = subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", str(Path(project).resolve()), str(review_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            if clone.returncode != 0:
                return _review_infrastructure_failure("review workspace clone failed")
            checkout = subprocess.run(
                ["git", "checkout", "--quiet", "--detach", candidate_sha],
                cwd=review_path,
                text=True,
                capture_output=True,
                check=False,
            )
            if checkout.returncode != 0:
                return _review_infrastructure_failure("review candidate checkout failed")
            before_head = _git_output(review_path, "rev-parse", "HEAD")
            if before_head != candidate_sha:
                return _review_infrastructure_failure("review workspace did not checkout exact candidate")
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
                    **popen_session_kwargs(),
                )
            except FileNotFoundError:
                return _review_infrastructure_failure("provider_unavailable")
            stdout, stderr, timed_out = communicate_bounded(proc, prompt, provider_timeout_seconds(self.timeout_seconds))
            if timed_out:
                return _review_infrastructure_failure(PROVIDER_TIMEOUT)
            after_head = _git_output(review_path, "rev-parse", "HEAD")
            tracked_dirty = _tracked_content_changed(review_path)
            if after_head != before_head or tracked_dirty:
                return _review_failure("review execution mutated candidate workspace")
            if proc.returncode != 0:
                is_cap, reason = classify_failure(proc.returncode, stdout, stderr)
                if is_cap:
                    output, retry_after = capacity_evidence(stdout, stderr)
                    return _review_infrastructure_failure(reason, output, retry_after)
                return _review_infrastructure_failure(reason)
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


WRITE_CAPABLE_DEFAULT_COMMANDS = {
    "codex": "codex exec --sandbox workspace-write",
    "claude": (
        "claude -p --permission-mode acceptEdits --permission-prompts none "
        "--allowedTools 'Edit,Write,MultiEdit,Bash(git *),Bash(python *),Bash(pytest *),Bash(ruff *)'"
    ),
    "grok": (
        "grok --permission-mode acceptEdits "
        "--allow Edit --allow Write --allow MultiEdit --allow 'Bash(git *)' "
        "--allow 'Bash(python *)' --allow 'Bash(pytest *)' --allow 'Bash(ruff *)'"
    ),
}

LEGACY_DEFAULT_COMMANDS = {
    "codex": "codex exec",
    "claude": "claude -p",
    "grok": "grok",
}

PROVIDER_COMMAND_ENVS = {
    "codex": "STAGEMESH_CODEX_CMD",
    "claude": "STAGEMESH_CLAUDE_CMD",
    "grok": "STAGEMESH_GROK_CMD",
}


def approved_default_adapters() -> list[RuntimeCommandAdapter]:
    commands = {
        name: os.environ.get(PROVIDER_COMMAND_ENVS[name]) or command
        for name, command in WRITE_CAPABLE_DEFAULT_COMMANDS.items()
    }
    return adapters_from_commands(commands)


def _write_capable_command(name: str, command: str) -> str:
    if name in LEGACY_DEFAULT_COMMANDS and shlex.split(command) == shlex.split(LEGACY_DEFAULT_COMMANDS[name]):
        return WRITE_CAPABLE_DEFAULT_COMMANDS[name]
    return command


def adapters_from_config(config: StageMeshConfig) -> list[RuntimeCommandAdapter]:
    commands = {adapter.name: shlex.join(adapter.command) for adapter in approved_default_adapters()}
    commands.update({name: _write_capable_command(name, command) for name, command in config.provider_commands.items()})
    adapters = adapters_from_commands(commands)
    # Object-form entries declare which stages a provider serves; plain strings and built-ins serve both.
    stage_caps = {"IMPLEMENT": "code", "REVIEW": "review"}
    resolved = []
    for adapter in adapters:
        spec = config.provider_specs.get(adapter.name)
        if spec is not None:
            capabilities = frozenset({"validate", *(stage_caps[stage] for stage in spec.capabilities)})
            adapter = RuntimeCommandAdapter(adapter.name, adapter.command, capabilities, adapter.timeout_seconds)
        resolved.append(adapter)
    return resolved


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


def _build_task_prompt(
    task_id: str,
    task: object,
    project: Path | None = None,
    contract: object = None,
    remediation: dict[str, object] | None = None,
) -> str:
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
    remediation_text = _remediation_prompt(remediation) if remediation else ""
    return (
        f"StageMesh task: {title}\n\n"
        f"{contract_text}"
        f"{remediation_text}"
        "Please implement the changes described above. "
        "When you are done, commit all changes to git with a descriptive commit message "
        "so StageMesh can record the resulting commit SHA as the implementation candidate.\n"
    )


def _remediation_prompt(remediation: dict[str, object]) -> str:
    lines = [
        "",
        f"Previous candidate {remediation['candidate_sha']} failed {remediation['stage']}.",
        "",
        f"Findings recorded against candidate {remediation['candidate_sha']} (verbatim; fix exactly these):",
        "",
    ]
    for finding in remediation["findings"][:50]:  # type: ignore[index]
        lines.append(f"- [{finding['severity']}] {finding['message']}")
    diagnosis = remediation.get("diagnosis")
    if isinstance(diagnosis, dict):
        lines.extend(["", f"Diagnosis ({diagnosis.get('category')}): {str(diagnosis.get('summary'))[:500]}"])
        if diagnosis.get("provider_analysis"):
            lines.append(f"Independent analysis: {str(diagnosis['provider_analysis'])[:800]}")
    lines.extend(
        [
            "",
            (
                "Fix only these findings while preserving the original task objective and the change contract above. "
                "Do not make unrelated changes."
            ),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _git_output(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _tracked_content_changed(path: Path) -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet"], cwd=path, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=path, check=False)
    return unstaged.returncode != 0 or staged.returncode != 0


def _review_infrastructure_failure(reason: str, output: str | None = None, retry_after: str | None = None) -> str:
    import json

    payload: dict[str, str] = {"decision": "INFRASTRUCTURE_FAILURE", "reason": reason}
    if output:
        payload["provider_output"] = output[:500]
    if retry_after:
        payload["retry_after"] = retry_after[:120]
    return json.dumps(payload)


def _review_failure(message: str) -> str:
    import json

    return json.dumps({"decision": "FAIL", "findings": [{"severity": "error", "message": message}]})
