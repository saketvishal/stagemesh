from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .attribution import attribution_for_worker
from .contract_binding import ContractRejected, bind_task_contract
from .domain import ExecutionKind, ExecutionStatus
from .git import GitWorkspace
from .persistence import Store
from .process_identity import popen_identity
from .remediation import remediation_context
from .workspaces import (
    NO_IMPLEMENTATION_CHANGE,
    commit_implementation_candidate,
    prepare_task_workspace,
    record_task_baseline,
)


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    candidate_sha: str | None = None
    durable_handoff: bool = False
    capacity_failure: bool = False
    failure_reason: str | None = None


def classify_failure(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    exc: Exception | None = None,
) -> tuple[bool, str]:
    """Classify execution failure into capacity/provider failure vs code defect.

    Returns (is_capacity_failure: bool, reason: str).
    """
    if exc is not None and isinstance(exc, FileNotFoundError):
        return True, "provider_unavailable"

    combined = f"{stdout}\n{stderr}".lower()

    if any(m in combined for m in ["not found", "no such file or directory", "command not found", "cannot find"]):
        return True, "provider_unavailable"

    if any(m in combined for m in [
        "unauthorized", "authentication", "not logged in", "login required",
        "invalid api key", "auth failure", "missing credentials", "authenticate",
        "invalid_api_key", "authentication_error", "forbidden", "401", "403"
    ]):
        return True, "authentication_failure"

    if any(m in combined for m in [
        "rate limit", "rate_limit", "quota", "too many requests", "429",
        "exceeded your current quota", "capacity exhausted", "overloaded_error",
        "rate_limit_error", "insufficient_quota"
    ]):
        return True, "quota_rate_limit"

    if any(m in combined for m in [
        "503", "502", "service unavailable", "bad gateway", "connection refused",
        "connection reset", "overloaded", "server_error", "timed out", "timeout"
    ]):
        return True, "transient_provider_failure"

    return False, "implementation_failure"


DEFAULT_PROVIDER_TIMEOUT_SECONDS = 3600.0
PROVIDER_TIMEOUT = "provider_timeout"


def provider_timeout_seconds(configured: float | None = None) -> float:
    """Wall-clock limit for one provider subprocess: explicit value, STAGEMESH_PROVIDER_TIMEOUT_SECONDS, or 1h."""
    if configured is not None and configured > 0:
        return float(configured)
    try:
        value = float(os.environ.get("STAGEMESH_PROVIDER_TIMEOUT_SECONDS", ""))
    except ValueError:
        return DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_PROVIDER_TIMEOUT_SECONDS


def popen_session_kwargs() -> dict[str, object]:
    """Start providers in their own process group so the whole tree can be killed on timeout."""
    return {} if sys.platform == "win32" else {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def communicate_bounded(proc: subprocess.Popen[str], input_text: str, timeout: float) -> tuple[str, str, bool]:
    """Run proc to completion, killing its process tree if it exceeds `timeout`. Returns (stdout, stderr, timed_out)."""
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return stdout, stderr, False
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return stdout, stderr, True


class Executor:
    name = "executor"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        raise NotImplementedError


class FakeExecutor(Executor):
    name = "fake"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        run_path = prepare_task_workspace(project, task_id)
        record_task_baseline(store, task_id, run_path)
        workspace = GitWorkspace(run_path)
        workspace.init_if_needed()
        task_file = run_path / f"stagemesh-task-{task_id}.txt"
        task_file.write_text(f"implemented {task_id}\n", encoding="utf-8")
        sha = workspace.commit_all(
            f"StageMesh implementation for {task_id}",
            attribution=attribution_for_worker("local-worker", self.name),
        )
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(self, command: list[str], name: str | None = None, timeout_seconds: float | None = None):
        self.command = command
        self.timeout_seconds = timeout_seconds
        if name is not None:
            self.name = name

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        import shutil
        executable = self.command[0] if self.command else ""
        if not executable or not shutil.which(executable):
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=True,
                failure_reason="provider_unavailable",
            )

        task = store.get_task(task_id)
        from .providers import _build_task_prompt
        run_path = prepare_task_workspace(project, task_id)
        baseline_sha = record_task_baseline(store, task_id, run_path)
        try:
            bound = bind_task_contract(store, project, task_id, baseline_sha)
        except ContractRejected as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"{exc.reason}: {exc}")
        task_prompt = _build_task_prompt(
            task_id, task, run_path, contract=bound.contract, remediation=remediation_context(store, task_id)
        )

        try:
            proc = subprocess.Popen(
                self.command,
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
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=is_cap,
                failure_reason=reason,
            )

        ident = popen_identity(proc)
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            pid=ident.pid,
            process_create_time=ident.create_time,
            boot_id=ident.boot_id,
            executable=ident.executable,
        )
        stdout, stderr, timed_out = communicate_bounded(proc, task_prompt, provider_timeout_seconds(self.timeout_seconds))
        code = proc.returncode

        if timed_out:
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
        if code != 0:
            is_cap, reason = classify_failure(code, stdout, stderr)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=is_cap,
                failure_reason=reason,
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
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=NO_IMPLEMENTATION_CHANGE)
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
