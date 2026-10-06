"""Typed autonomy decisions, human-escalation contract, and the durable decision trace.

Every autonomous choice the supervisor makes is an `AutonomyDecision`: what was observed, how it was classified, which policy
applied, which action was taken and which SHAs were involved. A decision is written to the audit log (`autonomy.decision`) so any
outcome can be explained after the fact. Escalating to the founder is itself a typed decision and must carry the three things the
founder needs: what was already tried, why the safe answer cannot be determined, and the smallest decision required.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..audit import record_audit
from ..persistence import Store

DECISION_EVENT = "autonomy.decision"
OUTCOME_EVENT = "autonomy.task_outcome"
REQUIRED_STREAK = 10


class Condition(StrEnum):
    """What the supervisor observed. One value per distinguishable engineering situation."""

    EXTERNAL_WORKSPACE_MUTATION = "EXTERNAL_WORKSPACE_MUTATION"
    EVIDENCE_NOT_BOUND_TO_CANDIDATE = "EVIDENCE_NOT_BOUND_TO_CANDIDATE"
    BASE_UNCHANGED = "BASE_UNCHANGED"
    BASE_ADVANCED = "BASE_ADVANCED"
    BASE_HISTORY_REWRITTEN = "BASE_HISTORY_REWRITTEN"
    BASE_HISTORY_REWRITTEN_CONTENT_CHANGED = "BASE_HISTORY_REWRITTEN_CONTENT_CHANGED"
    BASE_DEPENDENCY_LANDED = "BASE_DEPENDENCY_LANDED"
    CANDIDATE_ALREADY_INTEGRATED = "CANDIDATE_ALREADY_INTEGRATED"
    BASE_PROVENANCE_UNRECOVERABLE = "BASE_PROVENANCE_UNRECOVERABLE"
    CI_CANDIDATE_REGRESSION = "CI_CANDIDATE_REGRESSION"
    CI_BASELINE_FAILURE = "CI_BASELINE_FAILURE"
    CI_BROKEN_FRAGILE_TEST = "CI_BROKEN_FRAGILE_TEST"
    CI_INFRASTRUCTURE_FAILURE = "CI_INFRASTRUCTURE_FAILURE"
    CI_DEPENDENCY_BASE_PR_FAILURE = "CI_DEPENDENCY_BASE_PR_FAILURE"
    CI_UNSUPPORTED_ENVIRONMENT = "CI_UNSUPPORTED_ENVIRONMENT"
    CI_GENUINE_UNKNOWN = "CI_GENUINE_UNKNOWN"
    CI_PENDING = "CI_PENDING"
    CI_GREEN = "CI_GREEN"
    TEST_FIXTURE_MISMATCH = "TEST_FIXTURE_MISMATCH"
    REVIEW_BLOCKING_IN_SCOPE = "REVIEW_BLOCKING_IN_SCOPE"
    REVIEW_UNRELATED_SUGGESTION = "REVIEW_UNRELATED_SUGGESTION"
    REVIEW_STALE_FOR_CANDIDATE = "REVIEW_STALE_FOR_CANDIDATE"
    REVIEW_NOT_INDEPENDENT = "REVIEW_NOT_INDEPENDENT"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    DEPENDENCY_PENDING = "DEPENDENCY_PENDING"
    DEPENDENCY_RED = "DEPENDENCY_RED"
    DEPENDENCY_LANDED = "DEPENDENCY_LANDED"
    DEPENDENCY_CLOSED_WITHOUT_LANDING = "DEPENDENCY_CLOSED_WITHOUT_LANDING"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    EXECUTION_IDENTITY_UNKNOWN = "EXECUTION_IDENTITY_UNKNOWN"
    EXECUTION_IDENTITY_LIVE = "EXECUTION_IDENTITY_LIVE"
    EXECUTION_IDENTITY_DEAD = "EXECUTION_IDENTITY_DEAD"
    DESTRUCTIVE_GIT_OPERATION_REQUESTED = "DESTRUCTIVE_GIT_OPERATION_REQUESTED"
    OUT_OF_SCOPE_CHANGE = "OUT_OF_SCOPE_CHANGE"
    MERGE_POLICY_SATISFIED = "MERGE_POLICY_SATISFIED"
    MERGE_POLICY_UNSATISFIED = "MERGE_POLICY_UNSATISFIED"
    POST_MERGE_VERIFIED = "POST_MERGE_VERIFIED"
    POST_MERGE_VERIFICATION_FAILED = "POST_MERGE_VERIFICATION_FAILED"
    ISOLATION_VIOLATION = "ISOLATION_VIOLATION"
    REMEDIATION_BUDGET_EXHAUSTED = "REMEDIATION_BUDGET_EXHAUSTED"
    CI_FAILURE_UNRESOLVED = "CI_FAILURE_UNRESOLVED"


class Action(StrEnum):
    """What the supervisor did (or decided the caller must do)."""

    PROCEED = "PROCEED"
    WAIT = "WAIT"
    FAIL_CLOSED_QUARANTINE = "FAIL_CLOSED_QUARANTINE"
    REVOKE_EVIDENCE_REQUIRE_REVALIDATION = "REVOKE_EVIDENCE_REQUIRE_REVALIDATION"
    REFRESH_CANDIDATE = "REFRESH_CANDIDATE"
    CREATE_RETARGETED_CANDIDATE = "CREATE_RETARGETED_CANDIDATE"
    RECONSTRUCT_ON_NEW_BASE = "RECONSTRUCT_ON_NEW_BASE"
    REMEDIATE_CANDIDATE = "REMEDIATE_CANDIDATE"
    FIX_TEST_FIXTURE = "FIX_TEST_FIXTURE"
    RERUN_CI = "RERUN_CI"
    REQUEST_BASE_CI = "REQUEST_BASE_CI"
    RECORD_BASELINE_FAILURE_AND_PROCEED = "RECORD_BASELINE_FAILURE_AND_PROCEED"
    RECORD_AND_DEFER = "RECORD_AND_DEFER"
    BLOCK_ON_DEPENDENCY = "BLOCK_ON_DEPENDENCY"
    RESUME_AFTER_DEPENDENCY = "RESUME_AFTER_DEPENDENCY"
    REQUIRE_INDEPENDENT_REVIEW = "REQUIRE_INDEPENDENT_REVIEW"
    REQUIRE_RE_REVIEW = "REQUIRE_RE_REVIEW"
    HOLD_FAIL_CLOSED = "HOLD_FAIL_CLOSED"
    FENCE_AND_REPLACE_EXECUTION = "FENCE_AND_REPLACE_EXECUTION"
    RELEASE_DEAD_EXECUTION = "RELEASE_DEAD_EXECUTION"
    USE_REPLACEMENT_BRANCH = "USE_REPLACEMENT_BRANCH"
    PRESERVE_THEN_PROCEED = "PRESERVE_THEN_PROCEED"
    MERGE = "MERGE"
    MARK_DONE = "MARK_DONE"
    REFUSE_START = "REFUSE_START"
    ESCALATE_TO_FOUNDER = "ESCALATE_TO_FOUNDER"


class EscalationReason(StrEnum):
    """The only reasons StageMesh may stop and ask the founder. Everything else has a deterministic policy."""

    PRODUCT_REQUIREMENT_AMBIGUOUS = "PRODUCT_REQUIREMENT_AMBIGUOUS"
    SECURITY_POLICY_DECISION_REQUIRED = "SECURITY_POLICY_DECISION_REQUIRED"
    DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE = "DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE"
    CONFLICT_REQUIRES_SEMANTIC_PRODUCT_DECISION = "CONFLICT_REQUIRES_SEMANTIC_PRODUCT_DECISION"
    EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED = "EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED"
    BASE_PROVENANCE_UNRECOVERABLE = "BASE_PROVENANCE_UNRECOVERABLE"
    DEPENDENCY_CLOSED_WITHOUT_LANDING = "DEPENDENCY_CLOSED_WITHOUT_LANDING"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    SCOPE_EXTENSION_REQUIRED_BY_ACCEPTANCE_CRITERION = "SCOPE_EXTENSION_REQUIRED_BY_ACCEPTANCE_CRITERION"
    REMEDIATION_BUDGET_EXHAUSTED = "REMEDIATION_BUDGET_EXHAUSTED"
    CI_FAILURE_UNRESOLVED = "CI_FAILURE_UNRESOLVED"


# Questions StageMesh must never put to the founder; a smallest-decision that reduces to one of these is rejected at construction.
_GENERIC_QUESTIONS = (
    re.compile(r"what should i do", re.IGNORECASE),
    re.compile(r"should i (rebase|merge|continue|retry|proceed|force)", re.IGNORECASE),
    re.compile(r"ci failed,? should", re.IGNORECASE),
    re.compile(r"the branch moved", re.IGNORECASE),
    re.compile(r"what (next|now)\b", re.IGNORECASE),
    re.compile(r"how (should|do) (we|i) (proceed|continue)", re.IGNORECASE),
)


class EscalationError(ValueError):
    pass


@dataclass(frozen=True)
class Escalation:
    """A founder-facing escalation. All three explanatory fields are mandatory and the question must be specific."""

    reason: EscalationReason
    attempted: tuple[str, ...]
    why_undeterminable: str
    smallest_decision: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, EscalationReason):
            raise EscalationError("escalation reason must be a typed EscalationReason")
        if not self.attempted or any(not str(item).strip() for item in self.attempted):
            raise EscalationError("an escalation must state what StageMesh already tried")
        if not self.why_undeterminable.strip():
            raise EscalationError("an escalation must state why the safe answer cannot be determined")
        question = self.smallest_decision.strip()
        if not question:
            raise EscalationError("an escalation must state the smallest decision required from the founder")
        for pattern in _GENERIC_QUESTIONS:
            if pattern.search(question):
                raise EscalationError(f"escalation asks a generic operational question: {question!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "attempted": list(self.attempted),
            "why_undeterminable": self.why_undeterminable,
            "smallest_decision": self.smallest_decision,
        }


@dataclass(frozen=True)
class AutonomyDecision:
    """One explainable autonomous decision.

    `observed` is what was seen, `condition` how it was classified, `policy` which rule applied, `action` what was done, and `shas`
    every commit involved (role -> sha). `escalation` is set if and only if the action is ESCALATE_TO_FOUNDER.
    """

    condition: Condition
    policy: str
    action: Action
    task_id: str | None = None
    observed: dict[str, str] = field(default_factory=dict)
    shas: dict[str, str] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    escalation: Escalation | None = None

    def __post_init__(self) -> None:
        if (self.action is Action.ESCALATE_TO_FOUNDER) != (self.escalation is not None):
            raise EscalationError("ESCALATE_TO_FOUNDER requires an Escalation, and an Escalation requires ESCALATE_TO_FOUNDER")

    @property
    def requires_human(self) -> bool:
        return self.escalation is not None

    def trace_line(self) -> str:
        """Single-line, grep-able explanation, e.g. `BASE_HISTORY_REWRITTEN old_base=141d756 new_base=970efeb ... action=...`."""
        parts = [self.condition.value]
        parts += [f"{key}={_short(value)}" for key, value in self.observed.items()]
        parts += [f"{key}={_short(value)}" for key, value in self.shas.items()]
        parts.append(f"policy={self.policy}")
        parts.append(f"action={self.action.value}")
        parts.append(f"human_escalation={'true' if self.requires_human else 'false'}")
        if self.escalation is not None:
            parts.append(f"escalation={self.escalation.reason.value}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "condition": self.condition.value,
            "policy": self.policy,
            "action": self.action.value,
            "observed": dict(self.observed),
            "shas": dict(self.shas),
            "detail": self.detail,
            "requires_human": self.requires_human,
            "escalation": self.escalation.to_dict() if self.escalation else None,
            "trace": self.trace_line(),
        }


def _short(value: object) -> str:
    text = str(value)
    return text[:7] if re.fullmatch(r"[0-9a-f]{40}", text) else text


def record_decision(store: Store, decision: AutonomyDecision) -> str:
    """Persist the decision; the audit log is the durable trace."""
    return record_audit(store, DECISION_EVENT, decision.to_dict())


def decision_trace(store: Store, task_id: str | None = None, limit: int = 500, after_rowid: int = 0) -> list[dict[str, Any]]:
    """Recorded decisions, oldest first. The task filter is applied in SQL, so other tasks' decisions never push this task's out."""
    query = "SELECT rowid AS rid, payload, created_at FROM audit_events WHERE event_type=? AND rowid>?"
    params: list[Any] = [DECISION_EVENT, after_rowid]
    if task_id is not None:
        query += " AND json_extract(payload, '$.task_id')=?"
        params.append(task_id)
    rows = store.conn.execute(query + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*params, limit)).fetchall()
    decisions: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        decisions.append({**payload, "recorded_at": row["created_at"], "rowid": row["rid"]})
    return decisions


def trace_marker(store: Store) -> int:
    """The newest audit rowid right now; `decision_trace(..., after_rowid=marker)` is then exactly what was decided afterwards."""
    row = store.conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM audit_events").fetchone()
    return int(row["m"])


# --- Founder Hands-Off ledger -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskOutcome:
    """The result of one real development task, for the ten-task Founder Hands-Off streak.

    `operational_interventions` counts times the founder had to give instructions about branches, commits, worktrees, rebasing,
    candidate SHAs, CI failures, review cycles, PR dependencies, stale branches, base advancement or agent-failure recovery.
    `escalations` lists typed escalation reasons raised (legitimate product/security/destructive decisions are not interventions).
    """

    task_id: str
    completed: bool
    operational_interventions: int = 0
    escalations: tuple[str, ...] = ()
    notes: str = ""

    def counts_toward_streak(self) -> bool:
        return self.completed and self.operational_interventions == 0


def record_task_outcome(store: Store, outcome: TaskOutcome) -> str:
    return record_audit(
        store,
        OUTCOME_EVENT,
        {
            "task_id": outcome.task_id,
            "completed": outcome.completed,
            "operational_interventions": outcome.operational_interventions,
            "escalations": list(outcome.escalations),
            "notes": outcome.notes,
        },
    )


def autonomy_streak(store: Store) -> dict[str, Any]:
    """Consecutive most-recent completed tasks with zero operational interventions; any intervened task resets it."""
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (OUTCOME_EVENT,)
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        task = str(payload.get("task_id"))
        if task in latest:
            order.remove(task)  # a task counts once, at the position of its latest outcome: re-recording can never add to the streak
        latest[task] = payload
        order.append(task)
    streak = 0
    escalations: list[str] = []
    total = len(order)
    for task in order:
        payload = latest[task]
        escalations.extend(str(item) for item in payload.get("escalations", []))
        clean = int(payload.get("operational_interventions", 0)) == 0
        if payload.get("completed") and clean:
            streak += 1
        elif payload.get("completed") or not clean:
            streak = 0
        elif not payload.get("escalations"):
            streak = 0  # a task that failed without waiting on a legitimate decision breaks "consecutive"
        # else: waiting on a typed escalation is neutral
    return {
        "streak": streak,
        "required": REQUIRED_STREAK,
        "gate_met": streak >= REQUIRED_STREAK,
        "tasks_recorded": total,
        "escalations_observed": escalations,
    }
