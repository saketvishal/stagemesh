from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit import record_audit
from .auto_plan import AutoPlanError, create_contract, refresh_stale_generated_contract, task_labels
from .blocked_recovery import auto_retry_blocked_provider_failures, exhaustion_message
from .config import AdverseReviewPolicy, ConfigValidationError, TaskSelectionConfig, load_config
from .contract_binding import contract_for_candidate
from .contract_check import contract_section_sizes
from .contract_feasibility import check_contract_feasibility
from .contracts import (
    ContractError,
    bind_contract,
    canonical_contract_json,
    contract_admission_problem,
    parse_contract,
    task_contract_path,
)
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .delivery_state import task_delivery
from .diagnosis import format_findings
from .domain import EvidenceKind, ExecutionKind, Stage, TaskStatus
from .efficiency import BUDGET_EXHAUSTED, BudgetPolicy, block_reason, check_task_budget
from .git import GitWorkspace
from .objective_roots import objective_root_reason
from .observability import health
from .operator_actions import recover_stale, task_details
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope
from .recovery import (
    auto_rebaseline_blocked_stale_baselines,
    auto_rebind_blocked_missing_validation_gates,
    auto_reintegrate_blocked_runtime_failures,
    auto_revalidate_blocked_validation_gates,
)
from .review import adverse_gate
from .scheduling import Scheduler
from .spec_planning import evaluate_planning_gate, load_planning_artifacts
from .stage_dispatch import stage_dispatch_refusal
from .step_progress import (
    STEP_CAP_STALL_LIMIT,
    ProgressState,
    progress_state,
    record_step_cap,
    unproductive_step_caps,
)
from .task_selection import Candidate, Selection, SelectionRefusal, contract_state, has_exact_label, select_next_task
from .timing import execution_timings, format_duration, step_duration
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION
from .workspaces import task_workspace, worktree_root

# Task-local backlog problems: another task's failed execution or BLOCKED status says nothing about the selected task.
# The selected task's own BLOCKED status is refused explicitly in run_ready. Global hazards (unknown/stale executions) stay fatal.
_IGNORED_PROBLEMS = frozenset({"current_failed_executions", "blocked_tasks"})


class RunReadyRefusal(Exception):
    def __init__(self, reason: str, message: str, **detail: Any):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


@dataclass
class RunSummary:
    started: bool
    stop_reason: str
    task_id: str | None = None
    message: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    recovered: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] | None = None
    selection: dict[str, Any] = field(default_factory=dict)
    auto_plan: dict[str, Any] = field(default_factory=lambda: {"occurred": False, "reused_existing": False, "events": []})
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == "DONE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "stop_reason": self.stop_reason,
            "succeeded": self.succeeded,
            "task_id": self.task_id,
            "message": self.message,
            "steps_run": len(self.steps),
            "steps": self.steps,
            "recovered": self.recovered,
            "final": self.final,
            "selection": self.selection,
            "auto_plan": self.auto_plan,
            **({"detail": self.detail} if self.detail else {}),
        }


def current_problems(store: Store) -> tuple[str, ...]:
    report = health(store)
    # Inspection visibility is task-local. Preserve the pre-existing admission
    # policy: unknown parent identity alone does not stop unrelated work, while
    # a provably dead parent remains a global stale-parent hazard.
    problems = tuple(problem for problem in report.current_problems
                     if problem not in _IGNORED_PROBLEMS and problem not in {"unresolved_validation_trees", "unresolved_process_trees"})
    if (report.dead_validation_parent_count or report.dead_process_tree_root_count) and "stale_running_executions" not in problems:
        problems += ("stale_running_executions",)
    return problems


def _recover_dead(store: Store, task_id: str, *, own_task: bool = True) -> list[dict[str, Any]]:
    """Release what is provably dead for one task.

    `own_task` is True only when the caller is about to drive `task_id` itself. A built-in stage row with no recorded owner
    can then be fenced, because this runner is the one that would have started it. Sweeping other tasks (`own_task=False`) never
    touches such a row: with no owner identity it could belong to a live sibling runner.
    """
    recovered = [
        {"task_id": task_id, **action.to_dict()}
        for action in recover_stale(store, task_id)
        if action.action == "RELEASED"
    ]
    recovered.extend(_recover_detached_builtin_executions(store, task_id, include_unidentified=own_task))
    return recovered


def _recover_all_dead(store: Store) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    for row in store.tasks():
        recovered.extend(_recover_dead(store, str(row["id"]), own_task=False))
    return recovered


def _recover_detached_builtin_executions(store: Store, task_id: str, *, include_unidentified: bool = True) -> list[dict[str, Any]]:
    """Terminalize built-in stage executions that have no owner identity (rows written before owners were recorded).

    Validation may own child gates: neither an ownerless row nor a dead parent proves tree cleanup, so its durable fence stays.
    Explicit subprocess-tree authority is preserved by the Store backstop; it is never inferred from configurable actors.
    Legacy ownerless review/integration rows retain their existing recovery for the one task this caller is about to drive
    (`include_unidentified`), never in a sweep across tasks where they may belong to a live sibling runner.
    """
    if not include_unidentified:
        return []
    recovered: list[dict[str, Any]] = []
    for execution in [row for row in store.running_executions() if row["task_id"] == task_id]:
        kind = str(execution["kind"])
        if kind == ExecutionKind.IMPLEMENTATION or execution["pid"] is not None:
            continue
        reason = "ORPHANED_BUILTIN_STAGE_EXECUTION"
        if store.mark_orphan_running_execution_failed(str(execution["id"]), reason):
            recovered.append(
                {
                    "task_id": task_id,
                    "execution_id": str(execution["id"]),
                    "kind": kind,
                    "pid": None,
                    "process_state": "DETACHED_BUILTIN",
                    "action": "RELEASED",
                    "reason": reason,
                }
            )
    return recovered


def _require_runnable_task(store: Store, task_id: str) -> None:
    task = store.get_task(task_id)
    if task is None:
        raise RunReadyRefusal("task_not_found", f"task does not exist: {task_id}")
    objective_reason = objective_root_reason(store, task)
    if objective_reason:
        raise RunReadyRefusal(
            "objective_root_not_runnable",
            f"task {task_id} is a GitHub objective root ({objective_reason}); run objective planning/decomposition instead",
            task_id=task_id,
            objective_root_reason=objective_reason,
        )


def choose_task(
    store: Store,
    project: Path,
    requested: str | None,
    policy: TaskSelectionConfig,
    auto_plan: bool,
    chooser: Callable[[list[Candidate]], str | None] | None,
) -> Selection:
    """--task bypasses ranking only; objective-root/history safety still applies."""
    if requested is not None:
        _require_runnable_task(store, requested)
        if policy.required_label:
            task = store.get_task(requested)
            if task is None or not has_exact_label(task_labels(store, task), policy.required_label):
                raise RunReadyRefusal(
                    "label_selector_conflict",
                    f"task {requested} does not carry the active label selector {policy.required_label!r}; "
                    "--task cannot bypass --label",
                    task_id=requested,
                )
        return Selection("explicit", requested, "explicit --task (selection policy bypassed)")
    try:
        return select_next_task(store, project, policy, auto_plan=auto_plan, chooser=chooser)
    except SelectionRefusal as refusal:
        raise RunReadyRefusal(refusal.reason, refusal.message, **refusal.detail) from refusal


def _log_selection(selection: Selection, log: Callable[[str], None] | None) -> None:
    if log is None:
        return
    log(f"task selection ({selection.mode}): task {selection.task_id} - {selection.reason}")
    if selection.mode in {"auto", "chosen"}:
        for rank, candidate in enumerate(selection.candidates[:5], start=1):
            log(f"  candidate {rank}: task {candidate['task_id']} ({candidate['priority'] or 'no priority'}, contract {candidate['contract']})")
    for skip in selection.skipped:
        log(f"  skipped task {skip['task_id']}: {skip['reason']}")


def select_task(store: Store, requested: str | None) -> str:
    if requested is not None:
        _require_runnable_task(store, requested)
        return requested
    scheduler = Scheduler(store)
    eligible = [
        str(row["id"])
        for row in store.tasks()
        if row["status"] == TaskStatus.OPEN and scheduler.decision(row["id"]).eligible
    ]
    if not eligible:
        raise RunReadyRefusal("no_eligible_task", "no eligible OPEN task")
    if len(eligible) > 1:
        raise RunReadyRefusal(
            "multiple_eligible_tasks",
            f"{len(eligible)} eligible tasks ({', '.join(eligible)}); pass --task <id>",
            eligible=eligible,
        )
    return eligible[0]


def _ensure_contract(
    store: Store, project: Path, task_id: str, auto_plan: bool, plan_info: dict[str, Any], note: Callable[[str], None]
) -> None:
    refreshed = refresh_stale_generated_contract(store, project, task_id)
    if refreshed is not None:
        plan_info.update(occurred=True, refreshed_existing=True, **refreshed.to_dict())
        note(f"task {task_id}: refreshed generated contract at {refreshed.path} (gates: {', '.join(refreshed.gates)})")
    if store.task_contract(task_id) is not None:
        state = contract_state(store, project, task_id, auto_plan=False)
        if state != "valid":
            raise RunReadyRefusal("invalid_contract", f"contract for {task_id} is invalid: {state}", task_id=task_id)
    if store.task_contract(task_id) is not None:
        plan_info["reused_existing"] = True
        return  # already frozen for this task
    path = task_contract_path(project, task_id)
    if path is None:
        note(f"task {task_id}: missing change contract detected")
        if not auto_plan:
            raise RunReadyRefusal(
                "missing_contract",
                f"task {task_id} has no change contract (auto-planning disabled by --no-auto-plan)",
                task_id=task_id,
                next_action=f"write .stagemesh/contracts/{task_id}.json or rerun without --no-auto-plan",
            )
        note(f"task {task_id}: auto-planning started (deterministic contract from the task objective)")
        try:
            result = create_contract(store, project, task_id)
        except AutoPlanError as exc:
            raise RunReadyRefusal(
                "auto_plan_failed",
                f"auto-planning failed for task {task_id}: {exc.message}",
                task_id=task_id,
                auto_plan_reason=exc.reason,
                next_action=exc.next_action,
            ) from exc
        plan_info.update(occurred=True, **result.to_dict())
        note(f"task {task_id}: contract created at {result.path} (gates: {', '.join(result.gates)})")
        note(f"task {task_id}: continuing to implementation")
        path = result.path
    else:
        plan_info["reused_existing"] = True
    try:
        contract = parse_contract(json.loads(path.read_text(encoding="utf-8")))
        problem = contract_admission_problem(contract)
        if problem:
            raise ContractError(problem)
        size = len(canonical_contract_json(contract))
        feasibility = check_contract_feasibility(project, contract)
        if not feasibility.feasible:
            raise RunReadyRefusal(
                "contract_policy_conflict", feasibility.message(task_id, contract), task_id=task_id,
                conflicts=[conflict.to_dict() for conflict in feasibility.conflicts],
            )
    except (OSError, ValueError, ContractError) as exc:
        raise RunReadyRefusal("invalid_contract", f"contract for {task_id} is invalid: {exc}", task_id=task_id) from exc
    if size > MAX_CANONICAL_CONTRACT_CHARS:
        largest = contract_section_sizes(contract)[:3]
        top = ", ".join(f"{item['section']} ({item['chars']})" for item in largest)
        raise RunReadyRefusal(
            "invalid_contract",
            f"contract for {task_id} is {size} characters canonicalized; the limit is {MAX_CANONICAL_CONTRACT_CHARS} "
            f"(over by {size - MAX_CANONICAL_CONTRACT_CHARS}). Shorten the largest sections: {top}. "
            f"Check with `stagemesh contract-check --task {task_id}`.",
            task_id=task_id,
            size=size,
            limit=MAX_CANONICAL_CONTRACT_CHARS,
            largest_sections=largest,
        )


def _enforce_planning_gate(store: Store, project: Path, task_id: str, enabled: bool | None) -> None:
    """Opt-in: refuse to start implementation without an approved, identity-current planning spec."""
    if enabled is None:
        try:
            enabled = load_config(project).require_approved_spec
        except ConfigValidationError as exc:
            raise RunReadyRefusal("invalid_config", f"config is invalid, planning gate state unknown: {exc}", task_id=task_id) from exc
    if not enabled:
        return
    record = store.task_contract(task_id)
    if record is not None:
        contract_hash = str(record["digest"])
    else:  # not yet bound by the coordinator: the on-disk contract is what will be bound
        try:
            contract_hash = bind_contract(project, task_id).digest
        except (OSError, ValueError, ContractError) as exc:
            raise RunReadyRefusal("invalid_contract", f"contract for {task_id} is invalid: {exc}", task_id=task_id) from exc
    baseline = store.task_baseline(task_id) or GitWorkspace(project).head()
    spec_payload, approval = load_planning_artifacts(project, task_id)
    decision = evaluate_planning_gate(
        enabled=True, spec_payload=spec_payload, approval=approval, contract_hash=contract_hash, baseline_sha=baseline
    )
    if not decision.allowed:
        raise RunReadyRefusal(
            "planning_gate",
            f"task {task_id} is blocked before implementation: {decision.reason}",
            task_id=task_id,
            next_action=decision.next_action,
            planning_gate_reason=decision.reason,
        )


def _configured_adverse_policy(
    project: Path, task_id: str, explicit: AdverseReviewPolicy | None,
) -> AdverseReviewPolicy:
    """Resolve the integration gate policy; production runs never silently omit it."""
    if explicit is not None:
        return explicit
    try:
        return load_config(project).adverse_review
    except ConfigValidationError as exc:
        raise RunReadyRefusal(
            "invalid_config", f"config is invalid, adverse review policy unknown: {exc}", task_id=task_id,
        ) from exc


_FAILURE_EVENTS = (
    "task.implementation_unsuccessful",
    "task.capacity_failure",
    "integration.ref_missing_candidate",
    "review.infrastructure_failure",
)


def _latest_failure(store: Store, task_id: str, since: float) -> dict[str, Any] | None:
    """The concrete reason a tick made no progress, taken from audit events recorded during that tick."""
    marks = ",".join("?" for _ in _FAILURE_EVENTS)
    rows = store.conn.execute(
        f"SELECT event_type, payload FROM audit_events WHERE created_at >= ? AND event_type IN ({marks}) "
        "ORDER BY created_at DESC, rowid DESC",
        (since, *_FAILURE_EVENTS),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            return {
                "event": row["event_type"],
                **{
                    k: v
                    for k, v in payload.items()
                    if k
                    in {
                        "reason",
                        "executor",
                        "result_status",
                        "execution_id",
                        "candidate_sha",
                        "integration_ref",
                        "providers",
                        "pool_exhausted",
                        "provider_sequence",
                        "provider_outcomes",
                        "no_further_provider",
                    }
                },
            }
    return None


def _latest_evidence(store: Store, task_id: str, candidate_sha: str, kind: EvidenceKind) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT status, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, kind),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return {"status": str(row["status"]), "payload": payload}


def workspace_info(project: Path, task_id: str, worktree_root_path: Path | None = None) -> dict[str, str]:
    head = GitWorkspace(project).run("symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = head.stdout.strip()
    root = Path(worktree_root_path).resolve() if worktree_root_path is not None else worktree_root(project)
    return {
        "project_checkout": str(Path(project).resolve()),
        "worktree_root": str(root),
        "worktree": str(task_workspace(project, task_id, root)),
        "checkout": f"branch {branch}" if head.returncode == 0 and branch else "detached HEAD",
    }


def _snapshot(store: Store, task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    details = task_details(store, task_id)
    claim = details["active_claim"]
    candidate = details["latest_candidate"] or {}
    candidate_sha = candidate.get("sha")
    evidence = {}
    if candidate_sha:
        evidence = {
            "validation": _latest_evidence(store, task_id, str(candidate_sha), EvidenceKind.VALIDATION),
            "review": _latest_evidence(store, task_id, str(candidate_sha), EvidenceKind.REVIEW),
            "integration": _latest_evidence(store, task_id, str(candidate_sha), EvidenceKind.INTEGRATION),
        }
    return {
        "stage": str(task["stage"]),
        "status": str(task["status"]),
        "latest_candidate": candidate_sha,
        "latest_candidate_provider": candidate.get("producer"),
        "latest_agent": candidate.get("producer") or _latest_claimed_agent(store, task_id),
        "latest_validation": (details["latest_validation"] or {}).get("status"),
        "latest_review": (details["latest_review"] or {}).get("status"),
        "latest_integration": (details["latest_integration"] or {}).get("status"),
        "latest_evidence": evidence,
        "active_claim": (
            {
                "id": claim["id"],
                "stage": claim["stage"],
                "worker_id": claim["worker_id"],
                "agent": _latest_claimed_agent(store, task_id) or claim["worker_id"],
                "age_seconds": claim["age_seconds"],
            }
            if claim
            else None
        ),
        "active_executions": [
            {"id": e["id"], "kind": e["kind"], "pid": e["pid"], "process_state": e["process_state"]}
            for e in details["active_executions"]
        ],
    }


def _task_heading(store: Store, task_id: str) -> dict[str, str]:
    task = store.get_task(task_id)
    if task is None:
        return {"id": task_id}
    title = str(task["title"] or "").strip()
    return {"id": task_id, **({"title": title} if title else {})}


def _latest_claimed_agent(store: Store, task_id: str) -> str | None:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 50",
        ("task.claimed",),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            agent = payload.get("executor") or payload.get("provider") or payload.get("worker_id")
            return str(agent) if agent else None
    return None


def format_running(step_number: int, task_id: str, snapshot: dict[str, Any]) -> str:
    stage = str(snapshot["stage"])
    actor = _running_stage_actor(stage, snapshot)
    header = _stage_header(step_number, stage, actor)
    return f"{header}\n    status: running"



def _running_stage_actor(stage: str, snapshot: dict[str, Any]) -> str | None:
    active = snapshot.get("active_claim") or {}
    if stage == "IMPLEMENT":
        # Before the tick starts, the next provider is not known. Do not show the stale
        # producer of the previous candidate as the actor for a remediation attempt.
        actor = active.get("agent") if active.get("stage") == stage else None
        return _display_actor(str(actor)) if actor else None
    return _stage_actor(stage, snapshot)


def _latest_blocked_reason(store: Store, task_id: str) -> dict[str, Any] | None:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 20",
        ("task.blocked",),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id:
            return payload
    return None


def _blocked_message(store: Store, task_id: str) -> str:
    blocked = _latest_blocked_reason(store, task_id)
    if blocked and blocked.get("reason") == EXTERNAL_WORKSPACE_MUTATION:
        stage = blocked.get("stage") or "the task workspace"
        return f"workspace integrity failed at {stage}; StageMesh blocked the task instead of adopting untrusted provider output"
    return "task exhausted its remediation budget; StageMesh will keep other eligible work moving. After the task or environment changes, run `stagemesh continue`."

def format_start(summary: RunSummary) -> str:
    workspace = summary.detail.get("workspace", {})
    task = summary.detail.get("task", {})
    task_id = task.get("id") or summary.task_id
    task_text = str(task_id)
    if task.get("title"):
        task_text += f"  {task['title']}"
    lines = ["StageMesh run", f"task: {task_text}"]
    if summary.detail.get("run_id"):
        lines.append(f"run: {summary.detail['run_id']}")
    if workspace.get("worktree"):
        lines.append(f"workspace: {workspace['worktree']}")
    if workspace.get("project_checkout"):
        lines.append("protected: main checkout is not edited")
    return "\n".join(lines)


def _settle_step_cap(
    summary: RunSummary, store: Store, project: Path, task_id: str, coordinator: Coordinator, started: ProgressState, steps: int,
) -> None:
    """A step cap with meaningful progress stays resumable. Repeated caps without it end as one typed blocker, not a new admission."""
    from .block_kinds import BlockKind, record_block, runtime_fingerprint

    note = record_step_cap(store, task_id, started, progress_state(store, task_id), steps)
    summary.detail["step_cap"] = note
    unproductive = unproductive_step_caps(store, task_id)
    if note["progressed"] or unproductive < STEP_CAP_STALL_LIMIT:
        return
    task = store.get_task(task_id)
    if task is None or task["status"] != TaskStatus.OPEN:
        return
    reason = (
        f"step_cap_no_progress: {unproductive} consecutive step-capped runs made no meaningful progress (latest: {note['reason']}). "
        "Another admission would repeat the same loop. Run `stagemesh diagnose --task " + task_id + "` and fix the cause, "
        "then `stagemesh retry-task`."
    )
    latest = store.latest_candidate(task_id)
    try:
        fingerprint = runtime_fingerprint(project)
    except Exception:  # noqa: BLE001 - the fingerprint only decides when a bounded automatic re-entry is allowed
        fingerprint = None
    record_block(
        store, task_id, BlockKind.RECOVERABLE_OPERATIONAL, reason, stage=str(task["stage"]),
        candidate_sha=None if latest is None else str(latest["sha"]), runtime=fingerprint,
        detail={"failure_code": "step_cap_no_progress", "unproductive_step_caps": unproductive},
    )
    summary.stop_reason, summary.message = "BLOCKED", reason
    _attach_diagnosis(summary, store, project, task_id, coordinator)


def _settle_budget(summary: RunSummary, store: Store, project: Path, task_id: str, coordinator: Coordinator, verdict: Any) -> None:
    """A configured effort limit was reached at a stage boundary: one typed blocker, recorded once, nothing interrupted."""
    from .audit import record_audit
    from .block_kinds import BlockKind, record_block, runtime_fingerprint

    reason = block_reason(task_id, verdict)
    task = store.get_task(task_id)
    if task is not None and task["status"] == TaskStatus.OPEN:
        latest = store.latest_candidate(task_id)
        try:
            fingerprint = runtime_fingerprint(project)
        except Exception:  # noqa: BLE001 - the fingerprint only decides when a bounded automatic re-entry is allowed
            fingerprint = None
        record_block(
            store, task_id, BlockKind.RECOVERABLE_OPERATIONAL, reason, stage=str(task["stage"]),
            candidate_sha=None if latest is None else str(latest["sha"]), runtime=fingerprint,
            detail={"failure_code": "budget_exhausted", "budget": verdict.exceeded, "observed": verdict.observed, "limit": verdict.limit},
        )
        record_audit(store, BUDGET_EXHAUSTED, {"task_id": task_id, "budget": verdict.exceeded, "observed": verdict.observed, "limit": verdict.limit})
    summary.stop_reason, summary.message = "BLOCKED", reason
    summary.detail["budget"] = {"exceeded": verdict.exceeded, "observed": verdict.observed, "limit": verdict.limit}
    _attach_diagnosis(summary, store, project, task_id, coordinator)


def _attach_diagnosis(
    summary: RunSummary, store: Store, project: Path, task_id: str, coordinator: Coordinator | None
) -> None:
    """Put the actionable diagnosis next to the stop reason; for a repeat-failure stop it replaces the generic message."""
    from .diagnosis import DIAGNOSIS_STOP_EVENT, diagnose

    try:
        threshold = coordinator.diagnosis_policy.repeat_threshold if coordinator is not None else 2
        diagnosis = diagnose(store, task_id, project, threshold)
        if diagnosis is None:
            return
        summary.detail["diagnosis"] = diagnosis.to_dict()
        rows = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 20", (DIAGNOSIS_STOP_EVENT,)
        ).fetchall()
        early = next(
            (
                p
                for p in (json.loads(r["payload"]) for r in rows)
                if p.get("task_id") == task_id
                and p.get("category") == diagnosis.category
                and p.get("candidate_sha") == diagnosis.candidate_sha
            ),
            None,
        )
        if summary.stop_reason == "BLOCKED" and early is not None:
            summary.detail["stopped_early"] = True
            summary.message = f"stopped early, {early['category']} ({early['repeat_count']} identical failures): {early['recommendation']}"
    except Exception as exc:  # noqa: BLE001 - advice must never break the run summary
        summary.detail["diagnosis_error"] = f"{type(exc).__name__}: {exc}"


def format_stop(summary: RunSummary) -> str:
    reason = _human_stop_reason(summary.stop_reason)
    delivery = summary.detail.get("delivery")
    if summary.stop_reason == "DONE" and isinstance(delivery, dict):
        reason += f" (ledger complete; {str(delivery['state']).replace('_', ' ').lower()}: {delivery['detail']})"
    lines = [f"stopped: {reason}"]
    if summary.message:
        lines.append(f"  reason: {summary.message}")
    diagnosis = summary.detail.get("diagnosis")
    if diagnosis:
        lines.append(f"  diagnosis: {diagnosis['category']} at {diagnosis['stage']}: {diagnosis['summary']}")
        if diagnosis.get("recommendation"):
            lines.append(f"  next step: {diagnosis['recommendation']}")
        lines.extend(format_excerpt(diagnosis))
        lines.extend(format_findings(diagnosis.get("review_findings", [])))
    if summary.final:
        lines.append(f"  final: {summary.final['stage']}/{summary.final['status']}")
    return "\n".join(lines)


def format_excerpt(diagnosis: dict[str, Any]) -> list[str]:
    """Bounded gate stdout/stderr excerpt of the latest failing evidence, as indented operator lines."""
    evidence = diagnosis.get("failing_evidence") or []
    latest = evidence[-1] if evidence else {}
    excerpt = str(latest.get("excerpt") or "").strip() if isinstance(latest, dict) else ""
    if not excerpt:
        return []
    return ["  output excerpt:"] + [f"    {line[:240]}" for line in excerpt.splitlines()[:8]]


def format_step(step: dict[str, Any], notices: list[str] | None = None) -> str:
    previous = step["previous"]
    new = step["new"]
    stage = str(previous["stage"])
    actor = _stage_actor(stage, new, step.get("executions") or [])
    lines = [_stage_header(int(step["step"]), stage, actor)]
    if actor:
        lines.append(f"    actor: {actor}")
    result = _stage_result(stage, new, step.get("progressed", 0), step.get("executions") or [])
    lines.append(f"    result: {result}")
    for detail in _stage_details(stage, previous, new):
        lines.append(f"    {detail}")
    lines.extend(f"    {notice}" for notice in _operator_notices(notices or []))
    return "\n".join(lines)


def format_step_update(step: dict[str, Any], notices: list[str] | None = None) -> str:
    lines = format_step(step, notices).splitlines()
    update = lines[1:]
    header = lines[0] if lines else ""
    parts = header.split("  ", 1)
    actor_line = f"    actor: {parts[1].strip()}" if len(parts) == 2 and parts[1].strip() else ""
    if actor_line and actor_line not in update:
        update.insert(0, actor_line)
    return "\n".join(update)


def _stage_label(stage: str) -> str:
    return {
        "PLAN": "PLAN",
        "IMPLEMENT": "IMPLEMENT",
        "VALIDATE": "VALIDATE",
        "REVIEW": "REVIEW",
        "INTEGRATE": "INTEGRATE",
        "DONE": "DONE",
    }.get(stage, stage.upper())


def _stage_header(step_number: int, stage: str, actor: str | None) -> str:
    suffix = f"  {actor}" if actor else ""
    return f"[{step_number}] {_stage_label(stage)}{suffix}"


def _stage_result(
    stage: str, new: dict[str, Any], progressed: int, executions: list[dict[str, Any]]
) -> str:
    execution = _execution_for_stage(stage, executions)
    if execution:
        result = _execution_result(stage, execution)
        if execution.get("reason"):
            result += f" ({execution['reason']})"
        if execution.get("duration_seconds") is not None:
            result += f" in {format_duration(execution['duration_seconds'])}"
        return result
    if stage == "PLAN":
        return "advanced" if progressed else "waiting"
    evidence = _evidence_for_stage(stage, new)
    if evidence:
        return str(evidence["status"]).lower()
    if new.get("status") == TaskStatus.BLOCKED:
        return "blocked"
    if progressed:
        return "advanced"
    return "no progress"


def _stage_status(stage: str, new: dict[str, Any], progressed: int) -> str:
    return _stage_result(stage, new, progressed, [])


def _execution_for_stage(stage: str, executions: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [rec for rec in executions if rec.get("stage") == _stage_label(stage)]
    return matches[-1] if matches else None


def _execution_result(stage: str, execution: dict[str, Any]) -> str:
    status = str(execution.get("status") or "").upper()
    if status == "SUCCEEDED":
        return "succeeded" if stage == "IMPLEMENT" else "passed"
    if status == "FAILED":
        return "failed"
    if status == "RUNNING":
        return "running"
    if status == "UNKNOWN":
        return "unknown"
    return str(execution.get("result") or status).lower()


def _stage_actor(stage: str, new: dict[str, Any], executions: list[dict[str, Any]] | None = None) -> str | None:
    execution = _execution_for_stage(stage, executions or [])
    if execution and execution.get("actor"):
        return _display_actor(str(execution["actor"]))
    evidence = _evidence_for_stage(stage, new)
    payload = evidence.get("payload", {}) if evidence else {}
    if stage == "IMPLEMENT":
        return new.get("latest_candidate_provider") or new.get("latest_agent")
    if stage == "VALIDATE":
        return str(payload.get("validator") or "contract validator")
    if stage == "REVIEW":
        actor = payload.get("review_execution_provider") or payload.get("review_provider")
        return str(actor) if actor else "review provider"
    if stage == "INTEGRATE":
        return str(payload.get("integrator") or "builtin integrator")
    return None


def _display_actor(actor: str) -> str:
    return {
        "stagemesh-validator": "builtin-validator",
        "stagemesh-integrator": "builtin-integrator",
    }.get(actor, actor)


def _stage_details(stage: str, previous: dict[str, Any], new: dict[str, Any]) -> list[str]:
    details: list[str] = []
    candidate = new.get("latest_candidate")
    if stage == "IMPLEMENT" and candidate:
        details.append(f"candidate: {str(candidate)[:12]}")
    if stage in {"VALIDATE", "REVIEW", "INTEGRATE"} and candidate:
        details.append(f"candidate: {str(candidate)[:12]}")
    if stage == "VALIDATE":
        evidence = _evidence_for_stage(stage, new)
        checks = ((evidence or {}).get("payload") or {}).get("validation_checks") or {}
        executed = checks.get("executed") or []
        if executed:
            details.append("checks: " + ", ".join(str(item) for item in executed))
    if stage == "REVIEW":
        evidence = _evidence_for_stage(stage, new)
        payload = ((evidence or {}).get("payload") or {})
        if payload.get("finding_count") is not None:
            details.append(f"findings: {payload['finding_count']}")
        if payload.get("independent_review_required") is not None:
            verified = payload.get("independent_reviewer") or payload.get("independent_review_verified")
            details.append(f"independent: {bool(verified)}")
    if stage == "INTEGRATE":
        evidence = _evidence_for_stage(stage, new)
        payload = ((evidence or {}).get("payload") or {})
        if payload.get("integration_ref"):
            details.append(f"ref: {payload['integration_ref']}")
        if payload.get("integration_method"):
            details.append(f"method: {payload['integration_method']}")
    if previous["stage"] != new["stage"]:
        details.append(f"next: {new['stage']}")
    return details


def _evidence_for_stage(stage: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    key = {"VALIDATE": "validation", "REVIEW": "review", "INTEGRATE": "integration"}.get(stage)
    if key is None:
        return None
    return (snapshot.get("latest_evidence") or {}).get(key)


def _operator_notices(lines: list[str]) -> list[str]:
    raw = [line.strip() for line in lines]
    explain = any(
        any(marker in line for marker in ("fallback:", "skipped ", "REFUSED", "no review provider eligible", "all eligible"))
        for line in raw
    )
    notices: list[str] = []
    for line in raw:
        if explain and line.startswith("provider pool considered:"):
            notices.append("pool: " + line.split(":", 1)[1].strip())
        elif line.startswith("skipped "):
            provider, _, reason = line[len("skipped ") :].partition(":")
            notices.append(f"{provider}: skipped, {_human_provider_reason(reason.strip())}")
        elif line.startswith("fallback:"):
            notices.append(line)
        elif line.startswith("REFUSED:"):
            notices.append("reason: " + line[len("REFUSED:") :].strip())
        elif "no review provider eligible" in line or "all eligible" in line:
            notices.append(line)
    return notices


def _human_provider_reason(reason: str) -> str:
    if reason.startswith("cli_not_installed:"):
        return "not authenticated or unavailable"
    if reason.startswith("not_independent:"):
        return "same provider as implementer"
    if reason.startswith("provider_cooldown:"):
        return reason.replace("_", " ")
    if reason.startswith("recent_failure:"):
        return reason.replace("_", " ")
    return reason.replace("_", " ")


def _human_stop_reason(reason: str) -> str:
    if reason == "DONE":
        return "done"
    if reason == "NO_PROGRESS":
        return "no progress"
    if reason == "MAX_STEPS":
        return "step budget reached"
    if reason == "BLOCKED":
        return "blocked"
    if reason.startswith("REFUSED:"):
        return "refused (" + reason.split(":", 1)[1].replace("_", " ") + ")"
    return reason.replace("_", " ").lower()


def run_ready(
    store: Store,
    project: Path,
    make_coordinator: Callable[[TargetSelection], Coordinator],
    *,
    task_id: str | None = None,
    max_steps: int = 50,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    on_start: Callable[[str], None] | None = None,
    auto_plan: bool = True,
    policy: TaskSelectionConfig | None = None,
    chooser: Callable[[list[Candidate]], str | None] | None = None,
    worktree_root_path: Path | None = None,
    no_auto_retry: frozenset[str] | set[str] = frozenset(),
    planning_gate: bool | None = None,
    adverse_policy: AdverseReviewPolicy | None = None,
    adverse_override: dict[str, str] | None = None,
) -> RunSummary:
    """Drive exactly one task through the existing coordinator until DONE, BLOCKED or a safe stop."""
    if max_steps < 1:
        return RunSummary(False, "REFUSED:invalid_max_steps", message="--max-steps must be at least 1")
    recovered: list[dict[str, Any]] = []
    selection_info: dict[str, Any] = {}
    plan_info: dict[str, Any] = {"occurred": False, "reused_existing": False, "events": []}

    def note(message: str) -> None:
        plan_info["events"].append(message)
        if on_start is not None:
            on_start(message)

    try:
        recovered.extend(_recover_all_dead(store))  # provably dead claims/executions only; live/unknown are never touched
        recovered.extend(auto_rebaseline_blocked_stale_baselines(store, project, task_id=task_id))
        recovered.extend(auto_rebind_blocked_missing_validation_gates(store, project, task_id=task_id))
        recovered.extend(auto_revalidate_blocked_validation_gates(store, project, task_id=task_id))
        recovered.extend(auto_reintegrate_blocked_runtime_failures(store, project, task_id=task_id))
        recovered.extend(auto_retry_blocked_provider_failures(store, project, task_id=task_id, exclude=no_auto_retry))
        selection = choose_task(store, project, task_id, policy or TaskSelectionConfig(), auto_plan, chooser)
        selection_info.update(selection.to_dict())
        _log_selection(selection, on_start)
        selected = selection.task_id
        recovered.extend(_recover_dead(store, selected))  # this runner is about to drive it: an owner-less stage row is its own
        if store.get_task(selected)["status"] == TaskStatus.BLOCKED:
            raise RunReadyRefusal(
                "task_blocked",
                exhaustion_message(store, selected)
                or f"task {selected} is BLOCKED; inspect diagnosis and use retry-task after remediation",
                task_id=selected,
            )
        _ensure_contract(store, project, selected, auto_plan, plan_info, note)
        _enforce_planning_gate(store, project, selected, planning_gate)
        if any(row["task_id"] == selected for row in store.running_executions()):
            raise RunReadyRefusal(
                "active_execution", f"task {selected} has a live or unknown execution; not recovering it", task_id=selected
            )
        problems = current_problems(store)
        if problems:
            raise RunReadyRefusal(
                "current_problems", f"health has current problems: {', '.join(problems)}", problems=list(problems)
            )
    except RunReadyRefusal as refusal:
        refused = RunSummary(
            False,
            f"REFUSED:{refusal.reason}",
            message=refusal.message,
            recovered=recovered,
            detail=refusal.detail,
            auto_plan=plan_info,
            selection=selection_info,
        )
        if refusal.reason == "task_blocked" and refusal.detail.get("task_id"):
            # the stored failure evidence (e.g. failed gate output) is still the operator's next-action context
            _attach_diagnosis(refused, store, project, refusal.detail["task_id"], None)
        return refused

    summary = RunSummary(True, "UNSET", task_id=selected, recovered=recovered, auto_plan=plan_info, selection=selection_info)
    try:
        coordinator = make_coordinator(TargetSelection(selected))
        coordinator.validate_target()
    except TargetSelectionError as exc:
        summary.started, summary.stop_reason, summary.message = False, "REFUSED:target_not_runnable", str(exc)
        return summary

    drive_task(
        store,
        project,
        coordinator,
        selected,
        summary,
        max_steps=max_steps,
        on_step=on_step,
        on_start=on_start,
        worktree_root_path=worktree_root_path,
        adverse_policy=adverse_policy,
        adverse_override=adverse_override,
    )
    return summary


def _adverse_integration_block(
    store: Store, project: Path, task_id: str, policy: AdverseReviewPolicy, override: dict[str, str] | None
) -> tuple[str, ...] | None:
    """Reasons the adverse gate refuses integration of the task's candidate, or None when it may proceed."""
    task = store.get_task(task_id)
    candidate = store.latest_candidate(task_id)
    if task is None or candidate is None or Stage(task["stage"]) is not Stage.INTEGRATE:
        return None
    sha = str(candidate["sha"])
    decision = adverse_gate(store, task_id, sha, contract_for_candidate(store, task_id, sha, project).digest, policy, override=override)
    return None if decision.allowed else decision.reasons


def drive_task(
    store: Store,
    project: Path,
    coordinator: Coordinator,
    selected: str,
    summary: RunSummary,
    *,
    max_steps: int,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    on_start: Callable[[str], None] | None = None,
    global_health: bool = True,
    should_stop: Callable[[], bool] | None = None,
    worktree_root_path: Path | None = None,
    adverse_policy: AdverseReviewPolicy | None = None,
    adverse_override: dict[str, str] | None = None,
) -> None:
    """Tick one task until DONE, BLOCKED or a safe stop, filling `summary`.

    When `adverse_policy` is given, integration is never ticked unless the adverse review gate allows it; `adverse_override` ({actor, reason}) is audited.

    `global_health=False` is for parallel runs: another task's failure or a blocked task elsewhere says nothing about this
    one, so only this task's own progress decides when it stops.
    """
    if adverse_policy is None and getattr(coordinator, "require_independent_review", True):
        try:
            adverse_policy = _configured_adverse_policy(project, selected, None)
        except RunReadyRefusal as refusal:
            summary.stop_reason = f"REFUSED:{refusal.reason}"
            summary.message = refusal.message
            summary.detail.update(refusal.detail)
            return
    if adverse_policy is not None:
        coordinator.adverse_policy = adverse_policy
    if adverse_override is not None:
        coordinator.adverse_override = adverse_override
    summary.detail["workspace"] = workspace_info(project, selected, worktree_root_path)
    summary.detail["task"] = _task_heading(store, selected)
    if on_start is not None:
        on_start(format_start(summary))
    control = QueueStopControl(store, selected)
    started_state = progress_state(store, selected)
    for number in range(1, max_steps + 1):
        if control.poll() is not None:
            summary.stop_reason, summary.message = "OPERATOR_STOP", "operator stop requested; current stage retained"
            break
        if should_stop is not None and should_stop():
            summary.stop_reason, summary.message = "INTERRUPTED", "the run was interrupted; claims were released and the worktree kept"
            break
        # A stage boundary: the previous tick has fully ended and the next has not begun, so stopping here never interrupts work.
        current = store.get_task(selected)
        # Validation, integration and publication ticks call no provider, so a spent provider budget never blocks delivery of
        # verified work; it only stops the stages that would spend more.
        spends_provider = current is not None and str(current["stage"]) in (str(Stage.IMPLEMENT), str(Stage.REVIEW))
        verdict = check_task_budget(store, selected, getattr(coordinator, "budget_policy", None) or BudgetPolicy()) if spends_provider else None
        if verdict is not None:
            _settle_budget(summary, store, project, selected, coordinator, verdict)
            break
        summary.recovered.extend(_recover_dead(store, selected))
        refused = (
            _adverse_integration_block(store, project, selected, adverse_policy, adverse_override)
            if adverse_policy is not None
            else None
        )
        if refused is not None:
            store.block_task(selected)
            record_audit(store, "adverse_review.blocked", {"task_id": selected, "reasons": list(refused)})
            summary.stop_reason, summary.message = "BLOCKED", "adverse review gate refused integration: " + "; ".join(refused)
            summary.detail["adverse_review"] = list(refused)
            break
        previous = _snapshot(store, selected)
        if on_start is not None:
            on_start(format_running(number, selected, previous))
        tick_started = time.time()
        try:
            from .validation_slot import bind_validation_wait, reset_validation_wait

            tokens = bind_validation_wait(cancel=should_stop)
            try:
                with provider_stop_scope(control):
                    progressed = coordinator.tick()
            finally:
                reset_validation_wait(tokens)
        except OperatorStopRequested as exc:
            summary.stop_reason = "OPERATOR_STOP" if exc.cleanup_complete else "STOP_CLEANUP_INCOMPLETE"
            if exc.cleanup_complete and exc.monitor_failed:
                summary.stop_reason = "STOP_MONITOR_FAILED"
            summary.message = str(exc)
            break
        except TargetSelectionError as exc:
            summary.stop_reason, summary.message = "TARGET_ERROR", str(exc)
            break
        ownership_refusals = [
            item for item in getattr(coordinator, "last_execution_refusals", []) if item["task_id"] == selected
        ]
        if ownership_refusals:
            summary.stop_reason, summary.message = stage_dispatch_refusal(ownership_refusals)
            summary.detail["active_execution_refusals"] = ownership_refusals
            if not summary.steps:
                summary.started = False
            break
        step = {"step": number, "task_id": selected, "progressed": progressed, "previous": previous, "new": _snapshot(store, selected)}
        step["executions"] = [rec for rec in execution_timings(store, selected) if rec["started_at"] >= tick_started]
        step["duration_seconds"] = step_duration(step["executions"])
        summary.steps.append(step)
        if on_step is not None:
            on_step(step)
        new = step["new"]
        if new["stage"] == Stage.DONE or new["status"] == TaskStatus.DONE:
            summary.stop_reason = "DONE"
            summary.detail["delivery"] = task_delivery(store, selected).to_dict()  # ledger DONE is not delivery
            break
        if new["status"] == TaskStatus.BLOCKED:
            summary.stop_reason, summary.message = "BLOCKED", _blocked_message(store, selected)
            _attach_diagnosis(summary, store, project, selected, coordinator)
            break
        if global_health:
            summary.recovered.extend(_recover_all_dead(store))
            problems = current_problems(store)
            if problems:
                summary.stop_reason, summary.message = "CURRENT_PROBLEM", ", ".join(problems)
                summary.detail["problems"] = list(problems)
                break
        moved = progressed or new["stage"] != previous["stage"] or new["latest_candidate"] != previous["latest_candidate"]
        if not moved:
            failure = _latest_failure(store, selected, tick_started)
            summary.stop_reason = "NO_PROGRESS"
            if failure is not None:
                summary.detail["failure"] = failure
                summary.message = f"{failure['event']}: {failure.get('reason', 'unknown')}" + (
                    f" ({failure['providers']})" if failure.get("providers") else ""
                )
            else:
                summary.message = "a tick made no progress and recorded no failure (review infrastructure unavailable or an open claim)"
            _attach_diagnosis(summary, store, project, selected, coordinator)
            break
    else:
        summary.stop_reason, summary.message = "MAX_STEPS", f"stopped after {max_steps} steps"
        _settle_step_cap(summary, store, project, selected, coordinator, started_state, max_steps)
    if should_stop is not None and should_stop() and summary.stop_reason not in {"DONE", "BLOCKED", "STOP_CLEANUP_INCOMPLETE", "STOP_MONITOR_FAILED"}:
        # A provider killed by the interrupt reports a failure; that is the interrupt, not a verdict on the task.
        # A task that already finished or was blocked keeps its own outcome and reason.
        summary.stop_reason, summary.message = "INTERRUPTED", "the run was interrupted; claims were released and the worktree kept"
    if summary.stop_reason not in {"STOP_CLEANUP_INCOMPLETE", "STOP_MONITOR_FAILED"}:
        if control.poll() is not None and summary.stop_reason not in {"DONE", "BLOCKED"}:
            summary.stop_reason, summary.message = "OPERATOR_STOP", "operator stop requested; current stage retained"
        control.complete_if_idle()
    summary.final = _snapshot(store, selected)
