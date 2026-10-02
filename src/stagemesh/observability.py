from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .domain import ExecutionStatus, Stage, TaskStatus
from .persistence import Store


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    task_count: int
    blocked_task_count: int
    running_count: int
    done_count: int
    failed_execution_count: int
    unknown_execution_count: int
    backlog_state: str
    latest_implementation_failure: dict[str, Any] | None


def health(store: Store) -> HealthReport:
    tasks = store.tasks()
    running = list(store.running_executions())
    done = [task for task in tasks if task["stage"] == Stage.DONE or task["status"] == "DONE"]
    blocked = [task for task in tasks if task["status"] == TaskStatus.BLOCKED]
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
        ok=not blocked and failed_execution_count == 0 and unknown_execution_count == 0,
        task_count=len(tasks),
        blocked_task_count=len(blocked),
        running_count=len(running),
        done_count=len(done),
        failed_execution_count=failed_execution_count,
        unknown_execution_count=unknown_execution_count,
        backlog_state=backlog_state,
        latest_implementation_failure=_latest_implementation_failure(store),
    )


def _latest_implementation_failure(store: Store) -> dict[str, Any] | None:
    row = store.conn.execute(
        """
        SELECT event_type, payload, created_at
        FROM audit_events
        WHERE event_type IN (?, ?)
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        ("task.implementation_unsuccessful", "recovery.failed_implementation_claim_released"),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return {"created_at": row["created_at"], "reason": "unparseable_audit_payload"}
    payload["created_at"] = row["created_at"]
    payload["event_type"] = row["event_type"]
    return payload
