"""Quiet follow-through: one notification per fact, one blocker per cause, and follow-ups that end when their work is done.

Everything is persisted as append-only audit events, so it survives restarts and needs no scheduler of its own:

* `notify_once(key)`            True exactly once per key. A completion, blocker or decision is announced the first time and never
                                again, however many windows, restarts or follow-up runs observe it.
* `register_follow_up` / `complete_follow_up` / `follow_up_active`
                                a follow-up (a pending merge, an install-when-idle) is registered with a key; once completed it
                                reports itself inactive, and whatever schedules it must stop (see docs/reliable-delivery.md).
* `consolidate_blockers`        the same cause on several tasks becomes one line naming all of them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from .audit import record_audit
from .persistence import Store

NOTIFIED = "notification.sent"
FOLLOW_UP_REGISTERED = "followup.registered"
FOLLOW_UP_COMPLETED = "followup.completed"

_NUMBER = re.compile(r"\d+")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TASK_REF = re.compile(r"\b(?:task|issue|pr|#)\s*[-\w]*\d+\b", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def _events(store: Store, event_type: str, key: str) -> list[dict[str, Any]]:
    found = []
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("key") == key:
            found.append(payload)
    return found


def notify_once(store: Store, key: str, summary: str) -> bool:
    """Record that `key` was announced. True the first time only; the caller sends the notification when this returns True."""
    if _events(store, NOTIFIED, key):
        return False
    record_audit(store, NOTIFIED, {"key": key, "summary": summary[:300]})
    return True


def register_follow_up(store: Store, key: str, purpose: str) -> bool:
    """Register a follow-up once. False when it already exists (registering again never creates a second schedule)."""
    if _events(store, FOLLOW_UP_REGISTERED, key):
        return False
    record_audit(store, FOLLOW_UP_REGISTERED, {"key": key, "purpose": purpose[:300]})
    return True


def complete_follow_up(store: Store, key: str, outcome: str) -> bool:
    """Mark the follow-up finished. True the first time; the single completion notice belongs to that first call."""
    if _events(store, FOLLOW_UP_COMPLETED, key):
        return False
    record_audit(store, FOLLOW_UP_COMPLETED, {"key": key, "outcome": outcome[:300]})
    return True


def follow_up_active(store: Store, key: str) -> bool:
    """A follow-up keeps running only while it is registered and not completed."""
    return bool(_events(store, FOLLOW_UP_REGISTERED, key)) and not _events(store, FOLLOW_UP_COMPLETED, key)


def blocker_signature(reason: str) -> str:
    """The cause of a blocker without the incidental parts (shas, numbers, task and PR references)."""
    text = _SHA.sub("<sha>", reason)
    text = _TASK_REF.sub("<ref>", text)
    text = _NUMBER.sub("#", text)
    return _SPACE.sub(" ", text).strip().casefold()


def consolidate_blockers(items: Iterable[tuple[str, str]], *, exact: bool = False) -> list[dict[str, Any]]:
    """[(task_id, reason)] -> one entry per distinct cause: {"reason", "tasks", "count"}, in first-seen order.

    By default causes are compared by `blocker_signature`; `exact=True` merges only identical text (for operator-facing lists
    where a differing task reference is information).
    """
    grouped: dict[str, dict[str, Any]] = {}
    for task_id, reason in items:
        entry = grouped.setdefault(reason if exact else blocker_signature(reason), {"reason": reason, "tasks": [], "count": 0})
        entry["tasks"].append(task_id)
        entry["count"] += 1
    return list(grouped.values())
