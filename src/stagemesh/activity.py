"""Read-only activity query over existing task, execution, and audit records.

Display only: it runs SELECTs on rows the runner already writes, adds no schema, and never
mutates state. Results are bounded (page size and scan cap) and ordered newest first with a
stable tie-break, so pages never overlap. Free-form text (execution results, audit payloads,
provider output) is never copied into entries; result codes use a fixed outcome allowlist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime
from typing import Any, Literal

from .domain import ExecutionStatus, Stage, TaskStatus
from .persistence import Store
from .redaction import redact_url_credentials

ActivityKind = Literal["task", "execution", "audit"]
ActivityState = Literal["open", "closed"]
TimeBasis = Literal["activity", "opened", "updated", "completed"]

STATES: tuple[str, ...] = ("open", "closed")
TIME_BASES: tuple[str, ...] = ("activity", "opened", "updated", "completed")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SCAN = 1000  # retained rows per source and across the combined projection
MAX_TITLE_CHARS = 200
_KIND_ORDER = {"audit": 0, "execution": 1, "task": 2}
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
    for field, value in (("task_id", query.task_id), ("provider", query.provider), ("project", query.project)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise ActivityQueryError(f"activity {field} must be a non-empty string of 200 characters or fewer")
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
    if query.provider is None and query.time_basis != "completed":
        sources.append(_task_entries)
    sources.append(_execution_entries)
    if (query.provider is None and query.state != "open" and query.time_basis != "opened"
            and (query.stage is None or (query.time_basis == "completed" and query.stage == Stage.DONE.value))):
        sources.append(_audit_entries)
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
    }
    for entry in entries:
        group = counts[entry.kind]
        status = entry.status if entry.status in group else ("EVENT" if entry.kind == "audit" else "UNKNOWN")
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
    column = "created_at" if query.time_basis == "opened" else "updated_at"
    clauses = _time_clause(column, query, params)
    if query.project is not None:
        clauses.append("project = ?")
        params.append(query.project)
    if query.task_id is not None:
        clauses.append("id = ?")
        params.append(query.task_id)
    if query.stage is not None:
        clauses.append("stage = ?")
        params.append(query.stage)
    if query.state == "open":
        clauses.append("status != ?")
        params.append(TaskStatus.DONE.value)
    elif query.state == "closed":
        clauses.append("status = ?")
        params.append(TaskStatus.DONE.value)
    rows = store.conn.execute(
        f"SELECT id, title, stage, status, project, {column} AS at FROM tasks{_where(clauses)} "
        f"ORDER BY {column} DESC, id LIMIT ?",
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
    rows = store.conn.execute(
        f"SELECT e.id, e.task_id, e.kind, e.status, e.actor, e.candidate_sha, e.result, {column} AS at, t.stage, t.project "
        f"FROM executions e JOIN tasks t ON t.id = e.task_id{_where(clauses)} "
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
        )
        for row in rows[:MAX_SCAN]
    ]
    return entries, len(rows) > MAX_SCAN


def _audit_entries(store: Store, query: ActivityQuery) -> tuple[list[ActivityEntry], bool]:
    """Audit events are history only (always closed); only the event type and task id are exposed."""
    params: list[Any] = []
    clauses = _time_clause("a.created_at", query, params)
    task_id_sql = _audit_text("task_id")
    if query.task_id is not None:
        clauses.append(f"{task_id_sql} = ?")
        params.append(query.task_id)
    if query.project is not None:
        clauses.append("t.project = ?")
        params.append(query.project)
    completed = query.time_basis == "completed"
    if completed:
        prior_stages = [stage.value for stage in Stage if stage is not Stage.DONE]
        clauses.extend(("a.event_type = ?", f"{_audit_text('to')} = ?",
                        f"{_audit_text('from')} IN ({','.join('?' for _ in prior_stages)})",
                        f"length({task_id_sql}) > 0"))
        params.extend(("stage.transition", Stage.DONE.value, *prior_stages))
    rows = store.conn.execute(
        "SELECT a.id, a.event_type, a.payload, a.created_at, t.project FROM audit_events a "
        f"LEFT JOIN tasks t ON t.id = {task_id_sql}{_where(clauses)} "
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
            )
        )
    return entries, len(rows) > MAX_SCAN


def _audit_text(name: Literal["task_id", "from", "to"]) -> str:
    # SQL identifiers/JSON paths are fixed by the caller, values remain parameters.
    # Guard malformed JSON and non-text payload fields before joining/filtering.
    return (f"CASE WHEN json_valid(a.payload) THEN CASE WHEN json_type(a.payload, '$.{name}') = 'text' "
            f"AND length(json_extract(a.payload, '$.{name}')) <= 200 "
            f"THEN json_extract(a.payload, '$.{name}') END END")


def _safe_project(project: object) -> str | None:
    return _safe_title(project) if isinstance(project, str) and project.strip() else None


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
