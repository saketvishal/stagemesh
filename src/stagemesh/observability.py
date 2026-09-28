from __future__ import annotations

from dataclasses import dataclass

from .domain import ExecutionStatus, Stage
from .persistence import Store


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    task_count: int
    running_count: int
    done_count: int
    failed_execution_count: int
    unknown_execution_count: int
    backlog_state: str


def health(store: Store) -> HealthReport:
    tasks = store.tasks()
    running = list(store.running_executions())
    done = [task for task in tasks if task["stage"] == Stage.DONE or task["status"] == "DONE"]
    failed_execution_count = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status=?",
            (ExecutionStatus.FAILED,),
        ).fetchone()[0]
    )
    unknown_execution_count = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status=?",
            (ExecutionStatus.UNKNOWN,),
        ).fetchone()[0]
    )
    backlog_state = "EMPTY" if not tasks else "ACTIVE"
    return HealthReport(
        ok=failed_execution_count == 0 and unknown_execution_count == 0,
        task_count=len(tasks),
        running_count=len(running),
        done_count=len(done),
        failed_execution_count=failed_execution_count,
        unknown_execution_count=unknown_execution_count,
        backlog_state=backlog_state,
    )
