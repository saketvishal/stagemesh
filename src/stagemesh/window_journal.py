"""The run-window journal: `window.started` / `window.finished` / `window.refused` audit events, and the last-window summary doctor shows.

A window that has a `started` event and no matching `finished` event was interrupted (crash, kill, power loss): doctor says so and what
the next safe step is, so nobody has to open the ledger to find out.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .audit import record_audit
from .persistence import Store

STARTED, FINISHED, REFUSED = "window.started", "window.finished", "window.refused"
TASK_CONTEXT = "window.task_context"
_EVENTS = (STARTED, FINISHED, REFUSED)


def record_started(store: Store, window_id: str, details: dict[str, Any]) -> None:
    record_audit(store, STARTED, {"window_id": window_id, **details})


def record_finished(store: Store, window_id: str, details: dict[str, Any]) -> None:
    record_audit(store, FINISHED, {"window_id": window_id, **details})


def record_refused(store: Store, window_id: str, details: dict[str, Any]) -> None:
    record_audit(store, REFUSED, {"window_id": window_id, **details})


def record_task_context(store: Store, window_id: str, task_id: str, details: dict[str, Any]) -> None:
    record_audit(store, TASK_CONTEXT, {"window_id": window_id, "task_id": task_id, **details})


def latest_task_context(store: Store, task_id: str) -> dict[str, Any] | None:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 200",
        (TASK_CONTEXT,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("task_id")) == task_id:
            return payload
    return None


def last_window(store: Store, *, now: float | None = None) -> dict[str, Any] | None:
    """The most recent window as {window_id, state: finished|interrupted|refused, ...}; None when no window ever ran."""
    rows = store.conn.execute(
        f"SELECT event_type, payload, created_at FROM audit_events WHERE event_type IN ({','.join('?' * len(_EVENTS))}) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 200",
        _EVENTS,
    ).fetchall()
    events: list[tuple[str, dict[str, Any], float]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("window_id"):
            events.append((str(row["event_type"]), payload, float(row["created_at"])))
    if not events:
        return None
    window_id = str(events[0][1]["window_id"])
    mine = [event for event in events if event[1]["window_id"] == window_id]
    started = next((e for e in mine if e[0] == STARTED), None)
    finished = next((e for e in mine if e[0] == FINISHED), None)
    refused = next((e for e in mine if e[0] == REFUSED), None)
    current = time.time() if now is None else now
    if finished is not None:
        state, payload, at = "finished", finished[1], finished[2]
    elif refused is not None:
        state, payload, at = "refused", refused[1], refused[2]
    else:
        state, payload, at = "interrupted", (started[1] if started else {}), (started[2] if started else events[0][2])
    summary = {k: v for k, v in payload.items() if k != "window_id"}
    return {
        "window_id": window_id,
        "state": state,
        "at": at,
        "age_seconds": max(0.0, current - at),
        "started_at": None if started is None else started[2],
        **summary,
    }
