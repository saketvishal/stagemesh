"""Finding-aware review/remediation convergence.

Gives reviewer findings durable, content-fingerprinted identity across
review cycles so a task's finding_registry (see BuildTask.finding_registry)
can drive escalation decisions on unresolved substantive findings rather
than on a raw remediation-cycle count alone.

Reconciliation is content-driven and deterministic:

- A finding reported again in a later review cycle (by fingerprint) is the
  *same* finding, regardless of minor wording changes.
- A finding previously STILL_OPEN that is not re-reported in a later cycle
  is treated as RESOLVED (the reviewer had the opportunity to restate it and
  did not) unless a disposition override says otherwise.
- A finding that reappears after being closed is a reopen; the reappearance
  itself is the evidence/reason unless a disposition override supplies one.
- Explicit disposition overrides (id + status [+ reason]) always take
  precedence over the default absence-implies-resolved inference, and are
  required to keep a finding open despite it going unreported.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

STATUS_STILL_OPEN = "STILL_OPEN"
STATUS_RESOLVED = "RESOLVED"
STATUS_INVALID = "INVALID"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

FINDING_STATUS_VALUES = (
    STATUS_STILL_OPEN,
    STATUS_RESOLVED,
    STATUS_INVALID,
    STATUS_NOT_APPLICABLE,
)
CLOSED_FINDING_STATUSES = frozenset({STATUS_RESOLVED, STATUS_INVALID, STATUS_NOT_APPLICABLE})

_HISTORY_LIMIT = 20
_PROCESSED_EXECUTION_LIMIT = 50


def finding_fingerprint(description: str) -> str:
    """Stable content-based identity for a finding, tolerant of minor
    wording/whitespace/punctuation/case changes across review cycles."""
    normalized = re.sub(r"[^a-z0-9]+", " ", description.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def _normalize_status(raw: Any) -> str:
    status = str(raw or "").strip().upper()
    return status if status in FINDING_STATUS_VALUES else STATUS_STILL_OPEN


def reconcile_findings(
    registry: dict[str, Any] | None,
    *,
    findings: list[Any],
    finding_dispositions: list[dict[str, Any]] | None,
    execution_id: str | None,
    cycle_label: str,
) -> dict[str, Any]:
    """Merge one review cycle's findings/dispositions into the durable
    per-task finding registry. Idempotent: reprocessing the same
    execution_id is a no-op, so restart/duplicate-result handling is safe."""
    registry = dict(registry or {})
    entries: dict[str, dict[str, Any]] = {
        key: dict(value) for key, value in (registry.get("entries") or {}).items()
    }
    processed: list[str] = list(registry.get("processed_execution_ids") or [])
    if execution_id and execution_id in processed:
        return registry

    overrides: dict[str, dict[str, Any]] = {}
    for disposition in finding_dispositions or ():
        if not isinstance(disposition, dict):
            continue
        finding_id = str(disposition.get("id") or "")
        if finding_id:
            overrides[finding_id] = disposition

    current: dict[str, str] = {}
    for raw in findings or ():
        description = str(raw).strip()
        if description:
            current[finding_fingerprint(description)] = description

    def _append_history(entry: dict[str, Any], *, status: str, reason: str = "", reopened: bool = False) -> None:
        history = list(entry.get("history") or [])[-(_HISTORY_LIMIT - 1) :]
        history.append(
            {
                "execution_id": execution_id,
                "cycle": cycle_label,
                "status": status,
                "reason": reason,
                "reopened": reopened,
            }
        )
        entry["history"] = history

    # 1) Findings not restated this cycle: apply explicit disposition
    # overrides regardless of prior status (an override is always sufficient
    # evidence -- it is the reviewer's explicit classification, including
    # reopening a previously closed finding with a reason). Absent an
    # override, a previously STILL_OPEN finding that goes unreported is
    # presumed resolved; a previously closed finding with no override stays
    # as-is (silence is not evidence to reopen it).
    for finding_id, entry in entries.items():
        if finding_id in current:
            continue
        override = overrides.get(finding_id)
        if override is not None:
            status = _normalize_status(override.get("status"))
            reason = str(override.get("reason") or override.get("notes") or "").strip()
            was_closed = entry.get("status") in CLOSED_FINDING_STATUSES
            if status == STATUS_STILL_OPEN:
                if was_closed and not reason:
                    # no evidence supplied to reopen a closed finding
                    continue
                entry["status"] = STATUS_STILL_OPEN
                if was_closed:
                    entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry["last_seen_cycle"] = cycle_label
                _append_history(entry, status=STATUS_STILL_OPEN, reason=reason, reopened=was_closed)
                continue
            entry["status"] = status
            entry["resolution_reason"] = reason
            entry["last_seen_cycle"] = cycle_label
            _append_history(entry, status=status, reason=reason)
            continue
        if entry.get("status") != STATUS_STILL_OPEN:
            continue
        status = STATUS_RESOLVED
        reason = f"not re-reported by reviewer in {cycle_label}; presumed resolved"
        entry["status"] = status
        entry["resolution_reason"] = reason
        entry["last_seen_cycle"] = cycle_label
        _append_history(entry, status=status, reason=reason)

    # 2) Findings reported this cycle: new, repeated, or reopened.
    for finding_id, description in current.items():
        entry = entries.get(finding_id)
        was_closed = bool(entry) and entry.get("status") in CLOSED_FINDING_STATUSES
        if entry is None:
            entry = {
                "id": finding_id,
                "description": description,
                "status": STATUS_STILL_OPEN,
                "attempts": 0,
                "first_seen_cycle": cycle_label,
                "history": [],
            }
        override = overrides.get(finding_id)
        if override is not None and _normalize_status(override.get("status")) in CLOSED_FINDING_STATUSES:
            status = _normalize_status(override.get("status"))
            reason = str(override.get("reason") or override.get("notes") or "").strip()
            entry["status"] = status
            entry["resolution_reason"] = reason
        else:
            reason = str((override or {}).get("reason") or (override or {}).get("notes") or "").strip()
            entry["status"] = STATUS_STILL_OPEN
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["description"] = description
        entry["last_seen_cycle"] = cycle_label
        _append_history(entry, status=entry["status"], reason=reason, reopened=was_closed and entry["status"] == STATUS_STILL_OPEN)
        entries[finding_id] = entry

    if execution_id:
        processed = processed[-(_PROCESSED_EXECUTION_LIMIT - 1) :]
        processed.append(execution_id)

    registry["entries"] = entries
    registry["processed_execution_ids"] = processed
    return registry


def open_findings(registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    entries = (registry or {}).get("entries") or {}
    return [entry for entry in entries.values() if entry.get("status") == STATUS_STILL_OPEN]


def escalation_evidence(registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": entry.get("id"),
            "description": entry.get("description"),
            "attempts": entry.get("attempts"),
            "first_seen_cycle": entry.get("first_seen_cycle"),
        }
        for entry in open_findings(registry)
    ]
