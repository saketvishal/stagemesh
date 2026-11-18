from __future__ import annotations

import copy
import json

import pytest

from stagemesh.spec_planning import (
    MAX_CANONICAL_CHARS,
    MAX_ITEMS,
    PlanningSpecError,
    fit_provider_planning_challenge,
    fit_provider_planning_spec,
    freeze_planning_challenge,
    freeze_planning_spec,
    validate_planning_challenge,
    validate_planning_spec,
)

CONTRACT_HASH = "a" * 64
BASELINE = "b" * 40


def _payload() -> dict:
    return {
        "behavior": ["Validate a planning spec"],
        "non_goals": ["No automatic approval"],
        "affected_paths": ["src/stagemesh/spec_planning.py", "tests/*.py"],
        "acceptance_checks": ["pytest tests/test_spec_planning.py"],
        "decisions": [{"question": "Where does it live?", "resolution": "spec_planning module"}],
        "spec_refs": ["docs/spec.md"],
        "contract_hash": CONTRACT_HASH,
        "baseline_sha": BASELINE,
    }


def _problems(payload) -> tuple[str, ...]:
    spec, problems = validate_planning_spec(payload)
    assert spec is None
    return problems


def test_valid_spec_freezes_stable_identity_bound_to_contract_and_baseline():
    frozen = freeze_planning_spec(_payload())
    again = freeze_planning_spec(copy.deepcopy(_payload()))
    assert frozen.digest == again.digest and frozen.canonical_json == again.canonical_json
    assert frozen.evidence_payload()["contract_hash"] == CONTRACT_HASH
    assert frozen.evidence_payload()["baseline_sha"] == BASELINE
    assert frozen.spec.spec_refs == ("docs/spec.md",)
    for field, value in (("contract_hash", "c" * 64), ("baseline_sha", "d" * 40), ("behavior", ["Other"])):
        changed = _payload()
        changed[field] = value
        assert freeze_planning_spec(changed).digest != frozen.digest


def test_empty_non_goals_and_missing_optional_fields_are_accepted():
    payload = _payload()
    payload["non_goals"] = []
    del payload["decisions"], payload["spec_refs"]
    assert freeze_planning_spec(payload).spec.decisions == ()


def test_missing_and_empty_required_fields_are_all_reported():
    payload = _payload()
    del payload["behavior"], payload["contract_hash"]
    payload["acceptance_checks"] = []
    problems = _problems(payload)
    assert "behavior is required" in problems
    assert "acceptance_checks must not be empty" in problems
    assert "contract_hash must be a lowercase hex digest" in problems


def test_unresolved_decision_is_rejected():
    payload = _payload()
    payload["decisions"].append({"question": "Which format?", "resolution": "  "})
    assert any(p.startswith("unresolved decision: Which format?") for p in _problems(payload))


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "C:/x", "../outside", "src/../../x", ".git/config", ".stagemesh/queue.json", "./.stagemesh/x"]
)
def test_invalid_or_reserved_affected_paths_are_rejected(path):
    payload = _payload()
    payload["affected_paths"] = [path]
    assert _problems(payload)


@pytest.mark.parametrize(
    "path",
    [
        "src/bad\x00.py",
        "src/bad\x01.py",
        "src/bad\x1f.py",
        'src/bad".py',
        "src/bad<.py",
        "src/bad>.py",
        "src/bad|.py",
        ".GIT/config",
        ".STAGEMESH/queue.json",
        "./.GiT/config",
        ".StAgEmEsH\\queue.json",
    ],
)
def test_invalid_affected_paths_cannot_be_frozen(path):
    payload = _payload()
    payload["affected_paths"] = [path]
    assert any("affected_paths" in problem for problem in _problems(payload))
    with pytest.raises(PlanningSpecError):
        freeze_planning_spec(payload)


def test_invalid_spec_refs_are_rejected():
    payload = _payload()
    payload["spec_refs"] = ["../secret.md"]
    assert any("spec_refs" in p for p in _problems(payload))


def test_size_limits_are_enforced():
    payload = _payload()
    payload["behavior"] = [f"item {i}" for i in range(MAX_ITEMS + 1)]
    assert any("behavior has" in p for p in _problems(payload))
    payload = _payload()
    payload["behavior"] = ["x" * 501]
    assert any("limit is 500" in p for p in _problems(payload))
    payload = _payload()
    payload["behavior"] = [f"{i:03d}" + "y" * 400 for i in range(MAX_ITEMS)]
    payload["non_goals"] = [f"{i:03d}" + "z" * 400 for i in range(MAX_ITEMS)]
    assert any(f"limit is {MAX_CANONICAL_CHARS}" in p for p in _problems(payload))


def test_provider_fitting_does_not_rewrite_already_valid_artifacts() -> None:
    spec = _payload()
    challenge = {
        "version": 1,
        "planning_spec_hash": "c" * 64,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": "accept",
        "findings": [{"kind": "missing-test", "summary": "Add a boundary test"}],
        "required_decisions": [],
    }

    assert fit_provider_planning_spec(spec).canonical_json == freeze_planning_spec(spec).canonical_json
    assert (
        fit_provider_planning_challenge(challenge).canonical_json
        == freeze_planning_challenge(challenge).canonical_json
    )


def test_provider_fitting_refuses_to_truncate_protected_decisions() -> None:
    spec = _payload()
    spec["decisions"] = [
        {"question": f"question-{index}-" + "q" * 480, "resolution": "r" * 500}
        for index in range(MAX_ITEMS)
    ]
    challenge = {
        "version": 1,
        "planning_spec_hash": "c" * 64,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": "needs-human-decision",
        "findings": [],
        "required_decisions": [f"decision-{index}-" + "d" * 480 for index in range(MAX_ITEMS)],
    }

    with pytest.raises(PlanningSpecError, match="cannot fit"):
        fit_provider_planning_spec(spec)
    with pytest.raises(PlanningSpecError, match="cannot fit"):
        fit_provider_planning_challenge(challenge)


def test_provider_fitting_preserves_distinct_items_with_shared_prefixes() -> None:
    payload = _payload()
    shared = "shared-prefix-" + "p" * 380
    payload["behavior"] = [shared + f"-behavior-{index:02d}" for index in range(MAX_ITEMS)]
    payload["non_goals"] = [shared + f"-non-goal-{index:02d}" for index in range(MAX_ITEMS)]
    payload["acceptance_checks"] = [
        shared + f"-acceptance-{index:02d}" for index in range(MAX_ITEMS)
    ]

    fitted = fit_provider_planning_spec(payload)
    normalized = json.loads(fitted.canonical_json)
    normalized.pop("version")
    reparsed, problems = validate_planning_spec(normalized)

    assert problems == () and reparsed is not None
    assert len(set(reparsed.behavior)) == len(reparsed.behavior) == MAX_ITEMS
    assert len(set(reparsed.non_goals)) == len(reparsed.non_goals) == MAX_ITEMS
    assert len(set(reparsed.acceptance_checks)) == len(reparsed.acceptance_checks) == MAX_ITEMS
    assert 7_900 <= len(fitted.canonical_json) <= MAX_CANONICAL_CHARS


def test_challenge_fitting_preserves_distinct_shared_prefix_findings() -> None:
    shared = "shared-finding-" + "f" * 470
    challenge = {
        "version": 1,
        "planning_spec_hash": "c" * 64,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": "revise",
        "findings": [
            {"kind": "missing-test", "summary": shared + f"-{index:02d}"}
            for index in range(MAX_ITEMS)
        ],
        "required_decisions": [],
    }

    fitted = fit_provider_planning_challenge(challenge)

    assert len({item.summary for item in fitted.challenge.findings}) == MAX_ITEMS
    assert len(fitted.canonical_json) <= MAX_CANONICAL_CHARS


def _challenge_at_canonical_size(target: int) -> dict:
    payload = {
        "version": 1,
        "planning_spec_hash": "c" * 64,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": "accept",
        "findings": [
            {"kind": "missing-test", "summary": f"{index:02d}"}
            for index in range(MAX_ITEMS)
        ],
        "required_decisions": [],
    }
    current = len(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    remaining = target - current
    for finding in payload["findings"]:
        growth = min(remaining, 500 - len(finding["summary"]))
        finding["summary"] += "x" * growth
        remaining -= growth
    assert remaining == 0
    assert len(json.dumps(payload, sort_keys=True, separators=(",", ":"))) == target
    return payload


@pytest.mark.parametrize(("size", "valid"), [(7_999, True), (8_000, True), (8_001, False)])
def test_challenge_canonical_boundary_is_exact(size: int, valid: bool) -> None:
    challenge, problems = validate_planning_challenge(_challenge_at_canonical_size(size))

    assert (challenge is not None) is valid
    assert (not problems) is valid
    if not valid:
        assert problems == (
            f"planning challenge is {size} characters canonicalized; the limit is {MAX_CANONICAL_CHARS}",
        )


def test_unknown_fields_duplicates_and_non_objects_are_rejected():
    payload = _payload()
    payload["approved"] = True
    payload["behavior"] = ["same", "same"]
    problems = _problems(payload)
    assert "unknown field: approved" in problems
    assert "behavior contains duplicate entries" in problems
    assert _problems([]) == ("planning spec must be a JSON object",)


def test_freeze_raises_with_every_problem():
    with pytest.raises(PlanningSpecError) as caught:
        freeze_planning_spec({})
    assert len(caught.value.problems) > 3
