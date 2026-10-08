"""Deterministic choice of the next task when several are eligible.

Order of precedence (lower is better): priority label, preferred (prep/governance/readiness) label, valid contract over one that
needs auto-planning, then the configured tie-breaker. Tasks that are blocked, excluded, waiting on dependencies, in a stale failed
state, objective roots, or unplannable are never auto-selected; `--task <id>` bypasses selection entirely and is how a stale task is retried.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .auto_plan import cached_state as _cached_state
from .auto_plan import plannable, task_labels
from .config import TaskSelectionConfig
from .contracts import ContractError, canonical_contract_json, parse_contract, task_contract_path
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .objective_roots import objective_root_reason
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .scheduling import Scheduler


@dataclass(frozen=True)
class Candidate:
    task_id: str
    title: str
    labels: tuple[str, ...]
    priority_rank: int
    priority_label: str | None
    preferred_rank: int
    preferred_label: str | None
    contract: str  # "valid" | "needs_auto_plan" | "unplannable:<why>"
    tie_value: tuple[Any, ...]

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.priority_rank, self.preferred_rank, 0 if self.contract == "valid" else 1, self.tie_value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title[:120],
            "labels": list(self.labels),
            "priority": self.priority_label,
            "preferred_label": self.preferred_label,
            "contract": self.contract,
            "tie_value": list(self.tie_value),
        }

    def describe(self) -> str:
        parts = [self.priority_label or "no priority label"]
        if self.preferred_label:
            parts.append(f"preferred label {self.preferred_label}")
        parts.append("valid contract" if self.contract == "valid" else "needs auto-planning")
        return ", ".join(parts)


@dataclass
class Selection:
    mode: str  # explicit | single | auto | chosen
    task_id: str
    reason: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "task_id": self.task_id,
            "reason": self.reason,
            "candidates": self.candidates,
            "skipped": self.skipped,
            "policy": self.policy,
        }


class SelectionRefusal(Exception):
    def __init__(self, reason: str, message: str, **detail: Any):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail


def _first_match(labels: tuple[str, ...], ordered: tuple[str, ...]) -> tuple[int, str | None]:
    folded = {label.casefold() for label in labels}
    for index, wanted in enumerate(ordered):
        if wanted.casefold() in folded:
            return index, wanted
    return len(ordered), None


def _excluded(labels: tuple[str, ...], excluded: tuple[str, ...]) -> str | None:
    folded = {label.casefold() for label in labels}
    return next((label for label in excluded if label.casefold() in folded), None)


POOL_EXHAUSTION_MARKERS = (
    "all_implementation_providers_no_progress",
    "all_implementation_providers_exhausted",
    "all_implementation_providers_failed",
)
POOL_EXHAUSTION_SKIP_SECONDS = 21600.0


def stale_failure(store: Store, task: Any) -> str | None:
    """A previous attempt left this task failed, stale, or temporarily exhausted for autonomous selection."""
    exhausted = recent_provider_pool_exhaustion(store, str(task["id"]))
    if exhausted is not None:
        return exhausted
    candidate = store.latest_candidate(task["id"])
    if candidate is not None:
        row = store.conn.execute(
            "SELECT status FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task["id"], candidate["sha"], EvidenceKind.INTEGRATION),
        ).fetchone()
        if row is not None and row["status"] == EvidenceStatus.FAILED:
            return "latest integration failed"
    pending = store.conn.execute(
        "SELECT stage FROM task_remediations WHERE task_id=? AND cleared=0 ORDER BY created_at DESC LIMIT 1", (task["id"],)
    ).fetchone()
    if pending is not None and task["stage"] == Stage.IMPLEMENT:
        return f"remediation pending after failed {pending['stage']}"
    return None


def recent_provider_pool_exhaustion(store: Store, task_id: str) -> str | None:
    rows = store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 20",
        ("task.implementation_unsuccessful",),
    ).fetchall()
    now = time.time()
    for row in rows:
        age = now - float(row["created_at"])
        if age > POOL_EXHAUSTION_SKIP_SECONDS:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if str(payload.get("task_id")) != task_id:
            continue
        reason = str(payload.get("reason") or "")
        if any(reason.startswith(marker) for marker in POOL_EXHAUSTION_MARKERS):
            return f"recent provider pool exhaustion ({reason[:160]}); continuing with other eligible tasks until provider/task cooldown clears"
    return None


def _contract_state(store: Store, project: Path, task_id: str, auto_plan: bool) -> str:
    if store.task_contract(task_id) is not None:
        return "valid"
    path = task_contract_path(project, task_id)
    if path is not None:
        try:
            size = len(canonical_contract_json(parse_contract(json.loads(path.read_text(encoding="utf-8")))))
        except (OSError, ValueError, ContractError):
            return "unplannable:contract file is invalid"
        return "valid" if size <= MAX_CANONICAL_CONTRACT_CHARS else "unplannable:contract exceeds the size limit"
    if not auto_plan:
        return "unplannable:no contract and auto-planning is disabled"
    why = plannable(store, project, task_id)
    if why is not None:
        return f"unplannable:no contract and cannot auto-plan: {why}"
    return "needs_auto_plan"


def _tie_value(store: Store, task: Any, how: str) -> tuple[Any, ...]:
    if how == "created_at":
        created = _cached_state(store, task).get("created_at")
        if isinstance(created, str) and created:
            try:
                return (datetime.fromisoformat(created).timestamp(),)
            except ValueError:
                pass
        return (float(task["created_at"]),)
    source_id = str(task["source_id"] or task["id"])
    return (0, int(source_id), "") if source_id.isdigit() else (1, 0, source_id)


def _collect(
    store: Store, project: Path, policy: TaskSelectionConfig, auto_plan: bool
) -> tuple[list[Candidate], list[Candidate], list[dict[str, str]]]:
    """Every OPEN task as a viable candidate, an unplannable one, or a skip with its reason."""
    scheduler = Scheduler(store)
    skipped: list[dict[str, str]] = []
    candidates: list[Candidate] = []
    unplannable: list[Candidate] = []
    for task in store.tasks():
        task_id = str(task["id"])
        if task["status"] != TaskStatus.OPEN or task["stage"] == Stage.DONE:
            continue
        objective_reason = objective_root_reason(store, task)
        if objective_reason is not None:
            skipped.append({"task_id": task_id, "reason": objective_reason})
            continue
        decision = scheduler.decision(task_id)
        if not decision.eligible:
            skipped.append({"task_id": task_id, "reason": decision.reason})
            continue
        labels = task_labels(store, task)
        blocked_by = _excluded(labels, policy.excluded_labels)
        if blocked_by is not None:
            skipped.append({"task_id": task_id, "reason": f"excluded label {blocked_by}"})
            continue
        stale = stale_failure(store, task)
        if stale is not None:
            reason = stale if stale.startswith("recent provider pool exhaustion") else f"stale failed state ({stale}); continuing with other eligible tasks"
            skipped.append({"task_id": task_id, "reason": reason})
            continue
        priority_rank, priority_label = _first_match(labels, policy.priority_labels)
        preferred_rank, preferred_label = _first_match(labels, policy.preferred_labels)
        candidate = Candidate(
            task_id=task_id,
            title=str(task["title"]),
            labels=labels,
            priority_rank=priority_rank,
            priority_label=priority_label,
            preferred_rank=preferred_rank,
            preferred_label=preferred_label,
            contract=_contract_state(store, project, task_id, auto_plan),
            tie_value=_tie_value(store, task, policy.tie_breaker),
        )
        (unplannable if candidate.contract.startswith("unplannable:") else candidates).append(candidate)
    return candidates, unplannable, skipped


def _policy_info(policy: TaskSelectionConfig) -> dict[str, Any]:
    return {
        "auto_select": policy.auto_select,
        "order": ["priority_label", "preferred_label", "valid_contract", policy.tie_breaker],
        "priority_labels": list(policy.priority_labels),
        "tie_breaker": policy.tie_breaker,
    }


def select_next_task(
    store: Store,
    project: Path,
    policy: TaskSelectionConfig,
    *,
    auto_plan: bool = True,
    chooser: Callable[[list[Candidate]], str | None] | None = None,
) -> Selection:
    candidates, unplannable, skipped = _collect(store, project, policy, auto_plan)
    # Unplannable tasks are passed over while any other task is viable; if nothing else is, the single best one falls through
    # so the run reports its precise contract refusal (missing_contract / auto_plan_failed) instead of "no eligible task".
    pool = candidates or unplannable
    if candidates:
        skipped.extend({"task_id": c.task_id, "reason": c.contract.split(":", 1)[1]} for c in unplannable)
    policy_info = _policy_info(policy)
    if not pool:
        provider_exhausted = [item for item in skipped if item["reason"].startswith("recent provider pool exhaustion")]
        if provider_exhausted:
            recovered = provider_exhausted[0]
            return Selection(
                "auto_recovery",
                recovered["task_id"],
                "all otherwise eligible tasks are waiting on recent provider pool exhaustion; retrying one bounded task automatically",
                [],
                skipped,
                policy_info,
            )
        raise SelectionRefusal(
            "no_eligible_task", "no eligible OPEN task" + _skipped_text(skipped), skipped=skipped, policy=policy_info
        )
    ranked = sorted(pool, key=lambda c: c.key)
    listing = [c.to_dict() for c in ranked]
    if len(ranked) == 1:
        only = ranked[0]
        return Selection("single", only.task_id, f"the only eligible task ({only.describe()})", listing, skipped, policy_info)
    if chooser is not None:
        picked = chooser(ranked)
        if picked is None or picked not in {c.task_id for c in ranked}:
            raise SelectionRefusal("no_selection_made", "no task was chosen", candidates=listing, policy=policy_info)
        return Selection("chosen", picked, "chosen interactively", listing, skipped, policy_info)
    if not policy.auto_select:
        raise SelectionRefusal(
            "multiple_eligible_tasks",
            f"{len(ranked)} eligible tasks ({', '.join(c.task_id for c in ranked)}) and task_selection.auto_select is off; "
            "pass --task <id> or --choose",
            eligible=[c.task_id for c in ranked],
            candidates=listing,
            policy=policy_info,
        )
    best, runner_up = ranked[0], ranked[1]
    if best.key == runner_up.key:
        raise SelectionRefusal(
            "ambiguous_selection",
            f"tasks {best.task_id} and {runner_up.task_id} are tied under the selection policy "
            f"({best.describe()}; tie-breaker {policy.tie_breaker} cannot separate them); pass --task <id> or --choose",
            candidates=listing[:5],
            policy=policy_info,
        )
    reason = (
        f"highest-ranked of {len(ranked)} eligible tasks: {best.describe()}; "
        f"runner-up {runner_up.task_id} ({runner_up.describe()}); {_why_better(best, runner_up, policy)}"
    )
    return Selection("auto", best.task_id, reason, listing, skipped, policy_info)


def rank_batch_candidates(
    store: Store, project: Path, policy: TaskSelectionConfig, *, auto_plan: bool = True
) -> tuple[list[Candidate], list[dict[str, str]], dict[str, Any]]:
    """Every task that may be started concurrently, best first. Blocked, excluded, stale-failed, dependency-blocked and
    unplannable tasks are returned as skips and are never candidates, even when nothing else is runnable."""
    candidates, unplannable, skipped = _collect(store, project, policy, auto_plan)
    skipped.extend({"task_id": c.task_id, "reason": c.contract.split(":", 1)[1], "kind": "unplannable"} for c in unplannable)
    skipped.extend({"task_id": str(t["id"]), "reason": "blocked"} for t in store.tasks() if t["status"] == TaskStatus.BLOCKED)
    return sorted(candidates, key=lambda c: (c.key, c.task_id)), skipped, _policy_info(policy)


def _why_better(best: Candidate, other: Candidate, policy: TaskSelectionConfig) -> str:
    if best.priority_rank != other.priority_rank:
        return f"decided by priority label ({best.priority_label or 'none'} beats {other.priority_label or 'none'})"
    if best.preferred_rank != other.preferred_rank:
        return f"decided by preferred label ({best.preferred_label or 'none'} over {other.preferred_label or 'none'})"
    if (best.contract == "valid") != (other.contract == "valid"):
        return "decided by contract readiness (valid contract beats one needing auto-planning)"
    return f"decided by tie-breaker {policy.tie_breaker}"


def _skipped_text(skipped: list[dict[str, str]]) -> str:
    if not skipped:
        return ""
    return " (skipped: " + "; ".join(f"{s['task_id']}: {s['reason']}" for s in skipped[:10]) + ")"
