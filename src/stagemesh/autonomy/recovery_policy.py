"""Explicit recovery policy: unknown process identity, and destructive git operations.

UNKNOWN execution identity is not evidence of death. It is never released, never marked failed as orphaned and never treated as a
free claim. The explicit policy is either to hold (fail closed) or to fence: leave the old execution and its worktree untouched,
stop trusting anything it produces, and continue the task on a replacement worktree. Only a provably DEAD identity is released.

A workflow that would force-push, hard-reset or otherwise destroy history is answered with a provenance-preserving replacement
(a new branch, or a preserved snapshot ref) whenever one exists. Only when there is none (for example rewriting the integration ref
itself) does it escalate with `DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .decisions import Action, AutonomyDecision, Condition, Escalation, EscalationReason

POLICY = "recovery/v1"


class UnknownIdentityStrategy(StrEnum):
    HOLD = "HOLD"  # keep failing closed; progress only through an explicit operator release
    FENCE = "FENCE"  # preserve the old execution/worktree, ignore its output, continue on a replacement worktree


@dataclass(frozen=True)
class RecoveryPolicy:
    unknown_identity: UnknownIdentityStrategy = UnknownIdentityStrategy.FENCE
    hold_seconds: float = 0.0  # how long an UNKNOWN execution is simply waited on before the strategy applies


def decide_execution_recovery(
    task_id: str | None,
    execution_id: str,
    state: str,
    *,
    age_seconds: float = 0.0,
    policy: RecoveryPolicy | None = None,
) -> AutonomyDecision:
    """`state` is `process_identity.classify_process` output: LIVE, DEAD or UNKNOWN. Pure and deterministic."""
    policy = policy or RecoveryPolicy()
    observed = {"execution": execution_id, "identity": state, "age_seconds": str(int(age_seconds))}
    if state == "LIVE":
        return AutonomyDecision(Condition.EXECUTION_IDENTITY_LIVE, POLICY, Action.WAIT, task_id, observed)
    if state == "DEAD":
        return AutonomyDecision(
            Condition.EXECUTION_IDENTITY_DEAD,
            POLICY,
            Action.RELEASE_DEAD_EXECUTION,
            task_id,
            observed,
            detail={"proof": "saved process identity does not match any live process"},
        )
    # UNKNOWN (or anything unrecognized): fail closed. Never release, never mark failed.
    detail: dict[str, object] = {"never_treated_as_dead": True, "strategy": policy.unknown_identity.value}
    if policy.unknown_identity is UnknownIdentityStrategy.HOLD or age_seconds < policy.hold_seconds:
        return AutonomyDecision(Condition.EXECUTION_IDENTITY_UNKNOWN, POLICY, Action.HOLD_FAIL_CLOSED, task_id, observed, detail=detail)
    detail.update(old_execution_preserved=True, old_worktree_preserved=True, old_output_untrusted=True)
    return AutonomyDecision(Condition.EXECUTION_IDENTITY_UNKNOWN, POLICY, Action.FENCE_AND_REPLACE_EXECUTION, task_id, observed, detail=detail)


# --- destructive git operations -------------------------------------------------------------------------------------------------------


class GitOperation(StrEnum):
    FORCE_PUSH = "FORCE_PUSH"
    HISTORY_REWRITE = "HISTORY_REWRITE"  # filter-branch, rebase of published commits, amend of a pushed commit
    RESET_HARD = "RESET_HARD"
    CLEAN = "CLEAN"
    BRANCH_DELETE = "BRANCH_DELETE"


_REWRITES_REF = {GitOperation.FORCE_PUSH, GitOperation.HISTORY_REWRITE}
_DISCARDS_WORK = {GitOperation.RESET_HARD, GitOperation.CLEAN}


@dataclass(frozen=True)
class GitOperationRequest:
    operation: GitOperation
    ref: str  # e.g. refs/heads/feat/x
    reason: str
    target_sha: str | None = None  # the commit the ref points at / would be reset away from
    has_unpreserved_work: bool = False  # uncommitted changes, or commits reachable from no other ref
    published: bool = True  # the ref exists on a remote others may build on


def decide_git_operation(
    request: GitOperationRequest,
    *,
    task_id: str | None,
    protected_refs: tuple[str, ...] = ("refs/heads/main", "refs/heads/master"),
) -> AutonomyDecision:
    """Choose the safe replacement for a destructive request, or escalate when none exists."""
    protected = request.ref in protected_refs
    short = (request.target_sha or "unknown")[:7]
    observed = {"operation": request.operation.value, "ref": request.ref, "protected": str(protected).lower(), "reason": request.reason[:120]}
    shas = {"target": request.target_sha} if request.target_sha else {}
    condition = Condition.DESTRUCTIVE_GIT_OPERATION_REQUESTED

    if protected and request.operation in _REWRITES_REF | {GitOperation.BRANCH_DELETE, GitOperation.RESET_HARD}:
        return AutonomyDecision(
            condition,
            POLICY,
            Action.ESCALATE_TO_FOUNDER,
            task_id,
            observed,
            shas,
            {},
            Escalation(
                EscalationReason.DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE,
                attempted=(
                    "considered publishing the work on a replacement branch",
                    "considered preserving the current tip under a StageMesh provenance ref",
                ),
                why_undeterminable=f"{request.ref} is the protected integration ref: a replacement branch cannot substitute for it, and others build on its history",
                smallest_decision=f"Approve or reject {request.operation.value} on {request.ref} (current tip {short}) for this stated reason: {request.reason}",
            ),
        )
    if request.operation in _REWRITES_REF:
        branch = request.ref.removeprefix("refs/heads/")
        return AutonomyDecision(
            condition,
            POLICY,
            Action.USE_REPLACEMENT_BRANCH,
            task_id,
            observed,
            shas,
            {
                "replacement_branch": f"{branch}-sm-{short}",
                "original_preserved_under": f"refs/stagemesh/preserved/<task>/{short}",
                "provenance": "replacement records the original branch and tip; the original is never force-updated",
                "destructive_operation_executed": False,
            },
        )
    if request.operation in _DISCARDS_WORK or (request.operation is GitOperation.BRANCH_DELETE and request.has_unpreserved_work):
        if request.has_unpreserved_work:
            return AutonomyDecision(
                condition,
                POLICY,
                Action.PRESERVE_THEN_PROCEED,
                task_id,
                observed,
                shas,
                {"preserve": "snapshot all work under refs/stagemesh/preserved before the operation", "destructive_operation_executed": False},
            )
        return AutonomyDecision(condition, POLICY, Action.PROCEED, task_id, observed, shas, {"note": "nothing unpreserved would be lost"})
    return AutonomyDecision(condition, POLICY, Action.PROCEED, task_id, observed, shas, {"note": "operation loses no history"})
