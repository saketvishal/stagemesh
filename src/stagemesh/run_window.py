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

Coding and verification have separate process capacities. The survivor-wave
engine continuously admits ready work and yields at bounded stage boundaries;
automatic landing remains serialized after the current workers quiesce.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .audit import record_audit
from .block_kinds import classify_block, reenter_blocked_task, reentry_refusal, runtime_fingerprint
from .concurrency import contract_conflict
from .config import StageMeshConfig
from .contracts import ContractError
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .operator_actions import recover_stale
from .persistence import Store
from .queue_control import QueueStopControl
from .queue_visibility import queue_control_report
from .readiness import PrLister, build_readiness, gh_open_wave_prs
from .recovery_decision import STEP_BUDGET_EXHAUSTED, RecoveryAction, RecoveryContext, decide
from .runtime_freshness import RuntimeFreshness, check_runtime
from .scheduling import Scheduler
from .delivery_followup import register_pending_follow_ups, repository_of
from .delivery_state import externally_blocked, record_external_block, record_landing_result
from .efficiency import BudgetPolicy, check_window_budget
from .follow_through import consolidate_blockers
from .step_progress import made_progress, progress_state
from .survivor_wave import (
    INTEGRATION_LOCAL,
    INTEGRATION_PR,
    STAGING_REF,
    RepoState,
    TaskRecord,
    WaveHooks,
    WaveReport,
    git_repo_state,
    is_task_pr_branch,
    load_wave_contract,
    ordered_candidate_records,
    prepare_staging_ref,
    run_wave,
    skip_reason,
)
from .task_selection import contract_state
from .auto_plan import outside_label_selector
from .window_journal import record_finished, record_refused, record_started, record_task_context

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
    verification_concurrency: int = 2
    max_steps_per_task: int = 8
    max_tasks: int = 10
    max_continuations: int = 3  # extra runs a task gets after hitting the step cap, while each run still made progress
    integration_mode: str = INTEGRATION_PR
    open_prs: bool = True
    advance_baselines: bool = True
    auto_merge_ff: bool = False
    required_checks: tuple[str, ...] = ()
    merge_policy: str = "hosted"
    check_app_id: int | None = None
    strict_live: bool = False
    allow_stale_runtime: bool = False
    tasks: list[str] | None = None  # None: pick every eligible task; a list restricts the window to exactly those
    providers: tuple[str, ...] = ()  # empty: project pools; otherwise this run's selected enabled-provider subset
    required_label: str | None = None
    scope_id: str | None = None
    ci_mode: str = "immediate"
    batch_ci: bool = False
    report_path: Path | None = None
    # Explicit authorization to arm ONE scheduled follow-up per delivery this window leaves pending (publication deferred, or a
    # published PR waiting for hosted checks). Its scope is exactly this window's own: publish if PRs are enabled, merge only if
    # automatic merging was requested. Off unless asked for.
    follow_up: bool = False
    follow_up_interval_seconds: int = 600
    follow_up_ttl_seconds: int = 86400

    def __post_init__(self) -> None:
        if self.ci_mode == "batch":
            self.batch_ci = True
        elif self.batch_ci:
            self.ci_mode = "batch"


def resolve_merge_defaults(config: StageMeshConfig, args: Any) -> dict[str, Any]:
    """Explicit command options override reviewed project defaults; required checks accumulate."""
    policy = getattr(args, "merge_policy", None)
    manual = getattr(args, "pr_only", False)
    if manual and policy is not None:
        raise ValueError("--pr-only cannot be combined with --merge-policy")
    mode = ("pr_only" if manual else policy or
            ("hosted" if getattr(args, "auto_merge_ff", False) else config.merging.mode))
    automatic = mode != "pr_only"
    checks = tuple(dict.fromkeys((*config.merging.required_checks,
        *(c.strip() for c in (getattr(args, "required_checks", "") or "").split(",") if c.strip()))))
    concurrency = args.concurrency if args.concurrency is not None else (1 if automatic else 2)
    return {"auto_merge_ff": automatic, "merge_policy": mode if automatic else "hosted",
            "required_checks": checks, "check_app_id": config.merging.check_app_id, "concurrency": concurrency}


@dataclass
class WindowReport:
    window_id: str
    options: WindowOptions
    started_at: float = 0.0
    finished_at: float = 0.0
    preflight: list[Check] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    reentered: list[dict[str, Any]] = field(default_factory=list)  # recoverable blocks reconsidered this window
    merged_prs: list[dict[str, Any]] = field(default_factory=list)
    landing_results: list[dict[str, Any]] = field(default_factory=list)
    initial_repo_state: RepoState | None = None
    final_repo_state: RepoState | None = None
    continued: list[dict[str, Any]] = field(default_factory=list)  # step-capped tasks resumed from their recorded state
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
    follow_ups: list[dict[str, Any]] = field(default_factory=list)  # deliveries left pending and how each is followed
    ci_wait_seconds: float = 0.0
    duplicate_runs_avoided: int = 0
    batch_results: list[dict[str, Any]] = field(default_factory=list)
    next_command: str = ""
    scope: dict[str, Any] | None = None

    @property
    def tasks(self) -> list[Any]:
        """One record per task: the latest run of it, with how many runs the window gave it."""
        latest: dict[str, Any] = {}
        runs: dict[str, int] = {}
        for wave in self.waves:
            for task in wave.tasks:
                if task.outcome in ("skipped", "not_started") and task.task_id in latest:
                    continue
                runs[task.task_id] = runs.get(task.task_id, 0) + 1
                latest[task.task_id] = task
        return [replace(task, runs=runs[task_id]) for task_id, task in latest.items()]

    @property
    def stopped_early(self) -> bool:
        return self.refused or any(wave.stopped_early for wave in self.waves)

    @property
    def no_progress_reason(self) -> str | None:
        """Set when the window attempted tasks, delivered none, and every attempted task ended NO_PROGRESS."""
        attempted = [task for task in self.tasks if task.outcome not in ("skipped", "not_started")]
        if not attempted or self.merged_prs or any(task.outcome == "done" for task in attempted):
            return None
        if not all(task.outcome == "open" and task.stop_reason == "NO_PROGRESS" for task in attempted):
            return None
        return (f"no task delivered: {len(attempted)} attempted task(s) ended NO_PROGRESS ("
                + ", ".join(task.task_id for task in attempted)
                + "); the provider made no usable change - inspect with `stagemesh doctor`, then rerun")

    @property
    def ok(self) -> bool:
        return not self.refused and not self.interrupted and self.stop_reason is None and all(wave.ok for wave in self.waves) and self.report_error is None

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
            "scope": self.scope,
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
                "required_label": self.options.required_label,
                "scope_id": self.options.scope_id,
                "duration_seconds": self.options.duration_seconds,
                "concurrency": self.options.concurrency,
                "verification_concurrency": self.options.verification_concurrency,
                "max_steps_per_task": self.options.max_steps_per_task,
                "max_tasks": self.options.max_tasks,
                "providers": list(self.options.providers),
                "integration_mode": self.options.integration_mode,
                "open_prs": self.options.open_prs,
                "strict_live": self.options.strict_live,
                "auto_merge_ff": self.options.auto_merge_ff,
                "ci_mode": self.options.ci_mode,
                "batch_ci": self.options.batch_ci,
                "required_checks": list(self.options.required_checks),
                "merge_policy": self.options.merge_policy,
                "check_app_id": self.options.check_app_id,
                "follow_up": self.options.follow_up,
            },
            "follow_ups": self.follow_ups,
            "ci_wait_seconds": self.ci_wait_seconds,
            "duplicate_runs_avoided": self.duplicate_runs_avoided,
            "batch_results": self.batch_results,
            "preflight": [check.to_dict() for check in self.preflight],
            "recovered": self.recovered,
            "selected": self.selected,
            "reentered": self.reentered,
            "continued": self.continued,
            "not_selected": self.not_selected,
            "counts": self.counts(),
            "capacity": [wave.capacity for wave in self.waves if wave.capacity],
            "orphan_events_before": None if first is None else first.orphan_events_before,
            "orphan_events_after": None if last is None else last.orphan_events_after,
            "main_moved": any(wave.main_moved for wave in self.waves) or bool(
                self.initial_repo_state and self.final_repo_state
                and self.initial_repo_state.main_sha != self.final_repo_state.main_sha),
            "merged_prs": self.merged_prs,
            "landing_results": self.landing_results,
            "repo_before": self.initial_repo_state.to_dict() if self.initial_repo_state else (None if first is None or first.repo_before is None else first.repo_before.to_dict()),
            "repo_after": self.final_repo_state.to_dict() if self.final_repo_state else (None if last is None or last.repo_after is None else last.repo_after.to_dict()),
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
    store: Store, *, attempted: set[str], limit: int, only: list[str] | None = None, fingerprint: str | None = None,
    project: Path | None = None, compatible_with: list[str] | None = None, required_label: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Up to `limit` runnable tasks (oldest first) and why the others were left alone.

    Runnable means: OPEN, not DONE, eligible to the scheduler (so not blocked, parked, source-closed, waiting on a dependency or an
    objective root) and with no active claim or running execution. A BLOCKED task is picked only when it is blocked for a recoverable
    operational reason and the runtime or configuration changed since (`fingerprint`, see `block_kinds`); every other block stays blocked
    and is reported with its kind and cause. When `compatible_with` is supplied,
    reserve those tasks' contracts and pick mutually compatible work; conflicts
    remain queued and do not consume the selection limit.
    """
    scheduler = Scheduler(store)
    skipped: dict[str, str] = {}
    candidates = only if only is not None else [str(row["id"]) for row in store.tasks()]
    picked: list[str] = []
    if compatible_with is not None and project is None:
        raise ValueError("conflict-aware selection requires a project")
    contracts = {task_id: load_wave_contract(store, project, task_id) for task_id in (compatible_with or [])}

    def admit(task_id: str) -> bool:
        if compatible_with is not None:
            try:
                contract = load_wave_contract(store, project, task_id)
            except ContractError as exc:
                skipped[task_id] = f"invalid_contract: {exc}"
                return False
            for other_id, other in contracts.items():
                conflict = contract_conflict(contract, other)
                if conflict is not None:
                    skipped[task_id] = f"waiting for compatible slot: conflicts with task {other_id} ({conflict})"
                    return False
            contracts[task_id] = contract
        picked.append(task_id)
        return True

    for task_id in sorted(dict.fromkeys(candidates), key=_task_sort_key):
        if task_id in attempted:
            continue
        task = store.get_task(task_id)
        if task is None:
            source_state = store.source_state("github", task_id)
            source_reason = source_state.get("eligibility_reason") if source_state else None
            skipped[task_id] = (
                f"GitHub issue is not runnable: {source_reason}"
                if source_reason
                else "no such task"
            )
            continue
        if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
            continue  # finished work is not news
        if outside_label_selector(store, task, required_label):
            from .selection_scope import task_problem
            skipped[task_id] = task_problem(store, task) or f"outside label selector {required_label!r}"
            continue
        if task["status"] == TaskStatus.BLOCKED:
            external = scheduler.decision(task_id).reason  # "blocked" unless the work itself is gone (source closed, objective root, ...)
            if external != "blocked":
                skipped[task_id] = external
                continue
            info = classify_block(store, task_id)
            refusal = None if fingerprint is None else reentry_refusal(store, task_id, fingerprint)
            if info is not None and refusal is None:
                reason = skip_reason(store, task_id)  # claims, running executions, objective roots still apply
                if reason is None:
                    if project is not None:
                        state = contract_state(store, project, task_id, auto_plan=True)
                        if state.startswith("unplannable:"):
                            skipped[task_id] = state.removeprefix("unplannable:")
                            continue
                    if admit(task_id) and len(picked) >= limit:
                        break
                else:
                    skipped[task_id] = reason
            elif info is not None:
                skipped[task_id] = f"{info.kind.value}: {info.reason[:300]}" + (f" [{refusal}]" if info.kind.value.endswith("OPERATIONAL") else "")
            elif only is not None:
                skipped[task_id] = "blocked with no recorded cause: not picked automatically (retry it deliberately)"
            continue
        if task["status"] != TaskStatus.OPEN:
            # Parked tasks are never picked. They are only named when the caller asked for them.
            if only is not None:
                skipped[task_id] = f"{str(task['status']).lower()}: not picked automatically (retry it deliberately)"
            continue
        decision = scheduler.decision(task_id)
        reason = None if decision.eligible else decision.reason
        reason = reason or skip_reason(store, task_id)
        if reason is not None:
            skipped[task_id] = reason
            continue
        if project is not None:
            state = contract_state(store, project, task_id, auto_plan=True)
            if state.startswith("unplannable:"):
                skipped[task_id] = state.removeprefix("unplannable:")
                continue
        if admit(task_id) and len(picked) >= limit:
            break
    return picked, skipped


def progress_key(store: Store, task_id: str) -> tuple[Any, ...]:
    """What a run of the task can change: stage, status, latest candidate, evidence recorded and remediation attempts spent."""
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    evidence = store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id=?", (task_id,)).fetchone()[0]
    remediations = store.conn.execute("SELECT COUNT(*) FROM task_remediations WHERE task_id=?", (task_id,)).fetchone()[0]
    return (
        None if task is None else (str(task["stage"]), str(task["status"])),
        None if latest is None else str(latest["sha"]),
        evidence,
        remediations,
    )


def _meaningful_progress(store: Store, task_id: str, before: Any) -> bool:
    """Stage advance or a resolved failure on a new candidate; activity such as more evidence rows or remediation attempts is not progress."""
    return before is not None and made_progress(before, progress_state(store, task_id))[0]


def continuation(store: Store, task_id: str, used: int, limit: int, progressed: bool) -> dict[str, Any]:
    """Should a task that ran out of steps get another run from its recorded state? Decided by the recovery rules, and audited.

    Nothing is restarted: the next run resumes the stage the task is in, with its candidate, evidence lineage and (when review findings
    are open) the remediation context already in the ledger.
    """
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    sha = None if latest is None else str(latest["sha"])

    def status(kind: EvidenceKind) -> str | None:
        if sha is None:
            return None
        row = store.conn.execute(
            "SELECT status FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id, sha, kind)
        ).fetchone()
        return None if row is None else str(row["status"])

    open_findings = 0 if sha is None else len(store.open_findings_for_candidate(task_id, sha))
    state = {
        "task_id": task_id,
        "stage": None if task is None else str(task["stage"]),
        "candidate_sha": sha,
        "validation": status(EvidenceKind.VALIDATION),
        "review": status(EvidenceKind.REVIEW),
        "integration": status(EvidenceKind.INTEGRATION),
        "open_findings": open_findings,
        "attempt": used + 1,
    }
    ctx = RecoveryContext(
        stage=str(state["stage"]),
        failure_category=STEP_BUDGET_EXHAUSTED,
        candidate_sha=sha,
        validation_state=state["validation"],
        review_state=state["review"],
        recovery_attempts=used,
        max_attempts=limit,
    )
    decision = decide(ctx)
    if used >= limit:
        why = f"{used} continuation(s) already used (limit {limit})"
    elif not progressed:
        why = "the run made no progress (same stage, candidate, evidence and remediation count), so another run would repeat it"
    elif decision.action is not RecoveryAction.RETRY_SAME:
        why = decision.reason
    else:
        why = ""
    note = {**state, "continue": not why, "why": why or decision.reason}
    record_audit(store, "task.max_steps_continuation", note)
    return note


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
    detail = (
        "implementation and independent review are both available; "
        f"IMPLEMENT pool: {', '.join(pools[IMPLEMENT])}; REVIEW pool: {', '.join(pools[REVIEW])}"
    )
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
    if options.auto_merge_ff:
        valid = (options.integration_mode == INTEGRATION_PR and options.open_prs and options.concurrency >= 1
                 and bool(options.required_checks) and config.require_independent_review
                 and options.merge_policy in {"hosted", "stagemesh"}
                 and (options.merge_policy != "stagemesh" or (type(options.check_app_id) is int and options.check_app_id > 0)))
        checks.append(Check("auto_merge_ff", valid, f"{options.merge_policy} parallel development with serialized exact-candidate landing and named hosted checks" if valid else "automatic merging requires PR mode, positive concurrency, PR publishing, independent review, named checks and a trusted check app for stagemesh mode"))
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
    if report.options.auto_merge_ff and report.stop_reason:
        return "stagemesh doctor  # fast-forward delivery stopped; published work is retained"
    """The one thing an operator should do next, from what the window found."""
    failed = [check for check in report.preflight if not check.ok and check.fatal]
    if report.refused and failed:
        return failed[0].fix or "stagemesh doctor"
    if report.report_error:
        return "stagemesh doctor  # the report could not be written; fix the --report path and rerun"
    reason = (report.stop_reason or "").lower()
    if report.no_progress_reason:
        return (
            "stagemesh doctor  # every attempted task made no progress; after provider health is ready, rerun exactly: "
            + _resume_command(report.options)
        )
    if "main moved" in reason or "orphan" in reason or "cleanup" in reason or "infrastructure" in reason:
        return "stagemesh doctor  # a shared fault stopped the window: inspect it before running again"
    prs = [t for t in report.tasks if getattr(t, "ready_for_merge", False)]
    if prs or report.open_wave_prs:
        return "review and merge the open wave PRs oldest first (normal merge commit), pull main, then run the window again"
    decisions = [t for t in report.tasks if t.outcome == "blocked_needs_decision"]
    if decisions:
        return "stagemesh doctor  # tasks " + ", ".join(t.task_id for t in decisions) + " need a design decision"
    if any(t.resumable for t in report.tasks):
        return _resume_command(report.options) + "  # unfinished tasks resume where they stopped"
    return "nothing to do: no eligible work left"


def _resume_command(options: WindowOptions) -> str:
    return shlex.join(_resume_argv(options))


def _resume_argv(options: WindowOptions) -> list[str]:
    argv = [
        "stagemesh",
        "run-window",
        "--duration",
        f"{options.duration_seconds:g}s",
        "--concurrency",
        str(options.concurrency),
        "--max-steps-per-task",
        str(options.max_steps_per_task),
        "--max-tasks",
        str(options.max_tasks),
        "--integration",
        options.integration_mode,
    ]
    if options.tasks:
        argv.extend(("--tasks", ",".join(options.tasks)))
    if options.providers:
        argv.extend(("--providers", ",".join(options.providers)))
    if options.scope_id and options.scope_id != "preview":
        argv.extend(("--resume-scope", options.scope_id))
    elif options.required_label:
        argv.extend(("--label", options.required_label))
    if not options.open_prs:
        argv.append("--no-pr")
    if not options.advance_baselines:
        argv.append("--no-advance-baselines")
    if options.strict_live:
        argv.append("--strict-live")
    if options.auto_merge_ff:
        argv.extend(("--merge-policy", options.merge_policy))
        if options.required_checks:
            argv.extend(("--required-checks", ",".join(options.required_checks)))
    else:
        argv.append("--pr-only")
    return argv


def _landing_order(store: Store, records: list[TaskRecord], hooks: WaveHooks) -> list[TaskRecord]:
    """Require a complete owned prefix, including after an interrupted publication."""
    ordered = ordered_candidate_records(records, hooks.is_ancestor)
    covered = {record.task_id: record.integrated_candidate for record in ordered}
    if not ordered:
        return ordered
    base = hooks.main_sha() or hooks.target_sha()
    rows = store.conn.execute(
        "SELECT DISTINCT task_id, candidate_sha FROM evidence WHERE kind=? AND status=?",
        (EvidenceKind.INTEGRATION, EvidenceStatus.PASSED),
    )
    for row in rows:
        task_id, sha = str(row["task_id"]), str(row["candidate_sha"])
        current = covered.get(task_id)
        if current and (sha == current or hooks.is_ancestor(sha, current)):
            continue
        if not any(hooks.is_ancestor(sha, record.integrated_candidate) for record in ordered):
            continue
        if base and hooks.is_ancestor(sha, base):
            continue
        raise RuntimeError(f"candidate contains unlanded task {task_id} ({sha[:12]}) without its own pending PR; recover ancestor publication first")
    return ordered


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
    merge_pr: Callable[[Any, float], dict[str, Any]] | None = None,
    merge_setup_error: str | None = None,
    on_merged: Callable[[str], None] | None = None,
    follow_up_scheduler: Any = None,
    repository: str | None = None,
) -> WindowReport:
    local = options.integration_mode == INTEGRATION_LOCAL
    report = WindowReport(window_id=uuid.uuid4().hex[:8], options=options, started_at=clock())
    deadline = report.started_at + options.duration_seconds
    control = QueueStopControl(store)
    window_wall_start = time.time()  # ledger timestamps are wall-clock even when the window runs on an injected clock
    report.preflight, report.recovered = preflight(
        store, project, config, options, base_ref=base_ref, runtime=runtime, provider_check=provider_check, repo_state=hooks.repo_state,
        checkout_ref=checkout_ref,
    )
    attached_scope = getattr(store, "selection_scope", None)
    if attached_scope is not None:
        mismatch = ((options.scope_id is not None and options.scope_id != attached_scope.id)
            or (options.required_label is not None
                and options.required_label.casefold() != (attached_scope.label or "").casefold()))
        if mismatch:
            report.preflight.append(Check("selection_scope", False, "requested scope differs from the Store's attached scope; nothing started"))
        else:
            options.scope_id, options.required_label = attached_scope.id, attached_scope.label
    if (options.required_label or options.scope_id) and not getattr(store, "selection_scope", None):
        from .selection_scope import ScopeError, attach, refresh, resolve_scope
        try:
            scope = resolve_scope(store, label=options.required_label, resume=options.scope_id)
            attach(store, project, config, scope)
            refresh(store)
            options.required_label, options.scope_id = scope.label, scope.id
        except ScopeError as exc:
            report.preflight.append(Check("selection_scope", False, str(exc)))
    if options.required_label and options.tasks:
        conflicts = [task_id for task_id in options.tasks if store.get_task(task_id) is None
            or outside_label_selector(store, store.get_task(task_id), options.required_label)]
        report.preflight.append(Check("label_selector", not conflicts,
            "explicit tasks match scope" if not conflicts else "explicit task conflicts with label scope: " + ", ".join(conflicts)))
    if options.auto_merge_ff and merge_pr is None:
        report.preflight.append(Check("merge_adapter", False, merge_setup_error or "no fast-forward merge adapter configured"))
    pending_records: list[TaskRecord] = []
    active_landing_recovery = None
    if options.auto_merge_ff and list_prs is not None:
        pending = []
        try:
            if callable(getattr(merge_pr, "preflight", None)):
                policy_error = merge_pr.preflight()
                report.preflight.append(Check("hosted_landing_policy", policy_error is None, policy_error or f"{options.merge_policy} guarded exact-candidate landing policy"))
            pending = list_prs(project)
            active_probe = getattr(merge_pr, "active_recovery", None)
            active_landing_recovery = active_probe() if callable(active_probe) else None
            if active_landing_recovery:
                problem = merge_pr.recovery_preflight(pending)
                if problem:
                    raise RuntimeError(problem)
                if options.tasks is not None and any(
                    record["task_id"] not in options.tasks
                    for record in active_landing_recovery["records"]
                ):
                    raise RuntimeError("active landing recovery is outside selected tasks")
                if any(outside_label_selector(store, store.get_task(record["task_id"]), options.required_label)
                       for record in active_landing_recovery["records"]):
                    raise RuntimeError("active landing recovery is outside selected label scope")
                pending = []  # only the precisely journaled recovery owns these PRs
            if callable(getattr(merge_pr, "pending_records", None)):
                for record in ([] if active_landing_recovery else merge_pr.pending_records()):
                    if not any(pr.get("url") == record.pr_url for pr in pending):
                        latest = store.latest_candidate(record.task_id)
                        if not latest or latest["sha"] != record.integrated_candidate:
                            raise RuntimeError("interrupted landing differs from the latest candidate; reconcile before continuing")
                        pending.append({"url": record.pr_url, "headRefName": f"wave/task-{record.task_id}"})
        except Exception as exc:  # noqa: BLE001 -- preflight failure must still finalize its report
            report.preflight.append(Check("prior_pr_lookup", False, f"{type(exc).__name__}: {exc}"))
        owned = True
        for pr in pending:
            head_ref = str(pr.get("headRefName", ""))
            raw_id = head_ref.removeprefix("wave/task-")
            possible_ids = {raw_id, raw_id.rsplit("-", 1)[0]} if head_ref.startswith("wave/task-") else set()
            owners = []
            for possible_id in possible_ids:
                task = store.get_task(possible_id)
                candidate = store.latest_candidate(possible_id) if task else None
                if candidate and is_task_pr_branch(head_ref, possible_id, str(candidate["sha"])):
                    owners.append((possible_id, task, candidate))
            if len(owners) != 1:
                owned = False
                break
            task_id, task, candidate = owners[0]
            sha = str(candidate["sha"])
            owned = bool(task["status"] == TaskStatus.DONE and task["stage"] == Stage.DONE
                         and (options.tasks is None or task_id in options.tasks)
                         and not outside_label_selector(store, task, options.required_label)
                         and pr.get("url") and all(store.has_evidence(task_id, sha, kind) for kind in
                             (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION)))
            if not owned:
                break
            pending_records.append(TaskRecord(task_id, title=str(task["title"]), outcome="done",
                pr_url=pr["url"], ready_for_merge=True, integrated_candidate=sha))
        if owned:
            try:
                pending_records = _landing_order(store, pending_records, hooks)
                validate = getattr(merge_pr, "validate_records", None)
                if callable(validate) and pending_records:
                    problem = validate(pending_records, deadline)
                    if problem:
                        raise RuntimeError(problem)
            except Exception as exc:
                owned = False
                report.preflight.append(Check("prior_pr_order", False, f"cannot verify pending candidate order: {exc}"))
        report.preflight.append(Check("prior_prs", owned,
            "no prior wave PR, or an owned evidenced candidate chain to resume" if owned else
            "prior PR is ambiguous, unowned, blocked or outside the selected tasks; resolve it before proceeding"))
    if prepare_staging is not None and not active_landing_recovery and not local and all(check.name == "providers" or check.ok or not check.fatal for check in report.preflight):
        try:
            prepare_staging()
            report.preflight.append(Check("staging", True, f"{staging_ref or STAGING_REF} is ready for this window"))
        except RuntimeError as exc:
            report.preflight.append(Check("staging", False, str(exc), fix="merge or delete the old wave/staging branch, then rerun"))
    # Refresh only after every non-provider admission check has passed. Plans and
    # diagnostics use preflight alone and never spend provider calls.
    if (not options.scope_id or planned_selection(store, options, project)[0]) and provider_check is provider_pool_check and all(
        check.name == "providers" or check.ok or not check.fatal for check in report.preflight
    ):
        from .provider_acceptance import refresh_expired_quota_checks
        from .providers import adapters_from_config

        try:
            refresh_expired_quota_checks(
                store, project, adapters_from_config(config), deadline=deadline,
                should_stop=control.blocks_admission, clock=clock,
            )
            refreshed = provider_check(store, config, strict_live=options.strict_live)
        except Exception as exc:
            refreshed = Check("providers", False, f"provider recheck unavailable: {type(exc).__name__}", fix="stagemesh capacity")
        report.preflight = [refreshed if check.name == "providers" else check for check in report.preflight]
    failed = [check for check in report.preflight if not check.ok and check.fatal]
    if failed:
        report.refused = True
        report.stop_reason = "preflight refused: " + "; ".join(f"{check.name}: {check.detail}" for check in failed)
        record_refused(store, report.window_id, {"stop_reason": report.stop_reason, "checks": [c.to_dict() for c in failed]})
        return _finish(store, project, config, report, list_prs, clock, journal=False)

    record_started(
        store,
        report.window_id,
        {"mode": options.integration_mode, "duration_seconds": options.duration_seconds, "concurrency": options.concurrency, "max_steps": options.max_steps_per_task,
         "scope_id": options.scope_id, "required_label": options.required_label},
    )
    attempted: set[str] = set()
    fingerprint = runtime_fingerprint(project)
    resume: dict[str, int] = {}  # step-capped tasks waiting for another run, and how many they have had
    if options.auto_merge_ff:
        report.initial_repo_state = hooks.repo_state()
    recovered_suffix = False

    def accept_recovery(result):
        nonlocal recovered_suffix
        report.waves.extend(result.get("waves", []))
        report.merged_prs.extend(result.get("merged_prs", []))
        report.landing_results.extend(result.get("landing_results", []))
        for wave in result.get("waves", []):
            for task in wave.tasks:
                if task.task_id not in report.selected:
                    report.selected.append(task.task_id)
                attempted.add(task.task_id)
        report.final_repo_state = hooks.repo_state()
        recovered_suffix = True
        if not result.get("ok"):
            report.stop_reason = "auto-merge recovery stopped: " + str(result.get("reason", "recovery unverified"))
            return False
        return True

    def land(record, suffix):
        if clock() >= deadline:
            report.time_limit_reached = True
            return False
        if control.blocks_admission():
            report.stop_reason = "queue admission " + control.state_label()
            return False
        if not record.ready_for_merge or not record.pr_url:
            report.stop_reason = "auto-merge stopped: completed task has no published PR"
            return False
        blocked = externally_blocked(store, record.task_id)
        if blocked is not None:  # a recorded permission denial is never asked again until an operator clears it
            report.stop_reason = (f"auto-merge stopped: task {record.task_id} needs {blocked.get('needed')} ({str(blocked.get('reason'))[:120]}); "
                                  f"after access is granted run `stagemesh delivery-unblock --task {record.task_id}`")
            return False
        try:
            validate = getattr(merge_pr, "validate_records", None)
            if callable(validate):
                problem = validate([record], deadline)
                if problem:
                    raise RuntimeError(problem)
            merged = merge_pr(record, deadline)
        except Exception as exc:  # noqa: BLE001 -- fail closed on any injected landing failure
            merged = {"merged": False, "reason": f"{type(exc).__name__}: {exc}"}
        report.landing_results.append(merged)
        if "remote_landed" in merged:
            report.final_repo_state = hooks.repo_state()
        if not merged.get("merged"):
            recover = getattr(merge_pr, "recover", None)
            if merged.get("refresh_required") and merged.get("remote_landed") is False and callable(recover):
                if len(attempted | {task.task_id for task in suffix}) > options.max_tasks:
                    report.task_limit_reached = True
                    report.stop_reason = "landing recovery suffix exceeds this window's task budget; published work retained"
                    return False
                try:
                    # Recovery may discover deferred publications beyond this
                    # published suffix; reserve room for earlier attempted work.
                    recovery_options = replace(options, max_tasks=options.max_tasks - len(
                        attempted - {task.task_id for task in suffix}))
                    return accept_recovery(recover(suffix, merged, deadline, hooks, recovery_options))
                except Exception as exc:
                    report.stop_reason = f"auto-merge recovery stopped: {type(exc).__name__}: {exc}"
                    return False
            if merged.get("remote_landed") is True:
                record.landing = "remote_landed_sync_blocked"
            why = str(merged.get("reason", "landing unverified"))
            outcome = record_landing_result(store, record.task_id, record.integrated_candidate, why)
            if outcome["permission_denied"]:  # one blocker, no second route to the same action
                record_external_block(store, record.task_id, record.integrated_candidate, why, "permission to merge this pull request")
                record.landing = "externally_blocked"
                why = (f"permission to merge was denied ({why[:160]}); the PR and its verified head are preserved. Grant access, then "
                       f"`stagemesh delivery-unblock --task {record.task_id}`")
            report.stop_reason = "auto-merge stopped: " + why
            return False
        if "task_id" not in merged:
            merged["task_id"] = record.task_id
        report.merged_prs.append(merged)
        record.pr_state, record.ready_for_merge, record.landing = "MERGED", False, "fast_forward_merged"
        if on_merged is not None:
            try:
                on_merged(record.task_id)  # e.g. close the upstream issue now that the exact head is merged; advisory only
            except Exception as exc:  # noqa: BLE001 - outbound sync never alters landing results, but a failure is visible
                record_audit(store, "source.outbound_after_merge_failed", {"task_id": record.task_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
        report.final_repo_state = hooks.repo_state()
        return True

    def arm_follow_ups() -> None:
        """Hand deliveries this window could not finish to the single follow-up mechanism. Never alters the window's own outcome."""
        if not options.follow_up or local:
            return
        try:
            task_ids = [*report.selected, *(record.task_id for record in pending_records), *(task.task_id for task in report.tasks)]
            report.follow_ups = register_pending_follow_ups(
                store, project, task_ids,
                repository=repository if repository is not None else repository_of(project),
                open_prs=options.open_prs, merge_authorized=options.auto_merge_ff,
                merge_policy=options.merge_policy, required_checks=options.required_checks,
                authorized_by="run-window --follow-up" + (f" --merge-policy {options.merge_policy}" if options.auto_merge_ff else ""),
                window_id=report.window_id, base_argv=_resume_argv(options), base=base_ref.removeprefix("refs/heads/"),
                scheduler=follow_up_scheduler, interval_seconds=options.follow_up_interval_seconds,
                ttl_seconds=options.follow_up_ttl_seconds, window_seconds=min(options.duration_seconds, 1800.0), clock=clock,
                landing_since=window_wall_start,
            )
        except Exception as exc:  # noqa: BLE001 - a follow-up problem is reported, never allowed to hide the window's result
            report.follow_ups = [{"state": "error", "reason": f"{type(exc).__name__}: {exc}"}]

    try:
        if active_landing_recovery:
            accept_recovery(merge_pr.resume_recovery(deadline, hooks, options))
        for index, pending_record in enumerate(pending_records):
            if report.stop_reason:
                break
            if len(attempted) >= options.max_tasks:
                report.task_limit_reached = True
                break
            if not land(pending_record, pending_records[index:]):
                break
            if recovered_suffix:
                break
            report.merged_prs[-1]["resumed"] = True
            attempted.add(pending_record.task_id)
        while report.stop_reason is None:
            from .selection_scope import ScopeError, refresh
            try:
                refresh(store)
            except ScopeError as exc:
                report.stop_reason = str(exc)
                break
            if control.blocks_admission():
                report.stop_reason = "queue admission " + control.state_label()
                break
            if clock() >= deadline:
                report.time_limit_reached = True
                break
            window_budget = check_window_budget(store, window_wall_start, BudgetPolicy.from_config(config.budgets))
            if window_budget is not None:  # a wave boundary: nothing is running, nothing is interrupted
                report.stop_reason = f"provider-call budget reached for this window ({window_budget.detail}; limit {window_budget.limit}); work is preserved"
                break
            room = options.max_tasks - len(attempted) + len(resume)
            # Fill the bounded wave with compatible work, not merely the first
            # N queue entries that the wave would then have to serialize.
            continuing, deferred = select_tasks(
                store, attempted=set(), limit=room, only=list(resume), fingerprint=fingerprint,
                project=project, compatible_with=[], required_label=options.required_label,
            )
            report.not_selected.update(deferred)

            if len(attempted) >= options.max_tasks and not continuing:
                report.task_limit_reached = True
                break
            if room - len(continuing) > 0 and not getattr(store, "selection_scope", None):
                try:
                    from .task_sources import LocalBacklogSource, sync_source, task_sources_from_config
                    backlog = project / ".stagemesh" / "backlog.json"
                    if backlog.is_file():
                        sync_source(store, LocalBacklogSource(backlog).discover(), preserve_existing_dependencies=True)
                    for source in task_sources_from_config(config):
                        sync_source(store, source.discover(), preserve_existing_dependencies=True)
                except Exception:
                    pass
            fresh, skipped = (
                select_tasks(
                    store,
                    attempted=attempted | set(resume),
                    limit=min(room - len(continuing), options.max_tasks - len(attempted)),
                    only=options.tasks,
                    fingerprint=fingerprint,
                    project=project,
                    compatible_with=continuing,
                    required_label=options.required_label,
                )
                if len(attempted) < options.max_tasks and room - len(continuing) > 0
                else ([], {})
            )
            report.not_selected.update(skipped)
            batch = [*continuing, *fresh]
            if not batch:
                break
            for task_id in batch:
                from .selection_scope import record_task
                record_task(store, task_id)
                record_task_context(
                    store,
                    report.window_id,
                    task_id,
                    {
                        "integration_mode": options.integration_mode,
                        "integration_ref": base_ref if local else staging_ref,
                        "open_prs": options.open_prs,
                        "resume_command": _resume_command(options),
                    },
                )
            for task_id in batch:
                report.not_selected.pop(task_id, None)
            for task_id in fresh:  # a recoverable block is reconsidered here, before the wave runs the task
                entry = reenter_blocked_task(store, task_id, fingerprint)
                if entry is not None:
                    report.reentered.append(entry)
                report.not_selected.pop(task_id, None)
            before = {task_id: progress_state(store, task_id) for task_id in batch}

            def admit_ready(*, fresh=fresh, batch=batch, before=before) -> list[str]:
                try:
                    refresh(store)
                except ScopeError as exc:
                    report.stop_reason = str(exc)
                    return []
                remaining = options.max_tasks - len(attempted) - len(fresh)
                if remaining <= 0:
                    return []
                added, reasons = select_tasks(
                    store, attempted=attempted | set(resume) | set(fresh), limit=remaining,
                    only=options.tasks, fingerprint=fingerprint, project=project, required_label=options.required_label,
                )
                report.not_selected.update(reasons)
                if options.auto_merge_ff:
                    # DONE on staging is not remote prerequisite delivery. Keep
                    # those dependents for the next, safely landed frontier.
                    safe = []
                    for task_id in added:
                        waiting = [str(row["depends_on_task_id"]) for row in store.conn.execute(
                            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (task_id,)
                        ) if str(row["depends_on_task_id"]) in batch]
                        if waiting:
                            report.not_selected[task_id] = "waiting for prerequisite landing: " + ", ".join(waiting)
                        else:
                            safe.append(task_id)
                    added = safe
                for task_id in added:
                    record_task(store, task_id)
                    entry = reenter_blocked_task(store, task_id, fingerprint)
                    if entry is not None:
                        report.reentered.append(entry)
                    report.not_selected.pop(task_id, None)
                    before[task_id] = progress_state(store, task_id)
                    record_task_context(
                        store, report.window_id, task_id,
                        {"integration_mode": options.integration_mode,
                         "integration_ref": base_ref if local else staging_ref,
                         "open_prs": options.open_prs, "resume_command": _resume_command(options)},
                    )
                fresh.extend(added)
                batch.extend(added)
                return added

            def admission_problem() -> str | None:
                if report.stop_reason:
                    return report.stop_reason
                verdict = check_window_budget(store, window_wall_start, BudgetPolicy.from_config(config.budgets))
                if verdict is None:
                    return None
                return (f"provider-call budget reached for this window ({verdict.detail}; limit {verdict.limit}); "
                        "work is preserved")


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
                ordered_publication=options.auto_merge_ff or options.batch_ci,
                project=project,
                verification_concurrency=options.verification_concurrency,
                admit_tasks=admit_ready,
                admission_problem=admission_problem,
                ci_mode=options.ci_mode,
                batch_ci=options.batch_ci,
            )
            attempted.update(fresh)
            report.selected.extend(task_id for task_id in batch if task_id not in report.selected)
            report.waves.append(wave)
            for record in wave.tasks:
                resume.pop(record.task_id, None)
                if record.outcome == "open" and record.stop_reason == "MAX_STEPS":
                    count = sum(1 for entry in report.continued if entry["task_id"] == record.task_id)
                    note = continuation(store, record.task_id, count, options.max_continuations, _meaningful_progress(store, record.task_id, before.get(record.task_id)))
                    if note["continue"]:
                        resume[record.task_id] = count + 1
                        report.continued.append(note)
                    else:
                        record.reason = f"{record.reason}; not continued: {note['why']}"
            if wave.stopped_early or not wave.ok:
                report.stop_reason = wave.stop_reason or "the wave reported a violation"
                break
            if options.auto_merge_ff:
                try:
                    ready = _landing_order(store, [record for record in wave.tasks if record.outcome == "done"], hooks)
                    validate = getattr(merge_pr, "validate_records", None)
                    if callable(validate) and ready:
                        problem = validate(ready, deadline)
                        if problem:
                            raise RuntimeError(problem)
                except Exception as exc:
                    report.stop_reason = f"auto-merge stopped: cannot verify candidate order: {exc}"
                    break
                if options.batch_ci and len(ready) > 1:
                    combined = ready[-1]
                    t0 = clock()
                    recovered_suffix = False
                    landed_ok = land(combined, ready)
                    ci_wait = max(0.0, clock() - t0)
                    report.ci_wait_seconds += ci_wait
                    if landed_ok:
                        for intermediate in ready[:-1]:
                            intermediate.pr_state = "MERGED"
                            intermediate.ready_for_merge = False
                            intermediate.landing = "fast_forward_merged"
                            store.add_audit_event("window.fast_forward_merged", {
                                "task_id": intermediate.task_id,
                                "candidate_sha": intermediate.integrated_candidate,
                                "combined_sha": combined.integrated_candidate,
                                "batched": True,
                                "pr": report.merged_prs[-1].get("pr") if report.merged_prs else None,
                            })
                            report.merged_prs.append({
                                "merged": True,
                                "task_id": intermediate.task_id,
                                "batched": True,
                                "candidate_sha": intermediate.integrated_candidate,
                                "combined_sha": combined.integrated_candidate,
                                "pr": report.merged_prs[-1].get("pr") if report.merged_prs else None,
                            })
                        duplicate_avoided = len(ready) - 1
                        report.duplicate_runs_avoided += duplicate_avoided
                        batch_entry = {
                            "batch_id": f"batch-{int(wave.started_at)}",
                            "task_ids": [r.task_id for r in ready],
                            "combined_sha": combined.integrated_candidate,
                            "duplicate_runs_avoided": duplicate_avoided,
                            "ci_wait_seconds": ci_wait,
                            "status": "merged",
                        }
                        report.batch_results.append(batch_entry)
                        store.add_audit_event("batch.integrated", batch_entry)
                    else:
                        blocker = externally_blocked(store, combined.task_id)
                        if blocker is not None:
                            for intermediate in ready[:-1]:
                                record_external_block(store, intermediate.task_id, intermediate.integrated_candidate,
                                                      str(blocker.get("reason") or "batch landing permission denied"),
                                                      str(blocker.get("needed") or "permission to merge this pull request"))
                                intermediate.landing, intermediate.ready_for_merge = "externally_blocked", False
                else:
                    for index, record in enumerate(ready):
                        recovered_suffix = False
                        t0 = clock()
                        landed = land(record, ready[index:])
                        report.ci_wait_seconds += max(0.0, clock() - t0)
                        if not landed:
                            break
                        if recovered_suffix:
                            break
                if report.stop_reason:
                    break
            if wave.time_limit_reached:
                report.time_limit_reached = True
                break
    except BaseException as exc:
        report.interrupted = True
        report.stop_reason = f"interrupted: {type(exc).__name__}"
        arm_follow_ups()
        _finish(store, project, config, report, list_prs, clock, journal=True)
        raise
    arm_follow_ups()
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
    QueueStopControl(store).complete_if_idle()
    report.finished_at = clock()
    if not report.batch_results:
        for wave in report.waves:
            report.duplicate_runs_avoided += wave.duplicate_runs_avoided
            report.ci_wait_seconds += wave.ci_wait_seconds
            report.batch_results.extend(wave.batch_results)
    if report.stop_reason is None:
        report.stop_reason = report.no_progress_reason
    try:
        report.readiness = build_readiness(store, project, config, list_prs=list_prs, strict_live=report.options.strict_live)
    except Exception as exc:  # noqa: BLE001 - the report must still be written
        report.readiness = {"error": f"{type(exc).__name__}: {exc}"}
    prs = (report.readiness or {}).get("wave_prs", {}).get("prs", [])
    mine = {task.pr_url for task in report.tasks if getattr(task, "pr_url", None)}
    report.open_wave_prs = [
        {**pr, "opened_this_window": pr.get("url") in mine} for pr in prs  # already in stack (merge) order
    ]
    report.next_command = next_command(report)
    from .selection_scope import preview
    report.scope = preview(store, project, replace(config.task_selection, required_label=report.options.required_label))
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
    lines = [f"run window {report.window_id}: {verdict} ({minutes:.1f} min of {report.options.duration_seconds / 60:.0f}, mode {report.options.integration_mode}, ci-mode {report.options.ci_mode})"]
    if report.scope:
        lines.append("scope: " + json.dumps(report.scope, sort_keys=True))
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
    if report.waves:
        coding_peak = max(wave.capacity.get("peak_coding", 0) for wave in report.waves)
        verification_peak = max(wave.capacity.get("peak_verification", 0) for wave in report.waves)
        lines.append(f"capacity: coding target {report.options.concurrency}, peak {coding_peak}; "
                     f"verification limit {report.options.verification_concurrency}, peak {verification_peak}")
    counts = report.counts()
    lines.append(f"what ran: {len(report.tasks)} task(s)" + (" - " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) if counts else ""))
    for task in report.tasks:
        landing = {"pr_opened": " -> PR opened", "locally_integrated": " -> integrated locally", "staged_not_published": " -> staged, no PR", "pr_failed": " -> PR NOT opened", "fast_forward_merged": " -> fast-forward merged"}.get(task.landing or "", "")
        lines.append(f"  {task.task_id}: {task.outcome}{landing} - {task.title}")
        if task.reason and task.outcome not in ("done",):
            lines.append(f"      why: {task.reason}")
        if task.pr_url:
            lines.append(f"      {task.pr_url}")
        if task.pr_error:
            lines.append(f"      PR error: {task.pr_error}")
    for entry in report.reentered:
        lines.append(f"  reconsidered blocked task {entry['task_id']} (attempt {entry['attempt']} of {entry['of']}): was blocked because {entry['was_blocked_because']}")
    for entry in report.continued:
        lines.append(f"  resumed {entry['task_id']} after the step cap (run {entry['attempt'] + 1}): stage {entry['stage']}, candidate {str(entry['candidate_sha'])[:7]}")
    if report.not_selected:
        ordered = sorted(report.not_selected.items(), key=lambda kv: _task_sort_key(kv[0]))
        lines.append(
            "left alone: "  # one cause on several tasks is one entry, not a repeated request
            + "; ".join(
                f"{', '.join(entry['tasks'])} ({entry['reason']})"
                for entry in consolidate_blockers(((task_id, str(reason)) for task_id, reason in ordered), exact=True)
            )
        )
    for entry in report.follow_ups:
        if entry.get("state") in ("registered", "existing"):
            where = "scheduled" if entry.get("scheduled") else "NOT scheduled (" + str(entry.get("reason", "")) + ")"
            lines.append(f"  follow-up for {entry['task_id']}: {where}; authorized: {', '.join(entry.get('scope', []))}; ends when delivered, blocked or expired")
        else:
            lines.append(f"  follow-up for {entry.get('task_id', '?')}: {entry.get('state')}: {entry.get('reason', '')}")
    if report.merged_prs:
        lines.append("fast-forward merged: " + ", ".join("#" + str(item["pr"]) for item in report.merged_prs))
    if report.batch_results:
        lines.append(f"batched CI: {report.duplicate_runs_avoided} duplicate run(s) avoided; {report.ci_wait_seconds:.1f}s spent waiting for CI")
        for batch in report.batch_results:
            lines.append(
                f"  batch {batch.get('batch_id', '-')}: {len(batch.get('task_ids', []))} task(s) "
                f"({', '.join(batch.get('task_ids', []))}) combined candidate {str(batch.get('combined_sha', ''))[:7]} -> {batch.get('status', 'complete')}"
            )
    if report.open_wave_prs:
        lines.append("open wave PRs, merge in this order (stacked: each branch carries the ones before it; normal merge commit):")
        lines += [f"  {i}. #{pr['number']} {pr.get('headRefName', '')}{' (this window)' if pr.get('opened_this_window') else ''}: {pr.get('title', '')} {pr.get('url', '')}" for i, pr in enumerate(report.open_wave_prs, 1)]
    else:
        lines.append("open wave PRs: none")
    first, last = (report.waves[0], report.waves[-1]) if report.waves else (None, None)
    if first is not None and last is not None:
        lines.append(f"orphan-recovery events: {first.orphan_events_before} -> {last.orphan_events_after}")
        if last.repo_after is not None:
            after = report.final_repo_state or last.repo_after
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


def planned_selection(store: Store, options: WindowOptions, project: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """What a window would pick right now (`run-window --plan`): the same selection, nothing started."""
    fingerprint = None if project is None else runtime_fingerprint(project)
    return select_tasks(store, attempted=set(), limit=options.max_tasks, only=options.tasks, fingerprint=fingerprint, project=project, required_label=options.required_label)


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
