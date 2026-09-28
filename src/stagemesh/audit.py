from __future__ import annotations

import json
from pathlib import Path

from .redaction import redact_mapping
from .persistence import Store


def record_audit(store: Store, event_type: str, payload: dict[str, object]) -> str:
    return store.add_audit_event(event_type, redact_mapping(payload))


def export_audit_jsonl(store: Store, output: Path, limit: int = 500) -> None:
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
