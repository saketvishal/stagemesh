from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_run_ready import FAKE_CONTRACT, _project, _run

from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contracts import canonical_contract_json, parse_contract
from stagemesh.git import GitWorkspace
from stagemesh.providers import _approved_scope_prompt
from stagemesh.spec_planning import (
    NEXT_APPROVE_SPEC,
    NEXT_REFRESH_SPEC,
    NEXT_RESOLVE_DECISIONS,
    NEXT_WRITE_SPEC,
    evaluate_planning_gate,
    freeze_planning_spec,
    planner_scope_violations,
    planning_artifact_paths,
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


def _approval(payload: dict) -> dict:
    return {"approved": True, "planning_spec_hash": freeze_planning_spec(payload).digest}


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


def test_planner_may_only_write_its_spec():
    assert planner_scope_violations([".stagemesh/planning/T-1.spec.json"], "T-1") == ()
    assert planner_scope_violations(["src/app.py", ".stagemesh/planning/T-1.approval.json"], "T-1") == (
        "src/app.py",
        ".stagemesh/planning/T-1.approval.json",
    )


def test_config_opt_in_defaults_off_and_validates(tmp_path: Path):
    assert load_config(tmp_path).require_approved_spec is False
    (tmp_path / ".stagemesh").mkdir()
    cfg = tmp_path / ".stagemesh" / "config.json"
    cfg.write_text(json.dumps({"planning_gate": {"require_approved_spec": True}}), encoding="utf-8")
    assert load_config(tmp_path).require_approved_spec is True
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
