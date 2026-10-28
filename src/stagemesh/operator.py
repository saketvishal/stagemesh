from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from .block_kinds import BlockKind
from .delivery_state import DELIVERY_UNBLOCKED, EXTERNALLY_BLOCKED
from .efficiency import usage_totals
from .execution_ownership import recovery_identity_state, tree_cleanup_state
from .external_evidence import external_evidence_records
from .observability import health
from .persistence import Store
from .process_identity import classify_process, process_identity
from .redaction import redact_output

RECENT_FAILURE_LIMIT = 10
RECENT_METRIC_LIMIT = 100
PENDING_EVENT_LIMIT = 100


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
    provider_selections = _provider_selection_rows(store)
    active_executions = _active_execution_rows(store)
    recent_failures = _recent_failure_rows(store)
    efficiency = _recent_efficiency_row(store)
    pending_decisions = _pending_decision_rows(store)
    pending_count = sum(row["status"] == "RECORDED" for row in pending_decisions)
    stage_counts = Counter(str(task["stage"]) for task in tasks)
    status_counts = Counter(str(task["status"]) for task in tasks)
    attention_rows = []
    if h.blocked_task_count:
        attention_rows.append({"kind": "blocked_tasks", "count": h.blocked_task_count, "status": "ACTION"})
    if h.current_failed_execution_count:
        attention_rows.append({"kind": "failed_executions", "count": h.current_failed_execution_count, "status": "ACTION"})
    historical_failures = h.failed_execution_count - h.current_failed_execution_count
    if historical_failures:
        attention_rows.append({"kind": "failed_execution_history", "count": historical_failures, "status": "INFO"})
    if h.unknown_execution_count:
        attention_rows.append({"kind": "unknown_executions", "count": h.unknown_execution_count, "status": "REVIEW"})
    if h.unresolved_validation_count:
        attention_rows.append({"kind": "unresolved_validation_trees", "count": h.unresolved_validation_count, "status": "REVIEW"})
    if h.unresolved_process_tree_count:
        attention_rows.append({"kind": "unresolved_process_trees", "count": h.unresolved_process_tree_count, "status": "REVIEW"})
    if pending_count:
        attention_rows.append({"kind": "recorded_pending_decisions", "count": pending_count, "status": "ACTION"})
    lines = [
        f"tasks={h.task_count}",
        f"running={h.running_count}",
        f"done={h.done_count}",
        f"backlog={h.backlog_state}",
        f"blocked_tasks={h.blocked_task_count}",
        f"failed_executions={h.failed_execution_count}",
        f"current_failed_executions={h.current_failed_execution_count}",
        f"historical_failed_executions={historical_failures}",
        f"unknown_executions={h.unknown_execution_count}",
        f"unresolved_validation_trees={h.unresolved_validation_count}",
        f"unresolved_process_trees={h.unresolved_process_tree_count}",
        f"workers={len(workers)}",
        f"recent_source_events={len(events)}",
        f"retry_states={len(retries)}",
        f"external_evidence={len(external_evidence)}",
        f"active_executions={len(active_executions)}",
        f"recent_failures={len(recent_failures)} (latest {RECENT_FAILURE_LIMIT}; current/history labelled)",
        f"recent_efficiency={json.dumps(efficiency, sort_keys=True)}",
        "pending_approval_coverage=UNKNOWN (recorded typed blockers only; no global approval registry)",
        f"recorded_pending_decisions={pending_count}",
    ]
    for failure in recent_failures:
        lines.append(
            f"recent_failure task={failure['task_id']} stage={failure['stage']} provider={failure['provider']} "
            f"scope={failure['scope']} candidate={failure['candidate_sha'] or 'unknown'} reason={failure['reason']}"
        )
    for pending in pending_decisions:
        lines.append(f"pending_decision task={pending['task_id']} kind={pending['kind']} status={pending['status']} detail={pending['detail']}")
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
    for selection in provider_selections:
        lines.append(
            "provider_selection "
            f"task={selection['task_id'] or ''} stage={selection['stage']} "
            f"policy={selection['policy']} order={selection['order']}"
        )
    for execution in active_executions:
        lines.append(
            "active_execution "
            f"task={execution['task_id']} stage={execution['stage']} "
            f"provider={execution['provider']} pid={execution['pid_label']} "
            f"process={execution['process_state']} stale={execution['stale']} "
            f"parent={execution['parent_process_state']}"
            + (f" cleanup={execution['cleanup_state']}" if execution["cleanup_state"] else "")
        )
    blocked_bucket_rows = tuple(
        {"reason": reason, "count": count}
        for reason, count in h.blocked_reason_buckets.items()
    )
    for bucket in blocked_bucket_rows:
        lines.append(f"blocked_bucket reason={bucket['reason']} count={bucket['count']}")
    sections = (
        OperatorSection("Active Executions", tuple(active_executions)),
        OperatorSection("Blocked Buckets", blocked_bucket_rows),
        OperatorSection(
            "Stage Summary",
            tuple({"stage": stage, "count": count} for stage, count in sorted(stage_counts.items())),
        ),
        OperatorSection(
            "Task Status Summary",
            tuple({"status": status, "count": count} for status, count in sorted(status_counts.items())),
        ),
        OperatorSection("Attention", tuple(attention_rows)),
        OperatorSection("Recent Failures", tuple(recent_failures)),
        OperatorSection("Recent Efficiency And Usage", (efficiency,)),
        OperatorSection("Recorded Pending Decisions", tuple(pending_decisions)),
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
        OperatorSection("Provider Selections", tuple(provider_selections)),
    )
    return OperatorReport(summary="ok" if h.ok and not pending_count else "degraded", lines=tuple(lines), sections=sections)


def _loads(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _recent_failure_rows(store: Store) -> list[Mapping[str, object]]:
    rows = store.conn.execute(
        "SELECT e.id, e.task_id, e.kind, e.actor, e.candidate_sha, e.result, e.updated_at, "
        "CASE WHEN t.stage!='DONE' AND t.status!='DONE' AND e.id=("
        "SELECT latest.id FROM executions latest WHERE latest.task_id=e.task_id "
        "ORDER BY latest.updated_at DESC, latest.rowid DESC LIMIT 1) THEN 'current' ELSE 'history' END AS scope "
        "FROM executions e JOIN tasks t ON t.id=e.task_id WHERE e.status='FAILED' "
        "ORDER BY e.updated_at DESC, e.rowid DESC LIMIT ?",
        (RECENT_FAILURE_LIMIT,),
    )
    return [
        {
            "id": row["id"], "task_id": row["task_id"], "stage": row["kind"], "provider": row["actor"] or "unknown",
            "candidate_sha": row["candidate_sha"], "scope": row["scope"], "finished_at": row["updated_at"],
            "reason": redact_output(str(row["result"] or "reason not recorded"))[:400],
        }
        for row in rows
    ]


def _recent_efficiency_row(store: Store) -> Mapping[str, object]:
    calls = [
        _loads(row["payload"])
        for row in store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='provider.call' ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (RECENT_METRIC_LIMIT,),
        )
    ]
    for call in calls:
        if not isinstance(call.get("usage"), dict):
            call["usage"] = {}
    ran = reused = 0
    for row in store.conn.execute(
        "SELECT payload FROM evidence WHERE kind='VALIDATION' ORDER BY created_at DESC, rowid DESC LIMIT ?", (RECENT_METRIC_LIMIT,)
    ):
        reuse = _loads(row["payload"]).get("validation_reuse")
        if isinstance(reuse, dict):
            ran += len(reuse["ran"]) if isinstance(reuse.get("ran"), list) else 0
            reused += len(reuse["reused"]) if isinstance(reuse.get("reused"), list) else 0
    validations = store.conn.execute(
        "SELECT started_at, updated_at FROM executions WHERE kind='VALIDATION' AND status!='RUNNING' "
        "ORDER BY updated_at DESC, rowid DESC LIMIT ?", (RECENT_METRIC_LIMIT,)
    ).fetchall()
    durations = [call["duration_seconds"] for call in calls if isinstance(call.get("duration_seconds"), (int, float))]
    return {
        "scope": f"latest {RECENT_METRIC_LIMIT} records per type; samples, not lifetime totals",
        "provider_attempts": len(calls),
        "attempts_by_purpose": dict(Counter(str(call.get("purpose") or "unknown") for call in calls)),
        "failed_provider_attempts": sum(call.get("outcome") in ("failed", "timeout") for call in calls),
        "provider_outcome_unknown": sum(call.get("outcome") not in ("succeeded", "failed", "timeout") for call in calls),
        "provider_seconds": round(sum(durations), 3) if durations else None,
        "provider_duration_unknown": len(calls) - len(durations),
        "checks_executed": ran,
        "checks_reused": reused,
        "validation_seconds": round(sum(max(0.0, row["updated_at"] - row["started_at"]) for row in validations), 3),
        "usage": usage_totals(calls),
        "billing_cost": "UNKNOWN",
    }


def _pending_decision_rows(store: Store) -> list[Mapping[str, object]]:
    """Show only typed, still-current facts in a bounded journal sample. No inference that a missing event grants permission."""
    rows = store.conn.execute(
        "SELECT event_type, payload, created_at FROM audit_events WHERE event_type IN "
        "('task.block_classified', 'task.blocked', 'task.unblocked', 'task.block_reentered', ?, ?, 'window.fast_forward_merged') "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (EXTERNALLY_BLOCKED, DELIVERY_UNBLOCKED, PENDING_EVENT_LIMIT)
    )
    pending: list[Mapping[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        payload = _loads(row["payload"])
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            continue
        event = row["event_type"]
        category = "human_decision" if str(event).startswith("task.") else "delivery_permission"
        key = (category, task_id)
        if key in seen:
            continue
        task = store.get_task(task_id)
        if task is None:
            continue
        candidate = store.latest_candidate(task_id)
        sha = None if candidate is None else candidate["sha"]
        if category == "delivery_permission" and event != DELIVERY_UNBLOCKED and payload.get("candidate_sha") not in (None, sha):
            continue  # A merge or blocker for an obsolete candidate says nothing about the current delivery.
        seen.add(key)
        if category == "human_decision":
            if task["status"] != "BLOCKED" or event != "task.block_classified" or payload.get("kind") != BlockKind.HUMAN_DECISION:
                continue
        elif event != EXTERNALLY_BLOCKED or payload.get("candidate_sha") not in (None, sha):
            continue
        detail = payload.get("reason") or "reason not recorded"
        if category == "delivery_permission":
            detail = f"needs {payload.get('needed') or 'unspecified permission'}: {detail}"
        pending.append({
            "task_id": task_id, "kind": category, "status": "RECORDED", "candidate_sha": payload.get("candidate_sha"),
            "detail": redact_output(str(detail))[:400], "recorded_at": row["created_at"],
        })
    pending.append({
        "task_id": None, "kind": "approval_coverage", "status": "UNKNOWN", "candidate_sha": None,
        "detail": f"latest {PENDING_EVENT_LIMIT} relevant events only; missing records do not establish permission or zero pending approvals",
        "recorded_at": None,
    })
    return pending


def _active_execution_rows(store: Store) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for execution in store.running_executions():
        pid = execution["pid"]
        parent_state = classify_process(
            store.execution_process_identity(execution["id"]),
            process_identity(pid),
        )
        process_state = recovery_identity_state(execution, parent_state)
        provider = execution["actor"] or "unknown"
        rows.append(
            {
                "id": execution["id"],
                "task_id": execution["task_id"],
                "stage": execution["kind"],
                "provider": provider,
                "pid": pid,
                "pid_label": str(pid) if pid is not None else "no pid recorded",
                "process_state": process_state,
                "parent_process_state": parent_state,
                "cleanup_state": tree_cleanup_state(execution),
                "stale": process_state == "DEAD",
            }
        )
    return rows


def _provider_selection_rows(store: Store) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for event in store.audit_events(limit=50):
        if event["event_type"] != "provider.selection":
            continue
        try:
            payload = json.loads(event["payload"])
        except (TypeError, ValueError):
            continue
        order = payload.get("order")
        verdicts = payload.get("verdicts")
        rows.append(
            {
                "task_id": payload.get("task_id"),
                "stage": payload.get("stage"),
                "policy": payload.get("policy"),
                "order": order if isinstance(order, list) else [],
                "verdicts": verdicts if isinstance(verdicts, list) else [],
                "created_at": event["created_at"],
            }
        )
    return rows
