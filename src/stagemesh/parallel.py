"""Run several independent tasks at once, each in its own worktree, with one-at-a-time integration.

The dispatcher (the calling thread) owns task selection and contract checks; every started task gets a thread with its own
database connection, coordinator and worktree, and drives itself to DONE or a safe stop exactly like a single `continue`.
Failures stay inside the task that had them. Ctrl+C stops every task, kills provider processes this run started, releases
their claims and discards uncommitted partial edits, so a restart resumes from committed state with nothing orphaned.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import record_audit
from .blocked_recovery import auto_retry_blocked_provider_failures, exhaustion_message
from .concurrency import IntegrationLockTimeout, ProviderLimiter, contract_conflict
from .config import RuntimeConfig, TaskSelectionConfig, load_config
from .contracts import ChangeContract, ContractError, parse_contract, task_contract_path
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .domain import ProcessIdentity, Stage
from .execution import kill_active_provider_processes, retain_provider_cleanup
from .execution_ownership import validation_tree_unresolved
from .git import GitWorkspace
from .persistence import Store
from .process_identity import classify_process, process_identity
from .provider_pool import ProviderLog
from .queue_control import QUEUE_CONTROL_EVENT, QueueStopControl, latest_queue_control
from .recovery import (
    auto_rebaseline_blocked_stale_baselines,
    auto_rebind_blocked_missing_validation_gates,
    auto_reintegrate_blocked_runtime_failures,
    auto_revalidate_blocked_validation_gates,
)
from .run_ready import (
    RunReadyRefusal,
    RunSummary,
    _ensure_contract,
    _recover_dead,
    _stage_status,
    drive_task,
    format_step_update,
)
from .task_selection import rank_batch_candidates
from .workspaces import remove_task_workspace, sweep_task_worktrees, task_workspace

WORKER_PREFIX = "parallel-"
_WORKER_PID = re.compile(r"^parallel-(\d+)-")


class SetupRefused(Exception):
    """A task could not even start (providers unavailable, no integration ref); only that task is refused."""


MakeCoordinator = Callable[[TargetSelection, Store, str], Coordinator]


def worker_id_for(task_id: str) -> str:
    """Claim owner for a task run by this process; the pid lets a restart prove the owner is dead."""
    return f"{WORKER_PREFIX}{os.getpid()}-{task_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _active_execution_rows(store: Store) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row["id"]),
            "task_id": str(row["task_id"]),
            "claim_id": row["claim_id"],
            "kind": str(row["kind"]),
            "pid": row["pid"],
            "candidate_sha": row["candidate_sha"],
        }
        for row in store.running_executions()
    ]


def queue_control_state(store: Store) -> dict[str, Any]:
    latest = latest_queue_control(store)
    state = str(latest.get("state")) if latest else "running"
    if state == "resumed":
        state = "running"
    return {
        "state": state,
        "paused": state == "paused",
        "stopping": state == "stopping",
        "latest": latest,
        "active_executions": _active_execution_rows(store),
    }


def request_queue_control(
    store: Store,
    state: str,
    reason: str,
    *,
    terminate_running: bool = False,
) -> dict[str, Any]:
    if state not in {"paused", "resumed", "stopping"}:
        raise ValueError(f"unsupported queue control state: {state}")
    event = {
        "at": _now(),
        "state": state,
        "reason": reason,
        "terminate_running": terminate_running,
        "source": "operator",
    }
    record_audit(store, QUEUE_CONTROL_EVENT, event)
    return queue_control_state(store)


@dataclass
class TaskLifecycle:
    task_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: RunSummary | None = None
    provider_events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        summary = self.summary
        base: dict[str, Any] = {"task_id": self.task_id, "lifecycle": self.events}
        if summary is None:
            return {**base, "stop_reason": "UNSET", "succeeded": False}
        return {
            **base,
            "started": summary.started,
            "stop_reason": summary.stop_reason,
            "succeeded": summary.succeeded,
            "message": summary.message,
            "steps_run": len(summary.steps),
            "steps": summary.steps,
            "final": summary.final,
            "selection": summary.selection,
            "auto_plan": summary.auto_plan,
            "recovered": summary.recovered,
            "provider_events": self.provider_events,
            **({"detail": summary.detail} if summary.detail else {}),
        }


@dataclass
class ParallelSummary:
    concurrency: int
    stop_reason: str = "UNSET"
    message: str = ""
    tasks: list[TaskLifecycle] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    swept: list[dict[str, str]] = field(default_factory=list)
    providers: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False
    control: dict[str, Any] = field(
        default_factory=lambda: {"state": "running", "events": [], "active_executions": []}
    )
    focus: dict[str, str] | None = None
    skipped_out_of_focus: list[dict[str, str]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "DONE"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "mode": "parallel",
            "concurrency": self.concurrency,
            "stop_reason": self.stop_reason,
            "succeeded": self.succeeded,
            "interrupted": self.interrupted,
            "message": self.message,
            "tasks": [task.to_dict() for task in self.tasks],
            "task_outcomes": {task.task_id: (task.summary.stop_reason if task.summary else "UNSET") for task in self.tasks},
            "deferred": self.deferred,
            "skipped": self.skipped,
            "recovered": self.recovered,
            "worktrees_swept": self.swept,
            "providers": self.providers,
            "control": self.control,
        }
        if self.focus is not None:
            data["focus"] = self.focus
            data["skipped_out_of_focus"] = self.skipped_out_of_focus
        return data


def load_task_contract(store: Store, project: Path, task_id: str) -> ChangeContract:
    """The contract that bounds this task's changes: the frozen one, else the contract file, else the unbounded default."""
    import json

    record = store.task_contract(task_id)
    try:
        if record is not None:
            return parse_contract(json.loads(record["canonical_json"]))
        path = task_contract_path(project, task_id)
        if path is not None:
            return parse_contract(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ContractError, KeyError, IndexError):
        pass
    return ChangeContract(objective=task_id)


def recover_orphaned_claims(
    store: Store, project: Path | None = None, is_dead: Callable[[int], bool] | None = None
) -> list[dict[str, Any]]:
    """Release claims held by a parallel run whose process is gone (crash, kill, power loss) so those tasks resume.

    Only claims whose owner pid is provably dead are touched; a live run's claims are never released.
    """
    dead = is_dead or (lambda pid: process_identity(pid) is None)
    released: list[dict[str, Any]] = []
    rows = store.conn.execute("SELECT * FROM claims WHERE active=1 AND worker_id LIKE ?", (WORKER_PREFIX + "%",)).fetchall()
    for claim in rows:
        match = _WORKER_PID.match(str(claim["worker_id"]))
        if match is None or not dead(int(match.group(1))):
            continue
        task_id = str(claim["task_id"])
        if any(e["task_id"] == task_id and validation_tree_unresolved(e) for e in store.running_executions()):
            record_audit(store, "recovery.parallel_claim_protected_validation", {
                "task_id": task_id, "claim_id": str(claim["id"]), "reason": "validation tree cleanup unconfirmed",
            })
            continue  # include claimless validators: a dead worker does not free their task
        running = [e for e in store.running_executions() if e["claim_id"] == claim["id"]]
        unknown = [
            e
            for e in running
            if e["pid"] is not None and classify_process(store.execution_process_identity(str(e["id"])), process_identity(e["pid"])) == "UNKNOWN"
        ]
        if unknown:
            # A dead runner proves nothing about a provider whose recorded process cannot be identified: the claim stays until
            # `recover-stale --release-unknown`, however many runs start meanwhile. The protection must outlive the runner process.
            for execution in unknown:
                record_audit(
                    store,
                    "recovery.parallel_claim_protected_unknown",
                    {
                        "task_id": task_id,
                        "claim_id": str(claim["id"]),
                        "execution_id": str(execution["id"]),
                        "pid": int(execution["pid"]),
                        "worker_id": str(claim["worker_id"]),
                        "reason": "unknown_process_identity",
                        "action": "RECOVERY_DEFERRED",
                    },
                )
            continue
        for execution in running:
            store.mark_orphan_running_execution_failed(str(execution["id"]), "PARALLEL_WORKER_DEAD")
        store.release_claim(str(claim["id"]))
        record_audit(
            store, "recovery.parallel_claim_released", {"task_id": task_id, "claim_id": str(claim["id"]), "worker_id": str(claim["worker_id"])}
        )
        if project is not None:
            discard_partial_edits(project, task_id)
        released.append({"task_id": task_id, "claim_id": str(claim["id"]), "worker_id": str(claim["worker_id"]), "action": "RELEASED"})
    return released


def discard_partial_edits(project: Path, task_id: str) -> bool:
    """Throw away uncommitted edits in the task worktree (a killed provider's half-finished work); commits are kept."""
    path = task_workspace(project, task_id)
    if not (path / ".git").exists():
        return False
    git = GitWorkspace(path)
    git.run("reset", "--hard", "HEAD", check=False)
    git.run("clean", "-fdq", check=False)
    return True


def _kill_pid(pid: int, expected: ProcessIdentity) -> bool:
    """Recheck the saved owner, signal its tree, and return positive tree proof only."""
    if pid == os.getpid() or expected.pid != pid or classify_process(expected, process_identity(pid)) != "LIVE":
        return False
    confirmed = False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False, timeout=5,
            )
            confirmed = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    else:
        try:
            group = os.getpgid(pid)
            if group == pid:
                os.killpg(group, signal.SIGKILL)
                confirmed = True
            else:
                os.kill(pid, signal.SIGKILL)  # root fallback cannot confirm shared-group descendants
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if not confirmed:
        return False
    deadline = time.monotonic() + 5
    while True:
        state = classify_process(expected, process_identity(pid))
        if state != "LIVE":
            return state == "DEAD"
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class ParallelRunner:
    def __init__(
        self,
        store: Store,
        project: Path,
        make_coordinator: MakeCoordinator,
        *,
        concurrency: int,
        policy: TaskSelectionConfig | None = None,
        auto_plan: bool = True,
        max_steps: int = 50,
        limiter: ProviderLimiter | None = None,
        emit: Callable[[str, str], None] | None = None,
        interrupt_grace_seconds: float = 20.0,
        poll_seconds: float = 0.2,
        wait_for_providers: bool = False,
        sweep_worktrees: bool = True,
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.store = store
        self._stop_control = QueueStopControl(store)
        self.project = Path(project)
        self.make_coordinator = make_coordinator
        self.concurrency = concurrency
        self.policy = policy or TaskSelectionConfig()
        self.auto_plan = auto_plan
        self.max_steps = max_steps
        self.limiter = limiter
        self.emit = emit
        self.interrupt_grace_seconds = interrupt_grace_seconds
        self.poll_seconds = poll_seconds
        self.wait_for_providers = wait_for_providers
        self.sweep_worktrees = sweep_worktrees
        self._provider_waits: dict[str, float] = {}
        self._exhaustion_said: set[str] = set()
        self.stop = threading.Event()
        self._admission_paused = threading.Event()
        self._admission_stopped = threading.Event()
        self._terminate_requested = threading.Event()
        self._stop_request_snapshot: list[dict[str, Any]] | None = None
        self._stop_request_task_ids: set[str] = set()
        self._protected_claims: set[str] = set()  # claims of executions with unknown process identity; only an operator releases them
        self._lock = threading.Lock()
        self._lifecycles: dict[str, TaskLifecycle] = {}
        self._provider_logs: dict[str, ProviderLog] = {}
        self._contracts: dict[str, ChangeContract] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._done: queue.Queue[str] = queue.Queue()
        self._pending_selection: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.summary = ParallelSummary(concurrency)
        self.global_failure: str | None = None
        self._deferred_this_round = False
        self._planned: dict[str, dict[str, Any]] = {}
        latest_control = latest_queue_control(self.store) or {}
        self._last_control_rowid = int(latest_control.get("rowid") or 0)
        if latest_control.get("state") == "paused":
            self._admission_paused.set()
        config = load_config(self.project)
        assert config.runtime is not None
        self.runtime: RuntimeConfig = config.runtime

    # -- shared, thread-safe helpers -------------------------------------------------------------------------------------

    def provider_log(self, task_id: str) -> ProviderLog:
        with self._lock:
            return self._provider_logs.setdefault(task_id, ProviderLog(echo=False))

    def note(self, task_id: str, event: str, **detail: Any) -> None:
        """Append a lifecycle event to one task's own timeline."""
        with self._lock:
            lifecycle = self._lifecycles.get(task_id)
            if lifecycle is not None:
                lifecycle.events.append({"seq": len(lifecycle.events) + 1, "at": _now(), "event": event, **detail})

    def _say(self, task_id: str, text: str) -> None:
        if self.emit is not None:
            self.emit(task_id, text)

    def pause_admission(self, reason: str = "operator_pause") -> None:
        """Stop admitting new tasks until resume_admission() is called; running tasks are left alone."""
        self._admission_paused.set()
        self._record_control("paused", reason, terminate_running=False)

    def resume_admission(self, reason: str = "operator_resume") -> None:
        """Resume task admission after pause_admission()."""
        self._admission_paused.clear()
        self._record_control("resumed", reason, terminate_running=False)

    def stop_admission(self, reason: str = "operator_stop", *, terminate_running: bool = False) -> None:
        """Stop admitting new tasks; optionally terminate active provider work through the audited cleanup path."""
        self._admission_stopped.set()
        if terminate_running:
            self._capture_stop_request()
            self._terminate_requested.set()
            self.stop.set()
        self._record_control("stopping", reason, terminate_running=terminate_running)

    @property
    def admission_paused(self) -> bool:
        return self._admission_paused.is_set()

    @property
    def admission_stopped(self) -> bool:
        return self._admission_stopped.is_set()

    def _record_control(self, state: str, reason: str, *, terminate_running: bool) -> None:
        event = {"at": _now(), "state": state, "reason": reason, "terminate_running": terminate_running, "source": "runner"}
        with self._lock:
            self.summary.control["state"] = state
            self.summary.control.setdefault("events", []).append(event)
        self._say("run", f"queue admission {state}: {reason}")
        audit = Store(self.store.db_path)
        try:
            record_audit(audit, QUEUE_CONTROL_EVENT, event)
            latest = latest_queue_control(audit)
            if latest is not None:
                self._last_control_rowid = int(latest["rowid"])
        finally:
            audit.close()

    def _record_stopped(self, reason: str, *, terminate_running: bool) -> None:
        """Close a stop with exactly one `stopped` control event, whichever path (graceful, terminate, Ctrl+C) finished it."""
        self._stop_control.complete_if_idle()
        latest = latest_queue_control(self.store)
        if latest is not None:
            self._refresh_control_state(queue_control_state(self.store)["state"])

    def _active_execution_snapshot(self) -> list[dict[str, Any]]:
        snapshot = Store(self.store.db_path)
        try:
            return _active_execution_rows(snapshot)
        finally:
            snapshot.close()

    def _refresh_control_state(self, state: str | None = None) -> None:
        with self._lock:
            if state is not None:
                self.summary.control["state"] = state
            self.summary.control["paused"] = self._admission_paused.is_set()
            self.summary.control["stopping"] = self._admission_stopped.is_set()
            self.summary.control["terminate_requested"] = self._terminate_requested.is_set()
            self.summary.control["active_executions"] = self._active_execution_snapshot()

    def _apply_control_requests(self) -> None:
        rows = self.store.conn.execute(
            """
            SELECT payload, rowid FROM audit_events
            WHERE event_type=? AND rowid>?
            ORDER BY rowid
            """,
            (QUEUE_CONTROL_EVENT, self._last_control_rowid),
        ).fetchall()
        for row in rows:
            self._last_control_rowid = int(row["rowid"])
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            state = str(payload.get("state"))
            reason = str(payload.get("reason") or "operator request")
            terminate = bool(payload.get("terminate_running"))
            if state == "paused":
                self._admission_paused.set()
                self._refresh_control_state("paused")
                self._say("run", f"queue admission paused: {reason}")
            elif state == "resumed":
                # Resume ends a pause. It does not cancel a stop that was already requested.
                self._admission_paused.clear()
                if not self._admission_stopped.is_set():
                    self._refresh_control_state("running")
                    self._say("run", f"queue admission resumed: {reason}")
            elif state == "stopping":
                self._admission_stopped.set()
                if terminate:
                    self._capture_stop_request()
                    self._terminate_requested.set()
                    self.stop.set()
                self._refresh_control_state("stopping")
                self._say("run", f"queue admission stopping: {reason}")

    # -- dispatcher --------------------------------------------------------------------------------------------------------

    def run(self) -> ParallelSummary:
        summary = self.summary
        try:
            self._startup(summary)
            self._dispatch_loop(summary)
        except KeyboardInterrupt:
            self._interrupt(summary)
        finally:
            self._finalize(summary)
        return summary

    def _startup(self, summary: ParallelSummary) -> None:
        latest = latest_queue_control(self.store)
        if latest is not None and latest.get("state") == "stopping":
            # The stop was requested before this runner polled. Honor it; do not rewrite it to running.
            self._admission_stopped.set()
            if bool(latest.get("terminate_running")):
                self._terminate_requested.set()
                self.stop.set()
        elif not self._admission_paused.is_set() and latest is not None and latest.get("state") == "stopped":
            self._record_control("running", "queue run started", terminate_running=False)
        summary.recovered.extend(recover_orphaned_claims(self.store, self.project))
        for row in self.store.tasks():  # provably dead provider processes only; live/unknown are never touched
            summary.recovered.extend(_recover_dead(self.store, str(row["id"]), own_task=False))
        if self.sweep_worktrees:
            summary.swept = sweep_task_worktrees(self.project, self.store)
        for item in summary.recovered:
            self._say("run", f"recovered {item.get('action', 'RELEASED').lower()} claim/execution for task {item['task_id']}")
        for item in summary.swept:
            self._say("run", f"removed orphaned worktree {item['worktree']} ({item['reason']})")

    def _dispatch_loop(self, summary: ParallelSummary) -> None:
        attempted: set[str] = set()
        while not self.stop.is_set():
            self._apply_control_requests()
            running = [task_id for task_id, thread in self._threads.items() if thread.is_alive()]
            if self._admission_stopped.is_set():
                if not running:
                    break
                self._refresh_control_state("stopping")
                try:
                    self._done.get(timeout=self.poll_seconds)
                except queue.Empty:
                    pass
                continue
            if self._admission_paused.is_set():
                if not running:
                    self._refresh_control_state("paused")
                    break
                self._refresh_control_state("paused")
                try:
                    self._done.get(timeout=self.poll_seconds)
                except queue.Empty:
                    pass
                continue
            free = self.concurrency - len(running)
            self._resume_provider_waits(attempted)
            self._deferred_this_round = False
            if free > 0:
                for task_id in self._select_batch(summary, free, running, attempted):
                    attempted.add(task_id)
                    self._start(task_id)
            running = [task_id for task_id, thread in self._threads.items() if thread.is_alive()]
            if not running:
                if self._provider_waits:
                    deadline = min(self._provider_waits.values())
                    self._refresh_control_state()
                    self.stop.wait(min(30.0, max(0.05, self.poll_seconds), max(0.05, deadline - time.time())))
                    continue
                if self._deferred_this_round:
                    continue  # a task was held back for a blocker that has since finished: select again instead of dropping it
                break
            try:
                self._done.get(timeout=self.poll_seconds)
            except queue.Empty:
                pass
        if self._terminate_requested.is_set():
            self._halt(summary, interrupted=False, reason="operator stop requested", stop_reason="STOPPED")
        elif self.global_failure is not None:
            self._halt(
                summary,
                interrupted=False,
                reason=f"global safety failure: {self.global_failure}",
                stop_reason="GLOBAL_SAFETY_FAILURE",
            )
        elif not self.stop.is_set():
            for task_id, thread in list(self._threads.items()):
                thread.join()

    def _resume_provider_waits(self, attempted: set[str]) -> None:
        with self._lock:
            waits = list(self._provider_waits.items())
        for task_id, deadline in waits:
            thread = self._threads.get(task_id)
            if thread is not None and thread.is_alive():
                continue
            task = self.store.get_task(task_id)
            if task is None or task["status"] != "OPEN":
                with self._lock:
                    self._provider_waits.pop(task_id, None)
                continue
            if time.time() < deadline:
                continue
            with self._lock:
                self._provider_waits.pop(task_id, None)
            attempted.discard(task_id)
            self.note(task_id, "provider_retry_ready")
            self._say(task_id, "provider cooldown ended; resuming automatic selection")
        with self._lock:
            self.summary.control["waiting_for_providers"] = [
                {"task_id": task_id, "retry_at": deadline} for task_id, deadline in self._provider_waits.items()
            ]

    def _select_batch(self, summary: ParallelSummary, free: int, running: list[str], attempted: set[str]) -> list[str]:
        # Give candidates already in the validation/review/integration pipeline priority over
        # fresh admissions. Otherwise short new tasks can repeatedly advance the ref while an
        # older candidate revalidates, exhausting its bounded rebase budget through starvation.
        for current_id in running:
            task = self.store.get_task(current_id)
            if task is not None and task["stage"] in (Stage.VALIDATE, Stage.REVIEW, Stage.INTEGRATE):
                return []
        summary.recovered.extend(auto_rebaseline_blocked_stale_baselines(self.store, self.project, limit=free))
        summary.recovered.extend(auto_rebind_blocked_missing_validation_gates(self.store, self.project, limit=free))
        summary.recovered.extend(auto_revalidate_blocked_validation_gates(self.store, self.project, limit=free))
        summary.recovered.extend(auto_reintegrate_blocked_runtime_failures(self.store, self.project, limit=free))
        retried = auto_retry_blocked_provider_failures(self.store, self.project, limit=free, exclude=attempted)
        summary.recovered.extend(retried)
        for entry in retried:
            self._say(entry["task_id"], f"automatic recovery: {entry['message']}")
        for row in self.store.tasks():  # a task whose automatic retries are spent is reported once, with what a human must do
            task_key = str(row["id"])
            if row["status"] == "BLOCKED" and task_key not in self._exhaustion_said:
                text = exhaustion_message(self.store, task_key)
                if text:
                    self._exhaustion_said.add(task_key)
                    self._say(task_key, text)
        ranked, skipped, _ = rank_batch_candidates(self.store, self.project, self.policy, auto_plan=self.auto_plan)
        ranked, skipped = self._filter_ranked_candidates(summary, ranked, skipped, running)
        self._refuse_unrunnable(summary, skipped, attempted)
        summary.skipped = [s for s in skipped if s["task_id"] not in attempted and s["task_id"] not in running]
        active = {task_id: self._contracts[task_id] for task_id in running if task_id in self._contracts}
        chosen: list[str] = []
        deferred: dict[str, dict[str, Any]] = {}
        for candidate in ranked:
            task_id = candidate.task_id
            if task_id in attempted or task_id in running or len(chosen) >= free:
                continue
            plan_info: dict[str, Any] = {"occurred": False, "reused_existing": False, "events": []}
            try:
                _ensure_contract(self.store, self.project, task_id, self.auto_plan, plan_info, lambda m, i=plan_info: i["events"].append(m))
                if any(row["task_id"] == task_id for row in self.store.running_executions()):
                    raise RunReadyRefusal("active_execution", f"task {task_id} has a live or unknown execution; not recovering it")
            except RunReadyRefusal as refusal:
                attempted.add(task_id)
                self._record_refusal(summary, candidate.to_dict(), plan_info, refusal)
                continue
            if plan_info["occurred"]:
                self._planned[task_id] = plan_info  # a task held back this round keeps its planning record for the round it starts in
                gates = ", ".join(plan_info.get("gates", []))
                self._say(task_id, f"task {task_id}: auto-planned contract {plan_info.get('path')} (gates: {gates})")
            elif task_id in self._planned:
                plan_info = self._planned[task_id]
            contract = load_task_contract(self.store, self.project, task_id)
            # Conflicts first: a task held back for a running blocker must not be refused for the dirty files that blocker is
            # writing (or integrating) right now. It is admitted, or refused, only once nothing it conflicts with is running.
            blocker = next(((other, why) for other, c in active.items() if (why := self._conflict(contract, c))), None)
            if blocker is not None:
                deferred[task_id] = {"task_id": task_id, "blocked_by": blocker[0], "reason": str(blocker[1])}
                continue
            refusal = self._admit(task_id, contract)
            if refusal is not None:
                attempted.add(task_id)
                self._record_refusal(summary, candidate.to_dict(), plan_info, refusal)
                continue
            active[task_id] = contract
            self._contracts[task_id] = contract
            self._pending_selection[task_id] = (candidate.to_dict(), plan_info)
            chosen.append(task_id)
        self._deferred_this_round = bool(deferred)
        for item in deferred.values():
            if not any(d["task_id"] == item["task_id"] and d["blocked_by"] == item["blocked_by"] for d in summary.deferred):
                summary.deferred.append(item)
                self._say("run", f"deferring task {item['task_id']}: conflicts with running task {item['blocked_by']} ({item['reason']})")
        return chosen

    def _filter_ranked_candidates(  # type: ignore[no-untyped-def]
        self, summary: ParallelSummary, ranked, skipped, running: list[str]
    ):
        """Optionally narrow queue admissions before contract/admission checks."""
        return ranked, skipped

    def _conflict(self, contract: ChangeContract, other: ChangeContract):
        """Why two tasks may not run together (subclasses can add rules)."""
        return contract_conflict(contract, other)

    def _admit(self, task_id: str, contract: ChangeContract) -> RunReadyRefusal | None:
        """A last per-task gate before a task is started; return a refusal to keep it out of the run."""
        return None

    def _refuse_unrunnable(self, summary: ParallelSummary, skipped: list[dict[str, str]], attempted: set[str]) -> None:
        """Hook for runners that report unrunnable tasks as refusals instead of silent skips."""

    def _record_refusal(self, summary: ParallelSummary, selection: dict[str, Any], plan_info: dict[str, Any], refusal: RunReadyRefusal) -> None:
        task_id = str(selection["task_id"])
        lifecycle = TaskLifecycle(task_id, summary=RunSummary(False, f"REFUSED:{refusal.reason}", task_id=task_id, message=refusal.message, selection=selection, auto_plan=plan_info, detail=refusal.detail))
        with self._lock:
            self._lifecycles[task_id] = lifecycle
            summary.tasks.append(lifecycle)
        self.note(task_id, "refused", reason=refusal.reason, message=refusal.message)
        self._say(task_id, f"refused: {refusal.message}")

    def _start(self, task_id: str) -> None:
        selection, plan_info = self._pending_selection.pop(task_id)
        lifecycle = self._lifecycles.get(task_id) or TaskLifecycle(task_id)
        previous = lifecycle.summary
        lifecycle.summary = RunSummary(True, "UNSET", task_id=task_id, selection={"mode": "parallel", **selection}, auto_plan=plan_info)
        if previous is not None:
            lifecycle.summary.detail["provider_wait_history"] = [
                *previous.detail.get("provider_wait_history", []),
                {"stop_reason": previous.stop_reason, "failure": previous.detail.get("failure")},
            ]
        with self._lock:
            self._lifecycles[task_id] = lifecycle
            if lifecycle not in self.summary.tasks:
                self.summary.tasks.append(lifecycle)
        self.note(task_id, "selected", priority=selection.get("priority"), contract=selection.get("contract"))
        self._say(task_id, f"selected (up to {self.concurrency} tasks run concurrently)")
        for message in plan_info.get("events", []):
            self._say(task_id, message)
        if plan_info.get("occurred"):
            self.note(task_id, "auto_planned", path=plan_info.get("path"), gates=plan_info.get("gates"))
        thread = threading.Thread(target=self._work, args=(task_id, lifecycle), name=f"stagemesh-{task_id}", daemon=True)
        self._threads[task_id] = thread
        thread.start()

    # -- per-task worker -------------------------------------------------------------------------------------------------

    def _work(self, task_id: str, lifecycle: TaskLifecycle) -> None:
        summary = lifecycle.summary
        assert summary is not None
        store = Store(self.store.db_path)
        notice_index = 0

        def on_start(message: str) -> None:
            self._say(task_id, message)

        def on_step(step: dict[str, Any]) -> None:
            nonlocal notice_index
            log = self.provider_log(task_id)
            notices = log.lines[notice_index:]
            notice_index = len(log.lines)
            stage = str(step["previous"]["stage"])
            self.note(
                task_id,
                "stage",
                stage=stage,
                result=_stage_status(stage, step["new"], step.get("progressed", 0)),
                next_stage=str(step["new"]["stage"]),
                candidate=step["new"].get("latest_candidate"),
                provider=step["new"].get("latest_agent"),
            )
            self._say(task_id, format_step_update(step, notices=notices))

        try:
            try:
                coordinator = self.make_coordinator(TargetSelection(task_id), store, task_id)
                coordinator.validate_target()
            except TargetSelectionError as exc:
                summary.started, summary.stop_reason, summary.message = False, "REFUSED:target_not_runnable", str(exc)
                return
            except SetupRefused as exc:
                summary.started, summary.stop_reason, summary.message = False, "REFUSED:setup_failed", str(exc)
                return
            self.note(task_id, "started", worktree=str(task_workspace(self.project, task_id)))
            drive_task(
                store, self.project, coordinator, task_id, summary,
                max_steps=self.max_steps, on_step=on_step, on_start=on_start, global_health=False, should_stop=self.stop.is_set,
                worktree_root_path=self.runtime.worktree_root,
            )
            if summary.stop_reason in {"STOP_CLEANUP_INCOMPLETE", "STOP_MONITOR_FAILED"}:
                self.global_failure = f"task {task_id}: {summary.message}"
                self.stop.set()
            elif summary.stop_reason == "OPERATOR_STOP":
                summary.stop_reason = "INTERRUPTED" if self.summary.interrupted else "STOPPED"
            if self.wait_for_providers and summary.stop_reason == "NO_PROGRESS":
                stage = str(store.get_task(task_id)["stage"])
                pool = getattr(coordinator.executor, "pool", None) if stage == "IMPLEMENT" else getattr(coordinator.reviewer, "review_pool", None)
                failure = summary.detail.get("failure", {})
                if pool is not None and failure.get("event") in {"task.capacity_failure", "review.infrastructure_failure"}:
                    candidate = store.latest_candidate(task_id)
                    implementer = str(candidate["produced_by"]) if candidate is not None else None
                    deadline = pool.next_retry_at(store, stage, task_id, implementer)
                    if deadline is not None:
                        with self._lock:
                            self._provider_waits[task_id] = deadline
                        record_audit(store, "queue.provider_wait", {"task_id": task_id, "stage": stage, "retry_at": deadline})
                        self.note(task_id, "waiting_for_provider", retry_at=deadline)
                        self._say(task_id, "waiting for temporary provider capacity; automatic recovery is scheduled")
            if summary.stop_reason == "DONE":
                remove_task_workspace(self.project, task_id)
                self.note(task_id, "worktree_removed")
        except BaseException as exc:  # noqa: BLE001 - one task's crash must never reach the other tasks
            summary.stop_reason = "ERROR"
            summary.message = f"{type(exc).__name__}: {exc}"
            self._abandon(store, task_id, "error")
            if isinstance(exc, IntegrationLockTimeout):  # shared state may be wedged: not one task's problem any more
                self.global_failure = f"task {task_id}: {summary.message}"
                self.stop.set()
        finally:
            lifecycle.provider_events = list(self.provider_log(task_id).lines)
            self.note(task_id, "finished", stop_reason=summary.stop_reason, message=summary.message)
            self._say(task_id, f"finished: {summary.stop_reason}" + (f" - {summary.message}" if summary.message else ""))
            try:
                store.close()
            finally:
                self._done.put(task_id)

    # -- interruption and cleanup ------------------------------------------------------------------------------------------

    def _abandon(self, store: Store, task_id: str, why: str) -> None:
        """Reap provable provider ownership; retain unconfirmed validation trees/claims and committed work."""
        protected_claims: set[str] = set()
        unresolved_execution = False
        termination_results: dict[ProcessIdentity, bool] = {}
        for execution in [e for e in store.running_executions() if e["task_id"] == task_id]:
            if validation_tree_unresolved(execution):
                unresolved_execution = True
                if execution["claim_id"]:
                    protected_claims.add(str(execution["claim_id"]))
                    self._protected_claims.add(str(execution["claim_id"]))
                continue  # neither kill the validator parent nor release/discard its owned tree
            saved = store.execution_process_identity(str(execution["id"]))
            if any(not result and saved.matches(owner) for owner, result in termination_results.items()):
                store.retain_execution_for_cleanup(str(execution["id"]), "PID-owned provider tree cleanup unconfirmed")
                unresolved_execution = True
                continue
            state = classify_process(saved, process_identity(execution["pid"]))
            if state == "UNKNOWN":
                unresolved_execution = True
                if execution["claim_id"]:
                    protected_claims.add(str(execution["claim_id"]))
                    self._protected_claims.add(str(execution["claim_id"]))
                record_audit(
                    store,
                    "task.abandon_skipped_unknown_execution",
                    {
                        "task_id": task_id,
                        "execution_id": str(execution["id"]),
                        "execution_kind": str(execution["kind"]),
                        "claim_id": execution["claim_id"],
                        "reason": why,
                    },
                )
                continue
            if execution["pid"] and state == "LIVE":
                try:
                    confirmed = _kill_pid(int(execution["pid"]), saved)
                except Exception:  # noqa: BLE001 -- a cleanup error must never release ownership or discard partial edits
                    confirmed = False
                termination_results[saved] = confirmed
                if not confirmed:
                    store.retain_execution_for_cleanup(str(execution["id"]), "PID-owned provider tree cleanup unconfirmed")
                    unresolved_execution = True
                    continue
            store.mark_orphan_running_execution_failed(str(execution["id"]), f"PARALLEL_{why.upper()}")
        for claim in store.conn.execute("SELECT id FROM claims WHERE task_id=? AND active=1 AND worker_id LIKE ?", (task_id, WORKER_PREFIX + "%")).fetchall():
            if unresolved_execution or str(claim["id"]) in protected_claims:
                self._protected_claims.add(str(claim["id"]))
                continue
            store.release_claim(str(claim["id"]))
        if not unresolved_execution and not protected_claims:
            discard_partial_edits(self.project, task_id)
        record_audit(store, "task.abandoned", {"task_id": task_id, "reason": why})

    def _interrupt(self, summary: ParallelSummary) -> None:
        self.stop_admission("keyboard interrupt", terminate_running=True)
        self._halt(summary, interrupted=True, reason="interrupted", stop_reason="INTERRUPTED")

    def _halt(self, summary: ParallelSummary, *, interrupted: bool, reason: str, stop_reason: str) -> None:
        summary.interrupted = interrupted
        self.stop.set()
        self._admission_stopped.set()
        self._refresh_control_state("stopping")
        self._say(
            "run",
            "interrupted: stopping all tasks, releasing claims and keeping their worktrees"
            if interrupted
            else f"{reason}; stopping all tasks",
        )
        active_thread_task_ids = {task_id for task_id, thread in self._threads.items() if thread.is_alive()}
        cleanup = Store(self.store.db_path)
        try:
            active_before = self._merged_stop_snapshot(cleanup)
            stopped_task_ids = (
                active_thread_task_ids
                | self._stop_request_task_ids
            )
            unconfirmed_execution_ids: set[str] = set()

            def retain_uncertain(identity):
                # Refresh rows, including a provider registered after the initial snapshot. Parent-owned review rows
                # are conservatively fenced only within this runner's active-at-stop tasks.
                unconfirmed_execution_ids.update(retain_provider_cleanup(cleanup, stopped_task_ids, identity))

            in_process_killed = kill_active_provider_processes(
                database=self.store.db_path, task_ids=stopped_task_ids, on_uncertain=retain_uncertain,
            )
            live_known_killed = self._terminate_live_known_executions(cleanup, {str(item["id"]) for item in active_before})
            record_audit(
                cleanup,
                "queue.stop",
                {
                    "reason": reason,
                    "stop_reason": stop_reason,
                    "interrupted": interrupted,
                    "active_executions": active_before,
                    "active_execution_ids": [item["id"] for item in active_before],
                    "terminated_in_process_providers": in_process_killed,
                    "unconfirmed_in_process_execution_ids": sorted(unconfirmed_execution_ids),
                    "terminated_live_execution_pids": live_known_killed,
                    "unknown_execution_ids": [item["id"] for item in active_before if item["process_state"] == "UNKNOWN"],
                },
            )
            deadline = time.monotonic() + self.interrupt_grace_seconds
            for thread in self._threads.values():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            for task_id, thread in self._threads.items():
                lifecycle = self._lifecycles[task_id]
                stuck = thread.is_alive()
                if (
                    task_id in stopped_task_ids
                    and lifecycle.summary is not None
                    and lifecycle.summary.stop_reason in {"UNSET", "INTERRUPTED"}
                ):
                    lifecycle.summary.stop_reason = stop_reason
                    lifecycle.summary.message = reason
                    self._abandon(cleanup, task_id, stop_reason.lower())
                    self.note(
                        task_id,
                        stop_reason.lower(),
                        worktree=str(task_workspace(self.project, task_id)),
                        worker_still_running=stuck,
                    )
        finally:
            cleanup.close()

    def _capture_stop_request(self) -> None:
        self._stop_request_task_ids.update(task_id for task_id, thread in self._threads.items() if thread.is_alive())
        self._capture_stop_request_snapshot()

    def _capture_stop_request_snapshot(self) -> None:
        """Remember executions active at termination request time before workers can finish or fail them."""
        if self._stop_request_snapshot is not None:
            return
        snapshot = Store(self.store.db_path)
        try:
            self._stop_request_snapshot = self._execution_stop_snapshot(snapshot)
        finally:
            snapshot.close()

    def _merged_stop_snapshot(self, store: Store) -> list[dict[str, Any]]:
        by_id = {item["id"]: item for item in self._stop_request_snapshot or []}
        for item in self._execution_stop_snapshot(store):
            by_id[item["id"]] = item
        return list(by_id.values())

    def _execution_stop_snapshot(self, store: Store) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        runner_task_ids = set(self._threads) | self._stop_request_task_ids
        for execution in store.running_executions():
            if str(execution["task_id"]) not in runner_task_ids:
                continue
            saved = store.execution_process_identity(str(execution["id"]))
            state = classify_process(saved, process_identity(execution["pid"]))
            rows.append(
                {
                    "id": str(execution["id"]),
                    "task_id": str(execution["task_id"]),
                    "claim_id": execution["claim_id"],
                    "kind": str(execution["kind"]),
                    "pid": execution["pid"],
                    "process_state": state,
                }
            )
        return rows

    def _terminate_live_known_executions(self, store: Store, execution_ids: set[str]) -> list[int]:
        killed: list[int] = []
        outcomes: dict[ProcessIdentity, bool] = {}
        for execution in list(store.running_executions()):
            if str(execution["id"]) not in execution_ids:
                continue
            saved = store.execution_process_identity(str(execution["id"]))
            previous = next((result for owner, result in outcomes.items() if saved.matches(owner)), None)
            if previous is not None:
                confirmed = previous
            else:
                if not saved.pid or saved.pid == os.getpid() or classify_process(saved, process_identity(saved.pid)) != "LIVE":
                    continue
                try:
                    confirmed = _kill_pid(saved.pid, saved)
                except Exception:  # noqa: BLE001 -- failed cleanup remains recoverable while other owned providers are stopped
                    confirmed = False
                outcomes[saved] = confirmed
                if confirmed:
                    killed.append(saved.pid)
            if not confirmed:
                store.retain_execution_for_cleanup(str(execution["id"]), "PID-owned provider tree cleanup unconfirmed")
        return killed

    def _finalize(self, summary: ParallelSummary) -> None:
        # A live or incompletely reaped execution must keep its claim for recovery.
        final = Store(self.store.db_path)
        try:
            running = list(final.running_executions())
            protected = {str(e["claim_id"]) for e in running if e["claim_id"]}
            protected_tasks = {str(e["task_id"]) for e in running}
            if any(str(e["task_id"]) in self._threads for e in final.running_executions()):
                self.global_failure = "provider cleanup incomplete; execution ownership retained for recovery"
            for claim in final.conn.execute("SELECT id, task_id FROM claims WHERE active=1 AND worker_id LIKE ?", (f"{WORKER_PREFIX}{os.getpid()}-%",)).fetchall():
                if str(claim["task_id"]) not in self._threads:
                    continue  # only tasks this run started; anything else is somebody else's claim
                if str(claim["id"]) in self._protected_claims | protected or str(claim["task_id"]) in protected_tasks:
                    continue  # unknown process identity: stays claimed until `recover-stale --release-unknown`
                final.release_claim(str(claim["id"]))
                self.note(str(claim["task_id"]), "claim_released")
        finally:
            final.close()
        if self.limiter is not None:
            summary.providers = {"limits": self.limiter.snapshot()}
        self._refresh_control_state()
        outcomes = [t.summary.stop_reason if t.summary else "UNSET" for t in summary.tasks]
        if "STOP_CLEANUP_INCOMPLETE" in outcomes:
            summary.stop_reason = "GLOBAL_SAFETY_FAILURE"
            summary.message = "provider cleanup incomplete; ownership retained for recovery"
        elif self.global_failure is not None:
            summary.stop_reason, summary.message = "GLOBAL_SAFETY_FAILURE", self.global_failure
        elif summary.interrupted:
            summary.stop_reason, summary.message = "INTERRUPTED", "interrupted; claims released, worktrees kept for resume"
            self._record_stopped("keyboard interrupt stop completed", terminate_running=True)
        elif self._terminate_requested.is_set():
            summary.stop_reason = "STOPPED"
            summary.message = "operator stop requested; claims released, worktrees kept for resume"
            self._record_stopped("operator stop completed", terminate_running=True)
        elif self._admission_stopped.is_set():
            summary.stop_reason = "STOPPED"
            summary.message = "queue admission stopped; active tasks were allowed to finish"
            self._record_stopped("queue admission stop completed", terminate_running=False)
        elif self._admission_paused.is_set():
            summary.stop_reason, summary.message = "PAUSED", "queue admission paused"
        elif not summary.tasks:
            summary.stop_reason = "REFUSED:no_eligible_task"
            summary.message = "no eligible OPEN task" + (
                " (skipped: " + "; ".join(f"{s['task_id']}: {s['reason']}" for s in summary.skipped[:10]) + ")" if summary.skipped else ""
            )
        elif all(outcome == "DONE" for outcome in outcomes):
            summary.stop_reason = "DONE"
        else:
            failed = [t.task_id for t in summary.tasks if not (t.summary and t.summary.succeeded)]
            summary.stop_reason = "MAX_STEPS" if {o for o in outcomes if o != "DONE"} == {"MAX_STEPS"} else "PARTIAL"
            summary.message = f"{len(failed)} of {len(outcomes)} task(s) did not finish: {', '.join(failed)}"
