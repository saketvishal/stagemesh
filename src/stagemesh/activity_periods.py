"""Bounded lifecycle intervals and opened/completed series from recorded task history.

The report never reconstructs legacy history from current state. A task contributes only
when it has one valid ``task.created`` anchor, a continuous status-change chain, and a
final recorded status matching the task row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import Stage, TaskStatus
from .redaction import redact_url_credentials

MAX_PERIOD_TASKS = 200
MAX_PERIOD_DAYS = 3660
BucketKind = Literal["day", "week"]


class ActivityPeriodError(ValueError):
    pass


@dataclass(frozen=True)
class ActivityPeriodQuery:
    since: datetime
    until: datetime
    bucket: BucketKind = "day"
    timezone: str = "UTC"
    task_id: str | None = None
    project: str | None = None
    source: str | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class ActiveTaskInterval:
    task_id: str
    active_from: datetime
    active_until: datetime | None
    current_status: str
    stage: str
    project: str | None
    source: str | None
    source_id: str | None


@dataclass(frozen=True)
class ActivitySeriesBucket:
    start: datetime
    end: datetime
    opened_occurrences: int
    opened_tasks: int
    completed_occurrences: int
    completed_tasks: int


@dataclass(frozen=True)
class ActivityPeriodReport:
    since: datetime
    until: datetime
    bucket: BucketKind
    timezone: str
    active_intervals: tuple[ActiveTaskInterval, ...]
    total_intervals: int
    limit: int
    offset: int
    next_offset: int | None
    series: tuple[ActivitySeriesBucket, ...]
    unavailable_tasks: int
    considered_tasks: int
    truncated: bool


@dataclass(frozen=True)
class _Lifecycle:
    intervals: tuple[tuple[datetime, datetime | None], ...]
    openings: tuple[datetime, ...]
    completions: tuple[datetime, ...]


def validate_period_query(query: ActivityPeriodQuery) -> ZoneInfo:
    if not isinstance(query, ActivityPeriodQuery):
        raise ActivityPeriodError("activity period query must be an ActivityPeriodQuery")
    for name, value in (("since", query.since), ("until", query.until)):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ActivityPeriodError(f"activity period {name} must be a timezone-aware datetime")
    if query.since >= query.until:
        raise ActivityPeriodError("activity period since must be earlier than until")
    if query.until.astimezone(UTC) - query.since.astimezone(UTC) > timedelta(days=MAX_PERIOD_DAYS):
        raise ActivityPeriodError(f"activity period cannot exceed {MAX_PERIOD_DAYS} days")
    if query.bucket not in {"day", "week"}:
        raise ActivityPeriodError("activity period bucket must be day or week")
    if not isinstance(query.timezone, str) or not query.timezone or len(query.timezone) > 100:
        raise ActivityPeriodError("activity period timezone must be a non-empty IANA name")
    try:
        zone = ZoneInfo(query.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ActivityPeriodError("activity period timezone is unavailable") from None
    for name, value in (("task_id", query.task_id), ("project", query.project), ("source", query.source)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise ActivityPeriodError(f"activity period {name} must be a non-empty string of 200 characters or fewer")
    if not isinstance(query.limit, int) or isinstance(query.limit, bool) or not 1 <= query.limit <= 200:
        raise ActivityPeriodError("activity period limit must be between 1 and 200")
    if not isinstance(query.offset, int) or isinstance(query.offset, bool) or not 0 <= query.offset < 1000:
        raise ActivityPeriodError("activity period offset must be between 0 and 999")
    return zone


def query_activity_periods(store: Any, query: ActivityPeriodQuery) -> ActivityPeriodReport:
    zone = validate_period_query(query)
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (("id", query.task_id), ("project", query.project), ("source", query.source)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    tasks = store.conn.execute(
        "SELECT id, stage, status, project, source, source_id FROM tasks"
        + where
        + " ORDER BY created_at, id LIMIT ?",
        (*params, MAX_PERIOD_TASKS + 1),
    ).fetchall()
    truncated = len(tasks) > MAX_PERIOD_TASKS
    tasks = tasks[:MAX_PERIOD_TASKS]
    since = query.since.astimezone(UTC)
    until = query.until.astimezone(UTC)
    intervals: list[ActiveTaskInterval] = []
    openings: list[tuple[datetime, str]] = []
    completions: list[tuple[datetime, str]] = []
    unavailable = 0
    for task in tasks:
        lifecycle = _lifecycle(store, str(task["id"]), str(task["status"]))
        if lifecycle is None:
            unavailable += 1
            continue
        task_id = str(task["id"])
        openings.extend((at, task_id) for at in lifecycle.openings if since <= at < until)
        completions.extend((at, task_id) for at in lifecycle.completions if since <= at < until)
        for start, end in lifecycle.intervals:
            if start < until and (end is None or end > since):
                intervals.append(
                    ActiveTaskInterval(
                        task_id=task_id,
                        active_from=start,
                        active_until=end,
                        current_status=str(task["status"]),
                        stage=str(task["stage"]),
                        project=_display(task["project"]),
                        source=_display(task["source"]),
                        source_id=_display(task["source_id"]),
                    )
                )
    intervals.sort(key=lambda item: (-item.active_from.timestamp(), item.task_id))
    total_intervals = len(intervals)
    page = intervals[query.offset : query.offset + query.limit]
    next_offset = query.offset + len(page)
    if next_offset >= total_intervals:
        next_offset = None
    return ActivityPeriodReport(
        since=since,
        until=until,
        bucket=query.bucket,
        timezone=query.timezone,
        active_intervals=tuple(page),
        total_intervals=total_intervals,
        limit=query.limit,
        offset=query.offset,
        next_offset=next_offset,
        series=_series(since, until, query.bucket, zone, openings, completions),
        unavailable_tasks=unavailable,
        considered_tasks=len(tasks),
        truncated=truncated,
    )


def _lifecycle(store: Any, task_id: str, durable_status: str) -> _Lifecycle | None:
    rows = store.conn.execute(
        "SELECT event_type, payload, created_at FROM audit_events "
        "WHERE event_type IN ('task.created','task.status_changed') "
        "AND json_valid(payload) AND json_extract(payload, '$.task_id')=? "
        "ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    if not rows or rows[0]["event_type"] != "task.created":
        return None
    created = _payload(rows[0]["payload"])
    if created is None or created.get("status") != TaskStatus.OPEN.value or created.get("stage") != Stage.PLAN.value:
        return None
    current = TaskStatus.OPEN.value
    interval_start = _instant(rows[0]["created_at"])
    intervals: list[tuple[datetime, datetime | None]] = []
    openings = [interval_start]
    completions: list[datetime] = []
    for row in rows[1:]:
        if row["event_type"] != "task.status_changed":
            return None
        data = _payload(row["payload"])
        if data is None or data.get("task_id") != task_id:
            return None
        previous, new = data.get("previous_status"), data.get("current_status")
        if previous != current or new not in {status.value for status in TaskStatus}:
            return None
        at = _instant(row["created_at"])
        if previous != TaskStatus.DONE.value and new == TaskStatus.DONE.value:
            if interval_start is None or at < interval_start:
                return None
            intervals.append((interval_start, at))
            interval_start = None
            completions.append(at)
        elif previous == TaskStatus.DONE.value and new != TaskStatus.DONE.value:
            if interval_start is not None:
                return None
            interval_start = at
            openings.append(at)
        current = str(new)
    if current != durable_status:
        return None
    if interval_start is not None:
        intervals.append((interval_start, None))
    return _Lifecycle(tuple(intervals), tuple(openings), tuple(completions))


def _series(
    since: datetime,
    until: datetime,
    bucket: BucketKind,
    zone: ZoneInfo,
    openings: list[tuple[datetime, str]],
    completions: list[tuple[datetime, str]],
) -> tuple[ActivitySeriesBucket, ...]:
    starts: list[datetime] = []
    local = since.astimezone(zone)
    start_date = local.date() - timedelta(days=local.weekday() if bucket == "week" else 0)
    cursor = datetime.combine(start_date, time.min, zone)
    step = 7 if bucket == "week" else 1
    while cursor.astimezone(UTC) < until:
        starts.append(cursor.astimezone(UTC))
        cursor = datetime.combine(cursor.date() + timedelta(days=step), time.min, zone)
    result: list[ActivitySeriesBucket] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else cursor.astimezone(UTC)
        opened = [(at, task) for at, task in openings if start <= at < end]
        completed = [(at, task) for at, task in completions if start <= at < end]
        result.append(
            ActivitySeriesBucket(
                start=start,
                end=end,
                opened_occurrences=len(opened),
                opened_tasks=len({task for _at, task in opened}),
                completed_occurrences=len(completed),
                completed_tasks=len({task for _at, task in completed}),
            )
        )
    return tuple(result)


def _payload(raw: object) -> dict[str, object] | None:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _instant(value: object) -> datetime:
    return datetime.fromtimestamp(float(value), UTC)


def _display(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200:
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in ("token", "secret", "password", "authorization", "api_key", "api-key")):
        return None
    return redact_url_credentials(value)
