from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .domain import ExecutionStatus, Stage, TaskStatus
from .persistence import Store
from .process_identity import classify_process, process_identity
from .scheduling import Scheduler


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    task_count: int
    open_task_count: int
    eligible_open_task_count: int
    blocked_task_count: int
    running_count: int
    source_ready_count: int
    done_count: int
    failed_execution_count: int
    unknown_execution_count: int
    backlog_state: str
    latest_implementation_failure: dict[str, Any] | None
    historical_failed_execution_count: int = 0
    current_failed_execution_count: int = 0
    stale_execution_count: int = 0
    current_problems: tuple[str, ...] = ()


def health(store: Store) -> HealthReport:
    tasks = store.tasks()
    running = list(store.running_executions())
    scheduler = Scheduler(store)
    open_tasks = [task for task in tasks if task["status"] == TaskStatus.OPEN and task["stage"] != Stage.DONE]
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
    current_failed = _current_failed_execution_count(store)
    stale = sum(
        1
        for execution in running
        if classify_process(store.execution_process_identity(execution["id"]), process_identity(execution["pid"])) == "DEAD"
    )
    problems = tuple(
        label
        for label, present in (
            ("blocked_tasks", bool(blocked)),
            ("current_failed_executions", current_failed > 0),
            ("unknown_executions", unknown_execution_count > 0),
            ("stale_running_executions", stale > 0),
        )
        if present
    )
    return HealthReport(
        # `ok` reflects current state only; failed_execution_count is a historical total kept for compatibility.
        ok=not problems,
        task_count=len(tasks),
        open_task_count=len(open_tasks),
        eligible_open_task_count=sum(1 for task in open_tasks if scheduler.decision(str(task["id"])).eligible),
        blocked_task_count=len(blocked),
        running_count=len(running),
        source_ready_count=_source_ready_count(store, tasks),
        done_count=len(done),
        failed_execution_count=failed_execution_count,
        unknown_execution_count=unknown_execution_count,
        backlog_state=backlog_state,
        latest_implementation_failure=_latest_implementation_failure(store),
        historical_failed_execution_count=failed_execution_count,
        current_failed_execution_count=current_failed,
        stale_execution_count=stale,
        current_problems=problems,
    )


def _source_ready_count(store: Store, tasks: list[Any]) -> int:
    count = 0
    for task in tasks:
        source = task["source"]
        source_id = task["source_id"]
        if source is None or source_id is None:
            continue
        state = store.source_state(str(source), str(source_id))
        if state.get("eligible") is True and str(state.get("state", "")).upper() == "OPEN":
            count += 1
    return count


def _current_failed_execution_count(store: Store) -> int:
    """Tasks not yet done whose most recent execution failed; superseded failures are history, not current."""
    count = 0
    for task in store.tasks():
        if task["stage"] == Stage.DONE or task["status"] == TaskStatus.DONE:
            continue
        latest = store.conn.execute(
            "SELECT status FROM executions WHERE task_id=? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        if latest is not None and latest["status"] == ExecutionStatus.FAILED:
            count += 1
    return count


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
