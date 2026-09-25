"""Append-only event helpers for the Build Coordinator."""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from build_coordinator.types import EventInput
from build_coordinator.models import BuildTaskEvent

# `event_id` doubles as the emission-order key: it is a fixed-width,
# zero-padded "<nanosecond-timestamp>-<process-local-sequence>" string, so
# lexical sort order matches emission order even when several events land
# within the same timestamp tick. The sequence counter (not just the clock)
# is what guarantees strict monotonicity, since wall-clock resolution alone
# is not fine enough to separate rapid successive inserts on every platform.
_id_lock = threading.Lock()
_id_counter = itertools.count()


def _new_event_id() -> str:
    with _id_lock:
        seq = next(_id_counter)
    return f"{time.time_ns():020d}-{seq:012d}"


def record_event(session: Session, event: EventInput) -> BuildTaskEvent:
    row = BuildTaskEvent(
        event_id=_new_event_id(),
        task_id=event.task_id,
        event_type=event.event_type,
        actor=event.actor,
        from_state=event.from_state,
        to_state=event.to_state,
        claim_id=str(event.claim_id) if event.claim_id is not None else None,
        event_data=event.event_data,
    )
    session.add(row)
    return row


@dataclass(frozen=True)
class EventRecord:
    """A single durable coordinator event as a structured, streamable record.

    `cursor` is an opaque, resumable position: passing it back as
    `after_cursor` to `stream_events` resumes immediately after this event,
    in the same stable order.
    """

    cursor: str
    event_id: str
    task_id: str | None
    event_type: str
    actor: str | None
    from_state: str | None
    to_state: str | None
    claim_id: str | None
    event_data: dict
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "cursor": self.cursor,
            "event_id": self.event_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "claim_id": self.claim_id,
            "event_data": self.event_data,
            "created_at": self.created_at.isoformat(),
        }


def decode_cursor(cursor: str) -> str:
    """Validate a cursor produced by `stream_events` / `EventRecord.cursor`.

    Cursors are event ids, which sort lexically in emission order. Raises
    `ValueError` if the cursor is malformed, so callers can surface a clear
    error instead of silently resuming from the wrong position.
    """

    if not isinstance(cursor, str) or "-" not in cursor:
        raise ValueError(f"invalid event cursor: {cursor!r}")
    timestamp_part, _, seq_part = cursor.partition("-")
    if not (timestamp_part.isdigit() and seq_part.isdigit()):
        raise ValueError(f"invalid event cursor: {cursor!r}")
    return cursor


def stream_events(
    session: Session,
    *,
    after_cursor: str | None = None,
    task_id: str | None = None,
    limit: int | None = None,
) -> list[EventRecord]:
    """Return durable events in stable emission order, optionally resuming
    from `after_cursor`.
    """

    query = session.query(BuildTaskEvent)
    if task_id is not None:
        query = query.filter(BuildTaskEvent.task_id == task_id)
    if after_cursor is not None:
        after_event_id = decode_cursor(after_cursor)
        query = query.filter(BuildTaskEvent.event_id > after_event_id)
    query = query.order_by(BuildTaskEvent.event_id.asc())
    if limit is not None:
        query = query.limit(limit)
    rows = query.all()
    return [
        EventRecord(
            cursor=row.event_id,
            event_id=row.event_id,
            task_id=row.task_id,
            event_type=row.event_type,
            actor=row.actor,
            from_state=row.from_state,
            to_state=row.to_state,
            claim_id=row.claim_id,
            event_data=row.event_data,
            created_at=row.created_at,
        )
        for row in rows
    ]
