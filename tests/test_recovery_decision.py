"""The recovery decision interface: a fixed action set, deterministic rules first, an advisor only for what the rules leave ambiguous."""

from __future__ import annotations

import pytest

from stagemesh.recovery_decision import (
    ACTIONS,
    BASE_ADVANCED,
    BASE_MISSING,
    ENVIRONMENT_BLOCKED,
    INTEGRATION_CONFLICT,
    INTEGRATION_TARGET_ADVANCED,
    INTEGRATION_TARGET_MISSING,
    INTEGRATION_TRANSIENT,
    JUDGMENT_REQUIRED,
    REMEDIATION_EXHAUSTED,
    STEP_BUDGET_EXHAUSTED,
    UNSAFE_MUTATION,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryContext,
    decide,
    permitted_actions,
)


def ctx(category: str, **kwargs) -> RecoveryContext:  # type: ignore[no-untyped-def]
    return RecoveryContext(stage=kwargs.pop("stage", "INTEGRATE"), failure_category=category, candidate_sha="a" * 40, **kwargs)


class Advisor:
    def __init__(self, answer: object = None, raises: bool = False):
        self.answer, self.raises, self.asked = answer, raises, 0

    def advise(self, context: RecoveryContext) -> object:
        self.asked += 1
        if self.raises:
            raise RuntimeError("model unavailable")
        return self.answer


def test_the_action_set_is_exactly_the_documented_nine() -> None:
    assert {a.value for a in ACTIONS} == {
        "RETRY_SAME", "REFRESH_REVALIDATE", "REFRESH_REVALIDATE_REREVIEW", "SWITCH_PROVIDER", "REPAIR_WORKTREE",
        "RETRY_INTEGRATION", "BLOCK_EXTERNAL", "BLOCK_HUMAN_DECISION", "ABORT_UNSAFE",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("context", "action"),
    [
        (ctx(INTEGRATION_TRANSIENT), RecoveryAction.RETRY_INTEGRATION),
        (ctx(INTEGRATION_TARGET_ADVANCED, base_relation=BASE_ADVANCED), RecoveryAction.REFRESH_REVALIDATE_REREVIEW),
        (ctx(INTEGRATION_CONFLICT, can_refresh_mechanically=False), RecoveryAction.REPAIR_WORKTREE),
        (ctx(INTEGRATION_TARGET_MISSING), RecoveryAction.BLOCK_EXTERNAL),
        (ctx(INTEGRATION_TARGET_ADVANCED, base_relation=BASE_MISSING), RecoveryAction.BLOCK_EXTERNAL),
        (ctx(UNSAFE_MUTATION), RecoveryAction.ABORT_UNSAFE),
        (ctx(JUDGMENT_REQUIRED, stage="REVIEW"), RecoveryAction.BLOCK_HUMAN_DECISION),
        (ctx(ENVIRONMENT_BLOCKED, stage="VALIDATE"), RecoveryAction.BLOCK_EXTERNAL),
        (ctx(STEP_BUDGET_EXHAUSTED, stage="REVIEW"), RecoveryAction.RETRY_SAME),
    ],
)
def test_the_rules_choose_the_documented_action(context: RecoveryContext, action: RecoveryAction) -> None:
    decision = decide(context)
    assert decision.action is action and decision.source == "rules" and decision.reason


def test_a_conflict_that_survives_a_redo_on_the_current_base_is_a_decision_for_a_person() -> None:
    redo = RecoveryAttempt("REPAIR_WORKTREE", INTEGRATION_CONFLICT, ("a.py",))
    decision = decide(ctx(INTEGRATION_CONFLICT, changed_files=("a.py",), retry_history=(redo,), recovery_attempts=1, can_refresh_mechanically=False))
    assert decision.action is RecoveryAction.BLOCK_HUMAN_DECISION and "design/spec conflict" in decision.reason


def test_a_failed_mechanical_refresh_is_not_mistaken_for_a_design_conflict() -> None:
    refresh = RecoveryAttempt("REFRESH_REVALIDATE_REREVIEW", INTEGRATION_CONFLICT, ("a.py",))
    decision = decide(ctx(INTEGRATION_CONFLICT, changed_files=("a.py",), retry_history=(refresh,), recovery_attempts=1, can_refresh_mechanically=False))
    assert decision.action is RecoveryAction.REPAIR_WORKTREE  # redo first; only a conflict that comes back after the redo is a design conflict


def test_a_conflict_outside_the_task_contract_is_never_resolved_automatically() -> None:
    decision = decide(ctx(INTEGRATION_CONFLICT, changed_files=("apps/api/other_module.py",), task_scoped=False))
    assert decision.action is RecoveryAction.BLOCK_HUMAN_DECISION and "expand the task's scope" in decision.reason


def test_a_spent_recovery_budget_blocks_as_external_not_as_a_human_decision() -> None:
    decision = decide(ctx(INTEGRATION_TARGET_ADVANCED, recovery_attempts=3, max_attempts=3))
    assert decision.action is RecoveryAction.BLOCK_EXTERNAL and not decision.external_dependency and "spent" in decision.reason


def test_an_exhausted_remediation_budget_climbs_a_ladder_before_it_blocks() -> None:
    healthy = {"codex": "ok", "claude": "ok"}
    first = decide(ctx(REMEDIATION_EXHAUSTED, stage="REVIEW", provider_health=healthy, max_attempts=2))
    assert first.action is RecoveryAction.SWITCH_PROVIDER
    history = (RecoveryAttempt("SWITCH_PROVIDER", REMEDIATION_EXHAUSTED),)
    second = decide(ctx(REMEDIATION_EXHAUSTED, stage="REVIEW", provider_health=healthy, retry_history=history, recovery_attempts=1, max_attempts=2))
    assert second.action is RecoveryAction.REPAIR_WORKTREE
    history += (RecoveryAttempt("REPAIR_WORKTREE", REMEDIATION_EXHAUSTED),)
    last = decide(ctx(REMEDIATION_EXHAUSTED, stage="REVIEW", provider_health=healthy, retry_history=history, recovery_attempts=2, max_attempts=2))
    assert last.action is RecoveryAction.BLOCK_EXTERNAL  # operational, not a founder decision: nothing says the findings need a choice


def test_with_a_single_healthy_provider_the_ladder_skips_the_switch() -> None:
    decision = decide(ctx(REMEDIATION_EXHAUSTED, stage="REVIEW", provider_health={"codex": "ok", "claude": "quota"}, max_attempts=2))
    assert decision.action is RecoveryAction.REPAIR_WORKTREE


# --- the advisor: only for the ambiguous, never outside the envelope -----------------------------------------------------------------


def test_the_rules_are_authoritative_and_the_advisor_is_not_even_asked() -> None:
    advisor = Advisor(RecoveryAction.BLOCK_HUMAN_DECISION)
    decision = decide(ctx(INTEGRATION_TRANSIENT), advisor)
    assert decision.action is RecoveryAction.RETRY_INTEGRATION and advisor.asked == 0


def test_an_ambiguous_context_may_be_answered_by_an_advisor_inside_the_envelope() -> None:
    advisor = Advisor("refresh_revalidate")
    ambiguous = ctx("something_new", stage="VALIDATE")
    decision = decide(ambiguous, advisor)
    assert (decision.action, decision.source, advisor.asked) == (RecoveryAction.REFRESH_REVALIDATE, "advisor", 1)


@pytest.mark.parametrize(
    "answer",
    [None, "DELETE_BRANCH", "force_push_main", 7, RecoveryAction.SWITCH_PROVIDER],  # invented, wrong type, or not permitted (no second provider)
)
def test_an_advisor_cannot_invent_an_action_or_leave_the_envelope(answer: object) -> None:
    decision = decide(ctx("something_new", stage="VALIDATE", provider_health={"codex": "ok"}), Advisor(answer))
    assert decision.action in ACTIONS and decision.source == "fallback"


def test_an_advisor_that_raises_gets_the_conservative_fallback() -> None:
    decision = decide(ctx("something_new", stage="VALIDATE"), Advisor(raises=True))
    assert decision.action is RecoveryAction.RETRY_SAME and decision.source == "fallback"


def test_an_advisor_cannot_skip_re_review_for_a_reviewed_candidate() -> None:
    reviewed = ctx("something_new", stage="INTEGRATE", review_state="PASSED")
    assert RecoveryAction.REFRESH_REVALIDATE not in permitted_actions(reviewed)
    assert decide(reviewed, Advisor(RecoveryAction.REFRESH_REVALIDATE)).source == "fallback"


def test_once_the_budget_is_spent_an_advisor_can_only_choose_to_stop() -> None:
    spent = ctx("something_new", stage="VALIDATE", recovery_attempts=3, max_attempts=3)
    assert permitted_actions(spent) <= {RecoveryAction.BLOCK_EXTERNAL, RecoveryAction.BLOCK_HUMAN_DECISION, RecoveryAction.ABORT_UNSAFE}
    assert decide(spent, Advisor(RecoveryAction.RETRY_SAME)).action is RecoveryAction.BLOCK_EXTERNAL


def test_a_conflict_after_a_redo_is_a_design_conflict_whatever_category_started_the_recovery() -> None:
    redo = RecoveryAttempt("REPAIR_WORKTREE", INTEGRATION_TARGET_ADVANCED, ("a.py",))  # the redo began as a stale-base recovery
    decision = decide(ctx(INTEGRATION_CONFLICT, changed_files=("a.py",), retry_history=(redo,), recovery_attempts=1, can_refresh_mechanically=False))
    assert decision.action is RecoveryAction.BLOCK_HUMAN_DECISION


def test_a_refresh_conflict_outside_the_contract_is_a_human_decision_even_when_the_ref_merely_advanced() -> None:
    decision = decide(ctx(INTEGRATION_TARGET_ADVANCED, changed_files=("apps/other.py",), task_scoped=False, can_refresh_mechanically=False))
    assert decision.action is RecoveryAction.BLOCK_HUMAN_DECISION
