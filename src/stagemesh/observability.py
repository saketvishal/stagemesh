from __future__ import annotations

import json
from collections import Counter
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
    blocked_reason_buckets: dict[str, int]
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
        blocked_reason_buckets=_blocked_reason_buckets(store, blocked),
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


def _blocked_reason_buckets(store: Store, blocked: list[Any]) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for task in blocked:
        buckets[_latest_blocked_reason(store, str(task["id"]))] += 1
    return dict(sorted(buckets.items()))


def _latest_blocked_reason(store: Store, task_id: str) -> str:
    """Why the task is blocked, from the newest stop event: an explicit block, a diagnosis stop, or an exhausted remediation."""
    rows = store.conn.execute(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN ('task.blocked', 'task.diagnosis_stop', 'task.remediation_exhausted') "
        "ORDER BY created_at DESC, rowid DESC LIMIT 400"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if str(payload.get("task_id") or "") != task_id:
            continue
        if row["event_type"] == "task.blocked":
            return str(payload.get("reason") or "blocked")
        if row["event_type"] == "task.diagnosis_stop":
            return str(payload.get("category") or payload.get("reason") or "diagnosis_stop")
        return str(payload.get("reason") or "remediation_exhausted")
    return "blocked"


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
