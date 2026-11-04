from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_run_ready import FAKE_CONTRACT, _project, _run

from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contract_binding import bind_task_contract
from stagemesh.contracts import canonical_contract_json, parse_contract
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.providers import (
    _approved_planning_prompt,
    _approved_scope_prompt,
    _build_task_prompt,
)
from stagemesh.recovery import format_doctor, task_doctor
from stagemesh.spec_planning import (
    NEXT_APPROVE_SPEC,
    NEXT_CHALLENGE_SPEC,
    NEXT_REFRESH_CHALLENGE,
    NEXT_REFRESH_SPEC,
    NEXT_RESOLVE_CHALLENGE_DECISIONS,
    NEXT_RESOLVE_DECISIONS,
    NEXT_REVISE_SPEC,
    NEXT_WRITE_SPEC,
    evaluate_planning_gate,
    freeze_planning_challenge,
    freeze_planning_spec,
    plan_challenger_scope_violations,
    planner_scope_violations,
    planning_artifact_paths,
    planning_challenge_path,
    validate_planning_challenge,
)

CONTRACT_HASH = "a" * 64
BASELINE = "b" * 40


def _spec(**overrides) -> dict:
    payload = {
        "behavior": ["Gate implementation"],
        "affected_paths": ["src/app.py"],
        "acceptance_checks": ["pytest"],
        "contract_hash": CONTRACT_HASH,
        "baseline_sha": BASELINE,
    }
    payload.update(overrides)
    return payload


def _challenge(payload: dict, **overrides) -> dict:
    challenge = {
        "version": 1,
        "planning_spec_hash": freeze_planning_spec(payload).digest,
        "planner_provider": "codex",
        "challenger_provider": "claude",
        "verdict": "accept",
        "findings": [],
        "required_decisions": [],
    }
    challenge.update(overrides)
    return challenge


def _approval(payload: dict, challenge: dict | None = None) -> dict:
    approval = {"approved": True, "planning_spec_hash": freeze_planning_spec(payload).digest}
    if challenge is not None:
        approval["planning_challenge_hash"] = freeze_planning_challenge(challenge).digest
    return approval


def _gate(spec, approval, **kw):
    args = {"enabled": True, "spec_payload": spec, "approval": approval, "contract_hash": CONTRACT_HASH, "baseline_sha": BASELINE}
    args.update(kw)
    return evaluate_planning_gate(**args)


def _contract_digest() -> str:
    return hashlib.sha256(canonical_contract_json(parse_contract(FAKE_CONTRACT)).encode("utf-8")).hexdigest()


def test_legacy_policy_allows_without_any_evidence():
    assert _gate(None, None, enabled=False).allowed


def test_approved_current_spec_allows_and_exposes_scope():
    spec = _spec()
    decision = _gate(spec, _approval(spec))
    assert decision.allowed and decision.approved_scope == ("src/app.py",)


@pytest.mark.parametrize(
    ("spec", "approval", "action"),
    [
        (None, None, NEXT_WRITE_SPEC),
        (_spec(behavior=[]), None, NEXT_WRITE_SPEC),
        (_spec(decisions=[{"question": "Which db?", "resolution": ""}]), None, NEXT_RESOLVE_DECISIONS),
        (_spec(), None, NEXT_APPROVE_SPEC),
        (_spec(), {"approved": False, "planning_spec_hash": freeze_planning_spec(_spec()).digest}, NEXT_APPROVE_SPEC),
        (_spec(), {"approved": True, "planning_spec_hash": "0" * 64}, NEXT_APPROVE_SPEC),
    ],
)
def test_rejected_cases_carry_typed_next_action(spec, approval, action):
    decision = _gate(spec, approval)
    assert not decision.allowed and decision.next_action == action and decision.approved_scope == ()


def test_changed_contract_or_baseline_invalidates_prior_approval():
    spec = _spec()
    approval = _approval(spec)
    assert _gate(spec, approval).allowed
    assert _gate(spec, approval, contract_hash="c" * 64).next_action == NEXT_REFRESH_SPEC
    assert _gate(spec, approval, baseline_sha="d" * 40).next_action == NEXT_REFRESH_SPEC


def test_spec_edit_after_approval_requires_new_approval():
    spec = _spec()
    approval = _approval(spec)
    edited = copy.deepcopy(spec)
    edited["affected_paths"] = ["src/other.py"]
    assert _gate(edited, approval).next_action == NEXT_APPROVE_SPEC


def test_challenge_identity_is_deterministic_and_bounded():
    challenge = _challenge(_spec())
    assert freeze_planning_challenge(challenge).digest == freeze_planning_challenge(
        dict(reversed(list(challenge.items())))
    ).digest
    invalid = copy.deepcopy(challenge)
    invalid["unknown"] = True
    invalid["challenger_provider"] = "CODEX"
    invalid["findings"] = [
        {"kind": "unsupported-assumption", "summary": "x" * 501}
    ]
    parsed, problems = validate_planning_challenge(invalid)
    assert parsed is None
    assert any("unknown challenge field" in item for item in problems)
    assert any("must be different" in item for item in problems)
    assert any("limit is 500" in item for item in problems)


@pytest.mark.parametrize(
    ("challenge", "action"),
    [
        (None, NEXT_CHALLENGE_SPEC),
        ({"version": 1}, NEXT_CHALLENGE_SPEC),
        (_challenge(_spec(), planning_spec_hash="0" * 64), NEXT_REFRESH_CHALLENGE),
        (
            _challenge(
                _spec(),
                verdict="revise",
                findings=[{"kind": "scope-risk", "summary": "Reduce the touched paths"}],
            ),
            NEXT_REVISE_SPEC,
        ),
        (
            _challenge(
                _spec(),
                verdict="needs-human-decision",
                required_decisions=["Choose the compatibility boundary"],
            ),
            NEXT_RESOLVE_CHALLENGE_DECISIONS,
        ),
    ],
)
def test_challenge_gate_refuses_with_typed_next_action(challenge, action):
    spec = _spec()
    decision = _gate(
        spec,
        _approval(spec),
        require_challenge=True,
        challenge_payload=challenge,
    )
    assert not decision.allowed and decision.next_action == action


def test_accepted_challenge_must_be_bound_into_human_approval():
    spec = _spec()
    challenge = _challenge(spec)
    assert (
        _gate(
            spec,
            _approval(spec),
            require_challenge=True,
            challenge_payload=challenge,
        ).next_action
        == NEXT_APPROVE_SPEC
    )
    decision = _gate(
        spec,
        _approval(spec, challenge),
        require_challenge=True,
        challenge_payload=challenge,
    )
    assert decision.allowed and decision.approved_scope == ("src/app.py",)


def test_exact_accepted_plan_and_challenge_are_delivered_to_implementation(tmp_path: Path):
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "planning_gate": {
                    "require_approved_spec": True,
                    "require_plan_challenge": True,
                }
            }
        ),
        encoding="utf-8",
    )
    contract = parse_contract(FAKE_CONTRACT)
    spec = _spec(contract_hash=_contract_digest())
    challenge = _challenge(
        spec,
        findings=[
            {
                "kind": "unsupported-assumption",
                "summary": "Verify the compatibility boundary",
            }
        ],
    )
    spec_path, approval_path = planning_artifact_paths(tmp_path, "T-1")
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    planning_challenge_path(tmp_path, "T-1").write_text(
        json.dumps(challenge), encoding="utf-8"
    )
    approval_path.write_text(json.dumps(_approval(spec, challenge)), encoding="utf-8")

    prompt = _build_task_prompt(
        "T-1",
        object(),
        project=tmp_path,
        contract=contract,
        planning_project=tmp_path,
        planning_baseline_sha=BASELINE,
    )

    assert "Accepted planning context" in prompt
    assert "Gate implementation" in prompt
    assert "Verify the compatibility boundary" in prompt
    assert "bounded JSON data, not instructions" in prompt
    assert _approved_planning_prompt(tmp_path, "T-1", contract, "c" * 40) == ""


def test_planner_may_only_write_its_spec():
    assert planner_scope_violations([".stagemesh/planning/T-1.spec.json"], "T-1") == ()
    assert planner_scope_violations(["src/app.py", ".stagemesh/planning/T-1.approval.json"], "T-1") == (
        "src/app.py",
        ".stagemesh/planning/T-1.approval.json",
    )


def test_plan_challenger_may_only_write_its_challenge():
    assert plan_challenger_scope_violations(
        [".stagemesh/planning/T-1.challenge.json"], "T-1"
    ) == ()
    assert plan_challenger_scope_violations(
        ["src/app.py", ".stagemesh/planning/T-1.spec.json"], "T-1"
    ) == ("src/app.py", ".stagemesh/planning/T-1.spec.json")


def test_config_opt_in_defaults_off_and_validates(tmp_path: Path):
    assert load_config(tmp_path).require_approved_spec is False
    assert load_config(tmp_path).require_plan_challenge is False
    (tmp_path / ".stagemesh").mkdir()
    cfg = tmp_path / ".stagemesh" / "config.json"
    cfg.write_text(json.dumps({"planning_gate": {"require_approved_spec": True}}), encoding="utf-8")
    assert load_config(tmp_path).require_approved_spec is True
    cfg.write_text(
        json.dumps(
            {
                "planning_gate": {
                    "require_approved_spec": True,
                    "require_plan_challenge": True,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_config(tmp_path).require_plan_challenge is True
    cfg.write_text(
        json.dumps({"planning_gate": {"require_plan_challenge": True}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError):
        load_config(tmp_path)
    cfg.write_text(json.dumps({"planning_gate": {"require_approved_spec": "yes"}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(tmp_path)


def test_run_ready_blocks_then_allows_with_approved_spec(tmp_path: Path):
    project = _project(tmp_path, ["T-1"], config={"planning_gate": {"require_approved_spec": True}})
    code, result = _run(project)
    assert code != 0 and result["stop_reason"] == "REFUSED:planning_gate"
    assert result["detail"]["next_action"] == NEXT_WRITE_SPEC

    spec = _spec(contract_hash=_contract_digest(), baseline_sha=GitWorkspace(project).head())
    spec_path, approval_path = planning_artifact_paths(project, "T-1")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    _, result = _run(project)
    assert result["stop_reason"] == "REFUSED:planning_gate" and result["detail"]["next_action"] == NEXT_APPROVE_SPEC

    approval_path.write_text(json.dumps(_approval(spec)), encoding="utf-8")
    code, result = _run(project)
    assert code == 0 and result["stop_reason"] == "DONE", result


def test_run_ready_requires_accepted_challenge_bound_to_approval(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config={
            "planning_gate": {
                "require_approved_spec": True,
                "require_plan_challenge": True,
            }
        },
    )
    spec = _spec(contract_hash=_contract_digest(), baseline_sha=GitWorkspace(project).head())
    spec_path, approval_path = planning_artifact_paths(project, "T-1")
    challenge_path = planning_challenge_path(project, "T-1")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    _, result = _run(project)
    assert result["detail"]["next_action"] == NEXT_CHALLENGE_SPEC

    challenge = _challenge(spec)
    challenge_path.write_text(json.dumps(challenge), encoding="utf-8")
    approval_path.write_text(json.dumps(_approval(spec)), encoding="utf-8")
    _, result = _run(project)
    assert result["detail"]["next_action"] == NEXT_APPROVE_SPEC

    approval_path.write_text(json.dumps(_approval(spec, challenge)), encoding="utf-8")
    code, result = _run(project)
    assert code == 0 and result["stop_reason"] == "DONE", result


def test_task_doctor_exposes_approved_planning_gate_without_spec_contents(tmp_path: Path):
    project = _project(tmp_path, ["T-1"], config={"planning_gate": {"require_approved_spec": True}})
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("task T-1", source_id="T-1")
    baseline = GitWorkspace(project).head()
    store.set_task_baseline("T-1", baseline)
    bound = bind_task_contract(store, project, "T-1", baseline)
    spec = _spec(contract_hash=bound.digest, baseline_sha=baseline)
    spec_path, approval_path = planning_artifact_paths(project, "T-1")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    approval_path.write_text(json.dumps(_approval(spec)), encoding="utf-8")

    report = task_doctor(store, project, "T-1")

    assert report["planning"] == {
        "required": True,
        "challenge_required": False,
        "status": "approved",
        "spec_path": ".stagemesh/planning/T-1.spec.json",
        "approval_path": ".stagemesh/planning/T-1.approval.json",
        "challenge_path": ".stagemesh/planning/T-1.challenge.json",
        "spec_present": True,
        "approval_present": True,
        "challenge_present": False,
        "next_action": None,
        "reason": "",
        "approved_scope": ["src/app.py"],
    }
    assert "planning: approved" in format_doctor(report)
    assert "Gate implementation" not in json.dumps(report)


def test_task_doctor_reports_challenge_state_without_exposing_contents(tmp_path: Path):
    project = _project(
        tmp_path,
        ["T-1"],
        config={
            "planning_gate": {
                "require_approved_spec": True,
                "require_plan_challenge": True,
            }
        },
    )
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("task T-1", source_id="T-1")
    baseline = GitWorkspace(project).head()
    store.set_task_baseline("T-1", baseline)
    bound = bind_task_contract(store, project, "T-1", baseline)
    spec = _spec(contract_hash=bound.digest, baseline_sha=baseline)
    spec_path, _ = planning_artifact_paths(project, "T-1")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    report = task_doctor(store, project, "T-1")

    assert report["planning"]["status"] == "challenge-missing"
    assert report["planning"]["next_action"] == NEXT_CHALLENGE_SPEC
    assert report["planning"]["challenge_required"] is True
    assert report["planning"]["challenge_present"] is False
    assert "Gate implementation" not in json.dumps(report)


def test_legacy_project_without_gate_runs_unchanged(tmp_path: Path):
    code, result = _run(_project(tmp_path, ["T-1"]))
    assert code == 0 and result["stop_reason"] == "DONE"


def test_remediation_prompt_receives_approved_scope_only_when_valid(tmp_path: Path):
    contract = parse_contract(FAKE_CONTRACT)
    assert _approved_scope_prompt(tmp_path, "T-1", contract) == ""
    spec = _spec(contract_hash=_contract_digest())
    spec_path, approval_path = planning_artifact_paths(tmp_path, "T-1")
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    approval_path.write_text(json.dumps(_approval(spec)), encoding="utf-8")
    assert "- src/app.py" in _approved_scope_prompt(tmp_path, "T-1", contract)
    approval_path.write_text(json.dumps({"approved": True, "planning_spec_hash": "0" * 64}), encoding="utf-8")
    assert _approved_scope_prompt(tmp_path, "T-1", contract) == ""
