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


def _legacy_contract(tmp_path, *, frozen=True):
    import hashlib
    from stagemesh.git import GitWorkspace

    project = _project(tmp_path, ["1", "2"])
    payload = dict(FAKE_CONTRACT, invariants=["Preserve the existing public API behavior.", "project-check --strict"])
    path = project / ".stagemesh/contracts/1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = _synced(project)
    baseline = GitWorkspace(project).head()
    store.set_task_baseline("1", baseline)
    canonical = canonical_contract_json(parse_contract(payload))
    if frozen:
        store.bind_task_contract("1", baseline, 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
    return project, store, path, canonical


@pytest.mark.parametrize("frozen", [False, True])
def test_invalid_invariants_refused_by_selection_binding_and_runner(tmp_path, frozen):
    from stagemesh.contract_binding import ContractRejected, bind_task_contract
    from stagemesh.git import GitWorkspace

    project, store, path, _ = _legacy_contract(tmp_path, frozen=frozen)
    before = path.read_bytes()
    try:
        assert select_next_task(store, project, TaskSelectionConfig()).task_id == "2"
        picked, skipped = planned_selection(store, WindowOptions(max_tasks=1), project)
        assert picked == ["2"] and "ambiguous invariant" in skipped["1"]
        with pytest.raises(RunReadyRefusal, match="ambiguous invariant"):
            _ensure_contract(store, project, "1", True, {}, lambda _: None)
        with pytest.raises(ContractRejected, match="ambiguous invariant") as exc:
            bind_task_contract(store, project, "1", GitWorkspace(project).head())
        assert exc.value.reason == "invalid_contract"
        assert path.read_bytes() == before
        assert not list(store.running_executions()) and not store.has_active_claim("1")
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["IMPLEMENT", "VALIDATE", "REVIEW", "INTEGRATE"])
def test_coordinator_blocks_invalid_contract_before_any_stage_or_provider(tmp_path, stage):
    from stagemesh.block_kinds import BlockKind, classify_block
    from stagemesh.coordinator import Coordinator, TargetSelection
    from stagemesh.domain import Stage

    class Never:
        name = "must-not-be-called"

        def run(self, *args):
            pytest.fail("invalid contract reached a provider")

        validate = review = integrate = run

    project, store, _, _ = _legacy_contract(tmp_path)
    try:
        store.advance_task("1", Stage(stage))
        coordinator = Coordinator(store, project, executor=Never(), validator=Never(), reviewer=Never(),
                                  integrator=Never(), target=TargetSelection("1"))
        assert coordinator.tick() == 1
        block = classify_block(store, "1")
        assert block.kind == BlockKind.HUMAN_DECISION and "invalid_contract" in block.reason
        assert store.task_remediation_count("1", stage) == 0
        assert not list(store.running_executions()) and not store.has_active_claim("1")
    finally:
        store.close()


def test_legacy_classification_plan_and_audited_rebind_preserve_every_requirement(tmp_path):
    from stagemesh.recovery import REBIND_EVENT, rebind_contract

    project, store, path, canonical = _legacy_contract(tmp_path)
    before = path.read_bytes()
    try:
        old = dict(store.task_contract("1"))
        count = store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        plan = rebind_contract(store, project, "1", review_invariants=(1,), reason="Classify the legacy prose requirement", plan=True)
        assert plan["plan"] and plan["classification"][0]["original"] == json.loads(canonical)["invariants"][0]
        assert dict(store.task_contract("1")) == old
        assert store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == count
        # The repair must not import unrelated edits from a mutable contract file.
        path.write_text(json.dumps(dict(FAKE_CONTRACT, required_tests=[])), encoding="utf-8")
        file_before_apply = path.read_bytes()
        report = rebind_contract(store, project, "1", review_invariants=(1,), reason="Classify the legacy prose requirement")
        new = json.loads(store.task_contract("1")["canonical_json"])
        expected = json.loads(canonical)
        expected["invariants"][0] = "review: " + expected["invariants"][0]
        assert new == expected and path.read_bytes() == file_before_apply
        assert report["new_digest"] != report["old_digest"] and report["invariant_classification"] == plan["classification"]
        audit = json.loads(store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY rowid DESC LIMIT 1", (REBIND_EVENT,)).fetchone()[0])
        assert audit["old_canonical_json"] == canonical
        assert audit["new_canonical_json"] == store.task_contract("1")["canonical_json"]
        assert before != file_before_apply  # only this test changed the file
    finally:
        store.close()


@pytest.mark.parametrize("kwargs", [
    {"review_invariants": (0,)}, {"review_invariants": (3,)}, {"review_invariants": (1, 1)},
    {"review_invariants": (1,), "command_invariants": (1,)}, {"review_invariants": (2,)},
])
def test_legacy_classification_refuses_invalid_indices_and_executable_downgrades(tmp_path, kwargs):
    from stagemesh.recovery import RecoveryRefusal, rebind_contract

    project, store, path, canonical = _legacy_contract(tmp_path)
    before = path.read_bytes()
    try:
        with pytest.raises(RecoveryRefusal):
            rebind_contract(store, project, "1", reason="Explicit classification", **kwargs)
        assert store.task_contract("1")["canonical_json"] == canonical and path.read_bytes() == before
    finally:
        store.close()


def test_legacy_rebind_requires_reason_and_retains_explicit_custom_commands(tmp_path):
    from stagemesh.recovery import RecoveryRefusal, rebind_contract

    project, store, _, _ = _legacy_contract(tmp_path)
    try:
        with pytest.raises(RecoveryRefusal, match="reason"):
            rebind_contract(store, project, "1", command_invariants=(1,))
        rebind_contract(store, project, "1", command_invariants=(1,), reason="This is an actual custom check")
        invariant = json.loads(store.task_contract("1")["canonical_json"])["invariants"][0]
        assert invariant == "command: Preserve the existing public API behavior."
    finally:
        store.close()


@pytest.mark.parametrize("stage,kind", [("VALIDATE", "VALIDATION"), ("REVIEW", "REVIEW")])
def test_validator_invalid_contract_never_requests_code_remediation(tmp_path, stage, kind):
    from stagemesh.coordinator import Coordinator
    from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage

    project, store, _, _ = _legacy_contract(tmp_path)
    try:
        sha = "f" * 40
        store.add_evidence("1", sha, EvidenceKind(kind), EvidenceStatus.FAILED,
                           {"findings": [{"code": "invalid_contract", "message": "bad contract binding"}]})
        assert Coordinator(store, project)._remediate_or_block("1", sha, Stage(stage)) == 1
        assert store.get_task("1")["status"] == "BLOCKED"
        assert store.task_remediation_count("1", stage) == 0
        assert not list(store.running_executions())
    finally:
        store.close()


def test_legacy_rebind_requires_force_for_old_passed_evidence_and_resets_validation(tmp_path):
    from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
    from stagemesh.recovery import RecoveryRefusal, rebind_contract

    project, store, _, canonical = _legacy_contract(tmp_path)
    try:
        sha = "c" * 40
        old = dict(store.task_contract("1"))
        store.add_candidate("1", sha, "implementer", True)
        store.bind_contract("1", sha, old["baseline_sha"], old["version"], old["digest"], canonical)
        store.advance_task("1", Stage.INTEGRATE)
        for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
            store.add_evidence("1", sha, kind, EvidenceStatus.PASSED,
                               {"contract_hash": old["digest"], "contract_version": 1, "candidate_sha": sha, "baseline_sha": old["baseline_sha"]})
        with pytest.raises(RecoveryRefusal, match="evidence"):
            rebind_contract(store, project, "1", review_invariants=(1,), reason="Classify legacy prose")
        assert dict(store.task_contract("1")) == old
        report = rebind_contract(store, project, "1", review_invariants=(1,), reason="Classify legacy prose", force=True)
        assert len(report["invalidated_evidence"]) == 3 and report["stage"] == "VALIDATE"
        for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
            assert not store.has_bound_evidence("1", sha, kind, report["new_digest"], EvidenceStatus.PASSED)
        assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id='1'").fetchone()[0] == 3
        assert store.latest_candidate("1")["sha"] == sha
    finally:
        store.close()


def test_legacy_classification_cli_plan_is_non_mutating(tmp_path, capsys):
    import stagemesh.cli as cli

    project, store, _, canonical = _legacy_contract(tmp_path)
    try:
        assert cli.main(["--project", str(project), "rebind-contract", "--task", "1", "--review-invariant", "1",
                         "--reason", "Classify prose explicitly", "--plan", "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["plan"] and report["classification"][0]["kind"] == "review"
        assert store.task_contract("1")["canonical_json"] == canonical
        assert not list(store.running_executions()) and not store.has_active_claim("1")
    finally:
        store.close()


def test_legacy_original_survives_failure_after_binding_write(tmp_path, monkeypatch):
    import hashlib
    from stagemesh.recovery import REBIND_EVENT, rebind_contract

    project, store, _, canonical = _legacy_contract(tmp_path)
    # Older bindings may omit newer defaults or use a different serialization.
    payload = json.loads(canonical)
    canonical = json.dumps({key: payload[key] for key in ("objective", "allowed_files", "invariants", "required_tests")}, indent=2)
    store.rebind_task_contract("1", 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
    write = store.rebind_task_contract

    def interrupted(*args, **kwargs):
        write(*args, **kwargs)
        raise RuntimeError("simulated interruption after binding commit")

    try:
        monkeypatch.setattr(store, "rebind_task_contract", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            rebind_contract(store, project, "1", review_invariants=(1,), reason="Explicit legacy classification")
        saved = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.contract_classification_authorized'").fetchone()
        assert json.loads(saved[0])["old_canonical_json"] == canonical
        assert json.loads(saved[0])["new_canonical_json"] == store.task_contract("1")["canonical_json"]
        assert not store.conn.execute("SELECT 1 FROM audit_events WHERE event_type=?", (REBIND_EVENT,)).fetchone()
    finally:
        store.close()


def test_legacy_classification_refuses_unknown_requirement_fields(tmp_path):
    import hashlib
    from stagemesh.recovery import RecoveryRefusal, rebind_contract

    project, store, _, canonical = _legacy_contract(tmp_path)
    payload = json.loads(canonical)
    payload["future_requirements"] = ["Do not discard this requirement"]
    canonical = json.dumps(payload)
    store.rebind_task_contract("1", 1, hashlib.sha256(canonical.encode()).hexdigest(), canonical)
    try:
        with pytest.raises(RecoveryRefusal, match="unknown fields"):
            rebind_contract(store, project, "1", review_invariants=(1,), reason="Classify legacy prose")
        assert store.task_contract("1")["canonical_json"] == canonical
    finally:
        store.close()
