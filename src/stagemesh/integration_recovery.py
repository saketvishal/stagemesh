"""Classified, bounded recovery from integration failures, so a routine one never returns control to the operator.

An integration failure is typed (the integrator leaves a finding code). Three kinds are routine and reversible:

* transient: git/runtime trouble with the integration ref or the rebase, no content conflict -> integrate again;
* target advanced: the integration ref moved on after review and the rebase budget is spent -> restart the task on the current ref;
* merge conflict: the candidate no longer applies to the advanced ref -> restart the task on the current ref, so the provider redoes
  the task-scoped change against what landed (the conflicting files and the preserved candidate are put in front of it).

A restart keeps every old candidate, evidence row and the worktree snapshot as history. The new candidate has a new SHA, so none of
the old validation/review/integration evidence applies to it: it is validated, independently reviewed on its exact SHA and integrated
normally. Every recovery is an audit event and the number of recoveries per task is bounded.

When the failure cannot be repaired safely (a missing ref, the same conflict again after a restart on the current ref, a refused
restart, or the budget spent) the task blocks with a typed blocker: the classification, the recoveries already attempted, why
automatic repair stops there and the minimum decision a person has to make. Anything else (missing evidence, scope or attribution
findings) is not handled here and goes through ordinary remediation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .audit import record_audit
from .diagnosis import _last_reset
from .domain import EvidenceKind, EvidenceStatus
from .persistence import Store

RECOVERY_EVENT = "integration.recovery"
BLOCKER_EVENT = "task.integration_blocker"
DEFAULT_MAX_RECOVERIES = 3

TRANSIENT = "transient_integration_failure"
TARGET_ADVANCED = "target_advanced_after_review"
MERGE_CONFLICT = "merge_conflict_with_current_target"
TARGET_MISSING = "integration_target_missing"

RETRY_INTEGRATION = "retry_integration"
RESTART_ON_CURRENT_BASE = "restart_on_current_base"
ESCALATE = "escalate"

_CODE_CATEGORY = {
    "integration_rebase_unavailable": TRANSIENT,
    "integration_ref_update_failed": TRANSIENT,
    "integration_ref_not_updated": TRANSIENT,
    "integration_non_fast_forward": TARGET_ADVANCED,
    "integration_stale_base": TARGET_ADVANCED,
    "integration_rebase_conflict": MERGE_CONFLICT,
    "integration_ref_missing": TARGET_MISSING,
}
_ACTION = {TRANSIENT: RETRY_INTEGRATION, TARGET_ADVANCED: RESTART_ON_CURRENT_BASE, MERGE_CONFLICT: RESTART_ON_CURRENT_BASE, TARGET_MISSING: ESCALATE}
_PRIORITY = (TARGET_MISSING, MERGE_CONFLICT, TARGET_ADVANCED, TRANSIENT)  # the most specific failure wins when several are reported
_DECISIONS = {
    TARGET_MISSING: "Restore or reconfigure the integration branch (integration_ref), then run `stagemesh retry-task`.",
    MERGE_CONFLICT: "Decide which side wins in the conflicting files (this task's change or what already landed), or correct the task so the two no longer overlap; then run `stagemesh retry-task`.",
    TARGET_ADVANCED: "Let the integration branch settle (other work keeps landing on it) or serialize this task after that work; then run `stagemesh retry-task`.",
    TRANSIENT: "Check git and filesystem health of the integration checkout; then run `stagemesh retry-task`.",
}


@dataclass
class IntegrationFailure:
    category: str
    action: str
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
    return IntegrationFailure(category, _ACTION[category], sorted(set(codes)), message, files)


def _conflict_files(message: str) -> list[str]:
    marker = " conflicts in "
    if marker not in message:
        return []
    listed = message.split(marker, 1)[1].split("; ref left unchanged", 1)[0]
    return [name.strip() for name in listed.split(",") if name.strip()]


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


def escalation_reason(failure: IntegrationFailure, attempted: list[dict[str, Any]], max_recoveries: int) -> str | None:
    """Why StageMesh must stop repairing this failure itself, or None when another automatic attempt is allowed."""
    if failure.action == ESCALATE:
        return "the integration target does not exist, so there is nothing to refresh or retry against"
    if len(attempted) >= max_recoveries:
        return f"{len(attempted)} automatic recovery attempt(s) are spent (limit {max_recoveries}) and the failure came back"
    if failure.category == MERGE_CONFLICT and any(a.get("category") == MERGE_CONFLICT and set(a.get("files") or []) & set(failure.files) for a in attempted):
        return (
            "the task was already redone against the current integration ref and still conflicts in the same file(s); "
            "that is a design/spec conflict between this task and work that landed, not a stale branch"
        )
    return None


def record_recovery(store: Store, task_id: str, sha: str, failure: IntegrationFailure, attempted: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    payload = {
        "task_id": task_id,
        "candidate_sha": sha,
        "category": failure.category,
        "action": failure.action,
        "codes": failure.codes,
        "files": failure.files,
        "attempt": len(attempted) + 1,
        "message": failure.message,
        **extra,
    }
    record_audit(store, RECOVERY_EVENT, payload)
    return payload


def block_with_decision(store: Store, task_id: str, sha: str, failure: IntegrationFailure, attempted: list[dict[str, Any]], why: str) -> dict[str, Any]:
    """Block the task with the typed blocker an operator can act on without opening Git."""
    store.block_task(task_id)
    blocker = {
        "task_id": task_id,
        "candidate_sha": sha,
        "stage": "INTEGRATE",
        "classification": failure.category,
        "codes": failure.codes,
        "files": failure.files,
        "detail": failure.message,
        "recoveries_attempted": [{"attempt": a.get("attempt"), "action": a.get("action"), "category": a.get("category")} for a in attempted],
        "why_not_automatic": why,
        "decision_needed": _DECISIONS[failure.category],
    }
    record_audit(store, BLOCKER_EVENT, blocker)
    record_audit(store, "task.remediation_exhausted", {"task_id": task_id, "candidate_sha": sha, "stage": "INTEGRATE", "reason": "integration_ref_state", "classification": failure.category})
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
