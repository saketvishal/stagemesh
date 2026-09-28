from __future__ import annotations

from dataclasses import dataclass

from .observability import health
from .persistence import Store


@dataclass(frozen=True)
class OperatorReport:
    summary: str
    lines: tuple[str, ...]


def operator_report(store: Store) -> OperatorReport:
    h = health(store)
    workers = store.workers()
    events = store.source_events(limit=10)
    lines = [
        f"tasks={h.task_count}",
        f"running={h.running_count}",
        f"done={h.done_count}",
        f"workers={len(workers)}",
        f"recent_source_events={len(events)}",
    ]
    for worker in workers:
        lines.append(f"worker {worker['id']} provider={worker['provider']} lease_expires_at={worker['lease_expires_at']}")
    for event in events:
        lines.append(f"source_event {event['source']}:{event['source_id']} {event['direction']} {event['status']}")
    return OperatorReport(summary="ok" if h.ok else "degraded", lines=tuple(lines))
