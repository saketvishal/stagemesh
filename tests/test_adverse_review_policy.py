"""Adverse-review policy: typed policy, distinct role evidence bound to candidate and policy digest, fail-closed outcomes."""
from __future__ import annotations

import pytest

from stagemesh.config import AdverseReviewPolicy, ConfigValidationError, _adverse_review_policy
from stagemesh.review import (
    ADVERSE_OUTCOME_BLOCKED,
    ADVERSE_OUTCOME_PASS,
    ADVERSE_OUTCOME_UNAVAILABLE,
    ADVERSE_REVIEW_DISCIPLINE,
    adverse_outcome,
    adverse_review_instructions,
    command_alias_errors,
    validate_role_evidence,
)

SHA = "a" * 40
POLICY = AdverseReviewPolicy()
IMPLEMENTER = {"provider": "codex", "command": "codex exec"}


def _evidence(normal=None, adverse=None):
    base = {"candidate_sha": SHA, "policy_digest": POLICY.digest(), "verdict": "PASS"}
    return {
        "normal": {**base, "provider": "claude", "command": "claude -p", **(normal or {})},
        "adverse": {**base, "provider": "grok", "command": "grok review", **(adverse or {})},
    }


def _check(evidence):
    return validate_role_evidence(evidence, candidate_sha=SHA, policy=POLICY, implementer=IMPLEMENTER)


def test_distinct_bound_evidence_is_accepted():
    assert _check(_evidence()) == []


@pytest.mark.parametrize("role", ["normal", "adverse"])
def test_evidence_for_another_candidate_or_policy_is_rejected(role):
    assert any("candidate" in e for e in _check(_evidence(**{role: {"candidate_sha": "b" * 40}})))
    assert any("policy digest" in e for e in _check(_evidence(**{role: {"policy_digest": "0" * 64}})))


def test_changed_policy_changes_digest():
    assert AdverseReviewPolicy(blocking_severities=("blocker",)).digest() != POLICY.digest()


def test_missing_role_is_rejected():
    assert _check({"adverse": _evidence()["adverse"]})


def test_reviewer_sharing_provider_with_implementer_or_each_other_is_rejected():
    assert _check(_evidence(adverse={"provider": " CODEX "}))
    assert _check(_evidence(adverse={"provider": "claude", "command": "other"}))


def test_aliases_sharing_a_command_are_rejected():
    assert _check(_evidence(adverse={"command": "CLAUDE  -p"}))
    assert _check(_evidence(adverse={"command": "codex exec"}))
    assert command_alias_errors({"a": "x --y", "b": "x --y"})
    assert command_alias_errors({"a": "x --y", "b": ""})
    assert command_alias_errors({"a": "x --y", "b": "x --z"}) == []


def test_blocking_severity_and_unavailable_reviewer_behavior():
    block = {"verdict": "FAIL_WITH_FINDINGS", "findings": [{"severity": "Error", "message": "m"}]}
    advisory = {"verdict": "FAIL_WITH_FINDINGS", "findings": [{"severity": "nit", "message": "m"}]}
    assert adverse_outcome({"verdict": "PASS"}, POLICY) == ADVERSE_OUTCOME_PASS
    assert adverse_outcome(block, POLICY) == ADVERSE_OUTCOME_BLOCKED
    assert adverse_outcome(advisory, POLICY) == ADVERSE_OUTCOME_PASS
    assert adverse_outcome({"verdict": "FAIL_WITH_FINDINGS", "findings": []}, POLICY) == ADVERSE_OUTCOME_UNAVAILABLE
    assert adverse_outcome({"verdict": "REVIEW_INCOMPLETE"}, POLICY) == ADVERSE_OUTCOME_UNAVAILABLE
    assert adverse_outcome({}, POLICY) == ADVERSE_OUTCOME_UNAVAILABLE


def test_policy_config_validation():
    assert _adverse_review_policy({}) == POLICY
    assert _adverse_review_policy({"blocking_severities": ["High", "high"]}).blocking_severities == ("high",)
    for bad in ({"blocking_severities": []}, {"blocking_severities": [1]}, {"unavailable_reviewer": "pass"}, {"extra": 1}):
        with pytest.raises(ConfigValidationError):
            _adverse_review_policy(bad)


def test_instructions_extend_existing_discipline():
    text = adverse_review_instructions(POLICY)
    assert text.startswith(ADVERSE_REVIEW_DISCIPLINE)
    assert "Blocking severities: blocker, critical, error, high" in text
