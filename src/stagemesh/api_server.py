"""Loopback, read-only HTTP API over the activity projection (docs/operator-ui-contract.md).

Standard library only. Every request opens the coordinator database read-only (`mode=ro`,
`query_only`), runs `query_activity`, and closes it, so nothing here can mutate coordinator state
and no connection outlives a request. Requests fail closed: unknown or repeated parameters,
wrong methods, bad hosts/origins and missing credentials are errors, never coerced.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from . import __version__
from .activity import (
    SAFE_EXECUTION_RESULTS,
    ActivityEntry,
    ActivityPage,
    ActivityQuery,
    ActivityQueryError,
    query_activity,
    safe_activity_code,
)
from .activity_periods import (
    ActivityPeriodError,
    ActivityPeriodQuery,
    ActivityPeriodReport,
    query_activity_periods,
)
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .operator import OperatorReport, execution_runtime_row, operator_report, pending_decision_rows
from .persistence import Store
from .redaction import redact_output

CONTRACT = "stagemesh.activity/1"
CONTRACT_V2 = "stagemesh.activity/2"
CONTRACT_V3 = "stagemesh.activity/3"
CONTRACT_V4 = "stagemesh.activity/4"
CONTRACT_V5 = "stagemesh.activity/5"
OPERATOR_CONTRACT = "stagemesh.operator/1"
OPERATOR_DETAIL_LIMIT = 100
TASK_DETAIL_CONTRACT = "stagemesh.task-detail/1"
TASK_DETAIL_LIMIT = 200
EXECUTION_DETAIL_CONTRACT = "stagemesh.execution-detail/1"
EXECUTION_EVIDENCE_LIMIT = 100
LOOPBACK = "127.0.0.1"
_PARAMS = frozenset({"state", "task_id", "stage", "provider", "since", "until", "limit", "offset"})
_PARAMS_V2 = _PARAMS | {"project", "time_basis"}
_PARAMS_V3 = _PARAMS_V2 | {"source", "source_status", "include_source_history"}
_PARAMS_V4 = _PARAMS_V3 | {
    "task_event", "task_status", "include_task_history", "retry_kind", "retry_state",
    "include_retry_history",
}
_PARAMS_V5 = _PARAMS_V4 | {
    "evidence_kind", "evidence_status", "include_evidence_history",
}
_INT = re.compile(r"^[0-9]{1,9}$")
_MAX_QUERY_CHARS = 2048
# The complete production shell. Packaging (build_backend.UI_ASSETS) and serving both use exactly this list.
UI_ASSETS = ("index.html", "app.js", "app.css")
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

Response = tuple[int, bytes, dict[str, str]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _json(status: int, body: Mapping[str, Any], contract: str = CONTRACT) -> Response:
    payload = json.dumps({"contract": contract, **body}, separators=(",", ":")).encode()
    return status, payload, {"Content-Type": "application/json; charset=utf-8"}


def _error(exc: ApiError, contract: str = CONTRACT) -> Response:
    return _json(exc.status, {"error": {"code": exc.code, "message": exc.message}}, contract)


# Exactly RFC 3339 `date-time`: uppercase T and Z, extended format, mandatory offset, optional fraction of 1-9 digits.
_RFC3339 = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-]([0-9]{2}):([0-9]{2}))",
    re.ASCII,
)
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URL_SAFE = re.compile(r"^[!-~]*$")


def _invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_query", message)


def _timestamp(name: str, value: str) -> datetime:
    """Parse the supported form `YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM)`; anything else is rejected."""
    match = _RFC3339.fullmatch(value)
    if not match:
        raise _invalid(f"{name} must be an RFC 3339 timestamp such as 2026-01-31T09:30:00Z or 2026-01-31T09:30:00%2B05:30")
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    micro = int((match.group(7) or "").ljust(6, "0")[:6] or 0)
    if match.group(8) == "Z":
        offset = timedelta(0)
    else:
        off_h, off_m = int(match.group(9)), int(match.group(10))
        if off_h > 23 or off_m > 59:
            raise _invalid(f"{name} has an invalid UTC offset")
        offset = timedelta(hours=off_h, minutes=off_m) * (-1 if match.group(8)[0] == "-" else 1)
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone(offset))
    except ValueError:
        raise _invalid(f"{name} is not a valid calendar time") from None


def _integer(name: str, value: str) -> int:
    if not _INT.match(value):
        raise _invalid(f"{name} must be a non-negative integer")
    return int(value)


def _boolean(name: str, value: str) -> bool:
    if value not in {"true", "false"}:
        raise _invalid(f"{name} must be true or false")
    return value == "true"


def _decode(component: str) -> str:
    """Strict form decoding: malformed escapes and invalid UTF-8 are errors, never replaced."""
    if _BAD_ESCAPE.search(component):
        raise _invalid("malformed percent escape in query string")
    try:
        text = unquote_to_bytes(component.replace("+", " ")).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _invalid("query string is not valid UTF-8") from None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise _invalid("query string contains control characters")
    return text


def parse_query(raw: str, *, version: int = 1) -> ActivityQuery:
    allowed = (
        _PARAMS_V5
        if version == 5
        else _PARAMS_V4
        if version == 4
        else (_PARAMS_V3 if version == 3 else (_PARAMS_V2 if version == 2 else _PARAMS))
    )
    if len(raw) > _MAX_QUERY_CHARS:
        raise _invalid("query string is too long")
    if not _URL_SAFE.match(raw):
        raise _invalid("query string must be percent-encoded ASCII")
    one: dict[str, str] = {}
    if raw:
        segments = raw.split("&")
        if len(segments) > len(allowed) + 1:
            raise _invalid("too many query parameters")
        for segment in segments:
            name, sep, value = segment.partition("=")
            if not segment or not sep or not name:
                raise _invalid("malformed query string")
            name = _decode(name)
            if name not in allowed:
                raise _invalid("unknown parameter")
            if name in one:
                raise _invalid(f"{name} must not be repeated")
            one[name] = _decode(value)
    kwargs: dict[str, Any] = {}
    for name in (
        "state", "task_id", "stage", "provider", "project", "time_basis", "source", "source_status",
        "task_event", "task_status", "retry_kind", "retry_state", "evidence_kind", "evidence_status",
    ):
        if name in one:
            kwargs[name] = one[name]
    if "include_source_history" in one:
        kwargs["include_source_history"] = _boolean(
            "include_source_history", one["include_source_history"]
        )
    if "include_task_history" in one:
        kwargs["include_task_history"] = _boolean(
            "include_task_history", one["include_task_history"]
        )
    if "include_retry_history" in one:
        kwargs["include_retry_history"] = _boolean(
            "include_retry_history", one["include_retry_history"]
        )
    if "include_evidence_history" in one:
        kwargs["include_evidence_history"] = _boolean(
            "include_evidence_history", one["include_evidence_history"]
        )
    for name in ("since", "until"):
        if name in one:
            kwargs[name] = _timestamp(name, one[name])
    for name in ("limit", "offset"):
        if name in one:
            kwargs[name] = _integer(name, one[name])
    return ActivityQuery(**kwargs)


def parse_period_query(raw: str) -> ActivityPeriodQuery:
    allowed = {
        "since", "until", "bucket", "timezone", "task_id", "project", "source", "limit", "offset"
    }
    if len(raw) > _MAX_QUERY_CHARS:
        raise _invalid("query string is too long")
    if not _URL_SAFE.match(raw):
        raise _invalid("query string must be percent-encoded ASCII")
    one: dict[str, str] = {}
    for segment in raw.split("&") if raw else ():
        name, sep, value = segment.partition("=")
        if not segment or not sep or not name:
            raise _invalid("malformed query string")
        name = _decode(name)
        if name not in allowed:
            raise _invalid("unknown parameter")
        if name in one:
            raise _invalid(f"{name} must not be repeated")
        one[name] = _decode(value)
    if "since" not in one or "until" not in one:
        raise _invalid("periods requires since and until")
    kwargs: dict[str, Any] = {}
    for name in ("limit", "offset"):
        if name in one:
            kwargs[name] = _integer(name, one[name])
    return ActivityPeriodQuery(
        since=_timestamp("since", one["since"]),
        until=_timestamp("until", one["until"]),
        bucket=one.get("bucket", "day"),  # type: ignore[arg-type]
        timezone=one.get("timezone", "UTC"),
        task_id=one.get("task_id"),
        project=one.get("project"),
        source=one.get("source"),
        **kwargs,
    )


def parse_task_detail_query(raw: str) -> str:
    if len(raw) > _MAX_QUERY_CHARS or not _URL_SAFE.match(raw):
        raise _invalid("task detail query must be percent-encoded ASCII")
    segments = raw.split("&") if raw else []
    if len(segments) != 1:
        raise _invalid("task detail requires exactly one task_id")
    name, sep, value = segments[0].partition("=")
    if not sep or _decode(name) != "task_id":
        raise _invalid("task detail requires exactly one task_id")
    task_id = _decode(value)
    if not task_id.strip() or len(task_id) > 200:
        raise _invalid("task_id must be a non-empty string of 200 characters or fewer")
    return task_id


def parse_execution_detail_query(raw: str) -> str:
    if len(raw) > _MAX_QUERY_CHARS or not _URL_SAFE.match(raw):
        raise _invalid("execution detail query must be percent-encoded ASCII")
    segments = raw.split("&") if raw else []
    if len(segments) != 1:
        raise _invalid("execution detail requires exactly one execution_id")
    name, sep, value = segments[0].partition("=")
    if not sep or _decode(name) != "execution_id":
        raise _invalid("execution detail requires exactly one execution_id")
    execution_id = _decode(value)
    if not execution_id.strip() or len(execution_id) > 200:
        raise _invalid("execution_id must be a non-empty string of 200 characters or fewer")
    return execution_id


def _entry(entry: ActivityEntry, *, version: int = 1) -> dict[str, Any]:
    data = asdict(entry)
    data["at"] = entry.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if version == 1:
        data.pop("project")
    if version < 3:
        for name in ("source", "source_id", "source_status", "previous_source_status"):
            data.pop(name)
    if version < 4:
        for name in (
            "previous_task_status", "current_task_status", "rework_required",
            "retry_kind", "retry_state", "retry_attempt", "retry_limit",
        ):
            data.pop(name)
    if version < 5:
        for name in ("evidence_kind", "evidence_status"):
            data.pop(name)
    return data


def serialize_page(page: ActivityPage, *, version: int = 1) -> dict[str, Any]:
    body = {
        "entries": [_entry(entry, version=version) for entry in page.entries],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "next_offset": page.next_offset,
        "truncated": page.truncated,
    }
    if version >= 2:
        counts = page.status_counts
        if version < 5:
            counts = {
                name: values for name, values in counts.items()
                if name not in {"evidence", "evidence_outcome"}
            }
        if version < 4:
            counts = {
                name: values for name, values in counts.items()
                if name not in {"task_transition", "retry", "retry_outcome"}
            }
        if version == 2:
            counts = {name: values for name, values in counts.items() if name != "source"}
        body.update(time_basis=page.time_basis, status_counts=counts)
    return body


def serialize_period_report(report: ActivityPeriodReport) -> dict[str, Any]:
    def instant(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return {
        "since": instant(report.since),
        "until": instant(report.until),
        "bucket": report.bucket,
        "timezone": report.timezone,
        "active_intervals": [
            {
                **asdict(interval),
                "active_from": instant(interval.active_from),
                "active_until": instant(interval.active_until),
            }
            for interval in report.active_intervals
        ],
        "total_intervals": report.total_intervals,
        "limit": report.limit,
        "offset": report.offset,
        "next_offset": report.next_offset,
        "series": [
            {**asdict(bucket), "start": instant(bucket.start), "end": instant(bucket.end)}
            for bucket in report.series
        ],
        "unavailable_tasks": report.unavailable_tasks,
        "considered_tasks": report.considered_tasks,
        "truncated": report.truncated,
    }


def _safe_text(value: object, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value[: max(1000, limit * 4)]
    value = redact_output(value).replace("\r", " ").replace("\n", " ")
    value = re.sub(r"\b(?:https?|file)://\S+", "<url>", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<![A-Za-z0-9])(?:[A-Za-z]:\\|/)[^\s,;]+", "<path>", value)
    value = value[:limit]
    return value or None


def _safe_sha(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _operator_sections(report: OperatorReport) -> dict[str, tuple[Mapping[str, object], ...]]:
    return {section.name: section.rows for section in report.sections}


def serialize_operator_report(report: OperatorReport) -> dict[str, Any]:
    """Serialize a fixed, privacy-safe subset of the broader local operator report."""
    sections = _operator_sections(report)

    def pending(row: Mapping[str, object]) -> dict[str, Any]:
        return {
            "task_id": _safe_text(row.get("task_id")),
            "kind": row.get("kind")
            if row.get("kind") in {"human_decision", "delivery_permission", "approval_coverage"}
            else "unknown",
            "status": row.get("status") if row.get("status") in {"RECORDED", "UNKNOWN"} else "UNKNOWN",
            "candidate_sha": _safe_sha(row.get("candidate_sha")),
            "detail": _safe_text(row.get("detail"), 400) or "detail unavailable",
            "recorded_at": _safe_number(row.get("recorded_at")),
        }

    def selection(row: Mapping[str, object]) -> dict[str, Any]:
        order = row.get("order") if isinstance(row.get("order"), list) else []
        verdicts = row.get("verdicts") if isinstance(row.get("verdicts"), list) else []
        safe_verdicts = []
        for verdict in verdicts[:20]:
            if not isinstance(verdict, Mapping):
                continue
            safe_verdicts.append(
                {
                    "provider": _safe_text(verdict.get("provider"), 80) or "unknown",
                    "eligible": verdict.get("eligible") if isinstance(verdict.get("eligible"), bool) else None,
                    "reason": _safe_text(verdict.get("reason"), 200) or "reason unavailable",
                }
            )
        return {
            "task_id": _safe_text(row.get("task_id")),
            "stage": _safe_text(row.get("stage"), 40) or "UNKNOWN",
            "policy": _safe_text(row.get("policy"), 80) or "unknown",
            "order": [_safe_text(item, 80) for item in order[:20] if _safe_text(item, 80)],
            "verdicts": safe_verdicts,
            "recorded_at": _safe_number(row.get("created_at")),
        }

    def worker(row: Mapping[str, object]) -> dict[str, Any]:
        capabilities = row.get("capabilities")
        if isinstance(capabilities, str):
            try:
                capabilities = json.loads(capabilities)
            except ValueError:
                capabilities = []
        if not isinstance(capabilities, list):
            capabilities = []
        return {
            "worker_id": _safe_text(row.get("id"), 120),
            "provider": _safe_text(row.get("provider"), 80) or "unknown",
            "capabilities": [
                item for value in capabilities[:20] if (item := _safe_text(value, 80))
            ],
            "lease_expires_at": _safe_number(row.get("lease_expires_at")),
        }

    efficiency_rows = sections.get("Recent Efficiency And Usage", ())
    efficiency = efficiency_rows[0] if efficiency_rows else {}
    usage = efficiency.get("usage") if isinstance(efficiency.get("usage"), Mapping) else {}
    attempts = efficiency.get("attempts_by_purpose")
    attempts = attempts if isinstance(attempts, Mapping) else {}
    safe_efficiency = {
        "scope": _safe_text(efficiency.get("scope"), 200) or "bounded recent sample",
        "provider_attempts": _safe_number(efficiency.get("provider_attempts")),
        "attempts_by_purpose": {
            purpose: _safe_number(attempts.get(purpose)) or 0
            for purpose in ("diagnosis", "implementation", "remediation", "review", "research", "unknown")
        },
        "failed_provider_attempts": _safe_number(efficiency.get("failed_provider_attempts")),
        "provider_outcome_unknown": _safe_number(efficiency.get("provider_outcome_unknown")),
        "provider_seconds": _safe_number(efficiency.get("provider_seconds")),
        "provider_duration_unknown": _safe_number(efficiency.get("provider_duration_unknown")),
        "checks_executed": _safe_number(efficiency.get("checks_executed")),
        "checks_reused": _safe_number(efficiency.get("checks_reused")),
        "validation_seconds": _safe_number(efficiency.get("validation_seconds")),
        "usage": {
            key: (_safe_text(usage.get(key), 20) if key == "unit" else _safe_number(usage.get(key)))
            for key in ("input", "output", "cached_input", "unit", "calls_reporting", "calls_unknown")
        },
        "billing_cost": "UNKNOWN",
    }
    body_sections: dict[str, list[dict[str, Any]]] = {
        "attention": [
            {
                "kind": _safe_text(row.get("kind"), 80) or "unknown",
                "count": _safe_number(row.get("count")),
                "status": row.get("status") if row.get("status") in {"ACTION", "INFO", "REVIEW"} else "REVIEW",
            }
            for row in sections.get("Attention", ())
        ],
        "pending_decisions": [pending(row) for row in sections.get("Recorded Pending Decisions", ())],
        "active_executions": [
            {
                "execution_id": _safe_text(row.get("id")),
                "task_id": _safe_text(row.get("task_id")),
                "stage": _safe_text(row.get("stage"), 40) or "UNKNOWN",
                "provider": _safe_text(row.get("provider"), 80) or "unknown",
                "process_state": _safe_text(row.get("process_state"), 40) or "UNKNOWN",
                "parent_process_state": _safe_text(row.get("parent_process_state"), 40) or "UNKNOWN",
                "cleanup_state": _safe_text(row.get("cleanup_state"), 80),
                "stale": bool(row.get("stale")),
            }
            for row in sections.get("Active Executions", ())
        ],
        "workers": [worker(row) for row in sections.get("Workers", ())],
        "recent_failures": [
            {
                "execution_id": _safe_text(row.get("id")),
                "task_id": _safe_text(row.get("task_id")),
                "stage": _safe_text(row.get("stage"), 40) or "UNKNOWN",
                "provider": _safe_text(row.get("provider"), 80) or "unknown",
                "candidate_sha": _safe_sha(row.get("candidate_sha")),
                "scope": row.get("scope") if row.get("scope") in {"current", "history"} else "history",
                "finished_at": _safe_number(row.get("finished_at")),
                "reason": _safe_text(row.get("reason"), 400) or "reason not recorded",
            }
            for row in sections.get("Recent Failures", ())
        ],
        "efficiency": [safe_efficiency],
        "provider_selections": [selection(row) for row in sections.get("Provider Selections", ())],
    }
    metrics = {
        name: value
        for name in (
            "task_count", "open_task_count", "eligible_open_task_count", "blocked_task_count",
            "running_count", "source_ready_count", "done_count", "failed_execution_count",
            "current_failed_execution_count", "unknown_execution_count", "unresolved_validation_count",
            "unresolved_process_tree_count", "backlog_state", "worker_count",
            "recorded_pending_decision_count",
        )
        if (value := report.metrics.get(name)) is not None
        and isinstance(value, (str, int, float))
        and not isinstance(value, bool)
    }
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "summary": report.summary if report.summary in {"ok", "degraded"} else "degraded",
        "metrics": metrics,
        "sections": body_sections,
        "bounded": True,
        "detail_limit": OPERATOR_DETAIL_LIMIT,
    }


def serialize_task_detail(store: Store, task_id: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise ApiError(404, "task_not_found", "task was not found")
    page = query_activity(
        store,
        ActivityQuery(
            task_id=task_id,
            include_source_history=True,
            include_task_history=True,
            include_retry_history=True,
            include_evidence_history=True,
            limit=TASK_DETAIL_LIMIT,
        ),
    )
    timeline = [_entry(entry, version=5) for entry in page.entries]
    current_entry = next((entry for entry in timeline if entry["kind"] == "task"), {})
    candidate = store.latest_candidate(task_id)
    candidate_sha = None if candidate is None else _safe_sha(candidate["sha"])
    dependencies = [
        {
            "task_id": _safe_text(row["id"]),
            "stage": _safe_text(row["stage"], 40) or "UNKNOWN",
            "status": _safe_text(row["status"], 40) or "UNKNOWN",
        }
        for row in store.conn.execute(
            "SELECT t.id,t.stage,t.status FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id "
            "WHERE d.task_id=? ORDER BY t.id LIMIT ?",
            (task_id, TASK_DETAIL_LIMIT),
        )
    ]
    pending = []
    for row in pending_decision_rows(store):
        if row.get("task_id") not in {task_id, None}:
            continue
        pending.append(
            {
                "task_id": _safe_text(row.get("task_id")),
                "kind": row.get("kind")
                if row.get("kind") in {"human_decision", "delivery_permission", "approval_coverage"}
                else "unknown",
                "status": row.get("status") if row.get("status") in {"RECORDED", "UNKNOWN"} else "UNKNOWN",
                "candidate_sha": _safe_sha(row.get("candidate_sha")),
                "detail": _safe_text(row.get("detail"), 400) or "detail unavailable",
                "recorded_at": _safe_number(row.get("recorded_at")),
            }
        )
    created = store.conn.execute(
        "SELECT created_at FROM audit_events WHERE event_type='task.created' "
        "AND json_extract(payload, '$.task_id')=? ORDER BY created_at LIMIT 1",
        (task_id,),
    ).fetchone()
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "task": {
            "id": _safe_text(task["id"]),
            "title": current_entry.get("title") or _safe_text(task["title"], 300),
            "stage": _safe_text(task["stage"], 40) or "UNKNOWN",
            "status": _safe_text(task["status"], 40) or "UNKNOWN",
            "project": current_entry.get("project"),
            "created_at": _safe_number(task["created_at"]),
            "updated_at": _safe_number(task["updated_at"]),
        },
        "source": {
            "kind": current_entry.get("source"),
            "id": current_entry.get("source_id"),
            "status": current_entry.get("source_status"),
        },
        "candidate": None
        if candidate is None
        else {
            "sha": candidate_sha,
            "produced_by": _safe_text(candidate["produced_by"], 80),
            "durable_handoff": bool(candidate["durable_handoff"]),
            "created_at": _safe_number(candidate["created_at"]),
        },
        "dependencies": dependencies,
        "pending_decisions": pending,
        "timeline": timeline,
        "evidence": [entry for entry in timeline if entry["kind"] == "evidence"],
        "retries": [
            entry
            for entry in timeline
            if entry["kind"] == "retry" or entry.get("rework_required") is True
        ],
        "history": {
            "recorded_creation": created is not None,
            "total": page.total,
            "limit": page.limit,
            "truncated": page.truncated or page.next_offset is not None,
            "note": (
                "Recorded history may be incomplete before durable lifecycle events were introduced."
                if created is None
                else "Recorded retained lifecycle history only; absence does not prove an event never occurred."
            ),
        },
    }


_RESULT_CLASSES = {
    "capacity": "provider_availability",
    "provider_permission_denied": "provider_availability",
    "provider_timeout": "provider_availability",
    "provider_unavailable": "provider_availability",
    "cleanup_incomplete": "recovery",
    "fenced_identity_unknown": "recovery",
    "orphaned": "recovery",
    "recovered_stale": "recovery",
    "executor_aborted": "control",
    "external_workspace_mutation": "control",
    "operator_stop": "control",
    "tool_execution_failure": "control",
    "findings": "outcome",
    "no_implementation_change": "outcome",
}


def serialize_execution_detail(store: Store, execution_id: str) -> dict[str, Any]:
    execution = store.conn.execute(
        "SELECT e.*,t.title,t.stage AS task_stage,t.status AS task_status,t.project "
        "FROM executions e JOIN tasks t ON t.id=e.task_id WHERE e.id=?",
        (execution_id,),
    ).fetchone()
    if execution is None:
        raise ApiError(404, "execution_not_found", "execution was not found")
    allowed_kinds = {item.value for item in ExecutionKind}
    allowed_statuses = {item.value for item in ExecutionStatus}
    kind = execution["kind"] if execution["kind"] in allowed_kinds else "UNKNOWN"
    status = execution["status"] if execution["status"] in allowed_statuses else "UNKNOWN"
    started_at = _safe_number(execution["started_at"])
    updated_at = _safe_number(execution["updated_at"])
    terminal = status in {ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value}
    terminal_at = updated_at if terminal and started_at is not None and updated_at is not None and updated_at >= started_at else None
    duration = terminal_at - started_at if terminal_at is not None and started_at is not None else None
    latest_execution = store.conn.execute(
        "SELECT id FROM executions WHERE task_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 1",
        (execution["task_id"],),
    ).fetchone()
    latest_candidate = store.latest_candidate(str(execution["task_id"]))
    candidate_sha = _safe_sha(execution["candidate_sha"])
    current_candidate_sha = None if latest_candidate is None else _safe_sha(latest_candidate["sha"])
    result_code = execution["result"] if execution["result"] in SAFE_EXECUTION_RESULTS else None
    claim = None
    if execution["claim_id"] is not None:
        claim = store.conn.execute(
            "SELECT id,worker_id,stage,lease_expires_at,active,created_at FROM claims WHERE id=?",
            (execution["claim_id"],),
        ).fetchone()
    evidence = []
    evidence_truncated = False
    if candidate_sha is not None:
        rows = store.conn.execute(
            "SELECT id,kind,status,created_at FROM evidence WHERE task_id=? AND candidate_sha=? "
            "ORDER BY created_at DESC,id LIMIT ?",
            (execution["task_id"], candidate_sha, EXECUTION_EVIDENCE_LIMIT + 1),
        ).fetchall()
        evidence_truncated = len(rows) > EXECUTION_EVIDENCE_LIMIT
        for row in rows[:EXECUTION_EVIDENCE_LIMIT]:
            evidence.append(
                {
                    "id": _safe_text(row["id"]),
                    "kind": row["kind"] if row["kind"] in {item.value for item in EvidenceKind} else "UNKNOWN",
                    "status": row["status"] if row["status"] in {item.value for item in EvidenceStatus} else "UNKNOWN",
                    "recorded_at": _safe_number(row["created_at"]),
                    "current_candidate": candidate_sha == current_candidate_sha,
                }
            )
    runtime = execution_runtime_row(store, execution)
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "execution": {
            "id": _safe_text(execution["id"]),
            "kind": kind,
            "status": status,
            "provider": safe_activity_code(execution["actor"]),
            "current_for_task": latest_execution is not None and latest_execution["id"] == execution["id"],
        },
        "task": {
            "id": _safe_text(execution["task_id"]),
            "title": _safe_text(execution["title"], 300),
            "stage": _safe_text(execution["task_stage"], 40) or "UNKNOWN",
            "status": _safe_text(execution["task_status"], 40) or "UNKNOWN",
            "project": _safe_text(execution["project"], 200),
        },
        "candidate": {
            "sha": candidate_sha,
            "current": candidate_sha is not None and candidate_sha == current_candidate_sha,
        },
        "timing": {
            "started_at": started_at,
            "updated_at": updated_at,
            "terminal_at": terminal_at,
            "recorded_duration_seconds": duration,
            "duration_kind": "recorded_terminal" if duration is not None else "unavailable",
        },
        "outcome": {
            "code": result_code,
            "classification": _RESULT_CLASSES.get(result_code, "unavailable"),
            "raw_result_available": execution["result"] is not None,
            "scope": "current" if latest_execution is not None and latest_execution["id"] == execution["id"] else "history",
        },
        "claim": None
        if claim is None
        else {
            "id": _safe_text(claim["id"]),
            "worker_id": safe_activity_code(claim["worker_id"]),
            "stage": _safe_text(claim["stage"], 40) or "UNKNOWN",
            "active": bool(claim["active"]),
            "lease_expires_at": _safe_number(claim["lease_expires_at"]),
            "created_at": _safe_number(claim["created_at"]),
        },
        "runtime": {
            "current": runtime["current"],
            "identity_coverage": runtime["identity_coverage"],
            "process_state": runtime["process_state"],
            "parent_process_state": runtime["parent_process_state"],
            "cleanup_state": runtime["cleanup_state"],
        },
        "evidence": evidence,
        "evidence_meta": {
            "limit": EXECUTION_EVIDENCE_LIMIT,
            "truncated": evidence_truncated,
            "correlation": "exact_task_and_candidate" if candidate_sha is not None else "unavailable_without_candidate",
        },
    }


class ActivityApi:
    """Transport-independent request handling, so tests need no sockets."""

    def __init__(self, db_path: Path, token: str, static_dir: Path | None = None):
        if not token:
            raise ValueError("an access token is required")
        self.db_path = Path(db_path)
        self.token = token
        self.static_dir = static_dir
        self.allowed_hosts: frozenset[str] = frozenset()

    def bind(self, port: int) -> None:
        hosts = {f"{LOOPBACK}:{port}", f"localhost:{port}"}
        self.allowed_hosts = frozenset(hosts)

    def handle(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        contract = CONTRACT
        try:
            if urlsplit(target).path == "/api/v1/execution":
                contract = EXECUTION_DETAIL_CONTRACT
            elif urlsplit(target).path == "/api/v1/task":
                contract = TASK_DETAIL_CONTRACT
            elif urlsplit(target).path == "/api/v1/operator":
                contract = OPERATOR_CONTRACT
            elif urlsplit(target).path.startswith("/api/v5/"):
                contract = CONTRACT_V5
            elif urlsplit(target).path.startswith("/api/v4/"):
                contract = CONTRACT_V4
            elif urlsplit(target).path.startswith("/api/v3/"):
                contract = CONTRACT_V3
            elif urlsplit(target).path.startswith("/api/v2/"):
                contract = CONTRACT_V2
            status, body, extra = self._route(method, target, headers)
        except ApiError as exc:
            status, body, extra = _error(exc, contract)
        except ActivityQueryError as exc:
            status, body, extra = _error(ApiError(400, "invalid_query", str(exc)), contract)
        except ActivityPeriodError as exc:
            status, body, extra = _error(ApiError(400, "invalid_query", str(exc)), contract)
        except (sqlite3.Error, OSError):
            status, body, extra = _error(ApiError(503, "store_unavailable", "activity store is unavailable"), contract)
        except Exception:  # noqa: BLE001 - never leak internals
            status, body, extra = _error(ApiError(500, "internal", "internal error"), contract)
        extra.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        return status, body, extra

    def _route(self, method: str, target: str, headers: Mapping[str, str]) -> Response:
        lowered = {key.lower(): value for key, value in headers.items()}
        host = lowered.get("host", "")
        if host not in self.allowed_hosts:
            raise ApiError(403, "forbidden", "host not allowed")
        origin = lowered.get("origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.allowed_hosts}:
            raise ApiError(403, "forbidden", "origin not allowed")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", "only GET is supported")
        if lowered.get("content-length", "0") not in {"", "0"} or "transfer-encoding" in lowered:
            raise ApiError(400, "invalid_query", "request body is not allowed")
        parts = urlsplit(target)
        path = parts.path
        version = 5 if path.startswith("/api/v5/") else (4 if path.startswith("/api/v4/") else (3 if path.startswith("/api/v3/") else (2 if path.startswith("/api/v2/") else 1)))
        contract = CONTRACT_V5 if version == 5 else (CONTRACT_V4 if version == 4 else (CONTRACT_V3 if version == 3 else (CONTRACT_V2 if version == 2 else CONTRACT)))
        if not path.startswith("/api/"):
            return self._static(path)  # the shell carries no data, so it needs no credential
        self._authenticate(lowered.get("authorization", ""))
        if path == "/api/v1/execution":
            execution_id = parse_execution_detail_query(parts.query)
            store = self._open()
            try:
                detail = serialize_execution_detail(store, execution_id)
            finally:
                store.conn.close()
            return _json(200, detail, EXECUTION_DETAIL_CONTRACT)
        if path == "/api/v1/task":
            task_id = parse_task_detail_query(parts.query)
            store = self._open()
            try:
                detail = serialize_task_detail(store, task_id)
            finally:
                store.conn.close()
            return _json(200, detail, TASK_DETAIL_CONTRACT)
        if path == "/api/v1/operator":
            if parts.query:
                raise ApiError(400, "invalid_query", "operator takes no parameters")
            store = self._open()
            try:
                report = operator_report(store, detail_limit=OPERATOR_DETAIL_LIMIT)
            finally:
                store.conn.close()
            return _json(200, serialize_operator_report(report), OPERATOR_CONTRACT)
        if path in {"/api/v1/health", "/api/v2/health", "/api/v3/health", "/api/v4/health", "/api/v5/health"}:
            if parts.query:
                raise ApiError(400, "invalid_query", "health takes no parameters")
            return _json(200, {"status": "ok", "version": __version__}, contract)
        if path in {"/api/v1/activity", "/api/v2/activity", "/api/v3/activity", "/api/v4/activity", "/api/v5/activity"}:
            query = parse_query(parts.query, version=version)
            store = self._open()
            try:
                page = query_activity(store, query)  # type: ignore[arg-type]
            finally:
                store.conn.close()
            return _json(200, serialize_page(page, version=version), contract)
        if path in {"/api/v4/periods", "/api/v5/periods"}:
            query = parse_period_query(parts.query)
            store = self._open()
            try:
                report = query_activity_periods(store, query)
            finally:
                store.conn.close()
            return _json(200, serialize_period_report(report), contract)
        raise ApiError(404, "not_found", "unknown route")

    def _authenticate(self, header: str) -> None:
        scheme, _, value = header.partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(value.encode(), self.token.encode()):
            raise ApiError(401, "unauthenticated", "missing or invalid credential")

    def _open(self) -> Store:
        if not self.db_path.is_file():
            raise ApiError(503, "store_unavailable", "activity store is unavailable")
        conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            store = object.__new__(Store)
            store.db_path = self.db_path
            store.conn = conn
            return store
        except BaseException:
            conn.close()
            raise

    def _static(self, path: str) -> Response:
        if self.static_dir is None:
            raise ApiError(404, "not_found", "unknown route")
        name = "index.html" if path == "/" else path.lstrip("/")
        if name not in UI_ASSETS:
            raise ApiError(404, "not_found", "unknown route")
        file = self.static_dir / name
        if not file.is_file():
            raise ApiError(404, "not_found", "unknown route")
        headers = {
            "Content-Type": _STATIC_TYPES[file.suffix],
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        }
        return 200, file.read_bytes(), headers


def make_server(
    db_path: Path,
    token: str | None = None,
    port: int = 0,
    static_dir: Path | None = None,
    api_class: type[ActivityApi] = ActivityApi,
) -> tuple[HTTPServer, ActivityApi]:
    api = api_class(db_path, token or secrets.token_urlsafe(32), static_dir)
    handler = _handler(api)
    server = HTTPServer((LOOPBACK, port), handler)
    api.bind(server.server_address[1])
    return server, api


def _handler(api: ActivityApi) -> Callable[..., BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10
        server_version = "stagemesh"
        sys_version = ""

        def _serve(self) -> None:
            status, body, headers = api.handle(self.command, self.path, dict(self.headers.items()))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
