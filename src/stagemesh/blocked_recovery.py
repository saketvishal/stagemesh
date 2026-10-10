"""Bounded automatic recovery of tasks blocked by ordinary provider or workspace trouble.

A task blocked because its provider pool made no progress, or because a provider changed its workspace behind StageMesh's back, is
not a decision for the operator: another attempt on a clean workspace, with the provider rotation continuing, usually works. These
blocks are therefore retried automatically, a bounded number of times per task. When the budget is spent the task stays blocked and
the report says plainly that a human is needed and why. Everything else (contract scope, validation gate, review findings, a real
implementation defect, integration conflicts) is untouched: another attempt would only repeat it.

A tampered workspace is never deleted or reset: its exact state is preserved under `refs/stagemesh/quarantine/...` and the task moves
to a fresh worktree generation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import record_audit
from .autonomy.gitfacts import GitFacts
from .diagnosis import PROVIDER_NO_PROGRESS, _last_reset
from .domain import Stage, TaskStatus
from .persistence import Store
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION
from .workspaces import _task_key, advance_worktree_generation, task_workspace

AUTO_RETRY_EVENT = "task.auto_retried_provider_failure"
AUTO_RETRY_EXHAUSTED_EVENT = "task.auto_retry_exhausted"
DEFAULT_MAX_AUTO_RETRIES = 2

WORKSPACE_MUTATION = "workspace_mutation"
_CAUSES = {
    PROVIDER_NO_PROGRESS: "the provider pool made no usable change",
    WORKSPACE_MUTATION: "a provider changed its workspace outside StageMesh (preserved under a quarantine ref)",
}


def recoverable_cause(store: Store, task_id: str) -> str | None:
    """Why the task is blocked, when that is an ordinary provider/workspace failure; otherwise None (a human decision)."""
    reset = _last_reset(store, task_id)
    rows = store.conn.execute(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN ('task.blocked', 'task.diagnosis_stop') "
        "AND created_at>=? ORDER BY created_at DESC, rowid DESC LIMIT 200",
        (reset,),
    )
    for row in rows:
        payload = _loads(row["payload"])
        if payload.get("task_id") != task_id:
            continue
        if row["event_type"] == "task.blocked":
            if payload.get("reason") == EXTERNAL_WORKSPACE_MUTATION and payload.get("stage") == "IMPLEMENT":
                return WORKSPACE_MUTATION
            return None  # blocked for some other reason, more recently
        return PROVIDER_NO_PROGRESS if payload.get("category") == PROVIDER_NO_PROGRESS else None
    return None


def auto_retries_used(store: Store, task_id: str) -> int:
    """Automatic retries since the operator last granted a fresh budget (a `retry-task`, which is an unblock StageMesh did not make)."""
    auto_times = [
        float(row["created_at"])
        for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=?", (AUTO_RETRY_EVENT,))
        if _loads(row["payload"]).get("task_id") == task_id
    ]
    manual_reset = 0.0
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type='task.unblocked'"):
        if _loads(row["payload"]).get("task_id") != task_id:
            continue
        at = float(row["created_at"])
        if not any(at <= when <= at + 5.0 for when in auto_times):  # our own retries record theirs right after the unblock
            manual_reset = max(manual_reset, at)
    return sum(1 for when in auto_times if when > manual_reset)


def auto_retry_blocked_provider_failures(
    store: Store,
    project: Path,
    *,
    task_id: str | None = None,
    limit: int = 1,
    max_retries: int = DEFAULT_MAX_AUTO_RETRIES,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Unblock tasks stopped by provider no-progress or workspace tampering, within a per-task budget. Returns report entries.

    `exclude` holds tasks that blocked earlier in the current run: they wait until the run has nothing else to do, so one failing
    task never starves the others (the caller lifts the exclusion for its final, bounded retry round).
    """
    recovered: list[dict[str, Any]] = []
    if limit < 1:
        return recovered
    for task in store.tasks():
        current = str(task["id"])
        if task_id is not None and current != task_id:
            continue
        if current in exclude:
            continue
        if task["status"] != TaskStatus.BLOCKED or task["stage"] != Stage.IMPLEMENT:
            continue
        if store.has_active_claim(current) or any(row["task_id"] == current for row in store.running_executions()):
            continue  # never take over a live or unknown owner
        cause = recoverable_cause(store, current)
        if cause is None:
            continue
        used = auto_retries_used(store, current)
        if used >= max_retries:
            _note_exhausted(store, current, cause, used, max_retries)
            continue
        entry: dict[str, Any] = {
            "task_id": current,
            "auto_recovery": f"{cause}_retry",
            "cause": cause,
            "attempt": used + 1,
            "of": max_retries,
            "message": f"{_CAUSES[cause]}; retrying automatically (attempt {used + 1} of {max_retries})",
        }
        if cause == WORKSPACE_MUTATION:
            entry.update(_move_to_fresh_workspace(project, current))
        if not store.unblock_task(current):
            continue
        record_audit(store, AUTO_RETRY_EVENT, {k: v for k, v in entry.items() if k != "message"})
        recovered.append(entry)
        if len(recovered) >= limit:
            break
    return recovered


def can_auto_retry(store: Store, task_id: str, max_retries: int = DEFAULT_MAX_AUTO_RETRIES) -> bool:
    """True when the task is blocked by an ordinary provider/workspace failure and still has automatic retries left."""
    task = store.get_task(task_id)
    return (
        task is not None
        and task["status"] == TaskStatus.BLOCKED
        and task["stage"] == Stage.IMPLEMENT
        and recoverable_cause(store, task_id) is not None
        and auto_retries_used(store, task_id) < max_retries
    )


def exhaustion_message(store: Store, task_id: str) -> str | None:
    """The operator-facing explanation for a task whose automatic retries are spent, or None if it is not that case."""
    cause = recoverable_cause(store, task_id)
    if cause is None:
        return None
    used = auto_retries_used(store, task_id)
    if used == 0:
        return None
    return (
        f"task {task_id} stayed blocked after {used} automatic retr{'y' if used == 1 else 'ies'}: {_CAUSES[cause]}. "
        "StageMesh has stopped retrying it and continues with other work; this needs a human to check provider availability and "
        "the task text (or, for a quarantined workspace, review the quarantine ref), then `stagemesh retry-task --task "
        f"{task_id}` grants a fresh budget."
    )


def _move_to_fresh_workspace(project: Path, task_id: str) -> dict[str, Any]:
    """Preserve the tampered worktree under a quarantine ref and point the task at a new worktree generation. Nothing is deleted."""
    old = task_workspace(project, task_id)
    detail: dict[str, Any] = {"previous_workspace": str(old)}
    try:
        if (old / ".git").exists():
            snapshot = GitFacts(old).snapshot_worktree(old, f"StageMesh quarantine of {task_id}: preserved before automatic retry")
            if snapshot:
                ref = f"refs/stagemesh/quarantine/{_task_key(task_id)}/{snapshot[:12]}"
                GitFacts(project).ensure_ref(ref, snapshot)
                detail["quarantine_ref"] = ref
        _old, new = advance_worktree_generation(project, task_id)
        detail["new_workspace"] = str(new)
    except Exception as exc:  # noqa: BLE001 - recovery is best effort; the task is only unblocked on a fresh workspace
        detail["workspace_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return detail


def _note_exhausted(store: Store, task_id: str, cause: str, used: int, max_retries: int) -> None:
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (AUTO_RETRY_EXHAUSTED_EVENT,)):
        if _loads(row["payload"]).get("task_id") == task_id:
            return
    record_audit(store, AUTO_RETRY_EXHAUSTED_EVENT, {"task_id": task_id, "cause": cause, "retries": used, "of": max_retries})


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
