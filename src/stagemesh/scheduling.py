from __future__ import annotations

from dataclasses import dataclass

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
        incomplete = self.store.incomplete_dependencies(task_id)
        if incomplete:
            return SchedulingDecision(task_id, False, f"waiting for {','.join(incomplete)}")
        return SchedulingDecision(task_id, True, "eligible")

    def eligible_task_ids(self) -> list[str]:
        return [row["id"] for row in self.store.tasks() if self.decision(row["id"]).eligible]
