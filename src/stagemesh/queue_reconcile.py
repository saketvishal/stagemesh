"""Read-only first queue reconciliation for old or noisy task ledgers.

The command this backs is intentionally conservative: it classifies every task
with the same primitives used by `doctor` and only mutates the ledger when the
operator asks for a specific repair class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import Stage, TaskStatus
from .persistence import Store
from .scheduling import Scheduler
from .survivor_wave import blocked_needs_decision


@dataclass(frozen=True)
class ReconcileOptions:
    write: bool = False
    park_design_decisions: bool = False
    unblock_retryable: bool = False


@dataclass
class QueueReconcileItem:
    task_id: str
    title: str
    stage: str
    status: str
    classification: str
    reason: str
    action: str = "none"
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "stage": self.stage,
            "status": self.status,
            "classification": self.classification,
            "reason": self.reason,
            "action": self.action,
            "applied": self.applied,
        }


@dataclass
class QueueReconcileReport:
    write: bool
    sync_sources: bool
    items: list[QueueReconcileItem]

    @property
    def actions(self) -> list[QueueReconcileItem]:
        return [item for item in self.items if item.action != "none"]

    @property
    def applied(self) -> list[QueueReconcileItem]:
        return [item for item in self.items if item.applied]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.classification] = counts.get(item.classification, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "write": self.write,
            "sync_sources": self.sync_sources,
            "counts": self.counts(),
            "actions": [item.to_dict() for item in self.actions],
            "items": [item.to_dict() for item in self.items],
        }


def reconcile_queue(store: Store, options: ReconcileOptions | None = None, *, sync_sources: bool = True) -> QueueReconcileReport:
    """Classify queue entries and optionally apply explicit, audited cleanup actions."""
    options = options or ReconcileOptions()
    items = [_classify(store, row, options) for row in store.tasks()]
    if options.write:
        for item in items:
            _apply(store, item)
        store.add_audit_event(
            "queue.reconciled",
            {
                "sync_sources": sync_sources,
                "actions": [item.to_dict() for item in items if item.applied],
                "counts": QueueReconcileReport(options.write, sync_sources, items).counts(),
            },
        )
    return QueueReconcileReport(options.write, sync_sources, items)


def _classify(store: Store, row: Any, options: ReconcileOptions) -> QueueReconcileItem:
    task_id = str(row["id"])
    title = str(row["title"])
    stage = str(row["stage"])
    status = str(row["status"])
    base = QueueReconcileItem(task_id, title, stage, status, "unknown", "not classified")

    if stage == Stage.DONE or status == TaskStatus.DONE:
        base.classification, base.reason = "done", "already done"
        return base
    if store.has_active_claim(task_id) or any(str(execution["task_id"]) == task_id for execution in store.running_executions()):
        base.classification, base.reason = "in_flight", "active claim or running execution"
        return base

    operator_reason = _operator_retirement(store, row)
    if operator_reason:
        base.classification, base.reason = "retired_operator", operator_reason
        return base

    source_reason = _source_retirement_reason(store, row)
    if source_reason == "source closed":
        base.classification, base.reason = "retired_closed", source_reason
        return base
    if source_reason:
        base.classification, base.reason = "parked_source", source_reason
        return base

    scheduler = Scheduler(store).decision(task_id)
    if not scheduler.eligible and scheduler.reason != "blocked":
        base.classification, base.reason = "parked", scheduler.reason
        return base

    if status == TaskStatus.BLOCKED:
        if decision := blocked_needs_decision(store, task_id):
            base.classification, base.reason = "needs_decision", decision
            if options.park_design_decisions:
                base.action = "park_design_decision"
            return base
        base.classification, base.reason = "retryable_blocked", "blocked without an unresolved review decision"
        if options.unblock_retryable:
            base.action = "unblock_retryable"
        return base

    if status == TaskStatus.OPEN:
        base.classification = "runnable" if scheduler.eligible else "parked"
        base.reason = scheduler.reason
        return base

    base.classification, base.reason = "in_flight", f"status {status}"
    return base


def _source_retirement_reason(store: Store, row: Any) -> str:
    source, source_id = row["source"], row["source_id"]
    if source is None or source_id is None:
        return ""
    return str(store.source_state(str(source), str(source_id)).get("retirement_reason") or "")


def _operator_retirement(store: Store, row: Any) -> str:
    source, source_id = row["source"], row["source_id"]
    if source is None or source_id is None:
        return ""
    return store.operator_retirement_reason(str(source), str(source_id)) or ""


def _apply(store: Store, item: QueueReconcileItem) -> None:
    if item.action == "unblock_retryable":
        item.applied = store.unblock_task(item.task_id)
    elif item.action == "park_design_decision":
        task = store.get_task(item.task_id)
        if task is None or task["source"] is None or task["source_id"] is None:
            item.reason = f"{item.reason}; cannot park task without a source id"
            return
        parked = store.retire_source_task(
            str(task["source"]),
            str(task["source_id"]),
            "Parked by queue reconcile: needs design decision",
            {"decision_reason": item.reason, "category": "design_decision"},
        )
        item.applied = parked == item.task_id
