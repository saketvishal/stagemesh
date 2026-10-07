from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auto_plan import AutoPlanError, create_contract
from .diagnosis import format_findings
from .contracts import ContractError, canonical_contract_json, parse_contract, task_contract_path
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .domain import EvidenceKind, Stage, TaskStatus
from .observability import health
from .operator_actions import recover_stale, task_details
from .git import GitWorkspace
from .objective_roots import objective_root_reason
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .config import TaskSelectionConfig
from .scheduling import Scheduler
from .task_selection import Candidate, Selection, SelectionRefusal, select_next_task
from .timing import execution_timings, format_duration, step_duration
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
    return tuple(problem for problem in health(store).current_problems if problem not in _IGNORED_PROBLEMS)


def _recover_dead(store: Store, task_id: str) -> list[dict[str, Any]]:
    return [
        {"task_id": task_id, **action.to_dict()}
        for action in recover_stale(store, task_id)
        if action.action == "RELEASED"
    ]


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
        size = len(canonical_contract_json(parse_contract(json.loads(path.read_text(encoding="utf-8")))))
    except (OSError, ValueError, ContractError) as exc:
        raise RunReadyRefusal("invalid_contract", f"contract for {task_id} is invalid: {exc}", task_id=task_id) from exc
    if size > MAX_CANONICAL_CONTRACT_CHARS:
        raise RunReadyRefusal(
            "invalid_contract",
            f"contract for {task_id} is {size} characters canonicalized; the limit is {MAX_CANONICAL_CONTRACT_CHARS}. Shorten it.",
            task_id=task_id,
            size=size,
            limit=MAX_CANONICAL_CONTRACT_CHARS,
        )


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
            return {"event": row["event_type"], **{k: v for k, v in payload.items() if k in {"reason", "executor", "result_status", "execution_id", "candidate_sha", "integration_ref", "providers"}}}
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
    stage = _stage_label(snapshot["stage"])
    return "\n".join([f"{stage} #{step_number}", f"  task: {task_id}", "  status: running"])


def format_start(summary: RunSummary) -> str:
    workspace = summary.detail.get("workspace", {})
    lines = [f"StageMesh continue: task {summary.task_id}"]
    if workspace.get("project_checkout"):
        lines.append(f"  project checkout: {workspace['project_checkout']}")
    if workspace.get("worktree_root"):
        lines.append(f"  worktree root: {workspace['worktree_root']}")
    if workspace.get("worktree"):
        lines.append(f"  task worktree: {workspace['worktree']}")
    if workspace.get("checkout"):
        lines.append(f"  integration checkout: {workspace['checkout']}")
    return "\n".join(lines)


def _attach_diagnosis(summary: RunSummary, store: Store, project: Path, task_id: str, coordinator: Coordinator) -> None:
    """Put the actionable diagnosis next to the stop reason; for a repeat-failure stop it replaces the generic message."""
    from .diagnosis import DIAGNOSIS_STOP_EVENT, diagnose

    try:
        threshold = coordinator.diagnosis_policy.repeat_threshold
        diagnosis = diagnose(store, task_id, project, threshold)
        if diagnosis is None:
            return
        summary.detail["diagnosis"] = diagnosis.to_dict()
        rows = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 20", (DIAGNOSIS_STOP_EVENT,)
        ).fetchall()
        early = next((p for p in (json.loads(r["payload"]) for r in rows) if p.get("task_id") == task_id), None)
        if summary.stop_reason == "BLOCKED" and early is not None:
            summary.detail["stopped_early"] = True
            summary.message = f"stopped early, {early['category']} ({early['repeat_count']} identical failures): {early['recommendation']}"
    except Exception as exc:  # noqa: BLE001 - advice must never break the run summary
        summary.detail["diagnosis_error"] = f"{type(exc).__name__}: {exc}"


def format_stop(summary: RunSummary) -> str:
    reason = _human_stop_reason(summary.stop_reason)
    lines = [f"Run stopped: {reason}"]
    if summary.message:
        lines.append(f"  reason: {summary.message}")
    diagnosis = summary.detail.get("diagnosis")
    if diagnosis:
        lines.append(f"  diagnosis: {diagnosis['category']} at {diagnosis['stage']}: {diagnosis['summary']}")
        if diagnosis.get("recommendation"):
            lines.append(f"  next step: {diagnosis['recommendation']}")
        lines.extend(format_findings(diagnosis.get("review_findings", [])))
    if summary.final:
        lines.append(f"  final: {summary.final['stage']}/{summary.final['status']}")
    return "\n".join(lines)


def format_step(step: dict[str, Any], notices: list[str] | None = None) -> str:
    notices = _operator_notices(notices or [])
    previous = step["previous"]
    new = step["new"]
    stage = str(previous["stage"])
    lines = [_stage_label(stage) + f" #{step['step']}", f"  task: {step['task_id']}"]
    status = _stage_status(stage, new, step.get("progressed", 0))
    lines.append(f"  status: {status}")
    actor = _stage_actor(stage, new)
    if actor:
        lines.append(f"  actor: {actor}")
    for detail in _stage_details(stage, previous, new):
        lines.append(f"  {detail}")
    if step.get("duration_seconds") is not None:
        lines.append(f"  duration: {format_duration(step['duration_seconds'])}")
    for notice in notices:
        lines.append(f"  note: {notice}")
    return "\n".join(lines)


def format_step_update(step: dict[str, Any], notices: list[str] | None = None) -> str:
    lines = format_step(step, notices).splitlines()
    update = lines[2:]
    if update and update[0].startswith("  status: "):
        update[0] = "  result: " + update[0].split(": ", 1)[1]
    return "\n".join(update)


def _stage_label(stage: str) -> str:
    return {
        "PLAN": "Plan",
        "IMPLEMENT": "Implementation",
        "VALIDATE": "Validation",
        "REVIEW": "Review",
        "INTEGRATE": "Integration",
        "DONE": "Done",
    }.get(stage, stage.title())


def _stage_status(stage: str, new: dict[str, Any], progressed: int) -> str:
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


def _stage_actor(stage: str, new: dict[str, Any]) -> str | None:
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
    interesting = ("fallback:", "skipped ", "REFUSED", "no review provider eligible", "all eligible")
    return [line.strip() for line in lines if any(marker in line for marker in interesting)]


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
        for row in store.tasks():  # provably dead claims/executions only; live/unknown are never touched
            recovered.extend(_recover_dead(store, str(row["id"])))
        selection = choose_task(store, project, task_id, policy or TaskSelectionConfig(), auto_plan, chooser)
        selection_info.update(selection.to_dict())
        _log_selection(selection, on_start)
        selected = selection.task_id
        if store.get_task(selected)["status"] == TaskStatus.BLOCKED:
            raise RunReadyRefusal(
                "task_blocked",
                f"task {selected} is BLOCKED; inspect diagnosis and use retry-task after remediation",
                task_id=selected,
            )
        _ensure_contract(store, project, selected, auto_plan, plan_info, note)
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
        return RunSummary(
            False,
            f"REFUSED:{refusal.reason}",
            message=refusal.message,
            recovered=recovered,
            detail=refusal.detail,
            auto_plan=plan_info,
            selection=selection_info,
        )

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
    )
    return summary


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
) -> None:
    """Tick one task until DONE, BLOCKED or a safe stop, filling `summary`.

    `global_health=False` is for parallel runs: another task's failure or a blocked task elsewhere says nothing about this
    one, so only this task's own progress decides when it stops.
    """
    summary.detail["workspace"] = workspace_info(project, selected, worktree_root_path)
    if on_start is not None:
        on_start(format_start(summary))
    for number in range(1, max_steps + 1):
        if should_stop is not None and should_stop():
            summary.stop_reason, summary.message = "INTERRUPTED", "the run was interrupted; claims were released and the worktree kept"
            break
        summary.recovered.extend(_recover_dead(store, selected))
        previous = _snapshot(store, selected)
        if on_start is not None:
            on_start(format_running(number, selected, previous))
        tick_started = time.time()
        try:
            progressed = coordinator.tick()
        except TargetSelectionError as exc:
            summary.stop_reason, summary.message = "TARGET_ERROR", str(exc)
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
            break
        if new["status"] == TaskStatus.BLOCKED:
            summary.stop_reason, summary.message = "BLOCKED", "task exhausted its remediation budget; use retry-task after review"
            _attach_diagnosis(summary, store, project, selected, coordinator)
            break
        if global_health:
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
    if should_stop is not None and should_stop() and summary.stop_reason != "DONE":
        # A provider killed by the interrupt reports a failure; that is the interrupt, not a verdict on the task.
        summary.stop_reason, summary.message = "INTERRUPTED", "the run was interrupted; claims were released and the worktree kept"
    summary.final = _snapshot(store, selected)
