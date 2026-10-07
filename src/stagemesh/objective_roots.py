from __future__ import annotations

import json
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


def folded_labels(labels: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset(str(label).casefold() for label in labels)


def source_issue_is_objective(labels: tuple[str, ...]) -> bool:
    """True only for source issues explicitly classified as planning/objective roots.

    StageMesh must not infer this from a broad-looking title/body; ordinary implementation tasks can also contain sections named
    "Objective". A source can opt a root back into direct implementation with a direct-execution label.
    """
    folded = folded_labels(labels)
    return bool(folded & OBJECTIVE_LABELS) and not bool(folded & DIRECT_EXECUTION_LABELS)


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
    if folded & DIRECT_EXECUTION_LABELS:
        return None
    if folded & OBJECTIVE_LABELS or state.get("objective_root") is True:
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
