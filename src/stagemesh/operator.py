from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from .external_evidence import external_evidence_records
from .observability import health
from .persistence import Store


@dataclass(frozen=True)
class OperatorSection:
    name: str
    rows: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class OperatorReport:
    summary: str
    lines: tuple[str, ...]
    sections: tuple[OperatorSection, ...]


def operator_report(store: Store) -> OperatorReport:
    h = health(store)
    tasks = store.tasks()
    workers = store.workers()
    events = store.source_events(limit=10)
    retries = store.retry_states()
    external_evidence = external_evidence_records(store)
    stage_counts = Counter(str(task["stage"]) for task in tasks)
    status_counts = Counter(str(task["status"]) for task in tasks)
    attention_rows = []
    if h.blocked_task_count:
        attention_rows.append({"kind": "blocked_tasks", "count": h.blocked_task_count, "status": "ACTION"})
    if h.failed_execution_count:
        attention_rows.append({"kind": "failed_executions", "count": h.failed_execution_count, "status": "ACTION"})
    if h.unknown_execution_count:
        attention_rows.append({"kind": "unknown_executions", "count": h.unknown_execution_count, "status": "REVIEW"})
    lines = [
        f"tasks={h.task_count}",
        f"running={h.running_count}",
        f"done={h.done_count}",
        f"backlog={h.backlog_state}",
        f"blocked_tasks={h.blocked_task_count}",
        f"failed_executions={h.failed_execution_count}",
        f"unknown_executions={h.unknown_execution_count}",
        f"workers={len(workers)}",
        f"recent_source_events={len(events)}",
        f"retry_states={len(retries)}",
        f"external_evidence={len(external_evidence)}",
    ]
    for task in tasks:
        lines.append(f"task {task['id']} stage={task['stage']} status={task['status']} title={task['title']}")
    for worker in workers:
        lines.append(f"worker {worker['id']} provider={worker['provider']} lease_expires_at={worker['lease_expires_at']}")
    for event in events:
        lines.append(f"source_event {event['source']}:{event['source_id']} {event['direction']} {event['status']}")
    for retry in retries:
        lines.append(f"retry {retry['key']} attempts={retry['attempts']} reason={retry['reason']}")
    for evidence in external_evidence:
        lines.append(f"external_evidence {evidence.kind} {evidence.status} {evidence.candidate_sha or ''}")
    sections = (
        OperatorSection(
            "Stage Summary",
            tuple({"stage": stage, "count": count} for stage, count in sorted(stage_counts.items())),
        ),
        OperatorSection(
            "Task Status Summary",
            tuple({"status": status, "count": count} for status, count in sorted(status_counts.items())),
        ),
        OperatorSection("Attention", tuple(attention_rows)),
        OperatorSection(
            "Tasks",
            tuple(
                {
                    "id": task["id"],
                    "stage": task["stage"],
                    "status": task["status"],
                    "title": task["title"],
                    "source": task["source"],
                }
                for task in tasks
            ),
        ),
        OperatorSection(
            "Workers",
            tuple(
                {
                    "id": worker["id"],
                    "provider": worker["provider"],
                    "capabilities": worker["capabilities"],
                    "lease_expires_at": worker["lease_expires_at"],
                }
                for worker in workers
            ),
        ),
        OperatorSection(
            "Source Events",
            tuple(
                {
                    "source": event["source"],
                    "source_id": event["source_id"],
                    "direction": event["direction"],
                    "status": event["status"],
                }
                for event in events
            ),
        ),
        OperatorSection(
            "Retries",
            tuple(
                {
                    "key": retry["key"],
                    "attempts": retry["attempts"],
                    "next_attempt_at": retry["next_attempt_at"],
                    "reason": retry["reason"],
                }
                for retry in retries
            ),
        ),
        OperatorSection(
            "External Evidence",
            tuple(
                {
                    "kind": evidence.kind,
                    "status": evidence.status,
                    "candidate_sha": evidence.candidate_sha or "",
                    "url": evidence.url,
                }
                for evidence in external_evidence
            ),
        ),
    )
    return OperatorReport(summary="ok" if h.ok else "degraded", lines=tuple(lines), sections=sections)
