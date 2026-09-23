"""Append-only event helpers for the Build Coordinator."""

from __future__ import annotations

from sqlalchemy.orm import Session

from build_coordinator.types import EventInput
from build_coordinator.models import BuildTaskEvent


def record_event(session: Session, event: EventInput) -> BuildTaskEvent:
    row = BuildTaskEvent(
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
