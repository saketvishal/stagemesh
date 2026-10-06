"""The decision trace, the human-escalation contract and the ten-task streak ledger."""

from __future__ import annotations

from pathlib import Path

import pytest
from autonomy_support import new_store

from stagemesh.autonomy.decisions import (
    REQUIRED_STREAK,
    Action,
    AutonomyDecision,
    Condition,
    Escalation,
    EscalationError,
    EscalationReason,
    TaskOutcome,
    autonomy_streak,
    decision_trace,
    record_decision,
    record_task_outcome,
)


def _escalation(**overrides) -> Escalation:
    values = dict(
        reason=EscalationReason.PRODUCT_REQUIREMENT_AMBIGUOUS,
        attempted=("read the acceptance criteria", "searched the issue thread for the intended behavior"),
        why_undeterminable="the criteria allow both opt-in and opt-out behavior",
        smallest_decision="Should the new flag default to on or off?",
    )
    values.update(overrides)
    return Escalation(**values)


def test_every_escalation_states_what_was_tried_why_and_the_smallest_decision() -> None:
    escalation = _escalation()
    assert escalation.to_dict()["attempted"] and escalation.to_dict()["why_undeterminable"] and escalation.to_dict()["smallest_decision"]
    for broken in (dict(attempted=()), dict(attempted=("",)), dict(why_undeterminable="  "), dict(smallest_decision="")):
        with pytest.raises(EscalationError):
            _escalation(**broken)


@pytest.mark.parametrize(
    "question",
    [
        "What should I do?",
        "Should I rebase?",
        "CI failed, should I continue?",
        "The branch moved; what next?",
        "Should I merge this now?",
        "How should we proceed?",
    ],
)
def test_generic_operational_questions_are_rejected(question: str) -> None:
    with pytest.raises(EscalationError):
        _escalation(smallest_decision=question)


def test_escalation_reason_must_be_typed() -> None:
    with pytest.raises(EscalationError):
        _escalation(reason="because")


def test_the_five_contract_escalation_reasons_exist() -> None:
    names = {reason.name for reason in EscalationReason}
    assert {
        "PRODUCT_REQUIREMENT_AMBIGUOUS",
        "SECURITY_POLICY_DECISION_REQUIRED",
        "DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE",
        "CONFLICT_REQUIRES_SEMANTIC_PRODUCT_DECISION",
        "EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED",
    } <= names


def test_a_decision_escalates_if_and_only_if_it_carries_an_escalation() -> None:
    with pytest.raises(EscalationError):
        AutonomyDecision(Condition.CI_GREEN, "p", Action.ESCALATE_TO_FOUNDER)
    with pytest.raises(EscalationError):
        AutonomyDecision(Condition.CI_GREEN, "p", Action.PROCEED, escalation=_escalation())
    escalated = AutonomyDecision(Condition.CI_GREEN, "p", Action.ESCALATE_TO_FOUNDER, escalation=_escalation())
    assert escalated.requires_human and "escalation=PRODUCT_REQUIREMENT_AMBIGUOUS" in escalated.trace_line()


def test_trace_line_matches_the_documented_format() -> None:
    decision = AutonomyDecision(
        Condition.BASE_HISTORY_REWRITTEN,
        "base-state/v1",
        Action.CREATE_RETARGETED_CANDIDATE,
        "T-1",
        {"old_base": "141d756", "new_base": "970efeb", "tree_equivalent": "true"},
        {"original_candidate": "4adfd88" + "0" * 33, "replacement_candidate": "a" * 40},
    )
    assert decision.trace_line() == (
        "BASE_HISTORY_REWRITTEN old_base=141d756 new_base=970efeb tree_equivalent=true "
        "original_candidate=4adfd88 replacement_candidate=aaaaaaa policy=base-state/v1 "
        "action=CREATE_RETARGETED_CANDIDATE human_escalation=false"
    )


def test_decisions_are_durable_and_filterable_by_task(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    for task in ("T-1", "T-2", "T-1"):
        record_decision(store, AutonomyDecision(Condition.CI_GREEN, "p", Action.PROCEED, task, {"n": task}))
    store.close()
    again = new_store(tmp_path)  # a new connection: the trace survives restarts
    assert [d["task_id"] for d in decision_trace(again)] == ["T-1", "T-2", "T-1"]
    assert len(decision_trace(again, "T-1")) == 2 and decision_trace(again, "T-1")[0]["trace"].startswith("CI_GREEN ")


# --- ten-task streak ---------------------------------------------------------------------------------------------------------------------------


def test_streak_counts_consecutive_hands_off_completions(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    assert autonomy_streak(store) == {"streak": 0, "required": REQUIRED_STREAK, "gate_met": False, "tasks_recorded": 0, "escalations_observed": []}
    for n in range(REQUIRED_STREAK):
        record_task_outcome(store, TaskOutcome(f"T-{n}", True))
    assert autonomy_streak(store)["gate_met"] is True


def test_any_operational_intervention_resets_the_streak(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    for n in range(4):
        record_task_outcome(store, TaskOutcome(f"T-{n}", True))
    record_task_outcome(store, TaskOutcome("T-4", True, operational_interventions=1, notes="founder said which branch to rebase"))
    assert autonomy_streak(store)["streak"] == 0
    record_task_outcome(store, TaskOutcome("T-5", True))
    assert autonomy_streak(store)["streak"] == 1


def test_a_task_waiting_on_a_legitimate_escalation_is_neutral_not_a_reset(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    record_task_outcome(store, TaskOutcome("T-0", True))
    record_task_outcome(store, TaskOutcome("T-1", False, escalations=("PRODUCT_REQUIREMENT_AMBIGUOUS",)))  # asked a real product question
    result = autonomy_streak(store)
    assert result["streak"] == 1 and result["escalations_observed"] == ["PRODUCT_REQUIREMENT_AMBIGUOUS"]
