"""Run several independent tasks at once, each in its own worktree, with one-at-a-time integration.

The dispatcher (the calling thread) owns task selection and contract checks; every started task gets a thread with its own
database connection, coordinator and worktree, and drives itself to DONE or a safe stop exactly like a single `continue`.
Failures stay inside the task that had them. Ctrl+C stops every task, kills provider processes this run started, releases
their claims and discards uncommitted partial edits, so a restart resumes from committed state with nothing orphaned.
"""

from __future__ import annotations

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import record_audit
from .concurrency import IntegrationLockTimeout, ProviderLimiter, contract_conflict
from .config import RuntimeConfig, TaskSelectionConfig, load_config
from .contracts import ChangeContract, ContractError, parse_contract, task_contract_path
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .domain import ExecutionKind
from .execution import kill_active_provider_processes
from .git import GitWorkspace
from .persistence import Store
from .process_identity import classify_process, process_identity
from .provider_pool import ProviderLog
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
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    providers: dict[str, Any] = field(default_factory=dict)
    interrupted: bool = False

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "DONE"

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "recommendations": self.recommendations,
            "providers": self.providers,
        }


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
        for execution in [e for e in store.running_executions() if e["claim_id"] == claim["id"]]:
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


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


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
    ):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.store = store
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
        self.stop = threading.Event()
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
        summary.recovered.extend(recover_orphaned_claims(self.store, self.project))
        for row in self.store.tasks():  # provably dead provider processes only; live/unknown are never touched
            summary.recovered.extend(_recover_dead(self.store, str(row["id"])))
        summary.swept = sweep_task_worktrees(self.project, self.store)
        for item in summary.recovered:
            self._say("run", f"recovered {item.get('action', 'RELEASED').lower()} claim/execution for task {item['task_id']}")
        for item in summary.swept:
            self._say("run", f"removed orphaned worktree {item['worktree']} ({item['reason']})")

    def _dispatch_loop(self, summary: ParallelSummary) -> None:
        attempted: set[str] = set()
        while not self.stop.is_set():
            running = [task_id for task_id, thread in self._threads.items() if thread.is_alive()]
            free = self.concurrency - len(running)
            self._deferred_this_round = False
            if free > 0:
                for task_id in self._select_batch(summary, free, running, attempted):
                    attempted.add(task_id)
                    self._start(task_id)
            running = [task_id for task_id, thread in self._threads.items() if thread.is_alive()]
            if not running:
                if self._deferred_this_round:
                    continue  # a task was held back for a blocker that has since finished: select again instead of dropping it
                break
            try:
                self._done.get(timeout=self.poll_seconds)
            except queue.Empty:
                pass
        if self.global_failure is not None:
            self._halt(summary, interrupted=False)
        elif not self.stop.is_set():
            for task_id, thread in list(self._threads.items()):
                thread.join()

    def _select_batch(self, summary: ParallelSummary, free: int, running: list[str], attempted: set[str]) -> list[str]:
        ranked, skipped, _ = rank_batch_candidates(self.store, self.project, self.policy, auto_plan=self.auto_plan)
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
                scope = (plan_info.get("scope") or {}).get("mode", "profile")
                self._say(task_id, f"task {task_id}: auto-planned contract {plan_info.get('path')} (gates: {gates}; scope: {scope})")
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

    def _conflict(self, contract: ChangeContract, other: ChangeContract):  # noqa: ANN202 - ConflictReason | None
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
        lifecycle = TaskLifecycle(task_id)
        lifecycle.summary = RunSummary(True, "UNSET", task_id=task_id, selection={"mode": "parallel", **selection}, auto_plan=plan_info)
        with self._lock:
            self._lifecycles[task_id] = lifecycle
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
        """Kill this task's provider processes, fail its running executions and release its claims; keep committed work."""
        for execution in [e for e in store.running_executions() if e["task_id"] == task_id]:
            if execution["kind"] == ExecutionKind.IMPLEMENTATION and execution["pid"]:
                saved = store.execution_process_identity(str(execution["id"]))
                if classify_process(saved, process_identity(execution["pid"])) != "DEAD":
                    _kill_pid(int(execution["pid"]))
            store.mark_orphan_running_execution_failed(str(execution["id"]), f"PARALLEL_{why.upper()}")
        for claim in store.conn.execute("SELECT id FROM claims WHERE task_id=? AND active=1 AND worker_id LIKE ?", (task_id, WORKER_PREFIX + "%")).fetchall():
            store.release_claim(str(claim["id"]))
        discard_partial_edits(self.project, task_id)
        record_audit(store, "task.abandoned", {"task_id": task_id, "reason": why})

    def _interrupt(self, summary: ParallelSummary) -> None:
        self._halt(summary, interrupted=True)

    def _halt(self, summary: ParallelSummary, *, interrupted: bool) -> None:
        summary.interrupted = interrupted
        self.stop.set()
        self._say("run", "interrupted: stopping all tasks, releasing claims and keeping their worktrees" if interrupted else f"global safety failure: {self.global_failure}; stopping all tasks")
        cleanup = Store(self.store.db_path)
        try:
            kill_active_provider_processes()  # implementation AND review providers: nothing may outlive the interrupt
            for task_id, thread in self._threads.items():
                if thread.is_alive():  # stop the provider so the worker's tick returns instead of running to its timeout
                    for execution in [e for e in cleanup.running_executions() if e["task_id"] == task_id and e["pid"]]:
                        if execution["kind"] == ExecutionKind.IMPLEMENTATION:
                            _kill_pid(int(execution["pid"]))
            deadline = time.monotonic() + self.interrupt_grace_seconds
            for thread in self._threads.values():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            for task_id, thread in self._threads.items():
                lifecycle = self._lifecycles[task_id]
                stuck = thread.is_alive()
                if lifecycle.summary is not None and lifecycle.summary.stop_reason in {"UNSET", "INTERRUPTED"}:
                    lifecycle.summary.stop_reason = "INTERRUPTED"
                    self._abandon(cleanup, task_id, "interrupted")
                    self.note(task_id, "interrupted", worktree=str(task_workspace(self.project, task_id)), worker_still_running=stuck)
        finally:
            cleanup.close()

    def _finalize(self, summary: ParallelSummary) -> None:
        # Defensive: whatever happened, no claim of this process may outlive the run.
        final = Store(self.store.db_path)
        try:
            for claim in final.conn.execute("SELECT id, task_id FROM claims WHERE active=1 AND worker_id LIKE ?", (f"{WORKER_PREFIX}{os.getpid()}-%",)).fetchall():
                if str(claim["task_id"]) not in self._threads:
                    continue  # only tasks this run started; anything else is somebody else's claim
                final.release_claim(str(claim["id"]))
                self.note(str(claim["task_id"]), "claim_released")
        finally:
            final.close()
        if self.limiter is not None:
            summary.providers = {"limits": self.limiter.snapshot()}
        outcomes = [t.summary.stop_reason if t.summary else "UNSET" for t in summary.tasks]
        if self.global_failure is not None:
            summary.stop_reason, summary.message = "GLOBAL_SAFETY_FAILURE", self.global_failure
        elif summary.interrupted:
            summary.stop_reason, summary.message = "INTERRUPTED", "interrupted; claims released, worktrees kept for resume"
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
