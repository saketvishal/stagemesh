from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .attribution import attribution_for_worker
from .contract_binding import ContractRejected, bind_task_contract
from .domain import ExecutionKind, ExecutionStatus
from .git import GitWorkspace
from .git_identity import provider_environment
from .persistence import Store
from .process_identity import classify_process, popen_identity, process_identity
from .queue_control import OperatorStopRequested, QueueStopControl, current_provider_stop
from .remediation import remediation_context
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION, WorkspaceMutation, owned_workspace
from .workspaces import (
    NO_IMPLEMENTATION_CHANGE,
    WorkspaceEnvironmentError,
    commit_implementation_candidate,
    record_task_baseline,
)


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    candidate_sha: str | None = None
    durable_handoff: bool = False
    capacity_failure: bool = False
    failure_reason: str | None = None
    provider_output: str | None = None
    retry_after: str | None = None
    already_satisfied: bool = False
    satisfaction: dict | None = None
    provider_attempts: list | None = None


_QUOTA = re.compile(
    r"weekly limit|usage limit|usage cap|plan limit|capacity exhausted|"
    r"rate[_ ]limit|quota exceeded|exceeded your current quota|too many requests|"
    r"\b429\b|insufficient_quota|overloaded_error|rate_limit_error|"
    r"credit balance|out of credits|insufficient credits|resource_exhausted|quota exhausted|usage limit reached|"
    r"\b402\b|payment required|balance exhausted|usage balance",
    re.IGNORECASE,
)
_SECRET = re.compile(r"(?i)(bearer\s+)\S+|(\bgh[pousr]_|sk-|xai-)[A-Za-z0-9_\-]+")
_RETRY = re.compile(r"(?i)(?:resets\s+[^\n]{1,80}|retry[- ]after[:\s]+[^\n]{1,40})")
TOOL_EXECUTION_FAILURE = "tool_execution_failure"
_TOOL_FAILURE = re.compile(
    r"npm ERR!\s+Missing script|"
    r"browser executable doesn't exist|"
    r"playwright install|"
    r"playwright.*command not found|"
    r"puppeteer.*command not found|"
    r"npx.*command not found|"
    r"npm.*command not found|"
    r"ModuleNotFoundError:\s+No module named|"
    r"Error:\s+Cannot find module|"
    r"No tests found",
    re.IGNORECASE,
)


def capacity_evidence(stdout: str, stderr: str) -> tuple[str, str | None]:
    """Redacted provider text and an observable reset hint. No tokens."""
    text = _SECRET.sub(lambda m: (m.group(1) or "") + "[redacted]", f"{stdout}\n{stderr}").strip()
    retry = _RETRY.search(text)
    # CLI startup banners and echoed prompts can hide the terminal error.
    excerpt = text if len(text) <= 500 else text[:180] + "\n...\n" + text[-315:]
    return excerpt, (retry.group(0).strip()[:120] if retry else None)


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

    if _TOOL_FAILURE.search(f"{stdout}\n{stderr}"):
        return False, TOOL_EXECUTION_FAILURE

    if any(m in combined for m in ["not found", "no such file or directory", "command not found", "cannot find"]):
        return True, "provider_unavailable"

    if any(m in combined for m in [
        "unauthorized", "authentication", "not logged in", "login required",
        "invalid api key", "auth failure", "missing credentials", "authenticate",
        "invalid_api_key", "authentication_error", "forbidden", "401", "403",
        "please log in", "please login", "please sign in", "log in again", "session has expired", "session expired",
        "not authenticated", "please run /login",
    ]):
        return True, "authentication_failure"

    if _QUOTA.search(combined):
        return True, "quota_rate_limit"

    if any(m in combined for m in [
        "503", "502", "service unavailable", "bad gateway", "connection refused",
        "connection reset", "overloaded", "server_error", "timed out", "timeout"
    ]):
        return True, "transient_provider_failure"

    return False, "implementation_failure"


DEFAULT_PROVIDER_TIMEOUT_SECONDS = 900.0
PROVIDER_TIMEOUT = "provider_timeout"


def provider_timeout_seconds(configured: float | None = None) -> float:
    """Wall-clock limit for one provider subprocess: explicit value, STAGEMESH_PROVIDER_TIMEOUT_SECONDS, or 15m."""
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


def kill_process_tree(proc: subprocess.Popen[str]) -> bool:
    confirmed = False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False, timeout=5
            )
            confirmed = result.returncode == 0
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            group = os.getpgid(proc.pid)
            if group == proc.pid:
                os.killpg(group, signal.SIGKILL)
                confirmed = True
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    return confirmed


_ACTIVE_PROVIDER_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()


def kill_active_provider_processes() -> int:
    """Kill every provider process (implementation or review) this process is currently waiting on; returns how many."""
    with _ACTIVE_LOCK:
        running = [proc for proc in _ACTIVE_PROVIDER_PROCESSES if proc.poll() is None]
    for proc in running:
        kill_process_tree(proc)
    return len(running)


def communicate_bounded(proc: subprocess.Popen[str], input_text: str, timeout: float) -> tuple[str, str, bool]:
    """Run proc to completion, killing its process tree if it exceeds `timeout`. Returns (stdout, stderr, timed_out).

    Every provider subprocess goes through here, so an interrupted run can find and kill all of them
    (see kill_active_provider_processes), not just the implementation ones that have a recorded pid.
    """
    with _ACTIVE_LOCK:
        _ACTIVE_PROVIDER_PROCESSES.add(proc)
    try:
        return _communicate_bounded(proc, input_text, timeout, current_provider_stop())
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROVIDER_PROCESSES.discard(proc)


STOP_CLEANUP_SECONDS = 5.0


def _communicate_bounded(
    proc: subprocess.Popen[str], input_text: str, timeout: float, control: QueueStopControl | None = None,
) -> tuple[str, str, bool]:
    identity = popen_identity(proc)
    done = threading.Event()
    outputs, failures = [], []

    def communicate():
        try:
            outputs.append(proc.communicate(input=input_text))
        except BaseException as exc:  # noqa: BLE001 -- transport failures cannot silently become a provider verdict
            failures.append(exc)
        finally:
            done.set()

    # Windows may block inside the stdin write before communicate's own timeout.
    # The caller enforces the deadline independently and retains ownership if I/O cannot be reaped.
    worker = threading.Thread(target=communicate, name=f"provider-io-{proc.pid}", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    cleanup_deadline = None
    timed_out, termination_attempted, termination_confirmed = False, False, False
    identity_unverified_reported = False
    monitor_failure, stop = None, None
    while True:
        if control is not None and monitor_failure is None:
            try:
                stop = control.poll()
            except Exception as exc:  # noqa: BLE001 -- stop observability failing must not accept provider output
                monitor_failure = exc
        finished = done.is_set()
        if finished:
            worker.join(timeout=1)
            if worker.is_alive() or failures or proc.poll() is None or (termination_attempted and not termination_confirmed):
                raise OperatorStopRequested(
                    "provider cleanup incomplete; execution ownership retained for recovery",
                    cleanup_complete=False, monitor_failed=monitor_failure is not None,
                )
            if monitor_failure is not None:
                raise OperatorStopRequested(
                    "provider stop monitoring failed: " + type(monitor_failure).__name__, monitor_failed=True,
                )
            if stop and stop["terminate_running"]:
                raise OperatorStopRequested(str(stop.get("reason") or "operator stop"))
            stdout, stderr = outputs[0]
            return stdout, stderr, timed_out
        timed_out = timed_out or time.monotonic() >= deadline
        if timed_out or monitor_failure is not None or (stop and stop["terminate_running"]):
            if cleanup_deadline is None:
                cleanup_deadline = time.monotonic() + STOP_CLEANUP_SECONDS
            if not termination_attempted and proc.poll() is None:
                observed = process_identity(proc.pid)
                if observed is None or not observed.is_known:
                    observed = popen_identity(proc)
                if classify_process(identity, observed) == "LIVE":
                    termination_attempted = True
                    try:
                        termination_confirmed = kill_process_tree(proc)
                    except Exception as exc:
                        raise OperatorStopRequested(
                            "provider cleanup failed: " + type(exc).__name__,
                            cleanup_complete=False, monitor_failed=monitor_failure is not None,
                        ) from exc
                    cleanup_deadline = time.monotonic() + STOP_CLEANUP_SECONDS
                    if stop and stop["terminate_running"] and monitor_failure is None:
                        try:
                            control.note("queue.provider_stop_termination", pid=proc.pid, request_rowid=stop["rowid"])
                        except Exception as exc:  # noqa: BLE001 -- failed observability cannot accept provider output
                            monitor_failure = exc
                elif control is not None and monitor_failure is None and not identity_unverified_reported:
                    try:
                        control.note(
                            "queue.provider_stop_identity_unverified", pid=proc.pid,
                            saved_create_time=getattr(identity, "create_time", None),
                            observed_create_time=getattr(observed, "create_time", None),
                        )
                        identity_unverified_reported = True
                    except Exception as exc:  # noqa: BLE001 -- auditing failure also stops the wait
                        monitor_failure = exc
            if time.monotonic() >= cleanup_deadline:
                raise OperatorStopRequested(
                    "provider cleanup incomplete; execution ownership retained for recovery",
                    cleanup_complete=False,
                )
        done.wait(0.05)


class Executor:
    name = "executor"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        raise NotImplementedError


class FakeExecutor(Executor):
    name = "fake"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        try:
            with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
                execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION, actor=self.name)
                lease.bind_execution(execution_id)
                run_path = lease.path
                record_task_baseline(store, task_id, run_path)
                workspace = GitWorkspace(run_path)
                workspace.init_if_needed()
                lease.check("before_agent")
                task_file = run_path / f"stagemesh-task-{task_id}.txt"
                task_file.write_text(f"implemented {task_id}\n", encoding="utf-8")
                lease.after_agent()
                sha = workspace.commit_all(
                    f"StageMesh implementation for {task_id}",
                    attribution=attribution_for_worker("local-worker", self.name),
                )
                store.add_candidate(task_id, sha, self.name, durable_handoff=True)
                lease.seal(sha)
                store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
                return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
        except WorkspaceMutation:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
        except WorkspaceEnvironmentError as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=str(exc))


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(self, command: list[str], name: str | None = None, timeout_seconds: float | None = None):
        self.command = command
        self.timeout_seconds = timeout_seconds
        if name is not None:
            self.name = name

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        import shutil

        from . import provider_authorization

        if provider_authorization.unattended():  # a bare command runner has no per-provider allowlist: never use it unattended
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="provider_not_authorized: SubprocessExecutor is not available unattended")
        executable = self.command[0] if self.command else ""
        if not executable or not shutil.which(executable):
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=True,
                failure_reason="provider_unavailable",
            )

        task = store.get_task(task_id)
        from .providers import _build_task_prompt

        try:
            with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
                return self._run_owned(store, task_id, claim_id, project, task, lease, _build_task_prompt)
        except WorkspaceMutation:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
        except WorkspaceEnvironmentError as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=str(exc))

    def _run_owned(self, store, task_id, claim_id, project, task, lease, build_prompt) -> ExecutionResult:  # type: ignore[no-untyped-def]
        run_path = lease.path
        baseline_sha = record_task_baseline(store, task_id, run_path)
        try:
            bound = bind_task_contract(store, project, task_id, baseline_sha)
        except ContractRejected as exc:
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=f"{exc.reason}: {exc}")
        remediation = remediation_context(store, task_id, project)
        task_prompt = build_prompt(task_id, task, run_path, contract=bound.contract, remediation=remediation)

        lease.check("before_agent")
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
                env=provider_environment(run_path),
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
            actor=self.name,
            pid=ident.pid,
            process_create_time=ident.create_time,
            boot_id=ident.boot_id,
            executable=ident.executable,
        )
        lease.bind_execution(execution_id)
        from .efficiency import CallRecorder

        call = CallRecorder(store, task_id, "remediation" if remediation else "implementation", self.name)
        stdout, stderr, timed_out = communicate_bounded(proc, task_prompt, provider_timeout_seconds(self.timeout_seconds))
        call.observe(stdout, stderr)
        code = proc.returncode
        lease.after_agent()

        if timed_out:
            call.finish("timeout", PROVIDER_TIMEOUT)
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=PROVIDER_TIMEOUT)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
        if code != 0:
            is_cap, reason = classify_failure(code, stdout, stderr)
            call.finish("failed", reason)
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=reason)
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
            call.finish("failed", NO_IMPLEMENTATION_CHANGE)
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=NO_IMPLEMENTATION_CHANGE)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=NO_IMPLEMENTATION_CHANGE)
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        lease.seal(sha)
        call.finish("succeeded", candidate=sha)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
