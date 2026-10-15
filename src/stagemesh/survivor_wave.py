"""Bounded survivor wave: run a chosen set of stuck or open tasks end to end with little operator hand-holding.

For each task the wave (1) advances a stale baseline when that cannot discard useful progress, (2) runs the task through the normal
`continue` loop with bounded concurrency, (3) records what happened, and (4) publishes a task that reached DONE. It never merges a PR
and never cleans a worktree.

Integration is explicit. In PR mode (the default) the child runs integrate into a wave staging ref (`refs/heads/wave/staging`), never
into the integration branch, so local `main` does not move; each DONE task is pushed to a `wave/task-N` branch and a PR is opened for a
person to merge. In local mode the child runs integrate straight into the integration branch, local `main` moves, no PR is opened, and
the report says so. The report also carries the repo state before and after (main versus origin, claims, running executions, worktree
census) so it is obvious whether the wave left the repo ready, ahead, diverged or waiting on PR merges.

The wave stops starting new work when two tasks hit the same infrastructure failure, when the orphan-recovery event count rises, or
(with `stop_on_blocked`) when a task is blocked. A task blocked on unresolved review findings is marked as needing a decision and the
wave carries on with the rest. Nothing here talks to a provider: the task runner, baseline advancer and PR opener are injected, so
the orchestration is tested with fakes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import EvidenceKind, Stage, TaskStatus
from .objective_roots import objective_root_reason
from .persistence import Store

ORPHAN_EVENT = "recovery.orphan_execution_failed"

INTEGRATION_PR = "pr"
INTEGRATION_LOCAL = "local"
INTEGRATION_MODES = (INTEGRATION_PR, INTEGRATION_LOCAL)
STAGING_REF = "refs/heads/wave/staging"

# `continue` stop reasons that say the environment, not the task, is the problem.
INFRASTRUCTURE_STOP_REASONS = frozenset({"CURRENT_PROBLEM", "TARGET_ERROR", "RUNNER_CRASH"})
INFRASTRUCTURE_MESSAGE_MARKERS = (
    "orphaned_builtin_stage_execution",
    "external_workspace_mutation",
    "database is locked",
    "provider setup failed",
    "health has current problems",
)


@dataclass
class RunResult:
    """What one `continue --task N` run reported. `stop_reason` is StageMesh's own (DONE, BLOCKED, MAX_STEPS, ...)."""

    stop_reason: str
    message: str = ""
    exit_code: int = 0


@dataclass
class TaskRecord:
    task_id: str
    title: str = ""
    baseline_before: str | None = None
    baseline_after: str | None = None
    baseline_advanced: bool = False
    unblocked: bool = False
    candidates: list[str] = field(default_factory=list)
    validation: str | None = None
    review: str | None = None
    integration: str | None = None
    stop_reason: str | None = None
    final_stage: str | None = None
    final_status: str | None = None
    outcome: str = "pending"  # done | blocked_needs_decision | blocked | open | skipped | infrastructure_failure | not_started
    reason: str = ""
    pr_url: str | None = None
    pr_error: str | None = None
    pr_state: str | None = None  # OPEN | MERGED | CLOSED as GitHub reports it; None when no PR
    landing: str | None = None  # pr_opened | pr_failed | staged_not_published | locally_integrated; None unless DONE
    ready_for_merge: bool = False  # a PR is open and waiting for a person
    resumable: bool = False  # stopped (e.g. at the step cap) with the task still OPEN: `continue` picks it up again
    integrated_candidate: str | None = None
    infrastructure_signature: str | None = None
    runs: int = 1  # how many times a run window ran this task (more than one after a step-cap continuation)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RepoState:
    """A read-only snapshot of what the wave could have changed. Taken before and after the wave."""

    main_ref: str = ""
    main_sha: str | None = None
    origin_sha: str | None = None
    ahead: int = 0
    behind: int = 0
    relation: str = "unknown"  # in_sync | ahead | behind | diverged | unknown
    active_claims: int = 0
    running_executions: int = 0
    orphan_events: int = 0
    cleanable_worktrees: int = 0  # task done and the worktree HEAD is already in the integration checkout: a sweep would remove it
    done_unmerged_worktrees: int = 0  # task done but its work is not in the integration checkout yet (e.g. waiting on a PR)
    resumable_worktrees: int = 0  # unfinished tasks: kept so a restart reuses them
    other_worktrees: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class WaveReport:
    tasks: list[TaskRecord] = field(default_factory=list)
    orphan_events_before: int = 0
    orphan_events_after: int = 0
    stopped_early: bool = False
    stop_reason: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    integration_mode: str = INTEGRATION_PR
    staging_ref: str | None = None
    repo_before: RepoState | None = None
    repo_after: RepoState | None = None
    runtime: dict[str, Any] | None = None
    time_limit_reached: bool = False  # the deadline passed with tasks still unstarted: a clean stop, not a failure

    @property
    def main_moved(self) -> bool:
        before, after = self.repo_before, self.repo_after
        return bool(before and after and before.main_sha and after.main_sha and before.main_sha != after.main_sha)

    @property
    def integration_violation(self) -> bool:
        """PR mode promises local main stays put; if it moved, the wave did not do what it said."""
        return self.integration_mode == INTEGRATION_PR and self.main_moved

    @property
    def ok(self) -> bool:
        return not self.stopped_early and self.orphan_events_after <= self.orphan_events_before and not self.integration_violation

    @property
    def wave_state(self) -> str:
        """Where the wave left the repo: ready | needs_pr_merges | unpublished_work | local_main_advanced | ahead | diverged | main_moved_unexpectedly."""
        after = self.repo_after
        if after is not None and after.relation == "diverged":
            return "diverged"
        if self.integration_violation:
            return "main_moved_unexpectedly"
        if self.integration_mode == INTEGRATION_LOCAL and self.main_moved:
            return "local_main_advanced"
        if after is not None and after.ahead > 0:
            return "ahead"
        if any(task.ready_for_merge and task.pr_state != "MERGED" for task in self.tasks):
            return "needs_pr_merges"
        if any(task.landing in ("staged_not_published", "pr_failed") for task in self.tasks):
            return "unpublished_work"  # DONE work sits on the staging ref with no open PR
        return "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "orphan_events_before": self.orphan_events_before,
            "orphan_events_after": self.orphan_events_after,
            "integration_mode": self.integration_mode,
            "staging_ref": self.staging_ref,
            "main_moved": self.main_moved,
            "wave_state": self.wave_state,
            "repo_before": None if self.repo_before is None else self.repo_before.to_dict(),
            "repo_after": None if self.repo_after is None else self.repo_after.to_dict(),
            "runtime": self.runtime,
            "time_limit_reached": self.time_limit_reached,
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass
class WaveHooks:
    """Everything the wave does outside the ledger. Real implementations are in `subprocess_task_runner` and `git_gh_pr_opener`."""

    run_task: Callable[[str, int], RunResult]
    advance_baseline: Callable[[str], dict[str, Any]]
    open_pr: Callable[[TaskRecord], str] | None = None
    target_sha: Callable[[], str | None] = lambda: None
    is_ancestor: Callable[[str, str], bool] = lambda older, newer: False
    repo_state: Callable[[], RepoState | None] = lambda: None
    pr_state: Callable[[str], str | None] = lambda url: None
    # Cheap checks the wave runs after every task. main_sha: the integration branch tip (None = unknown). leftover: how many RUNNING
    # executions and active claims a task still has after its runner exited (a provider process that was not cleaned up).
    main_sha: Callable[[], str | None] = lambda: None
    leftover: Callable[[str], tuple[int, int]] = lambda task_id: (0, 0)


def _safe_state(hooks: WaveHooks) -> RepoState | None:
    """The census is advice for the operator; a git or SQL hiccup in it must not sink a wave or lose its report."""
    try:
        return hooks.repo_state()
    except Exception:  # noqa: BLE001
        return None


def orphan_event_count(store: Store) -> int:
    return int(store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?", (ORPHAN_EVENT,)).fetchone()[0])


def parse_task_ids(text: str) -> list[str]:
    ids = [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]
    return list(dict.fromkeys(ids))


# --- selection and baseline advancement -------------------------------------------------------------------------------------------


def skip_reason(store: Store, task_id: str) -> str | None:
    """Why this task cannot be part of a wave, or None when it is eligible."""
    task = store.get_task(task_id)
    if task is None:
        return "no such task"
    if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
        return "already done"
    objective = objective_root_reason(store, task)
    if objective:
        return f"objective root ({objective}); plan it instead"
    if store.has_active_claim(task_id) or any(row["task_id"] == task_id for row in store.running_executions()):
        return "has an active claim or running execution"
    return None


def select_eligible(store: Store, task_ids: list[str]) -> tuple[list[str], dict[str, str]]:
    eligible: list[str] = []
    skipped: dict[str, str] = {}
    for task_id in task_ids:
        reason = skip_reason(store, task_id)
        if reason is None:
            eligible.append(task_id)
        else:
            skipped[task_id] = reason
    return eligible, skipped


def latest_evidence_status(store: Store, task_id: str, candidate_sha: str | None, kind: EvidenceKind) -> str | None:
    if candidate_sha is None:
        return None
    row = store.conn.execute(
        "SELECT status FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, kind),
    ).fetchone()
    return None if row is None else str(row["status"])


def should_advance_baseline(store: Store, task_id: str, hooks: WaveHooks) -> bool:
    """Advance only when it is possible and cannot throw away useful progress.

    `advance-baseline` restarts IMPLEMENT on a fresh worktree. A task whose latest candidate already passed validation keeps that
    progress instead: integration refreshes such a candidate onto the new base by itself.
    """
    task = store.get_task(task_id)
    baseline = store.task_baseline(task_id)
    target = hooks.target_sha()
    if task is None or baseline is None or target is None or baseline == target:
        return False
    if task["stage"] in (Stage.INTEGRATE, Stage.DONE) or not hooks.is_ancestor(baseline, target):
        return False
    latest = store.latest_candidate(task_id)
    return latest is None or latest_evidence_status(store, task_id, str(latest["sha"]), EvidenceKind.VALIDATION) != "PASSED"


# --- classification ---------------------------------------------------------------------------------------------------------------


def infrastructure_signature(result: RunResult) -> str | None:
    """A stable key when the run failed for a reason that is not about the task; the same key on two tasks means a shared fault."""
    text = result.message.lower()
    if result.stop_reason in INFRASTRUCTURE_STOP_REASONS:
        detail = re.sub(r"\d+", "N", text)[:60].strip()
        return f"{result.stop_reason}:{detail}"
    for marker in INFRASTRUCTURE_MESSAGE_MARKERS:
        if marker in text:
            return f"message:{marker}"
    if result.exit_code not in (0, 1, 2):
        return f"exit:{result.exit_code}"
    return None


def blocked_needs_decision(store: Store, task_id: str) -> str | None:
    """The concrete question when a person has to decide something, else None.

    Only `BLOCKED_HUMAN_DECISION` is a question for a person (see `block_kinds`). A spent remediation or recovery budget on its own is not:
    that block is recoverable and is reconsidered by `run-window` after the runtime changes.
    """
    from .block_kinds import BlockKind, classify_block
    from .integration_recovery import format_blocker, integration_blocker

    info = classify_block(store, task_id)
    if info is None or info.kind is not BlockKind.HUMAN_DECISION:
        return None
    blocker = integration_blocker(store, task_id)
    if blocker is not None and blocker.get("block_kind") == BlockKind.HUMAN_DECISION.value:
        return format_blocker(blocker)
    return info.reason


def _snapshot(store: Store, record: TaskRecord, since: float) -> None:
    task = store.get_task(record.task_id)
    if task is not None:
        record.final_stage, record.final_status = str(task["stage"]), str(task["status"])
    record.baseline_after = store.task_baseline(record.task_id)
    rows = store.conn.execute(
        "SELECT sha FROM candidates WHERE task_id=? AND created_at>=? ORDER BY created_at, rowid", (record.task_id, since)
    ).fetchall()
    record.candidates = [str(row["sha"]) for row in rows]
    latest = store.latest_candidate(record.task_id)
    sha = None if latest is None else str(latest["sha"])
    record.validation = latest_evidence_status(store, record.task_id, sha, EvidenceKind.VALIDATION)
    record.review = latest_evidence_status(store, record.task_id, sha, EvidenceKind.REVIEW)
    record.integration = latest_evidence_status(store, record.task_id, sha, EvidenceKind.INTEGRATION)
    if record.final_status == TaskStatus.DONE:
        record.integrated_candidate = sha


# --- the wave ---------------------------------------------------------------------------------------------------------------------


def run_wave(
    store: Store,
    task_ids: list[str],
    hooks: WaveHooks,
    *,
    concurrency: int = 2,
    max_steps: int = 12,
    advance_baselines: bool = True,
    unblock_blocked: bool = True,
    open_prs: bool = True,
    stop_on_blocked: bool = False,
    integration_mode: str = INTEGRATION_PR,
    staging_ref: str | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
) -> WaveReport:
    """Run `task_ids` with at most `concurrency` running at once. The store is only touched from the calling thread."""
    if integration_mode not in INTEGRATION_MODES:
        raise ValueError(f"integration_mode must be one of {', '.join(INTEGRATION_MODES)}")
    local = integration_mode == INTEGRATION_LOCAL
    report = WaveReport(
        started_at=clock(),
        orphan_events_before=orphan_event_count(store),
        integration_mode=integration_mode,
        staging_ref=None if local else staging_ref,
        repo_before=_safe_state(hooks),
    )
    report.orphan_events_after = report.orphan_events_before
    main_before = hooks.main_sha() if not local else None
    eligible, skipped = select_eligible(store, task_ids)
    records = {task_id: TaskRecord(task_id, title=str((store.get_task(task_id) or {"title": ""})["title"])) for task_id in task_ids}
    for task_id, reason in skipped.items():
        records[task_id].outcome, records[task_id].reason = "skipped", reason
    report.tasks = [records[task_id] for task_id in task_ids]

    for task_id in eligible:  # baselines first, in the calling thread: advancing writes the ledger and resets a worktree
        record = records[task_id]
        record.baseline_before = store.task_baseline(task_id)
        task = store.get_task(task_id)
        if advance_baselines and should_advance_baseline(store, task_id, hooks):
            try:
                hooks.advance_baseline(task_id)
                record.baseline_advanced = True
                record.unblocked = task["status"] == TaskStatus.BLOCKED
            except Exception as exc:  # noqa: BLE001 - a refusal here must not sink the wave; the task still runs on its old baseline
                record.reason = f"baseline not advanced: {exc}"
        elif unblock_blocked and task["status"] == TaskStatus.BLOCKED and store.unblock_task(task_id):
            record.unblocked = True

    signatures: dict[str, list[str]] = {}
    stop = False
    pending = list(eligible)
    running: dict[Future[RunResult], str] = {}
    started_at: dict[str, float] = {}

    def halt(reason: str) -> None:
        nonlocal stop
        if not stop:
            stop = True
            report.stopped_early, report.stop_reason = True, reason

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while pending or running:
            while pending and not stop and len(running) < max(1, concurrency):
                if deadline is not None and clock() >= deadline:
                    report.time_limit_reached = True
                    break
                task_id = pending.pop(0)
                started_at[task_id] = clock()
                running[pool.submit(hooks.run_task, task_id, max_steps)] = task_id
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for future in done:
                task_id = running.pop(future)
                record = records[task_id]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - a crashed runner is an infrastructure signal, not a task verdict
                    result = RunResult("RUNNER_CRASH", f"{type(exc).__name__}: {exc}", 3)
                record.stop_reason = result.stop_reason
                _snapshot(store, record, started_at[task_id])
                _classify(store, record, result)
                if record.outcome == "infrastructure_failure" and record.infrastructure_signature:
                    signatures.setdefault(record.infrastructure_signature, []).append(task_id)
                    if len(signatures[record.infrastructure_signature]) >= 2:
                        halt(f"shared infrastructure failure on tasks {', '.join(signatures[record.infrastructure_signature])}: {record.infrastructure_signature}")
                if stop_on_blocked and record.outcome in ("blocked", "blocked_needs_decision"):
                    halt(f"task {task_id} is blocked and the wave was asked to stop on blocked tasks")
                if orphan_event_count(store) > report.orphan_events_before:
                    halt("orphan-recovery events increased during the wave")
                running_left, claims_left = hooks.leftover(task_id)
                if running_left or claims_left:
                    record.outcome, record.reason = "infrastructure_failure", "execution or claim left behind after the runner exited"
                    halt(
                        f"provider process cleanup failure: task {task_id} still has {running_left} RUNNING execution(s) and "
                        f"{claims_left} active claim(s) after its runner exited"
                    )
                if main_before is not None:
                    main_now = hooks.main_sha()
                    if main_now is not None and main_now != main_before:
                        halt(f"local main moved in PR mode ({main_before[:7]} -> {main_now[:7]}) after task {task_id}")
    for task_id in pending:
        why = "time budget reached before this task started" if report.time_limit_reached else "wave stopped before this task started"
        records[task_id].outcome, records[task_id].reason = "not_started", why
        _snapshot(store, records[task_id], report.started_at)

    publish = open_prs and hooks.open_pr is not None and not local  # local mode integrates into the branch itself; a PR would be a second path
    for record in report.tasks:
        record.resumable = record.outcome == "open"
        if record.outcome != "done":
            continue
        if local:
            record.landing = "locally_integrated"
        elif not publish:
            record.landing = "staged_not_published"
        else:
            try:
                record.pr_url = hooks.open_pr(record)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - a failed PR must not hide that the task is done
                record.pr_error = f"{type(exc).__name__}: {exc}"
                record.landing = "pr_failed"
                continue
            record.landing, record.ready_for_merge = "pr_opened", True
            try:
                record.pr_state = hooks.pr_state(record.pr_url)
            except Exception:  # noqa: BLE001 - the PR exists; only its state is unknown
                record.pr_state = None
            record.pr_state = record.pr_state or "OPEN"
    report.orphan_events_after = orphan_event_count(store)
    report.repo_after = _safe_state(hooks)
    for state, count in ((report.repo_before, report.orphan_events_before), (report.repo_after, report.orphan_events_after)):
        if state is not None:
            state.orphan_events = count
    if report.orphan_events_after > report.orphan_events_before and not report.stopped_early:
        report.stopped_early, report.stop_reason = True, "orphan-recovery events increased during the wave"
    report.finished_at = clock()
    return report


def _classify(store: Store, record: TaskRecord, result: RunResult) -> None:
    signature = infrastructure_signature(result)
    if signature is not None:
        record.outcome, record.infrastructure_signature = "infrastructure_failure", signature
        record.reason = result.message or result.stop_reason
    elif record.final_status == TaskStatus.DONE:
        record.outcome = "done"
    elif record.final_status == TaskStatus.BLOCKED:
        from .block_kinds import classify_block

        decision = blocked_needs_decision(store, record.task_id)
        info = classify_block(store, record.task_id)
        record.outcome = "blocked_needs_decision" if decision else "blocked"
        record.reason = decision or (f"{info.kind.value}: {info.reason}" if info is not None else result.message or "blocked")
    else:
        record.outcome = "open"  # step budget used up or interrupted with the task still progressing
        record.reason = result.message or result.stop_reason


# --- report -----------------------------------------------------------------------------------------------------------------------


def format_report(report: WaveReport) -> str:
    if report.ok:
        verdict = "OK"
    elif report.integration_violation:
        verdict = "LOCAL MAIN MOVED IN PR MODE"
    elif report.stopped_early:
        verdict = "STOPPED"
    else:
        verdict = "ORPHAN EVENTS INCREASED"
    lines = [
        f"survivor wave: {verdict}" + (f" ({report.stop_reason})" if report.stop_reason else ""),
        f"integration mode: {report.integration_mode}"
        + (f" (staging {report.staging_ref}; local main is not advanced)" if report.integration_mode == INTEGRATION_PR and report.staging_ref else "")
        + (" (integrates into the integration branch; local main moves; no PRs)" if report.integration_mode == INTEGRATION_LOCAL else ""),
        f"repo state: {report.wave_state}",
        f"orphan-recovery events: {report.orphan_events_before} -> {report.orphan_events_after}",
    ]
    if report.runtime and report.runtime.get("message"):
        lines.append(f"runtime: {report.runtime['message']}")
    before, after = report.repo_before, report.repo_after
    if after is not None:
        main = f"{after.main_ref or 'main'} {(after.main_sha or '-')[:7]} vs origin {(after.origin_sha or '-')[:7]}: {after.relation}"
        main += f" (ahead {after.ahead}, behind {after.behind})"
        if report.main_moved and before is not None:
            main += f"; local main MOVED {(before.main_sha or '-')[:7]} -> {(after.main_sha or '-')[:7]}"
        lines.append(f"local main: {main}")
        lines.append(f"active claims: {after.active_claims}; running executions: {after.running_executions}")
        lines.append(
            "worktrees: "
            + (f"cleanable {before.cleanable_worktrees} -> " if before is not None else "cleanable ")
            + f"{after.cleanable_worktrees}; "
            + (f"done, work not in main yet {before.done_unmerged_worktrees} -> " if before is not None else "done, work not in main yet ")
            + f"{after.done_unmerged_worktrees}; "
            + (f"resumable {before.resumable_worktrees} -> " if before is not None else "resumable ")
            + f"{after.resumable_worktrees}; other {after.other_worktrees} (none were cleaned)"
        )
    for task in report.tasks:
        base = "-" if task.baseline_before is None else task.baseline_before[:7]
        if task.baseline_after and task.baseline_after != task.baseline_before:
            base += f" -> {task.baseline_after[:7]}"
        landing = ""
        if task.landing == "pr_opened":
            landing = f" -> PR opened, ready for merge ({task.pr_state or 'OPEN'})"
        elif task.landing == "locally_integrated":
            landing = " -> integrated into the local integration branch"
        elif task.landing == "staged_not_published":
            landing = " -> staged on the wave branch, no PR requested"
        elif task.landing == "pr_failed":
            landing = " -> PR not opened"
        lines.append(
            f"  {task.task_id}: {task.outcome}{landing}{f' ({task.final_stage}/{task.final_status})' if task.final_stage else ''} baseline {base}; "
            f"candidates {', '.join(sha[:7] for sha in task.candidates) or '-'}; validation {task.validation or '-'}, "
            f"review {task.review or '-'}, integration {task.integration or '-'}"
        )
        if task.reason:
            lines.append(f"      {task.reason}")
        if task.resumable:
            lines.append("      resumable: not a failure; `stagemesh continue --task " + task.task_id + "` picks it up")
        if task.pr_url:
            lines.append(f"      PR: {task.pr_url}")
        if task.pr_error:
            lines.append(f"      PR not opened: {task.pr_error}")
    return "\n".join(lines)


# --- real hooks -------------------------------------------------------------------------------------------------------------------


def subprocess_task_runner(
    project: Path, *, python: str | None = None, integration_ref: str | None = None
) -> Callable[[str, int], RunResult]:
    """Run `continue --task N --json` in a child process, so each task gets its own connection and process identity.

    `integration_ref` overrides where the child lands finished work (PR mode passes the wave staging ref); None keeps the configured one.
    """
    interpreter = python or sys.executable
    bootstrap = "import sys; from stagemesh.cli import main; sys.exit(main(sys.argv[1:]))"

    def run(task_id: str, max_steps: int) -> RunResult:
        argv = [interpreter, "-c", bootstrap, "--project", str(project), "continue", "--task", task_id, "--max-steps", str(max_steps), "--json"]
        if integration_ref:
            argv += ["--integration-ref", integration_ref]
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return RunResult("RUNNER_CRASH", (proc.stderr or proc.stdout or "no output").strip()[-300:], proc.returncode)
        if data.get("refused"):
            return RunResult(str(data.get("code") or "REFUSED").upper(), str(data.get("message") or ""), proc.returncode)
        return RunResult(str(data.get("stop_reason") or "UNKNOWN"), str(data.get("message") or ""), proc.returncode)

    return run


def is_task_pr_branch(head_ref: str, task_id: str, candidate_sha: str) -> bool:
    """Match only the publisher's original or exact-candidate retry branch."""
    base = f"wave/task-{task_id}"
    return head_ref in {base, f"{base}-{candidate_sha[:7]}"}


def git_gh_pr_opener(project: Path, base: str = "main") -> Callable[[TaskRecord], str]:
    """Push the task's integrated head to a branch and open a PR for it. Never force-pushes and never merges.

    The branch points at the candidate that was integrated, so it also carries the commits integrated before it (stacked); merge
    the PRs in order.
    """

    def run(*argv: str) -> str:
        proc = subprocess.run(argv, cwd=project, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:3])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return proc.stdout.strip()

    def open_pr(record: TaskRecord) -> str:
        sha = record.integrated_candidate
        if not sha:
            raise RuntimeError("no integrated candidate to open a PR for")
        branch = f"wave/task-{record.task_id}"
        existing = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project, capture_output=True, text=True, check=False)
        if existing.returncode == 0 and existing.stdout.strip() != sha:
            branch = f"{branch}-{sha[:7]}"
        if existing.returncode != 0 or branch.endswith(sha[:7]):
            run("git", "branch", branch, sha)
        run("git", "push", "-u", "origin", branch)
        commits = run("git", "rev-list", "--reverse", f"origin/{base}..{sha}").splitlines()
        body = "\n".join(
            [
                f"Task {record.task_id} ({record.title}) reached DONE/DONE in the local StageMesh ledger via a survivor wave.",
                "",
                f"- Integrated candidate: {sha}",
                f"- Commits on this branch ahead of {base}: {', '.join(c[:7] for c in commits) or '-'} (it also carries commits integrated before it; merge PRs in order)",
                f"- Baseline: {(record.baseline_before or '-')[:7]} -> {(record.baseline_after or '-')[:7]}",
                f"- Validation: {record.validation or '-'}; review: {record.review or '-'}; integration: {record.integration or '-'}",
                "- Opened automatically; not merged.",
            ]
        )
        return run("gh", "pr", "create", "--base", base, "--head", branch, "--title", f"{record.title} (task {record.task_id})", "--body", body)

    return open_pr


# --- staging ref and read-only repo census ----------------------------------------------------------------------------------------


def _git(project: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *argv], cwd=project, capture_output=True, text=True, check=False)


def _rev(project: Path, ref: str) -> str | None:
    proc = _git(project, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _is_ancestor(project: Path, older: str, newer: str) -> bool:
    return _git(project, "merge-base", "--is-ancestor", older, newer).returncode == 0


def prepare_staging_ref(project: Path, base_ref: str, staging_ref: str = STAGING_REF) -> str:
    """Make `staging_ref` a safe place to land wave work and return its tip. Only ever moves the wave's own ref.

    Absent: created at the base tip. Already contained in the base (merged or stale): moved up to the base tip, which loses nothing.
    Ahead of the base (earlier wave work still waiting on its PRs): kept, so this wave stacks on it. Diverged from the base: refused,
    because moving it would drop commits that are not in the base; the operator decides.
    """
    base = _rev(project, base_ref)
    if base is None:
        raise RuntimeError(f"integration base {base_ref} does not resolve")
    current = _rev(project, staging_ref)
    if current is None:
        proc = _git(project, "update-ref", staging_ref, base, "0" * 40)
    elif current == base or _is_ancestor(project, base, current):
        return current
    elif _is_ancestor(project, current, base):
        proc = _git(project, "update-ref", staging_ref, base, current)
    else:
        raise RuntimeError(
            f"{staging_ref} ({current[:7]}) has diverged from {base_ref} ({base[:7]}); not moving it because that would drop commits "
            "that are not in the base. Merge or delete the old wave branch first."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"could not set {staging_ref}: {(proc.stderr or proc.stdout).strip()[-200:]}")
    return _rev(project, staging_ref) or base


def git_repo_state(store: Store, project: Path, base_ref: str, remote: str = "origin") -> Callable[[], RepoState]:
    """A read-only census: it fetches nothing, creates no ref, removes no worktree and writes no audit event."""
    from .workspaces import _GENERATION_DIR, _task_key, worktree_root

    branch = base_ref.removeprefix("refs/heads/")
    short_main = branch

    def worktree_census(state: RepoState) -> None:
        root = worktree_root(project)
        if not root.is_dir():
            return
        tasks = {_task_key(str(row["id"])): row for row in store.tasks()}
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            row = tasks.get(entry.name.split("-g", 1)[0] if _GENERATION_DIR.match(entry.name) else entry.name)
            if row is None or not (entry / ".git").exists():  # no .git: git would resolve to the parent repo and describe the wrong tree
                state.other_worktrees += 1
            elif not (row["status"] == TaskStatus.DONE or row["stage"] == Stage.DONE):
                state.resumable_worktrees += 1
            else:
                head = _git(entry, "rev-parse", "HEAD").stdout.strip()
                dirty = _git(entry, "status", "--porcelain", "--untracked-files=all").stdout.strip()
                if not head or dirty:
                    state.other_worktrees += 1
                elif _is_ancestor(project, head, base_ref):
                    state.cleanable_worktrees += 1
                else:
                    state.done_unmerged_worktrees += 1

    def collect() -> RepoState:
        state = RepoState(main_ref=short_main, main_sha=_rev(project, base_ref), origin_sha=_rev(project, f"refs/remotes/{remote}/{branch}"))
        if state.main_sha and state.origin_sha:
            counts = _git(project, "rev-list", "--left-right", "--count", f"{state.main_sha}...{state.origin_sha}").stdout.split()
            if len(counts) == 2:
                state.ahead, state.behind = int(counts[0]), int(counts[1])
                state.relation = (
                    "in_sync" if not (state.ahead or state.behind) else "ahead" if not state.behind else "behind" if not state.ahead else "diverged"
                )
        state.active_claims = int(store.conn.execute("SELECT COUNT(*) FROM claims WHERE active=1").fetchone()[0])
        state.running_executions = len(list(store.running_executions()))
        worktree_census(state)
        return state

    return collect


def gh_pr_state(project: Path) -> Callable[[str], str | None]:
    """The PR's state as GitHub reports it (OPEN, MERGED, CLOSED); None when it cannot be read."""

    def state(url: str) -> str | None:
        proc = subprocess.run(["gh", "pr", "view", url, "--json", "state", "--jq", ".state"], cwd=project, capture_output=True, text=True, check=False)
        return proc.stdout.strip() or None if proc.returncode == 0 else None

    return state


def branch_tip(project: Path, ref: str) -> Callable[[], str | None]:
    """The commit a ref points at right now (None when it does not resolve). Cheap: one `git rev-parse`."""
    return lambda: _rev(project, ref)


def store_leftover(store: Store) -> Callable[[str], tuple[int, int]]:
    """(RUNNING executions, active claims) a task still has: after its runner exited both must be zero."""

    def leftover(task_id: str) -> tuple[int, int]:
        running = sum(1 for row in store.running_executions() if str(row["task_id"]) == task_id)
        return running, 1 if store.has_active_claim(task_id) else 0

    return leftover
