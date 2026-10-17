"""Contract admission regressions; synthetic stores only, no providers or development windows."""
from __future__ import annotations

import json

import pytest
from test_run_ready import FAKE_CONTRACT, _project
from test_task_selection import _synced

from stagemesh.auto_plan import AutoPlanError, create_contract, plannable
from stagemesh.config import TaskSelectionConfig
from stagemesh.contracts import canonical_contract_json, parse_contract
from stagemesh.profile import ProfileError, parse_profile
from stagemesh.profile_default import default_profile
from stagemesh.run_ready import RunReadyRefusal, _ensure_contract
from stagemesh.run_window import WindowOptions, planned_selection
from stagemesh.task_selection import select_next_task


def _profile(project, **updates):
    payload = default_profile(project)
    payload.update(updates)
    path = project / ".stagemesh/profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


@pytest.mark.parametrize("variant", ["legacy", "renamed", "unused_real_gate"])
def test_placeholder_cannot_generate_contract_even_with_a_misleading_name(tmp_path, variant):
    project = _project(tmp_path, ["1"], contracts=[])
    payload = _profile(project)
    payload.pop("contract_generation", None)  # existing profiles must be protected too
    if variant == "renamed":
        gate = payload["gates"].pop("default_acceptance_diff_check")
        gate["name"] = "project-acceptance-tests"
        payload["gates"]["renamed"] = gate
        payload["task_types"]["default"]["gates"] = ["renamed"]
    if variant == "unused_real_gate":
        payload["gates"]["unused"] = {"name": "acceptance-real", "command": ["pytest"]}
    (project / ".stagemesh/profile.json").write_text(json.dumps(payload), encoding="utf-8")
    store = _synced(project)
    try:
        assert "whitespace" in (plannable(store, project, "1") or "")
        with pytest.raises(AutoPlanError, match="whitespace"):
            create_contract(store, project, "1")
        assert not (project / ".stagemesh/contracts/1.json").exists()
        assert list(store.running_executions()) == []
    finally:
        store.close()


def test_explicit_only_policy_keeps_prepared_work_and_skips_unknown_work(tmp_path):
    project = _project(tmp_path, ["1", "2", "3"], contracts=["2", "3"])
    _profile(project, contract_generation="explicit_only")
    store = _synced(project)
    before = (project / ".stagemesh/contracts/2.json").read_bytes()
    try:
        selection = select_next_task(store, project, TaskSelectionConfig())
        assert selection.task_id == "2"
        picked, skipped = planned_selection(store, WindowOptions(max_tasks=1), project)
        assert picked == ["2"]  # an uncontracted task must not spend the task budget
        assert "explicit" in skipped["1"]
        info = {}
        _ensure_contract(store, project, "2", True, info, lambda _: None)
        assert info["reused_existing"]
        assert (project / ".stagemesh/contracts/2.json").read_bytes() == before
        with pytest.raises(RunReadyRefusal, match="explicit"):
            _ensure_contract(store, project, "1", True, {}, lambda _: None)
        assert list(store.running_executions()) == []
        assert not store.has_active_claim("1")
        assert not (project / ".stagemesh/contracts/1.json").exists()
    finally:
        store.close()


@pytest.mark.parametrize("frozen", [False, True])
def test_old_placeholder_contracts_cannot_bypass_admission(tmp_path, frozen):
    project = _project(tmp_path, ["1", "2"])
    payload = dict(FAKE_CONTRACT, required_tests=[{"name": "renamed-acceptance", "command": ["git", "diff", "--check"]}])
    path = project / ".stagemesh/contracts/1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = _synced(project)
    try:
        if frozen:
            canonical = canonical_contract_json(parse_contract(payload))
            import hashlib
            store.bind_task_contract("1", "a" * 40, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
            path.unlink()  # the stored canonical contract must be inspected without a file
        assert select_next_task(store, project, TaskSelectionConfig()).task_id == "2"
        picked, skipped = planned_selection(store, WindowOptions(max_tasks=1), project)
        assert picked == ["2"] and "whitespace" in skipped["1"]
        with pytest.raises(RunReadyRefusal, match="whitespace"):
            _ensure_contract(store, project, "1", True, {}, lambda _: None)
        assert list(store.running_executions()) == [] and not store.has_active_claim("1")
    finally:
        store.close()


def test_real_profile_autoplanning_remains_available(tmp_path):
    project = _project(tmp_path, ["1"], contracts=[])
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _profile(project)
    store = _synced(project)
    try:
        assert plannable(store, project, "1") is None
        assert create_contract(store, project, "1").gates == ("project-acceptance-pytest",)
    finally:
        store.close()


@pytest.mark.parametrize("value", [True, False, None, "", "manual", {}])
def test_contract_generation_policy_rejects_invalid_values(tmp_path, value):
    payload = default_profile(tmp_path)
    payload["contract_generation"] = value
    with pytest.raises(ProfileError, match="contract_generation"):
        parse_profile(payload)


def test_compatibility_smoke_uses_the_existing_contract_in_explicit_only_mode(tmp_path):
    from stagemesh.cli import _sync_all_sources
    from stagemesh.config import load_config
    from stagemesh.profile_smoke import run_smoke

    project = _project(tmp_path, ["1"])
    _profile(project, contract_generation="explicit_only")
    config = load_config(project)
    report = run_smoke(project, config, task_ids=["1"],
                       sync=lambda store, task: _sync_all_sources(store, project, config, task))
    assert report.ok, report.to_dict()
    assert report.tasks[0]["gates"] == ["smoke"]
    assert report.tasks[0]["allowed_files"] == FAKE_CONTRACT["allowed_files"]



@pytest.mark.parametrize("frozen", [False, True])
def test_provider_binding_rejects_placeholder_without_rewriting_it(tmp_path, frozen):
    import hashlib

    from stagemesh.contract_binding import ContractRejected, bind_task_contract

    project = _project(tmp_path, ["1"])
    payload = dict(FAKE_CONTRACT, required_tests=[
        {"name": "acceptance", "command": ["C:/tools/Git/bin/git.exe", "diff", "--check"]}])
    path = project / ".stagemesh/contracts/1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    store = _synced(project)
    try:
        if frozen:
            canonical = canonical_contract_json(parse_contract(payload))
            store.bind_task_contract("1", "a" * 40, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
        with pytest.raises(ContractRejected, match="whitespace"):
            bind_task_contract(store, project, "1", "a" * 40)
        assert path.read_bytes() == before
        assert (store.task_contract("1") is not None) == frozen
        assert list(store.running_executions()) == []
    finally:
        store.close()


def test_contract_check_rejects_whitespace_placeholder_before_labelling_ready():
    from stagemesh.contract_check import check_contract_text
    payload = dict(FAKE_CONTRACT, required_tests=[
        {"name": "acceptance", "command": ["git", "diff", "--check"]}])
    report = check_contract_text(json.dumps(payload))
    assert not report["ok"] and "whitespace" in report["errors"][0]


@pytest.mark.parametrize("restriction", ["protected_files", "forbidden_files"])
@pytest.mark.parametrize("frozen", [False, True])
def test_conflicting_edit_target_is_refused_before_provider_work(tmp_path, restriction, frozen):
    import hashlib

    project = _project(tmp_path, ["1", "2"])
    payload = dict(FAKE_CONTRACT, allowed_files=["src/feature.py"], **{restriction: ["src/*.py"]})
    path = project / ".stagemesh/contracts/1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    store = _synced(project)
    try:
        if frozen:
            canonical = canonical_contract_json(parse_contract(payload))
            store.bind_task_contract("1", "a" * 40, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
        assert select_next_task(store, project, TaskSelectionConfig()).task_id == "2"
        with pytest.raises(RunReadyRefusal, match="scope conflict"):
            _ensure_contract(store, project, "1", True, {}, lambda _: None)
        assert path.read_bytes() == before
        assert not store.has_active_claim("1")
        assert list(store.running_executions()) == []
    finally:
        store.close()


def test_broad_allowed_scope_can_keep_protected_exceptions():
    from stagemesh.contracts import contract_admission_problem

    contract = parse_contract(dict(FAKE_CONTRACT, allowed_files=["src/**"], protected_files=["src/secret.py"]))
    assert contract_admission_problem(contract) is None


def test_contract_authoring_reports_conflicting_scope():
    from stagemesh.contract_check import check_contract_text

    report = check_contract_text(json.dumps(dict(FAKE_CONTRACT, allowed_files=["src/feature.py"],
                                                protected_files=["src/feature.py"])))
    assert not report["ok"]
    assert "scope conflict" in report["errors"][0]
