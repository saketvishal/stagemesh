"""Read-only operator view of queue control: state, last event, stop reason and the executions still running.

Display/diagnostic only. It reads the same audit events and execution rows the queue runner already writes and
never changes pause/stop behavior.
"""

from __future__ import annotations

import json
from typing import Any

from .parallel import QUEUE_CONTROL_EVENT, queue_control_state
from .persistence import Store
from .process_identity import classify_process, process_identity

STOP_STATES = {"stopping", "stopped"}
CYCLE_BOUNDARY_STATES = {"resumed", "paused"}


def _stop_reason(store: Store, state: str) -> str | None:
    """Why the queue is stopping/stopped: the operator's reason from the current stop cycle.

    The runner's later 'stopped' event only says the stop finished, so look back for the 'stopping' request, but
    never past a resume/pause (an earlier cycle). A stop with no request in this cycle (e.g. Ctrl+C) uses the
    latest event's own reason.
    """
    if state not in STOP_STATES:
        return None
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC",
        (QUEUE_CONTROL_EVENT,),
    ).fetchall()
    latest_reason: str | None = None
    for index, row in enumerate(rows):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        reason = str(payload["reason"]) if payload.get("reason") else None
        if index == 0:
            latest_reason = reason
        if payload.get("state") in CYCLE_BOUNDARY_STATES:
            break
        if payload.get("state") == "stopping" and reason:
            return reason
    return latest_reason


def queue_control_report(store: Store) -> dict[str, Any]:
    """queue_control_state plus stop reason, per-execution process identity and flat id/pid summaries."""
    report = queue_control_state(store)
    executions = []
    for execution in report["active_executions"]:
        pid = execution.get("pid")
        state = classify_process(store.execution_process_identity(execution["id"]), process_identity(pid))
        executions.append({**execution, "process_state": state})
    latest = report.get("latest") or {}
    report["active_executions"] = executions
    report["last_event"] = (
        {key: latest.get(key) for key in ("state", "reason", "source", "terminate_running", "at", "created_at")}
        if latest
        else None
    )
    report["stop_reason"] = _stop_reason(store, str(report["state"]))
    report["execution_ids"] = [e["id"] for e in executions]
    report["task_ids"] = sorted({e["task_id"] for e in executions})
    report["pids"] = sorted({e["pid"] for e in executions if e["pid"] is not None})
    report["stale_execution_ids"] = [e["id"] for e in executions if e["process_state"] == "DEAD"]
    report["unknown_execution_ids"] = [e["id"] for e in executions if e["process_state"] == "UNKNOWN"]
    return report


def format_queue_control(report: dict[str, Any], indent: str = "") -> list[str]:
    """Text lines for a queue_control_report; the first line keeps the long-standing 'queue admission' wording."""
    executions = report["active_executions"]
    lines = [f"{indent}queue admission: {report['state']} (active executions: {len(executions)})"]
    event = report.get("last_event")
    if event:
        detail = f"{event.get('state')} at {event.get('at') or event.get('created_at')} by {event.get('source') or 'unknown'}"
        if event.get("reason"):
            detail += f": {event['reason']}"
        if event.get("terminate_running"):
            detail += " (terminate requested)"
        lines.append(f"{indent}  last event: {detail}")
    else:
        lines.append(f"{indent}  last event: none")
    if report.get("stop_reason"):
        lines.append(f"{indent}  stop reason: {report['stop_reason']}")
    for execution in executions:
        pid = execution["pid"] if execution["pid"] is not None else "unknown"
        lines.append(
            f"{indent}  execution {execution['id']}: task {execution['task_id']} {execution['kind']} pid {pid} process {execution['process_state']}"
        )
    if executions:
        pids = ", ".join(str(pid) for pid in report["pids"]) or "none known"
        lines.append(f"{indent}  tasks: {', '.join(report['task_ids'])}; pids: {pids}")
    if report["stale_execution_ids"]:
        lines.append(f"{indent}  stale (process gone): {', '.join(report['stale_execution_ids'])}")
    if report["unknown_execution_ids"]:
        lines.append(f"{indent}  unknown process identity: {', '.join(report['unknown_execution_ids'])}")
    return lines
