from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import Stage
from .persistence import Store


@dataclass(frozen=True)
class SchedulingDecision:
    task_id: str
    eligible: bool
    reason: str


class Scheduler:
    def __init__(self, store: Store):
        self.store = store

    def decision(self, task_id: str) -> SchedulingDecision:
        task = self.store.get_task(task_id)
        if task is None:
            return SchedulingDecision(task_id, False, "missing")
        if task["stage"] == Stage.DONE or task["status"] == "DONE":
            return SchedulingDecision(task_id, False, "done")
        source_reason = _source_refusal_reason(self.store, task)
        if source_reason is not None:
            return SchedulingDecision(task_id, False, source_reason)
        if task["status"] == "BLOCKED":
            return SchedulingDecision(task_id, False, "blocked")
        incomplete = self.store.incomplete_dependencies(task_id)
        if incomplete:
            return SchedulingDecision(task_id, False, f"waiting for {','.join(incomplete)}")
        return SchedulingDecision(task_id, True, "eligible")

    def eligible_task_ids(self) -> list[str]:
        return [row["id"] for row in self.store.tasks() if self.decision(row["id"]).eligible]


def _source_refusal_reason(store: Store, task: Any) -> str | None:
    if task["source"] != "github":
        return None
    source_id = task["source_id"]
    if source_id is None:
        return None
    state = store.source_state(str(task["source"]), str(source_id))
    source_state = state.get("state")
    if isinstance(source_state, str) and source_state.upper() != "OPEN":
        return "source closed"
    if state.get("eligible") is False:
        return "source no longer eligible"
    return None
