"""The bounded decision interface for recovery: a fixed set of actions, deterministic rules first, an optional advisor behind them.

Every recovery choice (what to do after an integration failure, an exhausted remediation budget, a step cap) is expressed as a
`RecoveryContext` and answered with exactly one `RecoveryAction`. The rules in `rules()` are authoritative: whenever they reach an
answer nobody else is asked. Only a context the rules call ambiguous (they return `None`) may be put to a `DecisionAdvisor`, for
example a decision model evaluated behind this interface. The advisor cannot invent an action: its answer is coerced into the fixed
set, checked against `permitted_actions()` (the safety envelope for the context), and replaced by the conservative `fallback()` when
it is missing, invalid, not permitted or raises. No model or provider is a dependency of this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class RecoveryAction(StrEnum):
    RETRY_SAME = "RETRY_SAME"  # run the same stage again on the same candidate
    REFRESH_REVALIDATE = "REFRESH_REVALIDATE"  # new SHA on the current base; validation reruns (no review exists for the old SHA yet)
    REFRESH_REVALIDATE_REREVIEW = "REFRESH_REVALIDATE_REREVIEW"  # new SHA on the current base; validation AND independent review rerun
    SWITCH_PROVIDER = "SWITCH_PROVIDER"  # the next attempt goes to a different healthy provider
    REPAIR_WORKTREE = "REPAIR_WORKTREE"  # the next attempt starts on a clean worktree, history preserved
    RETRY_INTEGRATION = "RETRY_INTEGRATION"  # integrate the same reviewed candidate again
    BLOCK_EXTERNAL = "BLOCK_EXTERNAL"  # waiting on something outside this run: environment, runtime, capacity, a missing dependency
    BLOCK_HUMAN_DECISION = "BLOCK_HUMAN_DECISION"  # a product, spec, security or legal choice that evidence cannot settle
    ABORT_UNSAFE = "ABORT_UNSAFE"  # every automatic action would break a safety rule (history rewrite, foreign mutation, ...)


ACTIONS = frozenset(RecoveryAction)
BLOCKING_ACTIONS = frozenset({RecoveryAction.BLOCK_EXTERNAL, RecoveryAction.BLOCK_HUMAN_DECISION, RecoveryAction.ABORT_UNSAFE})

# failure categories the rules understand (anything else is ambiguous)
INTEGRATION_TRANSIENT = "integration_transient"
INTEGRATION_TARGET_ADVANCED = "integration_target_advanced"
INTEGRATION_CONFLICT = "integration_conflict"
INTEGRATION_TARGET_MISSING = "integration_target_missing"
REMEDIATION_EXHAUSTED = "remediation_exhausted"
STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
UNSAFE_MUTATION = "unsafe_mutation"
JUDGMENT_REQUIRED = "judgment_required"  # the remaining issue is a product/spec/security/legal choice
ENVIRONMENT_BLOCKED = "environment_blocked"  # a baseline that cannot advance or a gate that cannot run: no provider attempt can fix it

# base relation of the candidate to the integration ref
BASE_UNCHANGED, BASE_ADVANCED, BASE_DIVERGED, BASE_MISSING, BASE_UNKNOWN = "unchanged", "advanced", "diverged", "missing", "unknown"


@dataclass(frozen=True)
class RecoveryAttempt:
    action: str
    category: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryContext:
    stage: str
    failure_category: str
    candidate_sha: str | None = None
    base_relation: str = BASE_UNKNOWN
    validation_state: str | None = None  # PASSED | FAILED | None for the candidate
    review_state: str | None = None
    retry_history: tuple[RecoveryAttempt, ...] = ()
    changed_files: tuple[str, ...] = ()  # files the failure is about (conflicting files, files a finding names)
    task_scoped: bool = True  # every file in `changed_files` is inside the task contract's allowed files
    provider_health: Mapping[str, str] = field(default_factory=dict)  # provider -> "ok" or the reason it is unusable
    recovery_attempts: int = 0
    max_attempts: int = 3
    can_refresh_mechanically: bool = True  # a refresh of the candidate onto the current base has not already failed on these files

    @property
    def healthy_providers(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, state in self.provider_health.items() if state == "ok"))

    def tried(self, action: RecoveryAction) -> bool:
        return any(a.action == action for a in self.retry_history)

    def conflict_repeated(self) -> bool:
        """The same files conflicted again after an earlier recovery already redid the work against the current base."""
        current = set(self.changed_files)
        return bool(current) and any(
            a.action == RecoveryAction.REPAIR_WORKTREE and a.category == self.failure_category and current & set(a.files) for a in self.retry_history
        )

    def summary(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "failure_category": self.failure_category,
            "candidate_sha": self.candidate_sha,
            "base_relation": self.base_relation,
            "validation_state": self.validation_state,
            "review_state": self.review_state,
            "retry_history": [a.action for a in self.retry_history],
            "changed_files": list(self.changed_files[:10]),
            "task_scoped": self.task_scoped,
            "healthy_providers": list(self.healthy_providers),
            "recovery_attempts": self.recovery_attempts,
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    source: str  # rules | advisor | fallback
    reason: str
    external_dependency: bool = False  # for BLOCK_EXTERNAL: the cause is a dependency, not a runtime/capacity limit that can clear

    @property
    def blocks(self) -> bool:
        return self.action in BLOCKING_ACTIONS


class DecisionAdvisor(Protocol):
    """Anything that can propose an action for an ambiguous context (a decision model, a human-written policy, a stub)."""

    def advise(self, context: RecoveryContext) -> object: ...


def permitted_actions(ctx: RecoveryContext) -> frozenset[RecoveryAction]:
    """The safety envelope: what an advisor may choose for this context, whatever it prefers."""
    allowed = set(ACTIONS)
    if ctx.stage != "INTEGRATE":
        allowed -= {RecoveryAction.RETRY_INTEGRATION}
    if ctx.review_state == "PASSED" or ctx.stage in {"REVIEW", "INTEGRATE"}:
        allowed.discard(RecoveryAction.REFRESH_REVALIDATE)  # a refreshed SHA has no review: skipping re-review would reuse stale evidence
    if ctx.base_relation == BASE_MISSING:
        allowed -= {RecoveryAction.REFRESH_REVALIDATE, RecoveryAction.REFRESH_REVALIDATE_REREVIEW, RecoveryAction.RETRY_INTEGRATION}
    if len(ctx.healthy_providers) < 2:
        allowed.discard(RecoveryAction.SWITCH_PROVIDER)
    if ctx.recovery_attempts >= ctx.max_attempts:
        allowed &= BLOCKING_ACTIONS
    return frozenset(allowed)


def rules(ctx: RecoveryContext) -> RecoveryDecision | None:
    """The authoritative deterministic policy. None means this context is ambiguous and may be put to an advisor."""
    category = ctx.failure_category
    if category == UNSAFE_MUTATION:
        return RecoveryDecision(RecoveryAction.ABORT_UNSAFE, "rules", "the workspace or ref changed outside StageMesh; continuing would build on unverified state")
    if category == JUDGMENT_REQUIRED:
        return RecoveryDecision(RecoveryAction.BLOCK_HUMAN_DECISION, "rules", "the remaining issue is a product, spec, security or legal choice that repository state cannot settle")
    if category == ENVIRONMENT_BLOCKED:
        return RecoveryDecision(RecoveryAction.BLOCK_EXTERNAL, "rules", "the environment, not the change, is what fails; it clears when the baseline or gate environment does")
    if category == INTEGRATION_TARGET_MISSING or ctx.base_relation == BASE_MISSING:
        return RecoveryDecision(RecoveryAction.BLOCK_EXTERNAL, "rules", "the integration target does not exist, so there is nothing to refresh or retry against", external_dependency=True)
    if category == INTEGRATION_CONFLICT and not ctx.task_scoped:
        return RecoveryDecision(
            RecoveryAction.BLOCK_HUMAN_DECISION,
            "rules",
            "the conflict reaches files outside the task contract; resolving it automatically would expand the task's scope",
        )
    if category == INTEGRATION_CONFLICT and ctx.conflict_repeated():
        return RecoveryDecision(
            RecoveryAction.BLOCK_HUMAN_DECISION,
            "rules",
            "the task was already redone against the current integration ref and still conflicts in the same file(s); that is a design/spec conflict "
            "between this task and work that landed, not a stale branch",
        )
    if ctx.recovery_attempts >= ctx.max_attempts:
        return RecoveryDecision(
            RecoveryAction.BLOCK_EXTERNAL,
            "rules",
            f"{ctx.recovery_attempts} automatic recovery attempt(s) are spent (limit {ctx.max_attempts}) and the failure came back; "
            "the task is eligible again once the runtime or environment changes",
        )
    if category == INTEGRATION_TRANSIENT:
        return RecoveryDecision(RecoveryAction.RETRY_INTEGRATION, "rules", "a transient git/runtime fault with no content conflict; the reviewed candidate is unchanged")
    if category in {INTEGRATION_TARGET_ADVANCED, INTEGRATION_CONFLICT}:
        if ctx.can_refresh_mechanically:
            return RecoveryDecision(
                RecoveryAction.REFRESH_REVALIDATE_REREVIEW,
                "rules",
                "the integration ref moved after review; replay only the task's own commits onto it, then validate and independently review the new SHA",
            )
        if not ctx.tried(RecoveryAction.REPAIR_WORKTREE):
            return RecoveryDecision(RecoveryAction.REPAIR_WORKTREE, "rules", "the candidate does not apply mechanically; redo the task on a clean worktree at the current base")
        return RecoveryDecision(RecoveryAction.BLOCK_EXTERNAL, "rules", "mechanical refresh and a redo on the current base were both tried")
    if category in {REMEDIATION_EXHAUSTED, STEP_BUDGET_EXHAUSTED}:
        if category == STEP_BUDGET_EXHAUSTED:
            return RecoveryDecision(RecoveryAction.RETRY_SAME, "rules", "the step cap ended a window with progress made; resume from the recorded state")
        if len(ctx.healthy_providers) >= 2 and not ctx.tried(RecoveryAction.SWITCH_PROVIDER):
            return RecoveryDecision(RecoveryAction.SWITCH_PROVIDER, "rules", "the same provider has not resolved the findings; try another healthy provider with a fresh context")
        if not ctx.tried(RecoveryAction.REPAIR_WORKTREE):
            return RecoveryDecision(RecoveryAction.REPAIR_WORKTREE, "rules", "retry the open findings only, from a clean worktree at the latest candidate")
        return RecoveryDecision(
            RecoveryAction.BLOCK_EXTERNAL,
            "rules",
            f"recovery already tried {', '.join(a.action for a in ctx.retry_history) or 'every available rung'} on the open findings and no healthy alternative remains; "
            "nothing in the evidence marks them as a product, spec, security or legal choice",
        )
    return None


def fallback(ctx: RecoveryContext) -> RecoveryDecision:
    """What an ambiguous context gets when no advisor answers validly: one more identical attempt if budget remains, else stop."""
    if ctx.recovery_attempts < ctx.max_attempts and RecoveryAction.RETRY_SAME in permitted_actions(ctx):
        return RecoveryDecision(RecoveryAction.RETRY_SAME, "fallback", "ambiguous failure; one more identical attempt within the recovery budget")
    return RecoveryDecision(RecoveryAction.BLOCK_EXTERNAL, "fallback", "ambiguous failure and the recovery budget is spent")


def coerce_action(value: object) -> RecoveryAction | None:
    if isinstance(value, RecoveryAction):
        return value
    if isinstance(value, str):
        try:
            return RecoveryAction(value.strip().upper())
        except ValueError:
            return None
    return None


def decide(ctx: RecoveryContext, advisor: DecisionAdvisor | None = None) -> RecoveryDecision:
    """Answer one context with one action from the fixed set. Rules first; an advisor only for what the rules leave ambiguous."""
    ruled = rules(ctx)
    if ruled is not None:
        return ruled
    if advisor is None:
        return fallback(ctx)
    try:
        proposed = coerce_action(advisor.advise(ctx))
    except Exception:  # noqa: BLE001 - an advisor is advice; whatever it does, the lifecycle gets a safe answer
        return fallback(ctx)
    if proposed is None or proposed not in permitted_actions(ctx):
        return fallback(ctx)
    return RecoveryDecision(proposed, "advisor", "proposed by the configured advisor and inside the safety envelope")
