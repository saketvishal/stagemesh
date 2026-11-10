from __future__ import annotations

import json
import re
from typing import Any

GITHUB_SOURCE = "github"
OBJECTIVE_LABELS = frozenset(
    {
        "stagemesh:objective",
        "type:objective",
        "objective",
    }
)
DIRECT_EXECUTION_LABELS = frozenset(
    {
        "stagemesh:direct-execution",
        "stagemesh:implementation-task",
        "type:task",
    }
)

_PROGRAM_MARKER = re.compile(r"<!--\s*stagemesh-program:v\d+\s*-->", re.IGNORECASE)
_OPERATIONS_GATE_MARKER = re.compile(r"<!--\s*stagemesh-operations-gate:v\d+\s*-->", re.IGNORECASE)
_DECLARED_BLOCKED = re.compile(r"^\s*StageMesh\s+status\s*:\s*BLOCKED\b", re.IGNORECASE | re.MULTILINE)
_NO_REPOSITORY_EDITS = re.compile(
    r"(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?Allowed files(?:\*\*)?\s*:\s*(?:\r?\n\s*)?"
    r"(?:[-*]\s*)?(?:\*\*)?No repository file edits\.?(?:\*\*)?\s*(?:\n|$)",
    re.IGNORECASE,
)


def folded_labels(labels: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset(str(label).casefold() for label in labels)


def _has_objective_label(labels: frozenset[str]) -> bool:
    return bool(labels & OBJECTIVE_LABELS) or any(label.endswith(":objective") for label in labels)


def source_issue_is_objective(labels: tuple[str, ...]) -> bool:
    """True only for source issues explicitly classified as planning/objective roots.

    StageMesh must not infer this from a broad-looking title/body; ordinary implementation tasks can also contain sections named
    "Objective". Namespaced labels such as ``caventra:objective`` are explicit source classification, and a source can opt a
    root back into direct implementation with a direct-execution label.
    """
    folded = folded_labels(labels)
    return _has_objective_label(folded) and not bool(folded & DIRECT_EXECUTION_LABELS)


def source_contract_refusal_reason(body: str) -> str | None:
    """Return an explicit issue-contract reason that makes a task non-runnable."""
    if _PROGRAM_MARKER.search(body):
        return "source program/tracking container"
    if _OPERATIONS_GATE_MARKER.search(body):
        return "source operations gate requires a non-code adapter"
    if _DECLARED_BLOCKED.search(body):
        return "source contract declares BLOCKED"
    if _NO_REPOSITORY_EDITS.search(body):
        return "source task permits no repository file edits"
    return None


def task_source_state(store: Any, task: Any) -> dict[str, Any]:
    try:
        row = store.conn.execute(
            "SELECT state FROM source_cache WHERE source=? AND source_id=?",
            (task["source"], task["source_id"]),
        ).fetchone()
    except Exception:  # pragma: no cover - defensive for non-sqlite store-like fakes
        return {}
    if row is None:
        return {}
    try:
        value = json.loads(row["state"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def objective_root_reason(store: Any, task: Any) -> str | None:
    """Why this task row is an objective root that must not be implemented directly, if any.

    This is intentionally checked at scheduling time as well as source-sync time so durable stores created by older runtimes are
    reconciled without erasing their task/audit/candidate history.
    """
    if task is None or task["source"] != GITHUB_SOURCE or task["source_id"] is None:
        return None
    state = task_source_state(store, task)
    labels = tuple(str(label) for label in state.get("labels", ()) if isinstance(label, str))
    folded = folded_labels(labels)
    body = state.get("objective")
    if isinstance(body, str):
        refusal = source_contract_refusal_reason(body)
        if refusal is not None:
            return refusal
    if folded & DIRECT_EXECUTION_LABELS:
        return None
    if _has_objective_label(folded) or state.get("objective_root") is True:
        return "source objective root"
    source_id = str(task["source_id"])
    row = store.conn.execute("SELECT 1 FROM objectives WHERE id IN (?, ?) LIMIT 1", (source_id, f"github:{source_id}")).fetchone()
    return "historical source objective root" if row is not None else None


def objective_payload(source_id: str, labels: tuple[str, ...], body: str, dependencies: tuple[str, ...]) -> dict[str, Any]:
    return {
        "source": GITHUB_SOURCE,
        "source_id": source_id,
        "labels": list(labels),
        "body": body[:6000],
        "dependencies": list(dependencies),
        "direct_execution": bool(folded_labels(labels) & DIRECT_EXECUTION_LABELS),
    }
