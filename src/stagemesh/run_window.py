"""Unattended bounded run window: pick the eligible work, run it in PR mode for a fixed time, stop safely, and say what happened.

`run_window` is the survivor-wave engine wrapped in what unattended use needs and a wave does not:

* automatic task selection (OPEN, eligible, not parked, not blocked, not an objective root, nothing claimed or running);
* a preflight that refuses to start on a stale installed runtime, a damaged ledger, a paused queue, a local `main` that is ahead of or
  diverged from origin, or a provider setup that cannot implement and independently review. Executions whose process is provably
  DEAD are recovered automatically; LIVE or UNKNOWN ones fail closed with the exact operator command;
* a deadline (a clean stop: running tasks finish within their step budget, nothing new starts);
* the wave's stop rules (new orphan events, the same infrastructure failure on two tasks, local `main` moving in PR mode, an execution
  or claim left behind after a runner exited) plus an unwritable report;
* a journal (`window.started` / `window.finished`) so `doctor` can show an interrupted window;
* one operator report: what ran, what completed, which PRs (in merge order, including older wave PRs), what blocked and why, what
  stopped the window, claims and executions left, provider and ledger health, repo state, and the next command.

It never merges. Nothing here talks to a provider: the hooks are injected, as for the wave.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import StageMeshConfig
from .domain import Stage, TaskStatus
from .operator_actions import recover_stale
from .persistence import Store
from .queue_visibility import queue_control_report
from .readiness import PrLister, build_readiness, gh_open_wave_prs
from .runtime_freshness import RuntimeFreshness, check_runtime
from .scheduling import Scheduler
from .survivor_wave import (
    INTEGRATION_LOCAL,
    INTEGRATION_PR,
    STAGING_REF,
    WaveHooks,
    WaveReport,
    git_repo_state,
    prepare_staging_ref,
    run_wave,
    skip_reason,
)
from .window_journal import record_finished, record_refused, record_started

EXIT_OK, EXIT_STOPPED, EXIT_REFUSED, EXIT_REPORT = 0, 1, 2, 3
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """`2h`, `90m`, `45s`, `1.5h` or a bare number of seconds."""
    match = _DURATION.match(str(text))
    if not match or float(match.group(1)) <= 0:
        raise ValueError(f"duration must look like 2h, 90m or 45s, got {text!r}")
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True  # a failed fatal check refuses to start the window
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class WindowOptions:
    duration_seconds: float = 7200.0
    concurrency: int = 2
    max_steps_per_task: int = 8
    max_tasks: int = 10
    integration_mode: str = INTEGRATION_PR
    open_prs: bool = True
    advance_baselines: bool = True
    strict_live: bool = False
    allow_stale_runtime: bool = False
    tasks: list[str] | None = None  # None: pick every eligible task; a list restricts the window to exactly those
    report_path: Path | None = None


@dataclass
class WindowReport:
    window_id: str
    options: WindowOptions
    started_at: float = 0.0
    finished_at: float = 0.0
    preflight: list[Check] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    not_selected: dict[str, str] = field(default_factory=dict)
    waves: list[WaveReport] = field(default_factory=list)
    refused: bool = False
    stop_reason: str | None = None
    time_limit_reached: bool = False
    task_limit_reached: bool = False
    report_error: str | None = None
    interrupted: bool = False
    readiness: dict[str, Any] | None = None
    open_wave_prs: list[dict[str, Any]] = field(default_factory=list)
    next_command: str = ""

    @property
    def tasks(self) -> list[Any]:
        return [task for wave in self.waves for task in wave.tasks]

    @property
    def stopped_early(self) -> bool:
        return self.refused or any(wave.stopped_early for wave in self.waves)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.interrupted and all(wave.ok for wave in self.waves) and self.report_error is None

    @property
    def exit_code(self) -> int:
        if self.refused:
            return EXIT_REFUSED
        if self.report_error:
            return EXIT_REPORT
        return EXIT_OK if self.ok else EXIT_STOPPED

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.tasks:
            counts[task.outcome] = counts.get(task.outcome, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        first, last = (self.waves[0], self.waves[-1]) if self.waves else (None, None)
        return {
            "window_id": self.window_id,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "refused": self.refused,
            "interrupted": self.interrupted,
            "stop_reason": self.stop_reason,
            "time_limit_reached": self.time_limit_reached,
            "task_limit_reached": self.task_limit_reached,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "options": {
                "duration_seconds": self.options.duration_seconds,
                "concurrency": self.options.concurrency,
                "max_steps_per_task": self.options.max_steps_per_task,
                "max_tasks": self.options.max_tasks,
                "integration_mode": self.options.integration_mode,
                "open_prs": self.options.open_prs,
                "strict_live": self.options.strict_live,
            },
            "preflight": [check.to_dict() for check in self.preflight],
            "recovered": self.recovered,
            "selected": self.selected,
            "not_selected": self.not_selected,
            "counts": self.counts(),
            "orphan_events_before": None if first is None else first.orphan_events_before,
            "orphan_events_after": None if last is None else last.orphan_events_after,
            "main_moved": any(wave.main_moved for wave in self.waves),
            "repo_before": None if first is None or first.repo_before is None else first.repo_before.to_dict(),
            "repo_after": None if last is None or last.repo_after is None else last.repo_after.to_dict(),
            "tasks": [task.to_dict() for task in self.tasks],
            "open_wave_prs": self.open_wave_prs,
            "readiness": self.readiness,
            "report_error": self.report_error,
            "next_command": self.next_command,
        }


# --- selection ------------------------------------------------------------------------------------------------------------------


def _task_sort_key(task_id: str) -> tuple[int, int, str]:
    return (0, int(task_id), task_id) if task_id.isdigit() else (1, 0, task_id)


def select_tasks(
    store: Store, *, attempted: set[str], limit: int, only: list[str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """Up to `limit` runnable tasks (oldest first) and why the others were left alone.

    Runnable means: OPEN, not DONE, eligible to the scheduler (so not blocked, parked, source-closed, waiting on a dependency or an
    objective root) and with no active claim or running execution. Blocked and design-decision tasks are never picked.
    """
    scheduler = Scheduler(store)
    skipped: dict[str, str] = {}
    candidates = only if only is not None else [str(row["id"]) for row in store.tasks()]
    picked: list[str] = []
    for task_id in sorted(dict.fromkeys(candidates), key=_task_sort_key):
        if task_id in attempted:
            continue
        task = store.get_task(task_id)
        if task is None:
            skipped[task_id] = "no such task"
            continue
        if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
            continue  # finished work is not news
        if task["status"] != TaskStatus.OPEN:
            # Blocked, parked and design-decision tasks are never picked. They are only named when the caller asked for them.
            if only is not None:
                skipped[task_id] = f"{str(task['status']).lower()}: not picked automatically (retry it deliberately)"
            continue
        decision = scheduler.decision(task_id)
        reason = None if decision.eligible else decision.reason
        reason = reason or skip_reason(store, task_id)
        if reason is not None:
            skipped[task_id] = reason
            continue
        picked.append(task_id)
        if len(picked) >= limit:
            break
    return picked, skipped


# --- preflight ------------------------------------------------------------------------------------------------------------------


def provider_pool_check(store: Store, config: StageMeshConfig, *, strict_live: bool) -> Check:
    """Can the configured providers implement a task AND review it independently? (cooldowns are reported, not fatal on their own)."""
    from .provider_pool import IMPLEMENT, REVIEW, ProviderPool, default_pools
    from .providers import adapters_from_config

    try:
        adapters = adapters_from_config(config)
        stage_caps = {IMPLEMENT: "code", REVIEW: "review"}
        capable = {stage: {a.name for a in adapters if cap in a.capabilities} for stage, cap in stage_caps.items()}
        priorities = {name: spec.priority for name, spec in config.provider_specs.items() if spec.priority is not None}
        pools = default_pools(
            sorted(a.name for a in adapters),
            config.stage_routes,
            config.provider_pools,
            config.single_agent_provider,
            config.routing_mode,
            capable=capable,
            priorities=priorities,
        )
        pool = ProviderPool(
            adapters,
            pools,
            require_independent=config.require_independent_review,
            cooldown_seconds=config.provider_failure_cooldown_seconds,
            policy=config.provider_selection_policy,
            weights=config.provider_weights,
            priorities=priorities,
            require_live_acceptance=True,
            block_unproven=strict_live,
            live_acceptance_ttl_seconds=config.live_acceptance_ttl_seconds,
        )
        ok, message, impl, review = pool.preflight(store)
    except Exception as exc:  # noqa: BLE001 - a broken provider setup is a refusal with its reason, not a traceback
        return Check("providers", False, f"provider setup is invalid: {type(exc).__name__}: {exc}", fix="stagemesh config")
    if not ok:
        return Check("providers", False, message, fix="stagemesh capacity")
    cooling = [v.provider for v in impl + review if not v.eligible and "cooldown" in v.reason]
    detail = "implementation and independent review are both available"
    if cooling:
        detail += f"; cooling down (the others carry the work): {', '.join(sorted(set(cooling)))}"
    return Check("providers", True, detail)


def preflight(
    store: Store,
    project: Path,
    config: StageMeshConfig,
    options: WindowOptions,
    *,
    base_ref: str,
    runtime: RuntimeFreshness | None = None,
    provider_check: Callable[..., Check] = provider_pool_check,
    repo_state: Callable[[], Any] | None = None,
    recover: bool = True,
    checkout_ref: Callable[[], str | None] | None = None,
) -> tuple[list[Check], list[dict[str, Any]]]:
    """Everything that must be true before unattended work starts. Returns (checks, executions auto-recovered)."""
    checks: list[Check] = []
    recovered: list[dict[str, Any]] = []

    fresh = runtime or check_runtime(project)
    if fresh.stale:
        detail = f"installed CLI is stale: it lacks {', '.join(fresh.missing_from_installed)}"
        fix = fresh.refresh_command or "python scripts/bootstrap_stagemesh.py --no-queue-config"
        checks.append(Check("runtime", options.allow_stale_runtime, detail + (" (allowed by --allow-stale-runtime)" if options.allow_stale_runtime else ""), fix=fix))
    else:
        checks.append(Check("runtime", True, "installed CLI has every command in source"))

    try:
        integrity = str(store.conn.execute("PRAGMA quick_check").fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        integrity = f"{type(exc).__name__}: {exc}"
    checks.append(Check("ledger", integrity == "ok", "ledger integrity check passed" if integrity == "ok" else f"ledger integrity check failed: {integrity}",
                        fix="restore .stagemesh/stagemesh.sqlite3 from a backup; do not edit it by hand"))

    control = queue_control_report(store)
    checks.append(
        Check("queue", control["state"] == "running", f"queue admission is {control['state']}",
              fix="stagemesh queue-control resume" if control["state"] != "running" else "")
    )

    # Stale executions: DEAD ones are recovered (provably safe); LIVE and UNKNOWN ones fail closed.
    active = list(control["active_executions"])
    for task_id in sorted({e["task_id"] for e in active if e["process_state"] == "DEAD"}) if recover else []:
        for action in recover_stale(store, task_id):
            recovered.append({"task_id": task_id, **action.to_dict()})
    still = [e for e in queue_control_report(store)["active_executions"]]
    if still:
        lines = []
        for e in still:
            if e["process_state"] == "UNKNOWN":
                lines.append(
                    f"execution {e['id']} (task {e['task_id']}) has an unknown process identity; if you have checked it is dead run: "
                    f"stagemesh recover-stale --task {e['task_id']} --release-unknown --execution {e['id']} --reason '<what you checked>'"
                )
            else:
                lines.append(f"execution {e['id']} (task {e['task_id']}) is {e['process_state']}: wait for it or stop it, then rerun")
        checks.append(Check("executions", False, "; ".join(lines), fix="stagemesh doctor"))
    else:
        checks.append(Check("executions", True, f"no running executions ({len(recovered)} dead one(s) recovered)" if recovered else "no running executions"))
    claims = int(store.conn.execute("SELECT COUNT(*) FROM claims WHERE active=1").fetchone()[0])
    checks.append(Check("claims", claims == 0, "no active claims" if claims == 0 else f"{claims} active claim(s) with nothing running",
                        fix="stagemesh doctor; stagemesh recover-stale --task <id>"))

    if options.integration_mode == INTEGRATION_PR and checkout_ref is not None:
        # New task worktrees are created from the checkout's HEAD. On any other branch the task commits would be stacked on that
        # branch's unmerged commits and the PR branches would carry them.
        current = checkout_ref()
        checks.append(
            Check(
                "checkout",
                current == base_ref,
                f"the project checkout is on {current or 'a detached HEAD'}"
                + (f", not {base_ref}: task branches start from the checked-out commit, so PRs would carry its unmerged commits" if current != base_ref else f" ({base_ref})"),
                fix=f"git switch {base_ref.removeprefix('refs/heads/')}",
            )
        )
    if options.integration_mode == INTEGRATION_PR:
        state = repo_state() if repo_state is not None else None
        if state is None:
            checks.append(Check("repo", False, f"cannot read local {base_ref} against origin", fix="git fetch origin"))
        elif state.relation in ("ahead", "diverged"):
            checks.append(Check("repo", False, f"local {state.main_ref} is {state.relation} origin (ahead {state.ahead}, behind {state.behind}); PR mode requires it to match",
                                fix="reconcile local main with origin/main first"))
        else:
            checks.append(Check("repo", True, f"local {state.main_ref} vs origin: {state.relation} (ahead {state.ahead}, behind {state.behind})"
                                + ("; consider git pull --ff-only" if state.behind else ""), fatal=False))
    checks.append(provider_check(store, config, strict_live=options.strict_live))

    path = options.report_path
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
            checks.append(Check("report", True, f"report will be written to {path}"))
        except OSError as exc:
            checks.append(Check("report", False, f"cannot write the report to {path}: {exc}", fix="choose a writable --report path"))
    return checks, recovered


# --- the window -----------------------------------------------------------------------------------------------------------------


def next_command(report: WindowReport) -> str:
    """The one thing an operator should do next, from what the window found."""
    failed = [check for check in report.preflight if not check.ok and check.fatal]
    if report.refused and failed:
        return failed[0].fix or "stagemesh doctor"
    if report.report_error:
        return "stagemesh doctor  # the report could not be written; fix the --report path and rerun"
    reason = (report.stop_reason or "").lower()
    if "main moved" in reason or "orphan" in reason or "cleanup" in reason or "infrastructure" in reason:
        return "stagemesh doctor  # a shared fault stopped the window: inspect it before running again"
    prs = [t for t in report.tasks if getattr(t, "ready_for_merge", False)]
    if prs or report.open_wave_prs:
        return "review and merge the open wave PRs oldest first (normal merge commit), pull main, then run the window again"
    decisions = [t for t in report.tasks if t.outcome == "blocked_needs_decision"]
    if decisions:
        return "stagemesh doctor  # tasks " + ", ".join(t.task_id for t in decisions) + " need a design decision"
    if any(t.resumable for t in report.tasks):
        return "stagemesh run-window  # unfinished tasks resume where they stopped"
    return "nothing to do: no eligible work left"


def run_window(
    store: Store,
    project: Path,
    config: StageMeshConfig,
    options: WindowOptions,
    hooks: WaveHooks,
    *,
    base_ref: str,
    staging_ref: str | None = None,
    runtime: RuntimeFreshness | None = None,
    list_prs: PrLister | None = gh_open_wave_prs,
    provider_check: Callable[..., Check] = provider_pool_check,
    prepare_staging: Callable[[], object] | None = None,
    checkout_ref: Callable[[], str | None] | None = None,
    clock: Callable[[], float] = time.time,
) -> WindowReport:
    local = options.integration_mode == INTEGRATION_LOCAL
    report = WindowReport(window_id=uuid.uuid4().hex[:8], options=options, started_at=clock())
    deadline = report.started_at + options.duration_seconds

    report.preflight, report.recovered = preflight(
        store, project, config, options, base_ref=base_ref, runtime=runtime, provider_check=provider_check, repo_state=hooks.repo_state,
        checkout_ref=checkout_ref,
    )
    if prepare_staging is not None and not local and all(check.ok or not check.fatal for check in report.preflight):
        try:
            prepare_staging()
            report.preflight.append(Check("staging", True, f"{staging_ref or STAGING_REF} is ready for this window"))
        except RuntimeError as exc:
            report.preflight.append(Check("staging", False, str(exc), fix="merge or delete the old wave/staging branch, then rerun"))
    failed = [check for check in report.preflight if not check.ok and check.fatal]
    if failed:
        report.refused = True
        report.stop_reason = "preflight refused: " + "; ".join(f"{check.name}: {check.detail}" for check in failed)
        record_refused(store, report.window_id, {"stop_reason": report.stop_reason, "checks": [c.to_dict() for c in failed]})
        return _finish(store, project, config, report, list_prs, clock, journal=False)

    record_started(
        store,
        report.window_id,
        {"mode": options.integration_mode, "duration_seconds": options.duration_seconds, "concurrency": options.concurrency, "max_steps": options.max_steps_per_task},
    )
    attempted: set[str] = set()
    try:
        while True:
            if clock() >= deadline:
                report.time_limit_reached = True
                break
            if len(attempted) >= options.max_tasks:
                report.task_limit_reached = True
                break
            batch, skipped = select_tasks(
                store, attempted=attempted, limit=min(options.concurrency * 2, options.max_tasks - len(attempted)), only=options.tasks
            )
            report.not_selected.update(skipped)
            if not batch:
                break
            wave = run_wave(
                store,
                batch,
                hooks,
                concurrency=options.concurrency,
                max_steps=options.max_steps_per_task,
                advance_baselines=options.advance_baselines,
                unblock_blocked=False,
                open_prs=options.open_prs,
                integration_mode=options.integration_mode,
                staging_ref=None if local else staging_ref,
                deadline=deadline,
                clock=clock,
            )
            attempted.update(batch)
            report.selected.extend(batch)
            report.waves.append(wave)
            if wave.stopped_early or not wave.ok:
                report.stop_reason = wave.stop_reason or "the wave reported a violation"
                break
            if wave.time_limit_reached:
                report.time_limit_reached = True
                break
    except BaseException as exc:
        report.interrupted = True
        report.stop_reason = f"interrupted: {type(exc).__name__}"
        _finish(store, project, config, report, list_prs, clock, journal=True)
        raise
    return _finish(store, project, config, report, list_prs, clock, journal=True)


def _finish(
    store: Store,
    project: Path,
    config: StageMeshConfig,
    report: WindowReport,
    list_prs: PrLister | None,
    clock: Callable[[], float],
    *,
    journal: bool,
) -> WindowReport:
    report.finished_at = clock()
    try:
        report.readiness = build_readiness(store, project, config, list_prs=list_prs, strict_live=report.options.strict_live)
    except Exception as exc:  # noqa: BLE001 - the report must still be written
        report.readiness = {"error": f"{type(exc).__name__}: {exc}"}
    prs = (report.readiness or {}).get("wave_prs", {}).get("prs", [])
    mine = {task.pr_url for task in report.tasks if getattr(task, "pr_url", None)}
    report.open_wave_prs = [
        {**pr, "opened_this_window": pr.get("url") in mine} for pr in sorted(prs, key=lambda pr: int(pr.get("number", 0)))
    ]
    report.next_command = next_command(report)
    path = report.options.report_path
    if path is not None:
        try:
            path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str), encoding="utf-8")
        except OSError as exc:
            report.report_error = f"cannot write the report to {path}: {exc}"
            report.stop_reason = report.stop_reason or report.report_error
    if journal:
        record_finished(
            store,
            report.window_id,
            {
                "ok": report.ok,
                "stop_reason": report.stop_reason,
                "time_limit_reached": report.time_limit_reached,
                "counts": report.counts(),
                "prs": [task.pr_url for task in report.tasks if getattr(task, "pr_url", None)],
                "report": None if path is None else str(path),
            },
        )
    return report


# --- the report -----------------------------------------------------------------------------------------------------------------


def format_window_report(report: WindowReport) -> str:
    verdict = "REFUSED TO START" if report.refused else "OK" if report.ok else "STOPPED"
    minutes = max(0.0, report.finished_at - report.started_at) / 60
    lines = [f"run window {report.window_id}: {verdict} ({minutes:.1f} min of {report.options.duration_seconds / 60:.0f}, mode {report.options.integration_mode})"]
    if report.stop_reason:
        lines.append(f"  stopped by: {report.stop_reason}")
    elif report.time_limit_reached:
        lines.append("  ended: time budget reached (clean stop)")
    elif report.task_limit_reached:
        lines.append(f"  ended: task limit ({report.options.max_tasks}) reached")
    else:
        lines.append("  ended: no eligible work left")
    lines.append("preflight:")
    lines += [f"  [{'ok' if c.ok else 'FAIL' if c.fatal else 'warn'}] {c.name}: {c.detail}" for c in report.preflight]
    if report.recovered:
        lines.append("recovered dead executions: " + ", ".join(f"{r['execution_id']} (task {r['task_id']}: {r['action']})" for r in report.recovered))
    counts = report.counts()
    lines.append(f"what ran: {len(report.tasks)} task(s)" + (" - " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) if counts else ""))
    for task in report.tasks:
        landing = {"pr_opened": " -> PR opened", "locally_integrated": " -> integrated locally", "staged_not_published": " -> staged, no PR", "pr_failed": " -> PR NOT opened"}.get(task.landing or "", "")
        lines.append(f"  {task.task_id}: {task.outcome}{landing} - {task.title}")
        if task.reason and task.outcome not in ("done",):
            lines.append(f"      why: {task.reason}")
        if task.pr_url:
            lines.append(f"      {task.pr_url}")
        if task.pr_error:
            lines.append(f"      PR error: {task.pr_error}")
    if report.not_selected:
        lines.append("left alone: " + "; ".join(f"{k} ({v})" for k, v in sorted(report.not_selected.items(), key=lambda kv: _task_sort_key(kv[0]))))
    if report.open_wave_prs:
        lines.append("open wave PRs, merge in this order (oldest first, normal merge commit):")
        lines += [f"  #{pr['number']} {pr.get('headRefName', '')}{' (this window)' if pr.get('opened_this_window') else ''}: {pr.get('title', '')} {pr.get('url', '')}" for pr in report.open_wave_prs]
    else:
        lines.append("open wave PRs: none")
    first, last = (report.waves[0], report.waves[-1]) if report.waves else (None, None)
    if first is not None and last is not None:
        lines.append(f"orphan-recovery events: {first.orphan_events_before} -> {last.orphan_events_after}")
        if last.repo_after is not None:
            after = last.repo_after
            lines.append(f"repo: local {after.main_ref} {(after.main_sha or '-')[:7]} vs origin {(after.origin_sha or '-')[:7]}: {after.relation}; main moved: {'YES' if report.to_dict()['main_moved'] else 'no'}")
    ready = report.readiness or {}
    if ready.get("activity"):
        act = ready["activity"]
        lines.append(f"left behind: {act['running_executions']} running execution(s), {act['active_claims']} active claim(s)")
    if ready.get("providers"):
        lines.append("provider health:")
        for item in ready["providers"]:
            cooldown = item["cooldown"]
            state = "ok" if cooldown is None else f"COOLING DOWN ({cooldown['failure_class']})"
            lines.append(f"  {item['provider']}: {state}; command {'available' if item['command_available'] else 'NOT FOUND' if item['command_available'] is False else 'unknown'}; live check {item['live_acceptance']['state']}")
    if ready.get("ledger"):
        led = ready["ledger"]
        lines.append(
            f"ledger: {led['done']} done; {led['retired_closed']} retired; {led['retired_ineligible']} parked; "
            f"{led['open']} open, {led['blocked']} blocked, {led['running']} running"
        )
    if ready.get("needs_decision"):
        lines.append("needs a design decision: " + "; ".join(f"{d['task_id']} {d['title']}" for d in ready["needs_decision"]))
    if report.report_error:
        lines.append(f"REPORT ERROR: {report.report_error}")
    lines.append(f"next: {report.next_command}")
    return "\n".join(lines)


def planned_selection(store: Store, options: WindowOptions) -> tuple[list[str], dict[str, str]]:
    """What a window would pick right now (`run-window --plan`): the same selection, nothing started."""
    return select_tasks(store, attempted=set(), limit=options.max_tasks, only=options.tasks)


__all__ = [
    "STAGING_REF",
    "Check",
    "WindowOptions",
    "WindowReport",
    "format_window_report",
    "git_repo_state",
    "next_command",
    "parse_duration",
    "planned_selection",
    "preflight",
    "prepare_staging_ref",
    "provider_pool_check",
    "run_window",
    "select_tasks",
]
