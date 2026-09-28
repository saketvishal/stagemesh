from __future__ import annotations

from dataclasses import dataclass

from .domain import Stage
from .persistence import Store


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    task_count: int
    running_count: int
    done_count: int
    backlog_state: str


def health(store: Store) -> HealthReport:
    tasks = store.tasks()
    running = list(store.running_executions())
    done = [task for task in tasks if task["stage"] == Stage.DONE or task["status"] == "DONE"]
    backlog_state = "EMPTY" if not tasks else "ACTIVE"
    return HealthReport(ok=True, task_count=len(tasks), running_count=len(running), done_count=len(done), backlog_state=backlog_state)
