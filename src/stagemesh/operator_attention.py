"""Bounded, read-only human-action queue for the operator UI.

Only durable, typed facts that explicitly require a person are projected. Ordinary
operational failures, cooldowns, unknown approval coverage, and inferred absence of
work never become queue items.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .block_kinds import BlockKind
from .delivery_state import DELIVERY_UNBLOCKED, EXTERNALLY_BLOCKED
from .persistence import Store
from .redaction import redact_output

ATTENTION_CONTRACT = "stagemesh.attention-queue/1"
ACTION_LIMIT = 100
EVENT_SCAN_LIMIT = 500
_EVENT_TYPES = (
    "task.block_classified",
    "task.unblocked",
    "task.block_reentered",
    EXTERNALLY_BLOCKED,
    DELIVERY_UNBLOCKED,
    "window.fast_forward_merged",
)


def _loads(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = redact_output(value).replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:limit] or None


def _sha(value: object) -> str | None:
    if not isinstance(value, str) or len(value) not in {40, 64}:
        return None
    return value.lower() if all(char in "0123456789abcdefABCDEF" for char in value) else None


def _item_id(kind: str, task_id: str, candidate_sha: str | None) -> str:
    value = f"{kind}\0{task_id}\0{candidate_sha or ''}".encode()
    return hashlib.sha256(value).hexdigest()[:20]


def serialize_attention_queue(store: Store) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in _EVENT_TYPES)
    rows = store.conn.execute(
        f"SELECT event_type,payload,created_at FROM audit_events "
        f"WHERE event_type IN ({placeholders}) "
        "ORDER BY created_at DESC,rowid DESC LIMIT ?",
        (*_EVENT_TYPES, EVENT_SCAN_LIMIT + 1),
    ).fetchall()
    history_truncated = len(rows) > EVENT_SCAN_LIMIT
    seen: set[tuple[str, str]] = set()
    task_cache: dict[str, Any] = {}
    candidate_cache: dict[str, str | None] = {}
    items: list[dict[str, Any]] = []

    for row in rows[:EVENT_SCAN_LIMIT]:
        payload = _loads(row["payload"])
        raw_task_id = payload.get("task_id")
        if not isinstance(raw_task_id, str):
            continue
        event = str(row["event_type"])
        kind = "HUMAN_DECISION" if event.startswith("task.") else "DELIVERY_PERMISSION"
        key = (kind, raw_task_id)
        if key in seen:
            continue

        if raw_task_id not in task_cache:
            task_cache[raw_task_id] = store.get_task(raw_task_id)
        task = task_cache[raw_task_id]
        if task is None:
            continue
        if raw_task_id not in candidate_cache:
            candidate = store.latest_candidate(raw_task_id)
            candidate_cache[raw_task_id] = None if candidate is None else _sha(candidate["sha"])
        current_sha = candidate_cache[raw_task_id]

        if kind == "HUMAN_DECISION":
            seen.add(key)
            if (
                event != "task.block_classified"
                or payload.get("kind") != BlockKind.HUMAN_DECISION
                or str(task["status"]) != "BLOCKED"
            ):
                continue
            detail = _text(payload.get("reason"), 400) or "Decision reason was not recorded."
            reason_code = "RECORDED_HUMAN_DECISION"
            candidate_sha = _sha(payload.get("candidate_sha"))
        else:
            raw_candidate_sha = payload.get("candidate_sha")
            candidate_sha = _sha(raw_candidate_sha)
            if event == EXTERNALLY_BLOCKED and (
                (raw_candidate_sha is not None and candidate_sha is None)
                or candidate_sha not in {None, current_sha}
            ):
                continue
            seen.add(key)
            if event != EXTERNALLY_BLOCKED:
                continue
            needed = _text(payload.get("needed"), 120) or "unspecified permission"
            reason = _text(payload.get("reason"), 260) or "Reason was not recorded."
            detail = f"Needs {needed}: {reason}"[:400]
            reason_code = "RECORDED_DELIVERY_PERMISSION"

        task_id = _text(raw_task_id, 200)
        if task_id is None:
            continue
        items.append(
            {
                "id": _item_id(kind, task_id, candidate_sha),
                "kind": kind,
                "reason_code": reason_code,
                "task_id": task_id,
                "title": _text(task["title"], 300) or "Untitled task",
                "stage": _text(task["stage"], 40) or "UNKNOWN",
                "task_status": _text(task["status"], 40) or "UNKNOWN",
                "candidate_sha": candidate_sha,
                "detail": detail,
                "recorded_at": float(row["created_at"]),
                "recommended_surface": "TASK_DETAIL",
            }
        )

    known_total = len(items)
    shown = items[:ACTION_LIMIT]
    truncated = history_truncated or known_total > ACTION_LIMIT
    return {
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "status": "AVAILABLE",
        "items": shown,
        "queue_total": known_total,
        "queue_total_kind": "LOWER_BOUND" if history_truncated else "EXACT",
        "queue_limit": ACTION_LIMIT,
        "truncated": truncated,
        "history_scan_limit": EVENT_SCAN_LIMIT,
        "history_truncated": history_truncated,
        "approval_coverage": "UNKNOWN",
        "human_action_only": True,
        "operational_blocks_excluded": True,
        "counts": {
            "HUMAN_DECISION": sum(item["kind"] == "HUMAN_DECISION" for item in shown),
            "DELIVERY_PERMISSION": sum(item["kind"] == "DELIVERY_PERMISSION" for item in shown),
        },
        "counts_scope": "SHOWN",
        "database_writes": 0,
        "external_calls": 0,
    }
