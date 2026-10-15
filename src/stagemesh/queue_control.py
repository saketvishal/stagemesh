"""Project-scoped operator stop observation shared by task drivers and provider waits."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from .audit import record_audit
from .persistence import Store

QUEUE_CONTROL_EVENT = "queue.admission_control"


def latest_queue_control(store: Store) -> dict[str, Any] | None:
    row = store.conn.execute(
        """
        SELECT payload, created_at, rowid
        FROM audit_events
        WHERE event_type=?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (QUEUE_CONTROL_EVENT,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return {**payload, "created_at": row["created_at"], "rowid": row["rowid"]}


class OperatorStopRequested(KeyboardInterrupt):
    """Control flow, never a provider outage or a reason to try another provider."""

    def __init__(self, message: str, *, cleanup_complete: bool = True):
        super().__init__(message)
        self.cleanup_complete = cleanup_complete


class QueueStopControl:
    def __init__(self, store: Store, task_id: str | None = None):
        self.store, self.task_id = store, task_id
        latest = latest_queue_control(store)
        self.state = str(latest.get("state")) if latest else "running"
        self.cursor = int(latest["rowid"]) if latest else 0
        if self.state in {"stopping", "stopped"}:
            self.cursor -= 1
        self.request = None
        self._noted = set()

    def poll(self):
        rows = self.store.conn.execute(
            "SELECT payload,rowid AS control_rowid FROM audit_events WHERE event_type=? AND rowid>? ORDER BY rowid",
            (QUEUE_CONTROL_EVENT, self.cursor),
        ).fetchall()
        for row in rows:
            self.cursor = int(row["control_rowid"])
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            self.state = str(payload.get("state"))
            if self.state in {"stopping", "stopped"}:
                terminate = payload.get("terminate_running") is True or bool(self.request and self.request["terminate_running"])
                self.request = {**payload, "rowid": self.cursor, "terminate_running": terminate}
        if self.state == "resumed":
            self.state = "running"
        return self.request

    def blocks_admission(self):
        self.poll()
        return self.request is not None or self.state != "running"

    def state_label(self):
        return "stopping" if self.request and self.state == "running" else self.state

    def note(self, event, **detail):
        if event not in self._noted:
            record_audit(self.store, event, {"task_id": self.task_id, **detail})
            self._noted.add(event)

    def complete_if_idle(self):
        self.poll()
        if self.request is None:
            return
        # Serialize the final state check with a concurrent resume/new stop.
        with self.store.conn:
            self.store.conn.execute("BEGIN IMMEDIATE")
            latest = latest_queue_control(self.store)
            if not latest or latest.get("state") != "stopping" or latest["rowid"] != self.request["rowid"]:
                return
            if list(self.store.running_executions()) or any(self.store.has_active_claim(t["id"]) for t in self.store.tasks()):
                return
            record_audit(self.store, QUEUE_CONTROL_EVENT, {
                "state": "stopped", "reason": "operator stop completed", "source": "runner",
                "terminate_running": self.request["terminate_running"],
                "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            })


_current_stop: ContextVar[QueueStopControl | None] = ContextVar("provider_stop_control", default=None)


@contextmanager
def provider_stop_scope(control):
    token = _current_stop.set(control)
    try:
        yield
    finally:
        _current_stop.reset(token)


def current_provider_stop():
    return _current_stop.get()
