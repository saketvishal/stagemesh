from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import task_contract_path
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .domain import Stage, TaskStatus
from .observability import health
from .operator_actions import recover_stale, task_details
from .persistence import Store
from .scheduling import Scheduler

# A task whose latest execution failed is a normal remediation state, not a reason to stop supervising.
_IGNORED_PROBLEMS = frozenset({"current_failed_executions"})


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


def select_task(store: Store, requested: str | None) -> str:
    if requested is not None:
        if store.get_task(requested) is None:
            raise RunReadyRefusal("task_not_found", f"task does not exist: {requested}")
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


def _snapshot(store: Store, task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    details = task_details(store, task_id)
    claim = details["active_claim"]
    return {
        "stage": str(task["stage"]),
        "status": str(task["status"]),
        "latest_candidate": (details["latest_candidate"] or {}).get("sha"),
        "latest_validation": (details["latest_validation"] or {}).get("status"),
        "latest_review": (details["latest_review"] or {}).get("status"),
        "active_claim": {"id": claim["id"], "stage": claim["stage"], "age_seconds": claim["age_seconds"]} if claim else None,
        "active_executions": [
            {"id": e["id"], "kind": e["kind"], "pid": e["pid"], "process_state": e["process_state"]}
            for e in details["active_executions"]
        ],
    }


def format_step(step: dict[str, Any]) -> str:
    new = step["new"]
    active = "none"
    if new["active_executions"]:
        active = ",".join(f"{e['kind']}:{e['process_state']}" for e in new["active_executions"])
    elif new["active_claim"]:
        active = f"claim:{new['active_claim']['stage']}"
    candidate = (new["latest_candidate"] or "-")[:10]
    return (
        f"step {step['step']} task {step['task_id']}: {step['previous']['stage']}/{step['previous']['status']} -> "
        f"{new['stage']}/{new['status']} candidate={candidate} validation={new['latest_validation'] or '-'} "
        f"review={new['latest_review'] or '-'} active={active}"
    )


def run_ready(
    store: Store,
    project: Path,
    make_coordinator: Callable[[TargetSelection], Coordinator],
    *,
    task_id: str | None = None,
    max_steps: int = 50,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> RunSummary:
    """Drive exactly one task through the existing coordinator until DONE, BLOCKED or a safe stop."""
    if max_steps < 1:
        return RunSummary(False, "REFUSED:invalid_max_steps", message="--max-steps must be at least 1")
    recovered: list[dict[str, Any]] = []
    try:
        for row in store.tasks():  # provably dead claims/executions only; live/unknown are never touched
            recovered.extend(_recover_dead(store, str(row["id"])))
        selected = select_task(store, task_id)
        if task_contract_path(project, selected) is None and store.task_contract(selected) is None:
            raise RunReadyRefusal("missing_contract", f"task {selected} has no change contract", task_id=selected)
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
            False, f"REFUSED:{refusal.reason}", message=refusal.message, recovered=recovered, detail=refusal.detail
        )

    summary = RunSummary(True, "UNSET", task_id=selected, recovered=recovered)
    try:
        coordinator = make_coordinator(TargetSelection(selected))
        coordinator.validate_target()
    except TargetSelectionError as exc:
        summary.started, summary.stop_reason, summary.message = False, "REFUSED:target_not_runnable", str(exc)
        return summary

    for number in range(1, max_steps + 1):
        summary.recovered.extend(_recover_dead(store, selected))
        previous = _snapshot(store, selected)
        try:
            progressed = coordinator.tick()
        except TargetSelectionError as exc:
            summary.stop_reason, summary.message = "TARGET_ERROR", str(exc)
            break
        step = {"step": number, "task_id": selected, "progressed": progressed, "previous": previous, "new": _snapshot(store, selected)}
        summary.steps.append(step)
        if on_step is not None:
            on_step(step)
        new = step["new"]
        if new["stage"] == Stage.DONE or new["status"] == TaskStatus.DONE:
            summary.stop_reason = "DONE"
            break
        if new["status"] == TaskStatus.BLOCKED:
            summary.stop_reason, summary.message = "BLOCKED", "task exhausted its remediation budget; use retry-task after review"
            break
        problems = current_problems(store)
        if problems:
            summary.stop_reason, summary.message = "CURRENT_PROBLEM", ", ".join(problems)
            summary.detail["problems"] = list(problems)
            break
        if progressed == 0:
            summary.stop_reason, summary.message = "NO_PROGRESS", "a tick made no progress (provider outage, review infrastructure or open claim)"
            break
    else:
        summary.stop_reason, summary.message = "MAX_STEPS", f"stopped after {max_steps} steps"
    summary.final = _snapshot(store, selected)
    return summary
