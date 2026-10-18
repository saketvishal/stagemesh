"""Quiet follow-through: one notification per fact, one blocker per cause, and follow-ups that end when their work is done.

Everything is persisted as append-only audit events, so it survives restarts:

* `notify_once(key)`            True exactly once per key. A completion, blocker or decision is announced the first time and never
                                again, however many windows, restarts or follow-up runs observe it.
* `register_follow_up` / `complete_follow_up` / `follow_up_active`
                                a follow-up (a pending publication/merge) is registered with a key and, for delivery follow-ups, an
                                exact identity (repository, task, candidate, PR) and an explicit authorization scope. Once
                                completed it reports itself inactive.
* `record_schedule` / `schedule_handle`
                                the scheduler handle that wakes the follow-up. `delivery_followup` removes that schedule when the
                                follow-up completes or is cancelled; marking the record complete is never treated as enough.
* `consolidate_blockers`        the same cause on several tasks becomes one line naming all of them.

Inserts of "once" facts are a single `INSERT ... WHERE NOT EXISTS` statement, so two processes (or two wakeups) racing to record the
same key produce exactly one row and exactly one winner.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .audit import record_audit
from .persistence import Store
from .redaction import redact_mapping

NOTIFIED = "notification.sent"
FOLLOW_UP_REGISTERED = "followup.registered"
FOLLOW_UP_COMPLETED = "followup.completed"
FOLLOW_UP_AUTHORIZED = "followup.authorized"
FOLLOW_UP_SCHEDULED = "followup.scheduled"
FOLLOW_UP_DESCHEDULED = "followup.descheduled"
FOLLOW_UP_CHECKED = "followup.checked"

SCOPE_PUBLISH = "publish"
SCOPE_MERGE = "merge"

_NUMBER = re.compile(r"\d+")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_TASK_REF = re.compile(r"\b(?:task|issue|pr|#)\s*[-\w]*\d+\b", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def _events(store: Store, event_type: str, key: str) -> list[dict[str, Any]]:
    found = []
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload, '$.key') = ? ORDER BY created_at, rowid",
        (event_type, key),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("key") == key:
            found.append(payload)
    return found


def _insert_once(store: Store, event_type: str, key: str, payload: dict[str, Any]) -> bool:
    """Append the event unless one with this type and key exists. Atomic across processes: True for exactly one caller."""
    body = redact_mapping({**payload, "key": key})
    cursor = store.conn.execute(
        "INSERT INTO audit_events SELECT ?, ?, ?, ? WHERE NOT EXISTS "
        "(SELECT 1 FROM audit_events WHERE event_type=? AND json_extract(payload, '$.key') = ?)",
        (str(uuid.uuid4()), event_type, json.dumps(body, sort_keys=True), time.time(), event_type, key),
    )
    store.conn.commit()
    return cursor.rowcount == 1


def notify_once(store: Store, key: str, summary: str) -> bool:
    """Record that `key` was announced. True the first time only; the caller sends the notification when this returns True."""
    return _insert_once(store, NOTIFIED, key, {"summary": summary[:300]})


@dataclass(frozen=True)
class FollowUpIdentity:
    """Exactly what a delivery follow-up may act on. Anything that no longer matches this is stale and is never acted on."""

    repository: str  # "owner/repo", lower-cased
    task_id: str
    candidate_sha: str
    pr_url: str | None = None
    base: str | None = None
    scope: tuple[str, ...] = (SCOPE_PUBLISH,)  # explicit authorization: SCOPE_PUBLISH and/or SCOPE_MERGE
    merge_policy: str | None = None
    required_checks: tuple[str, ...] = ()
    authorized_by: str = ""  # the invocation that granted it, e.g. "run-window --follow-up --merge-policy hosted"
    window_id: str = ""
    resume_argv: tuple[str, ...] = ()  # the exact, narrowed command that resumes this delivery (no implementation replay)
    expires_at: float | None = None
    check_interval_seconds: int = 600
    max_checks: int = 144
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def key(self) -> str:
        """Stable per repository + task + candidate (not per PR or scope), so a second window cannot create a second worker."""
        raw = json.dumps([self.repository.casefold(), self.task_id, self.candidate_sha], ensure_ascii=True)
        return "delivery-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository.casefold(), "task_id": self.task_id, "candidate_sha": self.candidate_sha, "pr_url": self.pr_url,
            "base": self.base, "scope": list(self.scope), "merge_policy": self.merge_policy, "required_checks": list(self.required_checks),
            "authorized_by": self.authorized_by, "window_id": self.window_id, "resume_argv": list(self.resume_argv),
            "expires_at": self.expires_at, "check_interval_seconds": self.check_interval_seconds, "max_checks": self.max_checks,
        }


def register_follow_up(store: Store, key: str, purpose: str, identity: FollowUpIdentity | None = None) -> bool:
    """Register a follow-up once. False when it already exists (registering again never creates a second schedule)."""
    payload: dict[str, Any] = {"purpose": purpose[:300]}
    if identity is not None:
        payload["identity"] = identity.to_dict()
    return _insert_once(store, FOLLOW_UP_REGISTERED, key, payload)


def authorize_follow_up(store: Store, key: str, identity: FollowUpIdentity) -> bool:
    """Record an explicit (re-)authorization for an existing follow-up. The latest explicit event defines the scope; nothing is
    ever widened implicitly. Appends only when the scope or PR identity actually changed. True when something changed."""
    current = follow_up_record(store, key)
    if current is None:
        return False
    now = identity.to_dict()
    same = all(current.get(name) == now[name] for name in ("scope", "merge_policy", "required_checks", "pr_url", "base", "resume_argv"))
    if same:
        return False
    record_audit(store, FOLLOW_UP_AUTHORIZED, {"key": key, "identity": now})
    return True


def follow_up_record(store: Store, key: str) -> dict[str, Any] | None:
    """The follow-up's current identity: the registered identity overlaid by the latest explicit authorization."""
    registered = _events(store, FOLLOW_UP_REGISTERED, key)
    if not registered:
        return None
    identity = dict(registered[0].get("identity") or {})
    for event in _events(store, FOLLOW_UP_AUTHORIZED, key):
        identity.update(event.get("identity") or {})
        identity["task_id"], identity["candidate_sha"], identity["repository"] = (
            registered[0]["identity"]["task_id"], registered[0]["identity"]["candidate_sha"], registered[0]["identity"]["repository"],
        )  # exactness fields are never changed by a later authorization
    identity["key"] = key
    identity["purpose"] = registered[0].get("purpose")
    return identity


def active_follow_ups(store: Store) -> list[dict[str, Any]]:
    keys = []
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (FOLLOW_UP_REGISTERED,)):
        payload = json.loads(row["payload"])
        key = payload.get("key")
        if key and key not in keys:
            keys.append(key)
    return [record for key in keys if follow_up_active(store, key) and (record := follow_up_record(store, key)) is not None]


def complete_follow_up(store: Store, key: str, outcome: str) -> bool:
    """Mark the follow-up finished. True the first time; the single completion notice belongs to that first call."""
    return _insert_once(store, FOLLOW_UP_COMPLETED, key, {"outcome": outcome[:300]})


def follow_up_outcome(store: Store, key: str) -> str | None:
    done = _events(store, FOLLOW_UP_COMPLETED, key)
    return None if not done else str(done[0].get("outcome"))


def follow_up_active(store: Store, key: str) -> bool:
    """A follow-up keeps running only while it is registered and not completed."""
    return bool(_events(store, FOLLOW_UP_REGISTERED, key)) and not _events(store, FOLLOW_UP_COMPLETED, key)


def record_schedule(store: Store, key: str, handle: str, scheduler: str) -> None:
    record_audit(store, FOLLOW_UP_SCHEDULED, {"key": key, "handle": handle, "scheduler": scheduler})


def schedule_handle(store: Store, key: str) -> str | None:
    """The handle of the live schedule for this follow-up, or None when none was created or it has been removed."""
    scheduled = _events(store, FOLLOW_UP_SCHEDULED, key)
    removed = _events(store, FOLLOW_UP_DESCHEDULED, key)
    if not scheduled:
        return None
    handle = str(scheduled[-1].get("handle"))
    return None if any(item.get("handle") == handle for item in removed) else handle


def record_descheduled(store: Store, key: str, handle: str) -> None:
    record_audit(store, FOLLOW_UP_DESCHEDULED, {"key": key, "handle": handle})


def checks_made(store: Store, key: str) -> int:
    return len(_events(store, FOLLOW_UP_CHECKED, key))


def record_check(store: Store, key: str, state: str, detail: str) -> None:
    record_audit(store, FOLLOW_UP_CHECKED, {"key": key, "state": state, "detail": detail[:300]})


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
