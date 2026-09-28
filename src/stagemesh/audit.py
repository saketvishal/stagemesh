from __future__ import annotations

import json
from pathlib import Path

from .redaction import redact_mapping
from .persistence import Store


class AuditValidationError(ValueError):
    pass


def record_audit(store: Store, event_type: str, payload: dict[str, object]) -> str:
    event_type = _validate_event_type(event_type)
    return store.add_audit_event(event_type, redact_mapping(payload))


def export_audit_jsonl(store: Store, output: Path, limit: int = 500) -> None:
    limit = _validate_limit(limit)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for event in reversed(store.audit_events(limit)):
            handle.write(
                json.dumps(
                    {
                        "id": event["id"],
                        "event_type": event["event_type"],
                        "payload": json.loads(event["payload"]),
                        "created_at": event["created_at"],
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def _validate_event_type(event_type: str) -> str:
    if not isinstance(event_type, str) or not event_type.strip():
        raise AuditValidationError("audit event type must be a non-empty string")
    if len(event_type) > 200:
        raise AuditValidationError("audit event type must be 200 characters or fewer")
    return event_type.strip()


def _validate_limit(limit: int) -> int:
    if limit < 1:
        raise AuditValidationError("audit export limit must be at least 1")
    if limit > 10000:
        raise AuditValidationError("audit export limit must be 10000 or fewer")
    return limit
