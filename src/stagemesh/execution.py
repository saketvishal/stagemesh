from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from .attribution import attribution_for_worker
from .audit import record_audit
from .contract_binding import ContractRejected, bind_task_contract
from .diagnosis import TOOL_FAILURE_EVENT, tool_failure_evidence
from .domain import ExecutionKind, ExecutionStatus, ProcessIdentity
from .git import GitWorkspace
from .git_identity import provider_environment
from .persistence import Store
from .process_identity import classify_process, popen_identity, process_identity
from .queue_control import OperatorStopRequested, QueueStopControl, current_provider_stop
from .remediation import remediation_context, tool_recovery_plan
from .review_identity import command_snapshot, record_implementation_identity
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
_ACTIVE_PROVIDER_SCOPES: dict[subprocess.Popen[str], tuple[Path, str] | None] = {}
_PROVIDER_TERMINATIONS: dict[subprocess.Popen[str], Future[bool]] = {}
_ACTIVE_LOCK = threading.Lock()


def kill_active_provider_processes(
    *, database: Path | None = None, task_ids: set[str] | None = None,
    on_uncertain: Callable[[ProcessIdentity], None] | None = None,
) -> int:
    """Terminate registered providers, optionally scoped to one project and its active tasks.

    Return the number of confirmed tree kills. Publish pending proof before signalling and persist uncertainty before
    releasing the communicating owner. No-scope callers retain the process-wide cleanup interface.
    """
    selected_database = None if database is None else Path(database).resolve()
    pending: list[tuple[subprocess.Popen[str], Future[bool], bool]] = []
    with _ACTIVE_LOCK:
        for proc in _ACTIVE_PROVIDER_PROCESSES:
            scope = _ACTIVE_PROVIDER_SCOPES.get(proc)
            if selected_database is not None and (scope is None or scope[0] != selected_database):
                continue
            if task_ids is not None and (scope is None or scope[1] not in task_ids):
                continue
            existing = _PROVIDER_TERMINATIONS.get(proc)
            if existing is not None:
                if on_uncertain is not None and (not existing.done() or not existing.result()):
                    pending.append((proc, existing, False))
                continue
            if proc.poll() is None:
                proof: Future[bool] = Future()
                _PROVIDER_TERMINATIONS[proc] = proof
                pending.append((proc, proof, True))
    confirmed_count = 0
    ownership_failure = None
    for proc, proof, terminate in pending:
        confirmed = not terminate and proof.done() and proof.result()
        try:
            try:
                identity = popen_identity(proc)
            except Exception:  # noqa: BLE001 -- an identity failure must retain uncertainty, never permit signalling
                identity = ProcessIdentity(proc.pid, None, None)
            if terminate:
                try:
                    confirmed = identity.is_known and proc.poll() is None and bool(kill_process_tree(proc))
                except Exception:  # noqa: BLE001 -- one termination error must not skip the remaining owned processes
                    confirmed = False
            if not confirmed and on_uncertain is not None:
                try:
                    on_uncertain(identity)
                except Exception as exc:  # noqa: BLE001 -- publish failure for every target, then surface persistence failure
                    ownership_failure = exc
        finally:
            if terminate:
                proof.set_result(confirmed)
        confirmed_count += int(confirmed and terminate)
    if ownership_failure is not None:
        raise OperatorStopRequested("provider cleanup ownership could not be persisted", cleanup_complete=False) from ownership_failure
    return confirmed_count


def retain_provider_cleanup(store: Store, task_ids: set[str], identity: ProcessIdentity) -> set[str]:
    """Fence owned child executions and conservative parent-owned review rows before releasing a provider handle."""
    retained: set[str] = set()
    for execution in list(store.running_executions()):
        if str(execution["task_id"]) not in task_ids:
            continue
        saved = store.execution_process_identity(str(execution["id"]))
        child_matches = saved.pid == identity.pid and classify_process(saved, identity) != "DEAD"
        parent_review = execution["kind"] == ExecutionKind.REVIEW and saved.pid == os.getpid()
        if child_matches or parent_review:
            store.retain_execution_for_cleanup(str(execution["id"]), "provider process-tree cleanup unconfirmed")
            retained.add(str(execution["id"]))
    return retained


def communicate_bounded(proc: subprocess.Popen[str], input_text: str, timeout: float) -> tuple[str, str, bool]:
    """Run proc to completion, killing its process tree if it exceeds `timeout`. Returns (stdout, stderr, timed_out).

    Every provider subprocess goes through here, so an interrupted run can find and kill all of them
    (see kill_active_provider_processes), not just the implementation ones that have a recorded pid.
    """
    control = current_provider_stop()
    database = getattr(getattr(control, "store", None), "db_path", None)
    task_id = getattr(control, "task_id", None)
    scope = None if database is None or task_id is None else (Path(database).resolve(), str(task_id))
    with _ACTIVE_LOCK:
        _ACTIVE_PROVIDER_PROCESSES.add(proc)
        _ACTIVE_PROVIDER_SCOPES[proc] = scope
    try:
        return _communicate_bounded(proc, input_text, timeout, control)
    except OperatorStopRequested as exc:
        if not exc.cleanup_complete and isinstance(control, QueueStopControl) and control.task_id is not None:
            try:
                retain_provider_cleanup(control.store, {str(control.task_id)}, ProcessIdentity(proc.pid, None, None))
            except Exception as ownership_error:
                raise OperatorStopRequested(
                    "provider cleanup ownership could not be persisted", cleanup_complete=False,
                    monitor_failed=exc.monitor_failed,
                ) from ownership_error
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_PROVIDER_PROCESSES.discard(proc)
            _ACTIVE_PROVIDER_SCOPES.pop(proc, None)
            _PROVIDER_TERMINATIONS.pop(proc, None)


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
            with _ACTIVE_LOCK:
                external = _PROVIDER_TERMINATIONS.get(proc)
                root_running = proc.poll() is None
            external_confirmed = False
            if external is not None:
                try:
                    external_confirmed = external.result(timeout=STOP_CLEANUP_SECONDS)
                except TimeoutError:
                    pass  # pending proof is uncertainty, never completion
                root_running = proc.poll() is None
            cleanup_unconfirmed = (termination_attempted or external is not None) and not (termination_confirmed or external_confirmed)
            if worker.is_alive() or failures or root_running or cleanup_unconfirmed:
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
                    local_proof = None
                    with _ACTIVE_LOCK:
                        external_proof = _PROVIDER_TERMINATIONS.get(proc)
                        if external_proof is None and proc in _ACTIVE_PROVIDER_PROCESSES:
                            local_proof = Future()
                            _PROVIDER_TERMINATIONS[proc] = local_proof
                    if external_proof is None:
                        try:
                            termination_confirmed = kill_process_tree(proc)
                        except Exception as exc:
                            raise OperatorStopRequested(
                                "provider cleanup failed: " + type(exc).__name__,
                                cleanup_complete=False, monitor_failed=monitor_failure is not None,
                            ) from exc
                        finally:
                            if local_proof is not None:
                                local_proof.set_result(termination_confirmed)
                    # An existing attempt owns signalling. Its shared proof is consumed at completion; a second
                    # taskkill can race the first and turn confirmed cleanup into a spurious safety failure.
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


def record_tool_failure(
    store: Store, task_id: str, execution_id: str, command: list[str] | tuple[str, ...],
    cwd: Path, code: int | None, stdout: str, stderr: str, reason: str, timed_out: bool,
) -> None:
    """Record a confirmed command failure using its pre-prompt command identity.

    Callers must establish process completion and workspace ownership first. This
    records a recommendation only; it never retries, installs, or claims recovery ran.
    """
    evidence = tool_failure_evidence(
        list(command), cwd, code, stdout, stderr, timed_out=timed_out, reason=None if timed_out else reason,
    )
    if evidence is not None:
        if hasattr(store, "task_audit_events"):
            evidence["recovery"] = tool_recovery_plan(store, task_id, evidence)
        record_audit(store, TOOL_FAILURE_EVENT, {"task_id": task_id, "execution_id": execution_id, "evidence": evidence})


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

    def _record_tool_evidence(self, store, task_id, execution_id, cwd, code, stdout, stderr, reason, timed_out) -> None:  # type: ignore[no-untyped-def]
        record_tool_failure(store, task_id, execution_id, self.command, cwd, code, stdout, stderr, reason, timed_out)

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
        configured_command = tuple(self.command)
        command_identity = command_snapshot(self.name, configured_command)
        try:
            proc = subprocess.Popen(
                configured_command,
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
            self._record_tool_evidence(store, task_id, execution_id, run_path, code, stdout, stderr, PROVIDER_TIMEOUT, True)
            store.finish_execution(execution_id, ExecutionStatus.FAILED, result=PROVIDER_TIMEOUT)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
        if code != 0:
            is_cap, reason = classify_failure(code, stdout, stderr)
            call.finish("failed", reason)
            self._record_tool_evidence(store, task_id, execution_id, run_path, code, stdout, stderr, reason, False)
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
        record_implementation_identity(store, task_id, sha, execution_id, command_identity)
        lease.seal(sha)
        call.finish("succeeded", candidate=sha)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)
