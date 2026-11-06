"""Bounded safe execution diagnostics for the operator UI.

This projection intentionally is not a log endpoint. It exposes allowlisted lifecycle,
ownership and exact-candidate evidence facts while withholding free-form results,
provider output, commands, paths, process identity and evidence payloads.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .activity import SAFE_EXECUTION_RESULTS, safe_activity_code
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .operator import execution_runtime_row
from .persistence import Store
from .redaction import redact_output

DIAGNOSTICS_CONTRACT = "stagemesh.execution-diagnostics/1"
EXECUTION_LIMIT = 100
EVIDENCE_SCAN_LIMIT = 500
_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
RESULT_CLASSES = {
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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = redact_output(value).replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:limit] or None


def _code(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _sha(value: object) -> str | None:
    if not isinstance(value, str) or not 7 <= len(value) <= 64:
        return None
    return value.lower() if all(char in "0123456789abcdefABCDEF" for char in value) else None


def _evidence(store: Store, task_id: str, candidate_sha: str | None) -> dict[str, Any]:
    empty = {
        "correlation": "UNAVAILABLE_WITHOUT_CANDIDATE",
        "scan_limit": EVIDENCE_SCAN_LIMIT,
        "truncated": False,
        "counts": {},
        "latest": None,
    }
    if candidate_sha is None:
        return empty
    rows = store.conn.execute(
        "SELECT kind,status,created_at FROM evidence WHERE task_id=? AND candidate_sha=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT ?",
        (task_id, candidate_sha, EVIDENCE_SCAN_LIMIT + 1),
    ).fetchall()
    kinds = {item.value for item in EvidenceKind}
    statuses = {item.value for item in EvidenceStatus}
    counts: dict[str, dict[str, int]] = {}
    latest = None
    for row in rows[:EVIDENCE_SCAN_LIMIT]:
        kind = row["kind"] if row["kind"] in kinds else "UNKNOWN"
        status = row["status"] if row["status"] in statuses else "UNKNOWN"
        counts.setdefault(kind, {})[status] = counts.setdefault(kind, {}).get(status, 0) + 1
        if latest is None:
            latest = {"kind": kind, "status": status, "recorded_at": _number(row["created_at"])}
    return {
        "correlation": "EXACT_TASK_AND_CANDIDATE",
        "scan_limit": EVIDENCE_SCAN_LIMIT,
        "truncated": len(rows) > EVIDENCE_SCAN_LIMIT,
        "counts": counts,
        "latest": latest,
    }


def _serialize_row(store: Store, row: Mapping[str, Any]) -> dict[str, Any]:
    allowed_kinds = {item.value for item in ExecutionKind}
    allowed_statuses = {item.value for item in ExecutionStatus}
    kind = row["kind"] if row["kind"] in allowed_kinds else "UNKNOWN"
    status = row["status"] if row["status"] in allowed_statuses else "UNKNOWN"
    started_at = _number(row["started_at"])
    updated_at = _number(row["updated_at"])
    terminal = status in {ExecutionStatus.SUCCEEDED.value, ExecutionStatus.FAILED.value}
    terminal_at = updated_at if terminal and started_at is not None and updated_at is not None and updated_at >= started_at else None
    duration = terminal_at - started_at if terminal_at is not None and started_at is not None else None
    candidate_sha = _sha(row["candidate_sha"])
    current_candidate_sha = _sha(row["current_candidate_sha"])
    result_code = row["result"] if row["result"] in SAFE_EXECUTION_RESULTS else None
    runtime = execution_runtime_row(store, row)
    return {
        "id": _code(row["id"]),
        "task": {
            "id": _text(row["task_id"], 200),
            "title": _text(row["title"], 300) or "Untitled task",
            "stage": _text(row["task_stage"], 40) or "UNKNOWN",
            "status": _text(row["task_status"], 40) or "UNKNOWN",
            "project": _text(row["project"], 200),
        },
        "kind": kind,
        "status": status,
        "provider": safe_activity_code(row["actor"]),
        "current_for_task": bool(row["current_for_task"]),
        "candidate": {
            "sha": candidate_sha,
            "current": candidate_sha is not None and candidate_sha == current_candidate_sha,
        },
        "timing": {
            "started_at": started_at,
            "updated_at": updated_at,
            "terminal_at": terminal_at,
            "recorded_duration_seconds": duration,
            "duration_kind": "RECORDED_TERMINAL" if duration is not None else "UNAVAILABLE",
        },
        "outcome": {
            "code": result_code,
            "classification": RESULT_CLASSES.get(result_code, "UNAVAILABLE"),
            "raw_result_available": row["result"] is not None,
            "raw_result_projected": False,
            "scope": "CURRENT" if row["current_for_task"] else "HISTORY",
        },
        "claim": None
        if row["claim_id"] is None
        else {
            "id": _code(row["claim_id"]),
            "worker_id": safe_activity_code(row["worker_id"]),
            "stage": _text(row["claim_stage"], 40) or "UNKNOWN",
            "active": bool(row["claim_active"]),
            "lease_expires_at": _number(row["lease_expires_at"]),
        },
        "runtime": {
            "current": runtime["current"],
            "identity_coverage": runtime["identity_coverage"],
            "process_state": runtime["process_state"],
            "parent_process_state": runtime["parent_process_state"],
            "cleanup_state": runtime["cleanup_state"],
        },
        "evidence": _evidence(store, str(row["task_id"]), candidate_sha),
    }


def serialize_execution_diagnostics(store: Store) -> dict[str, Any]:
    total = int(store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0])
    rows = store.conn.execute(
        "SELECT e.*,t.title,t.stage AS task_stage,t.status AS task_status,t.project,"
        "c.worker_id,c.stage AS claim_stage,c.lease_expires_at,c.active AS claim_active,"
        "(SELECT latest.id FROM executions latest WHERE latest.task_id=e.task_id "
        " ORDER BY latest.updated_at DESC,latest.rowid DESC LIMIT 1)=e.id AS current_for_task,"
        "(SELECT candidate.sha FROM candidates candidate WHERE candidate.task_id=e.task_id "
        " ORDER BY candidate.created_at DESC,candidate.rowid DESC LIMIT 1) AS current_candidate_sha "
        "FROM executions e JOIN tasks t ON t.id=e.task_id "
        "LEFT JOIN claims c ON c.id=e.claim_id "
        "ORDER BY e.updated_at DESC,e.rowid DESC LIMIT ?",
        (EXECUTION_LIMIT,),
    ).fetchall()
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "status": "AVAILABLE",
        "executions": [_serialize_row(store, row) for row in rows],
        "execution_total": total,
        "execution_limit": EXECUTION_LIMIT,
        "truncated": total > EXECUTION_LIMIT,
        "raw_logs": "WITHHELD",
        "database_writes": 0,
        "external_calls": 0,
    }
