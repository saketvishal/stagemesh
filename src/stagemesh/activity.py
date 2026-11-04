"""Read-only activity query over existing task, execution, and audit records.

Display only: it runs SELECTs on rows the runner already writes, adds no schema, and never
mutates state. Results are bounded (page size and scan cap) and ordered newest first with a
stable tie-break, so pages never overlap. Free-form text (execution results, audit payloads,
provider output) is never copied into entries; result codes use a fixed outcome allowlist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any, Literal

from .domain import ExecutionStatus, Stage, TaskStatus
from .persistence import Store
from .redaction import redact_url_credentials

ActivityKind = Literal["task", "execution", "audit", "source", "task_transition", "retry"]
ActivityState = Literal["open", "closed"]
TimeBasis = Literal["activity", "opened", "updated", "completed"]
RetryKind = Literal["automatic_provider", "blocked_reentry", "remediation", "operator"]

STATES: tuple[str, ...] = ("open", "closed")
TIME_BASES: tuple[str, ...] = ("activity", "opened", "updated", "completed")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SCAN = 1000  # retained rows per source and across the combined projection
MAX_TITLE_CHARS = 200
RETRY_KINDS: tuple[str, ...] = ("automatic_provider", "blocked_reentry", "remediation", "operator")
_KIND_ORDER = {"task_transition": 0, "retry": 1, "source": 2, "audit": 3, "execution": 4, "task": 5}
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
# Fixed application outcomes only: identifier-shaped provider text may still be a secret.
_SAFE_RESULTS = frozenset({
    "capacity", "cleanup_incomplete", "executor_aborted", "external_workspace_mutation",
    "fenced_identity_unknown", "findings", "no_implementation_change", "operator_stop",
    "orphaned", "provider_permission_denied", "provider_timeout", "provider_unavailable",
    "recovered_stale", "tool_execution_failure",
})
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SECRET_MARKERS = ("token", "secret", "password", "authorization", "apikey", "api_key", "api-key")


class ActivityQueryError(ValueError):
    pass


@dataclass(frozen=True)
class ActivityQuery:
    state: ActivityState | None = None
    task_id: str | None = None
    stage: str | None = None
    provider: str | None = None
    since: datetime | None = None  # inclusive, timezone-aware
    until: datetime | None = None  # exclusive, timezone-aware
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    project: str | None = None
    time_basis: TimeBasis = "activity"  # preserves the original mixed-source timestamps
    source: str | None = None
    source_status: str | None = None
    include_source_history: bool = False
    task_event: Literal["status_changed", "rework_required"] | None = None
    task_status: str | None = None
    include_task_history: bool = False
    retry_kind: RetryKind | None = None
    include_retry_history: bool = False


@dataclass(frozen=True)
class ActivityEntry:
    kind: ActivityKind
    id: str
    at: datetime  # UTC
    state: ActivityState
    task_id: str | None = None
    stage: str | None = None
    status: str | None = None
    provider: str | None = None
    title: str | None = None
    execution_kind: str | None = None
    event_type: str | None = None
    candidate_sha: str | None = None
    result_code: str | None = None
    project: str | None = None  # present recorded task attribution, not historical membership
    source: str | None = None
    source_id: str | None = None
    source_status: str | None = None
    previous_source_status: str | None = None
    previous_task_status: str | None = None
    current_task_status: str | None = None
    rework_required: bool = False
    retry_kind: RetryKind | None = None
    retry_state: Literal["QUEUED"] | None = None
    retry_attempt: int | None = None
    retry_limit: int | None = None


@dataclass(frozen=True)
class ActivityPage:
    entries: tuple[ActivityEntry, ...]
    total: int  # matches retained within the combined cap
    limit: int
    offset: int
    next_offset: int | None
    truncated: bool  # a source or combined cap excluded records; `total` may undercount
    time_basis: TimeBasis = "activity"
    status_counts: dict[str, dict[str, int]] = dataclass_field(default_factory=dict)


def validate_query(query: ActivityQuery) -> ActivityQuery:
    if not isinstance(query, ActivityQuery):
        raise ActivityQueryError("activity query must be an ActivityQuery")
    if query.state is not None and query.state not in STATES:
        raise ActivityQueryError(f"activity state must be one of: {', '.join(STATES)}")
    if query.time_basis not in TIME_BASES:
        raise ActivityQueryError(f"activity time_basis must be one of: {', '.join(TIME_BASES)}")
    if query.stage is not None and query.stage not in {stage.value for stage in Stage}:
        raise ActivityQueryError(f"activity stage must be one of: {', '.join(stage.value for stage in Stage)}")
    for field, value in (
        ("task_id", query.task_id),
        ("provider", query.provider),
        ("project", query.project),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise ActivityQueryError(f"activity {field} must be a non-empty string of 200 characters or fewer")
    for field, value in (
        ("source", query.source),
        ("source_status", query.source_status),
        ("task_status", query.task_status),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise ActivityQueryError(f"activity {field} must be a non-empty string of 200 characters or fewer")
        if value is not None and _safe_code(value) is None:
            raise ActivityQueryError(f"activity {field} must be a bounded non-secret identifier")
    if not isinstance(query.include_source_history, bool):
        raise ActivityQueryError("activity include_source_history must be a boolean")
    if query.task_event not in {None, "status_changed", "rework_required"}:
        raise ActivityQueryError("activity task_event must be status_changed or rework_required")
    if query.task_status is not None and query.task_status not in {
        status.value for status in TaskStatus
    }:
        raise ActivityQueryError(
            "activity task_status must be one of: "
            + ", ".join(status.value for status in TaskStatus)
        )
    if not isinstance(query.include_task_history, bool):
        raise ActivityQueryError("activity include_task_history must be a boolean")
    if query.task_event is not None and not query.include_task_history:
        raise ActivityQueryError("activity task_event requires include_task_history")
    if not isinstance(query.include_retry_history, bool):
        raise ActivityQueryError("activity include_retry_history must be a boolean")
    if query.retry_kind is not None and query.retry_kind not in RETRY_KINDS:
        raise ActivityQueryError(
            "activity retry_kind must be one of: " + ", ".join(RETRY_KINDS)
        )
    if query.retry_kind is not None and not query.include_retry_history:
        raise ActivityQueryError("activity retry_kind requires include_retry_history")
    if query.task_event is not None and query.retry_kind is not None:
        raise ActivityQueryError("activity task_event and retry_kind cannot be combined")
    for field, value in (("since", query.since), ("until", query.until)):
        if value is None:
            continue
        if not isinstance(value, datetime):
            raise ActivityQueryError(f"activity {field} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ActivityQueryError(f"activity {field} must be timezone-aware")
    if query.since is not None and query.until is not None and query.since >= query.until:
        raise ActivityQueryError("activity since must be earlier than until")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_LIMIT:
        raise ActivityQueryError(f"activity limit must be an integer from 1 to {MAX_LIMIT}")
    if isinstance(query.offset, bool) or not isinstance(query.offset, int) or query.offset < 0:
        raise ActivityQueryError("activity offset must be a non-negative integer")
    if query.offset >= MAX_SCAN:
        raise ActivityQueryError(f"activity offset must be less than {MAX_SCAN}")
    return query


def query_activity(store: Store, query: ActivityQuery | None = None) -> ActivityPage:
    query = validate_query(query or ActivityQuery())
    entries: list[ActivityEntry] = []
    truncated = False
    # Task and audit rows carry no provider; a provider filter selects executions only.
    sources = []
    if query.task_event is None and query.retry_kind is None and query.provider is None and query.time_basis != "completed":
        sources.append(_task_entries)
    if query.task_event is None and query.retry_kind is None:
        sources.append(_execution_entries)
    if (query.task_event is None and query.retry_kind is None and query.provider is None and query.state != "open" and query.time_basis != "opened"
            and (query.stage is None or (query.time_basis == "completed" and query.stage == Stage.DONE.value))):
        sources.append(_audit_entries)
    if (
        query.include_source_history
        and query.provider is None
        and query.time_basis in {"activity", "updated"}
        and query.task_event is None
        and query.retry_kind is None
    ):
        sources.append(_source_entries)
    if (
        query.include_task_history
        and query.provider is None
        and query.time_basis in {"activity", "updated"}
    ):
        sources.append(_task_transition_entries)
    if (
        query.include_retry_history
        and query.provider is None
        and query.time_basis in {"activity", "updated"}
        and query.task_event is None
    ):
        sources.append(_retry_entries)
    for source in sources:
        found, cut = source(store, query)
        entries.extend(found)
        truncated = truncated or cut
    entries.sort(key=lambda e: (-e.at.timestamp(), _KIND_ORDER[e.kind], e.id))
    truncated = truncated or len(entries) > MAX_SCAN
    entries = entries[:MAX_SCAN]
    page = tuple(entries[query.offset : query.offset + query.limit])
    end = query.offset + len(page)
    return ActivityPage(
        entries=page,
        total=len(entries),
        limit=query.limit,
        offset=query.offset,
        next_offset=end if end < len(entries) else None,
        truncated=truncated,
        time_basis=query.time_basis,
        status_counts=_status_counts(entries),
    )


def _status_counts(entries: list[ActivityEntry]) -> dict[str, dict[str, int]]:
    """Count retained matching rows before pagination, never blend attempts/events into tasks."""
    counts = {
        "task": {**{status.value: 0 for status in TaskStatus}, "UNKNOWN": 0},
        "execution": {status.value: 0 for status in ExecutionStatus},
        "audit": {"DONE": 0, "EVENT": 0},
        "source": {"OPEN": 0, "CLOSED": 0, "UNKNOWN": 0},
        "task_transition": {
            "OPEN": 0,
            "CLAIMED": 0,
            "BLOCKED": 0,
            "DONE": 0,
            "REWORK_REQUIRED": 0,
            "UNKNOWN": 0,
        },
        "retry": {**{kind: 0 for kind in RETRY_KINDS}, "UNKNOWN": 0},
    }
    for entry in entries:
        group = counts[entry.kind]
        observed = (
            entry.source_status
            if entry.kind == "source"
            else (entry.retry_kind if entry.kind == "retry" else entry.status)
        )
        status = observed if observed in group else ("EVENT" if entry.kind == "audit" else "UNKNOWN")
        group[status] += 1
    return counts


def _time_clause(column: str, query: ActivityQuery, params: list[Any]) -> list[str]:
    clauses = []
    if query.since is not None:
        clauses.append(f"{column} >= ?")
        params.append(query.since.timestamp())
    if query.until is not None:
        clauses.append(f"{column} < ?")
        params.append(query.until.timestamp())
    return clauses


def _where(clauses: list[str]) -> str:
    return f" WHERE {' AND '.join(clauses)}" if clauses else ""


def _at(value: float) -> datetime:
    return datetime.fromtimestamp(float(value), UTC)


def _task_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    params: list[Any] = []
    column = "t.created_at" if query.time_basis == "opened" else "t.updated_at"
    clauses = _time_clause(column, query, params)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    if query.task_id is not None:
        clauses.append("t.id = ?")
        params.append(query.task_id)
    if query.stage is not None:
        clauses.append("t.stage = ?")
        params.append(query.stage)
    if query.state == "open":
        clauses.append("t.status != ?")
        params.append(TaskStatus.DONE.value)
    elif query.state == "closed":
        clauses.append("t.status = ?")
        params.append(TaskStatus.DONE.value)
    _current_task_status_filter(clauses, params, query, "t")
    _source_filters(clauses, params, query, task_alias="t", cache_alias="s")
    rows = store.conn.execute(
        f"SELECT t.id, t.title, t.stage, t.status, t.project, t.source, t.source_id, "
        f"s.status AS source_status, {column} AS at FROM tasks t "
        f"LEFT JOIN source_cache s ON s.source=t.source AND s.source_id=t.source_id{_where(clauses)} "
        f"ORDER BY {column} DESC, t.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries = [
        ActivityEntry(
            kind="task",
            id=row["id"],
            at=_at(row["at"]),
            state="closed" if row["status"] == TaskStatus.DONE.value else "open",
            task_id=row["id"],
            stage=row["stage"],
            status=row["status"],
            title=_safe_title(row["title"]),
            project=_safe_project(row["project"]),
            source=_safe_code(row["source"]),
            source_id=_safe_identifier(row["source_id"]),
            source_status=_safe_code(row["source_status"]),
            current_task_status=row["status"],
        )
        for row in rows[:MAX_SCAN]
    ]
    return entries, len(rows) > MAX_SCAN


def _execution_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    params: list[Any] = []
    column = "e.updated_at" if query.time_basis in {"updated", "completed"} else "e.started_at"
    clauses = _time_clause(column, query, params)
    if query.time_basis == "completed":
        # UNKNOWN can retain a live provider: it is not proof of terminalization.
        clauses.append("e.status IN (?, ?)")
        params.extend((ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value))
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    if query.task_id is not None:
        clauses.append("e.task_id = ?")
        params.append(query.task_id)
    if query.stage is not None:
        clauses.append("t.stage = ?")
        params.append(query.stage)
    if query.provider is not None:
        clauses.append("e.actor = ?")
        params.append(query.provider)
    if query.state == "open":
        clauses.append("e.status = ?")
        params.append(ExecutionStatus.RUNNING.value)
    elif query.state == "closed":
        clauses.append("e.status != ?")
        params.append(ExecutionStatus.RUNNING.value)
    _current_task_status_filter(clauses, params, query, "t")
    _source_filters(clauses, params, query, task_alias="t", cache_alias="s")
    rows = store.conn.execute(
        f"SELECT e.id, e.task_id, e.kind, e.status, e.actor, e.candidate_sha, e.result, {column} AS at, "
        f"t.stage, t.status AS task_status, t.project, t.source, t.source_id, s.status AS source_status "
        f"FROM executions e JOIN tasks t ON t.id = e.task_id "
        f"LEFT JOIN source_cache s ON s.source=t.source AND s.source_id=t.source_id{_where(clauses)} "
        f"ORDER BY {column} DESC, e.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries = [
        ActivityEntry(
            kind="execution",
            id=row["id"],
            at=_at(row["at"]),
            state="open" if row["status"] == ExecutionStatus.RUNNING.value else "closed",
            task_id=row["task_id"],
            stage=row["stage"],
            status=row["status"],
            provider=_safe_code(row["actor"]),
            execution_kind=row["kind"],
            candidate_sha=row["candidate_sha"] if row["candidate_sha"] and _SAFE_SHA.match(row["candidate_sha"]) else None,
            result_code=row["result"] if row["result"] in _SAFE_RESULTS else None,
            project=_safe_project(row["project"]),
            source=_safe_code(row["source"]),
            source_id=_safe_identifier(row["source_id"]),
            source_status=_safe_code(row["source_status"]),
            current_task_status=row["task_status"],
        )
        for row in rows[:MAX_SCAN]
    ]
    return entries, len(rows) > MAX_SCAN


def _audit_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    """Audit events are history only (always closed); only the event type and task id are exposed."""
    params: list[Any] = []
    clauses = _time_clause("a.created_at", query, params)
    hidden_typed_events: list[str] = []
    if query.include_task_history:
        # Typed task occurrences replace their generic audit rows only in the opt-in feed.
        hidden_typed_events.extend(("task.status_changed", "task.remediation_queued"))
    if query.include_retry_history:
        hidden_typed_events.extend(
            (
                "task.auto_retried_provider_failure",
                "task.block_reentered",
                "task.remediation_queued",
                "task.operator_retry_requested",
            )
        )
    hidden_typed_events = list(dict.fromkeys(hidden_typed_events))
    if hidden_typed_events:
        clauses.append(f"a.event_type NOT IN ({','.join('?' for _ in hidden_typed_events)})")
        params.extend(hidden_typed_events)
    task_id_sql = _audit_text("task_id")
    if query.task_id is not None:
        clauses.append(f"{task_id_sql} = ?")
        params.append(query.task_id)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    _current_task_status_filter(clauses, params, query, "t")
    _source_filters(clauses, params, query, task_alias="t", cache_alias="s")
    completed = query.time_basis == "completed"
    if completed:
        prior_stages = [stage.value for stage in Stage if stage is not Stage.DONE]
        clauses.extend(("a.event_type = ?", f"{_audit_text('to')} = ?",
                        f"{_audit_text('from')} IN ({','.join('?' for _ in prior_stages)})",
                        f"length({task_id_sql}) > 0"))
        params.extend(("stage.transition", Stage.DONE.value, *prior_stages))
    rows = store.conn.execute(
        "SELECT a.id, a.event_type, a.payload, a.created_at, t.status AS task_status, "
        "t.project, t.source, t.source_id, "
        "s.status AS source_status FROM audit_events a "
        f"LEFT JOIN tasks t ON t.id = {task_id_sql} "
        f"LEFT JOIN source_cache s ON s.source=t.source AND s.source_id=t.source_id{_where(clauses)} "
        "ORDER BY a.created_at DESC, a.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries = []
    for row in rows[:MAX_SCAN]:
        task_id = _payload_task_id(row["payload"])
        entries.append(
            ActivityEntry(
                kind="audit",
                id=row["id"],
                at=_at(row["created_at"]),
                state="closed",
                task_id=task_id,
                event_type=row["event_type"] if _SAFE_CODE.match(row["event_type"]) else None,
                stage=Stage.DONE.value if completed else None,
                status=TaskStatus.DONE.value if completed else None,
                project=_safe_project(row["project"]),
                source=_safe_code(row["source"]),
                source_id=_safe_identifier(row["source_id"]),
                source_status=_safe_code(row["source_status"]),
                current_task_status=(
                    row["task_status"]
                    if row["task_status"] in {status.value for status in TaskStatus}
                    else None
                ),
            )
        )
    return entries, len(rows) > MAX_SCAN


def _source_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    """Durable linked-source status transitions; malformed payloads are ignored."""
    params: list[Any] = []
    clauses = _time_clause("e.created_at", query, params)
    clauses.append("e.status = ?")
    params.append("STATUS_CHANGED")
    if query.task_id is not None:
        clauses.append("t.id = ?")
        params.append(query.task_id)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    if query.stage is not None:
        clauses.append(f"{_task_event_text('stage')} = ?")
        params.append(query.stage)
    if query.state == "open":
        clauses.append("t.status != ?")
        params.append(TaskStatus.DONE.value)
    elif query.state == "closed":
        clauses.append("t.status = ?")
        params.append(TaskStatus.DONE.value)
    _current_task_status_filter(clauses, params, query, "t")
    if query.source is not None:
        clauses.append("e.source = ?")
        params.append(query.source)
    if query.source_status is not None:
        clauses.append(f"{_source_event_text('current_status')} = ?")
        params.append(query.source_status)
    rows = store.conn.execute(
        "SELECT e.id, e.source, e.source_id, e.payload, e.created_at, "
        "t.id AS task_id, t.stage, t.status AS task_status, t.project FROM source_events e "
        "LEFT JOIN tasks t ON t.source=e.source AND t.source_id=e.source_id"
        f"{_where(clauses)} ORDER BY e.created_at DESC, e.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries: list[ActivityEntry] = []
    for row in rows[:MAX_SCAN]:
        previous = _source_event_value(row["payload"], "previous_status")
        current = _source_event_value(row["payload"], "current_status")
        if previous is None or current is None:
            continue
        entries.append(
            ActivityEntry(
                kind="source",
                id=row["id"],
                at=_at(row["created_at"]),
                state=(
                    "closed"
                    if row["task_id"] is None or row["task_status"] == TaskStatus.DONE.value
                    else "open"
                ),
                task_id=_safe_identifier(row["task_id"]),
                stage=row["stage"] if row["stage"] in {stage.value for stage in Stage} else None,
                status=row["task_status"] if row["task_status"] in {status.value for status in TaskStatus} else None,
                event_type="source.status_changed",
                project=_safe_project(row["project"]),
                source=_safe_code(row["source"]),
                source_id=_safe_identifier(row["source_id"]),
                source_status=current,
                previous_source_status=previous,
                current_task_status=(
                    row["task_status"]
                    if row["task_status"] in {status.value for status in TaskStatus}
                    else None
                ),
            )
        )
    return entries, len(rows) > MAX_SCAN


def _task_transition_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    """Durable task status and rework occurrences; current task state stays separate."""
    params: list[Any] = []
    clauses = _time_clause("a.created_at", query, params)
    task_id_sql = _audit_text("task_id")
    event_types = {
        "status_changed": ("task.status_changed",),
        "rework_required": ("task.remediation_queued",),
        None: ("task.status_changed", "task.remediation_queued"),
    }[query.task_event]
    clauses.append(f"a.event_type IN ({','.join('?' for _ in event_types)})")
    params.extend(event_types)
    if query.task_id is not None:
        clauses.append(f"{task_id_sql} = ?")
        params.append(query.task_id)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    if query.stage is not None:
        clauses.append("t.stage = ?")
        params.append(query.stage)
    if query.state == "open":
        clauses.append("t.status != ?")
        params.append(TaskStatus.DONE.value)
    elif query.state == "closed":
        clauses.append("t.status = ?")
        params.append(TaskStatus.DONE.value)
    if query.task_status is not None:
        clauses.append("a.event_type = ?")
        params.append("task.status_changed")
        clauses.append(f"{_task_event_text('current_status')} = ?")
        params.append(query.task_status)
    _source_filters(clauses, params, query, task_alias="t", cache_alias="s")
    rows = store.conn.execute(
        "SELECT a.id, a.event_type, a.payload, a.created_at, t.id AS task_id, "
        "t.stage AS task_stage, t.status AS task_status, t.project, t.source, t.source_id, "
        "s.status AS source_status FROM audit_events a "
        f"LEFT JOIN tasks t ON t.id = {task_id_sql} "
        f"LEFT JOIN source_cache s ON s.source=t.source AND s.source_id=t.source_id{_where(clauses)} "
        "ORDER BY a.created_at DESC, a.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries: list[ActivityEntry] = []
    for row in rows[:MAX_SCAN]:
        payload = _task_event_payload(row["payload"])
        if payload is None:
            continue
        current_task_status = (
            row["task_status"]
            if row["task_status"] in {status.value for status in TaskStatus}
            else None
        )
        common = {
            "kind": "task_transition",
            "id": row["id"],
            "at": _at(row["created_at"]),
            "state": "closed" if current_task_status in {None, TaskStatus.DONE.value} else "open",
            "task_id": _safe_identifier(row["task_id"]),
            "project": _safe_project(row["project"]),
            "source": _safe_code(row["source"]),
            "source_id": _safe_identifier(row["source_id"]),
            "source_status": _safe_code(row["source_status"]),
            "current_task_status": current_task_status,
        }
        if row["event_type"] == "task.status_changed":
            previous = _task_status_value(payload.get("previous_status"))
            current = _task_status_value(payload.get("current_status"))
            stage = _task_stage_value(payload.get("stage"))
            cause = _safe_code(payload.get("cause"))
            if previous is None or current is None or stage is None or cause is None:
                continue
            entries.append(
                ActivityEntry(
                    **common,
                    stage=stage,
                    status=current,
                    event_type="task.status_changed",
                    previous_task_status=previous,
                )
            )
            continue
        candidate = payload.get("candidate_sha")
        failed_stage = _task_stage_value(payload.get("stage"))
        if not isinstance(candidate, str) or not _SAFE_SHA.fullmatch(candidate) or failed_stage is None:
            continue
        entries.append(
            ActivityEntry(
                **common,
                stage=failed_stage,
                status="REWORK_REQUIRED",
                event_type="task.rework_required",
                candidate_sha=candidate,
                rework_required=True,
            )
        )
    return entries, len(rows) > MAX_SCAN


def _retry_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    """Typed queued retry occurrences from an explicit allowlist of durable events."""
    event_kinds = {
        "task.auto_retried_provider_failure": "automatic_provider",
        "task.block_reentered": "blocked_reentry",
        "task.remediation_queued": "remediation",
        "task.operator_retry_requested": "operator",
    }
    selected = (
        tuple(event for event, kind in event_kinds.items() if kind == query.retry_kind)
        if query.retry_kind is not None
        else tuple(event_kinds)
    )
    params: list[Any] = list(selected)
    clauses = [f"a.event_type IN ({','.join('?' for _ in selected)})"]
    clauses.extend(_time_clause("a.created_at", query, params))
    task_id_sql = _audit_text("task_id")
    if query.task_id is not None:
        clauses.append(f"{task_id_sql} = ?")
        params.append(query.task_id)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    if query.stage is not None:
        clauses.append("t.stage = ?")
        params.append(query.stage)
    if query.state == "open":
        clauses.append("t.status != ?")
        params.append(TaskStatus.DONE.value)
    elif query.state == "closed":
        clauses.append("t.status = ?")
        params.append(TaskStatus.DONE.value)
    _current_task_status_filter(clauses, params, query, "t")
    _source_filters(clauses, params, query, task_alias="t", cache_alias="s")
    rows = store.conn.execute(
        "SELECT a.id, a.event_type, a.payload, a.created_at, t.id AS task_id, "
        "t.stage AS task_stage, t.status AS task_status, t.project, t.source, t.source_id, "
        "s.status AS source_status FROM audit_events a "
        f"LEFT JOIN tasks t ON t.id = {task_id_sql} "
        f"LEFT JOIN source_cache s ON s.source=t.source AND s.source_id=t.source_id{_where(clauses)} "
        "ORDER BY a.created_at DESC, a.id LIMIT ?",
        (*params, MAX_SCAN + 1),
    ).fetchall()
    entries: list[ActivityEntry] = []
    for row in rows[:MAX_SCAN]:
        payload = _task_event_payload(row["payload"])
        kind = event_kinds.get(str(row["event_type"]))
        task_id = _safe_identifier(row["task_id"])
        if payload is None or kind is None or task_id is None:
            continue
        candidate = payload.get("candidate_sha")
        candidate_sha = candidate if isinstance(candidate, str) and _SAFE_SHA.fullmatch(candidate) else None
        attempt = _bounded_positive_int(payload.get("attempt"))
        limit = _bounded_positive_int(payload.get("of"))
        if (attempt is None) != (limit is None) or (
            attempt is not None and limit is not None and attempt > limit
        ):
            attempt = limit = None
        current_task_status = (
            row["task_status"]
            if row["task_status"] in {status.value for status in TaskStatus}
            else None
        )
        entries.append(
            ActivityEntry(
                kind="retry",
                id=row["id"],
                at=_at(row["created_at"]),
                state="closed" if current_task_status in {None, TaskStatus.DONE.value} else "open",
                task_id=task_id,
                stage=_task_stage_value(payload.get("stage")) or _task_stage_value(row["task_stage"]),
                status="QUEUED",
                event_type="task.retry_queued",
                candidate_sha=candidate_sha,
                project=_safe_project(row["project"]),
                source=_safe_code(row["source"]),
                source_id=_safe_identifier(row["source_id"]),
                source_status=_safe_code(row["source_status"]),
                current_task_status=current_task_status,
                retry_kind=kind,  # type: ignore[arg-type]
                retry_state="QUEUED",
                retry_attempt=attempt,
                retry_limit=limit,
            )
        )
    return entries, len(rows) > MAX_SCAN


def _source_filters(
    clauses: list[str],
    params: list[Any],
    query: ActivityQuery,
    *,
    task_alias: str,
    cache_alias: str,
) -> None:
    if query.source is not None:
        clauses.append(f"{task_alias}.source = ?")
        params.append(query.source)
    if query.source_status is not None:
        clauses.append(f"{cache_alias}.status = ?")
        params.append(query.source_status)


def _current_task_status_filter(
    clauses: list[str], params: list[Any], query: ActivityQuery, task_alias: str
) -> None:
    if query.task_status is not None:
        clauses.append(f"{task_alias}.status = ?")
        params.append(query.task_status)


def _audit_text(name: Literal["task_id", "from", "to"]) -> str:
    # SQL identifiers/JSON paths are fixed by the caller, values remain parameters.
    # Guard malformed JSON and non-text payload fields before joining/filtering.
    return (f"CASE WHEN json_valid(a.payload) THEN CASE WHEN json_type(a.payload, '$.{name}') = 'text' "
            f"AND length(json_extract(a.payload, '$.{name}')) <= 200 "
            f"THEN json_extract(a.payload, '$.{name}') END END")


def _task_event_text(name: Literal["current_status", "stage"]) -> str:
    return (
        f"CASE WHEN json_valid(a.payload) AND json_type(a.payload) = 'object' "
        f"AND json_type(a.payload, '$.{name}') = 'text' "
        f"THEN json_extract(a.payload, '$.{name}') END"
    )


def _task_event_payload(payload: object) -> dict[str, object] | None:
    try:
        value = json.loads(payload) if isinstance(payload, str) else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _bounded_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1_000_000 else None


def _task_status_value(value: object) -> str | None:
    return value if value in {status.value for status in TaskStatus} else None


def _task_stage_value(value: object) -> str | None:
    return value if value in {stage.value for stage in Stage} else None


def _source_event_text(name: Literal["previous_status", "current_status"]) -> str:
    return (
        f"CASE WHEN json_valid(e.payload) AND json_type(e.payload) = 'object' "
        f"AND json_type(e.payload, '$.{name}') = 'text' "
        f"AND length(json_extract(e.payload, '$.{name}')) <= 200 "
        f"THEN json_extract(e.payload, '$.{name}') END"
    )


def _source_event_value(payload: object, name: Literal["previous_status", "current_status"]) -> str | None:
    try:
        data = json.loads(payload) if isinstance(payload, str) else None
    except (TypeError, ValueError):
        return None
    value = data.get(name) if isinstance(data, dict) else None
    return _safe_code(value)


def _safe_project(project: object) -> str | None:
    return _safe_title(project) if isinstance(project, str) and project.strip() else None


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    return _safe_title(value)


def _payload_task_id(payload: str) -> str | None:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    task_id = data.get("task_id") if isinstance(data, dict) else None
    return task_id if isinstance(task_id, str) and len(task_id) <= 200 else None


def _safe_code(value: object) -> str | None:
    """Identifier-shaped values only; anything free-form or secret-looking is dropped."""
    if not isinstance(value, str) or not _SAFE_CODE.match(value):
        return None
    if any(marker in value.lower() for marker in _SECRET_MARKERS):
        return None
    return value


def _safe_title(title: str) -> str:
    words = []
    for word in str(title).split():
        if any(marker in word.lower() for marker in _SECRET_MARKERS):
            # The value may be separated, quoted, or multiword: hide the whole suffix.
            words.append("***REDACTED***")
            break
        words.append(redact_url_credentials(word) or word)
    text = " ".join(words)
    return text if len(text) <= MAX_TITLE_CHARS else text[: MAX_TITLE_CHARS - 1] + "…"
