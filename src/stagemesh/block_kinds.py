"""Why a task is BLOCKED, as one of four kinds, and which of them StageMesh reconsiders by itself.

A BLOCKED task used to mean "an operator has to look". Most blocks are not that. The kind says who can unblock the task:

* `BLOCKED_HUMAN_DECISION`       a product, spec, security or legal choice that repository state cannot settle (needs a person);
* `BLOCKED_EXTERNAL_DEPENDENCY`  something outside StageMesh must exist first (a missing integration branch, a dependency);
* `BLOCKED_RECOVERABLE_OPERATIONAL` an ordinary engineering or runtime failure whose automatic recovery budget ran out; the task is
                                 reconsidered by `run-window` after the runtime or its configuration changed (bounded);
* `BLOCKED_POLICY_SAFETY`        a safety rule stopped the work (foreign mutation of a workspace or ref); never retried automatically.

New blocks record their kind (`task.block_classified`). Blocks recorded before that existed are classified from the audit events that
caused them, so the tasks that are blocked today are covered without a migration. A block with no recorded cause is left alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .audit import record_audit
from .diagnosis import CONTRACT_SCOPE, _last_reset
from .domain import TaskStatus
from .persistence import Store
from .recovery_decision import RecoveryAction, RecoveryDecision
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION

BLOCK_EVENT = "task.block_classified"
REENTER_EVENT = "task.block_reentered"
MAX_REENTRIES = 3


class BlockKind(StrEnum):
    HUMAN_DECISION = "BLOCKED_HUMAN_DECISION"
    EXTERNAL_DEPENDENCY = "BLOCKED_EXTERNAL_DEPENDENCY"
    RECOVERABLE_OPERATIONAL = "BLOCKED_RECOVERABLE_OPERATIONAL"
    POLICY_SAFETY = "BLOCKED_POLICY_SAFETY"


@dataclass(frozen=True)
class BlockInfo:
    kind: BlockKind
    reason: str
    source: str  # recorded | legacy:<event type>
    runtime: str | None = None  # runtime fingerprint when it was blocked; None for blocks that predate the fingerprint


def block_kind_for(decision: RecoveryDecision) -> BlockKind:
    """The kind a blocking recovery decision stands for."""
    if decision.action is RecoveryAction.BLOCK_HUMAN_DECISION:
        return BlockKind.HUMAN_DECISION
    if decision.action is RecoveryAction.ABORT_UNSAFE:
        return BlockKind.POLICY_SAFETY
    if decision.action is RecoveryAction.BLOCK_EXTERNAL:
        return BlockKind.EXTERNAL_DEPENDENCY if decision.external_dependency else BlockKind.RECOVERABLE_OPERATIONAL
    raise ValueError(f"{decision.action} does not block a task")


def runtime_fingerprint(project: Path) -> str:
    """A short hash of what a recovery decision depends on: the StageMesh code that is running and the project's StageMesh configuration."""
    import stagemesh

    digest = hashlib.sha256()
    package = Path(stagemesh.__file__).resolve().parent
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode())
        digest.update(source.read_bytes())
    for name in ("config.json", "project.yaml"):
        config = Path(project) / ".stagemesh" / name
        if config.is_file():
            digest.update(config.read_bytes())
    return digest.hexdigest()[:16]


def record_block(
    store: Store,
    task_id: str,
    kind: BlockKind,
    reason: str,
    *,
    stage: str | None = None,
    candidate_sha: str | None = None,
    runtime: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Block the task and record which kind of block it is and exactly why further automatic action stops."""
    store.block_task(task_id)
    record_audit(
        store,
        BLOCK_EVENT,
        {
            "task_id": task_id,
            "kind": kind.value,
            "reason": reason[:1500],
            "stage": stage,
            "candidate_sha": candidate_sha,
            "runtime": runtime,
            **(detail or {}),
        },
    )


def classify_block(store: Store, task_id: str) -> BlockInfo | None:
    """The kind of the task's current block, or None when its cause was not recorded (such a block is never retried automatically)."""
    reset = _last_reset(store, task_id)
    rows = store.conn.execute(
        "SELECT event_type, payload FROM audit_events WHERE event_type IN (?, 'task.remediation_exhausted', 'task.diagnosis_stop', 'task.blocked') "
        "AND created_at>=? ORDER BY created_at DESC, rowid DESC LIMIT 300",
        (BLOCK_EVENT, reset),
    )
    mine = [(str(row["event_type"]), _loads(row["payload"])) for row in rows]
    mine = [(event, payload) for event, payload in mine if payload.get("task_id") == task_id]
    for index, (event, payload) in enumerate(mine):
        if event == BLOCK_EVENT:
            try:
                kind = BlockKind(str(payload.get("kind")))
            except ValueError:
                return None
            return BlockInfo(kind, str(payload.get("reason", "")), "recorded", payload.get("runtime"))
        if event == "task.remediation_exhausted" and payload.get("reason") == "repeated_failure_diagnosed":
            # the diagnosis that caused this stop was recorded just before it and says what kind of failure repeated
            for earlier_event, earlier in mine[index + 1 :]:
                if earlier_event == "task.diagnosis_stop":
                    return _legacy(earlier_event, earlier)
        return _legacy(event, payload)
    return None


def _legacy(event: str, payload: dict[str, Any]) -> BlockInfo:
    """Blocks written before kinds were recorded: remediation budgets and integration states are operational, scope and safety are not."""
    if event == "task.blocked":
        if payload.get("reason") == EXTERNAL_WORKSPACE_MUTATION:
            return BlockInfo(BlockKind.POLICY_SAFETY, "a workspace was changed outside StageMesh", f"legacy:{event}")
        return BlockInfo(BlockKind.POLICY_SAFETY, str(payload.get("reason", "blocked by a safety rule")), f"legacy:{event}")
    if event == "task.diagnosis_stop":
        if payload.get("category") == CONTRACT_SCOPE:
            return BlockInfo(BlockKind.HUMAN_DECISION, "the contract's allowed files and the work disagree: should the contract be widened (stagemesh rebind-contract) to include what the work needed, or must the work stay inside it?", f"legacy:{event}")
        return BlockInfo(BlockKind.RECOVERABLE_OPERATIONAL, f"stopped on a diagnosed {payload.get('category', 'failure')}", f"legacy:{event}")
    reason = str(payload.get("reason") or "remediation budget exhausted")
    return BlockInfo(BlockKind.RECOVERABLE_OPERATIONAL, reason, f"legacy:{event}")


def reentries_used(store: Store, task_id: str) -> int:
    """Automatic re-entries since an operator last gave the task a fresh budget (an unblock StageMesh did not make itself)."""
    ours = [
        float(row["created_at"])
        for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=?", (REENTER_EVENT,))
        if _loads(row["payload"]).get("task_id") == task_id
    ]
    manual = 0.0
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type='task.unblocked'"):
        if _loads(row["payload"]).get("task_id") != task_id:
            continue
        at = float(row["created_at"])
        if not any(at <= when <= at + 5.0 for when in ours):  # our own re-entries record theirs right after the unblock
            manual = max(manual, at)
    return sum(1 for when in ours if when > manual)


def _last_reentry_runtime(store: Store, task_id: str) -> str | None:
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 200", (REENTER_EVENT,)):
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            return payload.get("runtime")
    return None


def reentry_refusal(store: Store, task_id: str, fingerprint: str) -> str | None:
    """Why a BLOCKED task is not reconsidered right now, or None when it is eligible."""
    task = store.get_task(task_id)
    if task is None or task["status"] != TaskStatus.BLOCKED:
        return "not blocked"
    info = classify_block(store, task_id)
    if info is None:
        return "blocked with no recorded cause; not retried automatically"
    if info.kind is not BlockKind.RECOVERABLE_OPERATIONAL:
        return f"{info.kind.value}: {info.reason}"
    if store.has_active_claim(task_id) or any(row["task_id"] == task_id for row in store.running_executions()):
        return "has an active claim or running execution"
    used = reentries_used(store, task_id)
    if used >= MAX_REENTRIES:
        return f"{used} automatic re-entries are spent (limit {MAX_REENTRIES}); `retry-task` grants a fresh budget"
    if fingerprint in (info.runtime, _last_reentry_runtime(store, task_id)):
        return "the runtime and configuration are unchanged since the last attempt; waiting for a change"
    return None


def reenter_blocked_task(store: Store, task_id: str, fingerprint: str) -> dict[str, Any] | None:
    """Return an eligible recoverable-operational task to OPEN with a fresh remediation budget. History, candidates and evidence stay."""
    if reentry_refusal(store, task_id, fingerprint) is not None:
        return None
    info = classify_block(store, task_id)
    assert info is not None
    task = store.get_task(task_id)
    stage = str(task["stage"]) if task is not None else None
    attempt = reentries_used(store, task_id) + 1  # counted before the unblock, which looks like an operator reset until our event follows
    if not store.unblock_task(task_id):
        return None
    entry = {
        "task_id": task_id,
        "auto_recovery": "blocked_reentry",
        "attempt": attempt,
        "of": MAX_REENTRIES,
        "stage": stage,
        "was_blocked_because": info.reason[:300],
        "block_source": info.source,
        "runtime": fingerprint,
    }
    record_audit(store, REENTER_EVENT, entry)
    return entry


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
