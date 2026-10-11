"""Bounded survivor wave: run a chosen set of stuck or open tasks end to end with little operator hand-holding.

For each task the wave (1) advances a stale baseline when that cannot discard useful progress, (2) runs the task through the normal
`continue` loop with bounded concurrency, (3) records what happened, and (4) opens a PR for a task that reached DONE locally. It never
merges a PR and never cleans a worktree.

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
    integrated_candidate: str | None = None
    infrastructure_signature: str | None = None

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

    @property
    def ok(self) -> bool:
        return not self.stopped_early and self.orphan_events_after <= self.orphan_events_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "orphan_events_before": self.orphan_events_before,
            "orphan_events_after": self.orphan_events_after,
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
    """A task StageMesh blocked after its remediation budget while the latest review still fails: a person has to decide."""
    latest = store.latest_candidate(task_id)
    if latest is None:
        return None
    sha = str(latest["sha"])
    if latest_evidence_status(store, task_id, sha, EvidenceKind.REVIEW) != "FAILED":
        return None
    open_findings = store.conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id=? AND candidate_sha=? AND status='OPEN'", (task_id, sha)
    ).fetchone()[0]
    return f"review findings on {sha[:7]} were not resolved within the remediation budget ({open_findings} open); needs a decision"


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
    clock: Callable[[], float] = time.time,
) -> WaveReport:
    """Run `task_ids` with at most `concurrency` running at once. The store is only touched from the calling thread."""
    report = WaveReport(started_at=clock(), orphan_events_before=orphan_event_count(store))
    report.orphan_events_after = report.orphan_events_before
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
    for task_id in pending:
        records[task_id].outcome, records[task_id].reason = "not_started", "wave stopped before this task started"
        _snapshot(store, records[task_id], report.started_at)

    if open_prs and hooks.open_pr is not None:
        for record in report.tasks:
            if record.outcome == "done":
                try:
                    record.pr_url = hooks.open_pr(record)
                except Exception as exc:  # noqa: BLE001 - a failed PR must not hide that the task is done
                    record.pr_error = f"{type(exc).__name__}: {exc}"
    report.orphan_events_after = orphan_event_count(store)
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
        decision = blocked_needs_decision(store, record.task_id)
        record.outcome = "blocked_needs_decision" if decision else "blocked"
        record.reason = decision or result.message or "blocked"
    else:
        record.outcome = "open"  # step budget used up or interrupted with the task still progressing
        record.reason = result.message or result.stop_reason


# --- report -----------------------------------------------------------------------------------------------------------------------


def format_report(report: WaveReport) -> str:
    lines = [
        f"survivor wave: {'OK' if report.ok else 'STOPPED' if report.stopped_early else 'ORPHAN EVENTS INCREASED'}"
        + (f" ({report.stop_reason})" if report.stop_reason else ""),
        f"orphan-recovery events: {report.orphan_events_before} -> {report.orphan_events_after}",
    ]
    for task in report.tasks:
        base = "-" if task.baseline_before is None else task.baseline_before[:7]
        if task.baseline_after and task.baseline_after != task.baseline_before:
            base += f" -> {task.baseline_after[:7]}"
        lines.append(
            f"  {task.task_id}: {task.outcome} ({task.final_stage}/{task.final_status}) baseline {base}; "
            f"candidates {', '.join(sha[:7] for sha in task.candidates) or '-'}; validation {task.validation or '-'}, "
            f"review {task.review or '-'}, integration {task.integration or '-'}"
        )
        if task.reason:
            lines.append(f"      {task.reason}")
        if task.pr_url:
            lines.append(f"      PR: {task.pr_url}")
        if task.pr_error:
            lines.append(f"      PR not opened: {task.pr_error}")
    return "\n".join(lines)


# --- real hooks -------------------------------------------------------------------------------------------------------------------


def subprocess_task_runner(project: Path, *, python: str | None = None) -> Callable[[str, int], RunResult]:
    """Run `continue --task N --json` in a child process, so each task gets its own connection and process identity."""
    interpreter = python or sys.executable
    bootstrap = "import sys; from stagemesh.cli import main; sys.exit(main(sys.argv[1:]))"

    def run(task_id: str, max_steps: int) -> RunResult:
        argv = [interpreter, "-c", bootstrap, "--project", str(project), "continue", "--task", task_id, "--max-steps", str(max_steps), "--json"]
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return RunResult("RUNNER_CRASH", (proc.stderr or proc.stdout or "no output").strip()[-300:], proc.returncode)
        if data.get("refused"):
            return RunResult(str(data.get("code") or "REFUSED").upper(), str(data.get("message") or ""), proc.returncode)
        return RunResult(str(data.get("stop_reason") or "UNKNOWN"), str(data.get("message") or ""), proc.returncode)

    return run


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
