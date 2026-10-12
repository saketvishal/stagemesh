"""Classified, bounded recovery from integration failures, so a routine one never returns control to the operator.

An integration failure is typed (the integrator leaves a finding code). Three kinds are routine and reversible:

* transient: git/runtime trouble with the integration ref or the rebase, no content conflict -> integrate the same candidate again;
* target advanced: the integration ref moved on after review -> refresh the candidate onto the current ref (only the task's own commits
  are replayed), then validate and independently review the new SHA before it is integrated;
* merge conflict: the candidate still does not apply after that refresh -> redo the task on a clean worktree at the current ref, so the
  provider redoes the task-scoped change against what landed (the conflicting files and the preserved candidate are put in front of it).

Which of those happens is decided by `recovery_decision.decide`, not here. A refresh or redo keeps every old candidate, evidence row
and the worktree snapshot as history. The new candidate has a new SHA, so none of the old validation/review/integration evidence
applies to it: it is validated, independently reviewed on its exact SHA and integrated normally. Every recovery is an audit event and
the number of recoveries per task is bounded.

When the failure cannot be repaired safely the task blocks with a typed blocker and a block kind: the classification, the recoveries
already attempted, why automatic repair stops there and what would let it continue. Only a conflict that survives a redo against the
current ref (a design/spec conflict) or one that reaches outside the task's contract needs a person; a spent budget or a missing
dependency is a different kind of block (see `block_kinds`). Anything else (missing evidence, scope or attribution findings) is not
handled here and goes through ordinary remediation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .audit import record_audit
from .block_kinds import block_kind_for, record_block
from .diagnosis import _last_reset
from .domain import EvidenceKind, EvidenceStatus
from .persistence import Store
from .recovery_decision import (
    BASE_ADVANCED,
    BASE_DIVERGED,
    BASE_MISSING,
    BASE_UNCHANGED,
    INTEGRATION_CONFLICT,
    INTEGRATION_TARGET_ADVANCED,
    INTEGRATION_TARGET_MISSING,
    INTEGRATION_TRANSIENT,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryContext,
    RecoveryDecision,
)

RECOVERY_EVENT = "integration.recovery"
BLOCKER_EVENT = "task.integration_blocker"
DECISION_EVENT = "recovery.decision"
DEFAULT_MAX_RECOVERIES = 3

TRANSIENT = "transient_integration_failure"
TARGET_ADVANCED = "target_advanced_after_review"
MERGE_CONFLICT = "merge_conflict_with_current_target"
TARGET_MISSING = "integration_target_missing"

_CODE_CATEGORY = {
    "integration_rebase_unavailable": TRANSIENT,
    "integration_ref_update_failed": TRANSIENT,
    "integration_ref_not_updated": TRANSIENT,
    "integration_non_fast_forward": TARGET_ADVANCED,
    "integration_stale_base": TARGET_ADVANCED,
    "integration_rebase_conflict": MERGE_CONFLICT,
    "integration_ref_missing": TARGET_MISSING,
}
_PRIORITY = (TARGET_MISSING, MERGE_CONFLICT, TARGET_ADVANCED, TRANSIENT)  # the most specific failure wins when several are reported
_DECISION_CATEGORY = {
    TRANSIENT: INTEGRATION_TRANSIENT,
    TARGET_ADVANCED: INTEGRATION_TARGET_ADVANCED,
    MERGE_CONFLICT: INTEGRATION_CONFLICT,
    TARGET_MISSING: INTEGRATION_TARGET_MISSING,
}
_DECISIONS = {
    TARGET_MISSING: "Restore or reconfigure the integration branch (integration_ref); the task is reconsidered automatically once it exists.",
    MERGE_CONFLICT: "Decide which side wins in the conflicting files (this task's change or what already landed), or correct the task so the two no longer overlap; then run `stagemesh retry-task`.",
    TARGET_ADVANCED: "Nothing is wrong with the change. The integration branch kept moving; run-window reconsiders the task automatically after the runtime or configuration changes, or `stagemesh retry-task` grants a fresh budget now.",
    TRANSIENT: "Nothing is wrong with the change. Check git and filesystem health of the integration checkout; run-window reconsiders the task automatically after the runtime or configuration changes.",
}


@dataclass
class IntegrationFailure:
    category: str
    codes: list[str]
    message: str
    files: list[str] = field(default_factory=list)


def classify_integration_failure(store: Store, task_id: str, sha: str) -> IntegrationFailure | None:
    """The routine/typed integration failure of the candidate's latest failed INTEGRATION evidence, or None when it is some other kind."""
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED),
    ).fetchone()
    if row is None:
        return None
    try:
        findings = [f for f in json.loads(row["payload"]).get("findings", []) if isinstance(f, dict)]
    except (TypeError, ValueError):
        return None
    codes = [str(f.get("code")) for f in findings]
    if not codes or any(code not in _CODE_CATEGORY for code in codes):
        return None  # a missing-evidence, scope, attribution or contract finding: ordinary remediation, not an integration repair
    category = next(c for c in _PRIORITY if c in {_CODE_CATEGORY[code] for code in codes})
    message = "; ".join(str(f.get("message", "")) for f in findings)[:600]
    files = _conflict_files(message) if category == MERGE_CONFLICT else []
    return IntegrationFailure(category, sorted(set(codes)), message, files)


def _conflict_files(message: str) -> list[str]:
    for marker in (" conflicts in ", " conflicts with it in "):
        if marker in message:
            listed = message.split(marker, 1)[1].split("; ref left unchanged", 1)[0]
            return [name.strip() for name in listed.split(",") if name.strip()]
    return []


def recoveries(store: Store, task_id: str) -> list[dict[str, Any]]:
    """Recovery attempts since the operator last gave the task a fresh budget."""
    since = _last_reset(store, task_id)
    found = []
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=? AND created_at>=? ORDER BY created_at, rowid", (RECOVERY_EVENT, since)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            found.append(payload)
    return found


def base_relation(is_ancestor: Callable[[str, str], bool], ref_tip: str | None, sha: str) -> str:
    """How the integration ref relates to the candidate: unchanged (fast-forwardable), advanced past it, diverged from it, or missing."""
    if ref_tip is None:
        return BASE_MISSING
    if ref_tip == sha or is_ancestor(ref_tip, sha):
        return BASE_UNCHANGED
    if is_ancestor(sha, ref_tip):
        return BASE_ADVANCED
    return BASE_DIVERGED


def build_context(
    failure: IntegrationFailure,
    sha: str,
    attempted: list[dict[str, Any]],
    max_recoveries: int,
    *,
    relation: str,
    validation_state: str | None,
    review_state: str | None,
    in_scope: Callable[[str], bool],
    provider_health: dict[str, str] | None = None,
    can_refresh_mechanically: bool = True,
) -> RecoveryContext:
    """The decision input for one integration failure. `in_scope` says whether a path is inside the task contract's allowed files."""
    history = tuple(
        RecoveryAttempt(str(a.get("action")), _DECISION_CATEGORY.get(str(a.get("category")), str(a.get("category"))), tuple(a.get("files") or ()))
        for a in attempted
    )
    return RecoveryContext(
        stage="INTEGRATE",
        failure_category=_DECISION_CATEGORY[failure.category],
        candidate_sha=sha,
        base_relation=relation,
        validation_state=validation_state,
        review_state=review_state,
        retry_history=history,
        changed_files=tuple(failure.files),
        task_scoped=all(in_scope(path) for path in failure.files),
        provider_health=provider_health or {},
        recovery_attempts=len(attempted),
        max_attempts=max_recoveries,
        can_refresh_mechanically=can_refresh_mechanically,
    )


def record_decision(store: Store, task_id: str, ctx: RecoveryContext, decision: RecoveryDecision) -> None:
    record_audit(
        store,
        DECISION_EVENT,
        {"task_id": task_id, "action": decision.action.value, "source": decision.source, "reason": decision.reason, "context": ctx.summary()},
    )


def record_recovery(
    store: Store, task_id: str, sha: str, failure: IntegrationFailure, attempted: list[dict[str, Any]], action: RecoveryAction, **extra: Any
) -> dict[str, Any]:
    payload = {
        "task_id": task_id,
        "candidate_sha": sha,
        "category": failure.category,
        "action": action.value,
        "codes": failure.codes,
        "files": failure.files,
        "attempt": len(attempted) + 1,
        "message": failure.message,
        **extra,
    }
    record_audit(store, RECOVERY_EVENT, payload)
    return payload


def block_with_decision(
    store: Store,
    task_id: str,
    sha: str,
    failure: IntegrationFailure,
    attempted: list[dict[str, Any]],
    decision: RecoveryDecision,
    runtime: str | None = None,
) -> dict[str, Any]:
    """Block the task with the typed blocker an operator can act on without opening Git, and with the kind of block it is."""
    kind = block_kind_for(decision)
    blocker = {
        "task_id": task_id,
        "candidate_sha": sha,
        "stage": "INTEGRATE",
        "classification": failure.category,
        "codes": failure.codes,
        "files": failure.files,
        "detail": failure.message,
        "recoveries_attempted": [{"attempt": a.get("attempt"), "action": a.get("action"), "category": a.get("category")} for a in attempted],
        "why_not_automatic": decision.reason,
        "decision_needed": _DECISIONS[failure.category],
        "block_kind": kind.value,
    }
    record_audit(store, BLOCKER_EVENT, blocker)
    record_audit(store, "task.remediation_exhausted", {"task_id": task_id, "candidate_sha": sha, "stage": "INTEGRATE", "reason": "integration_ref_state", "classification": failure.category})
    record_block(
        store,
        task_id,
        kind,
        f"integration {failure.category}: {decision.reason}",
        stage="INTEGRATE",
        candidate_sha=sha,
        runtime=runtime,
        detail={"classification": failure.category, "files": failure.files},
    )
    return blocker


def integration_blocker(store: Store, task_id: str) -> dict[str, Any] | None:
    """The latest typed integration blocker, unless the task was given a fresh budget since."""
    since = _last_reset(store, task_id)
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC LIMIT 100", (BLOCKER_EVENT,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("task_id") == task_id:
            return payload if float(row["created_at"]) >= since else None
    return None


def format_blocker(blocker: dict[str, Any]) -> str:
    tried = ", ".join(f"#{a['attempt']} {a['action']} ({a['category']})" for a in blocker.get("recoveries_attempted", [])) or "none"
    files = f" in {', '.join(blocker['files'])}" if blocker.get("files") else ""
    return (
        f"integration blocked ({blocker['classification']}{files}) on {str(blocker['candidate_sha'])[:7]}; "
        f"recoveries attempted: {tried}; not repairable automatically because {blocker['why_not_automatic']}; "
        f"decision needed: {blocker['decision_needed']}"
    )
