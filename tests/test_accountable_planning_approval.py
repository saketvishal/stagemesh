from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_review_terminalization import TASK as REVIEW_TASK
from test_review_terminalization import _project_with_candidate

from stagemesh import cli, spec_planning
from stagemesh.autonomy.review_adapter import ScopedReviewAdapter
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.domain import EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.fast_forward import promotion_approval_problem
from stagemesh.persistence import PLANNING_APPROVAL_EVENT, Store, StoreValidationError
from stagemesh.review import Reviewer
from stagemesh.spec_planning import (
    APPROVAL_ACTOR_MISSING,
    APPROVAL_ACTOR_UNAUTHORIZED,
    APPROVAL_AUTHORITY_MISSING,
    APPROVAL_AUTHORITY_STALE,
    APPROVAL_BINDING_STALE,
    APPROVAL_EXECUTION_IDENTITY_MISSING,
    APPROVAL_MALFORMED,
    APPROVAL_MISSING,
    APPROVAL_PROMOTION_EVENT,
    APPROVAL_RECEIPT_EVENT,
    APPROVAL_RECEIPT_MISMATCH,
    APPROVAL_RECEIPT_MISSING,
    APPROVAL_REFUSED_EVENT,
    APPROVAL_ROLE_CONFLICT,
    APPROVAL_VERSION_UNSUPPORTED,
    BOUNDARY_IMPLEMENTATION,
    BOUNDARY_INTEGRATION,
    BOUNDARY_PICKUP,
    BOUNDARY_PROMOTION,
    BOUNDARY_RECOVERY,
    BOUNDARY_REVIEW,
    PLANNING_INPUTS_INVALID,
    ApprovalAuthority,
    ExecutionIdentities,
    PlanningApprovalRefusal,
    approval_authority_digest,
    approval_authority_from_gate_config,
    check_accountable_approval,
    evaluate_accountable_approval,
    evaluate_planning_gate,
    freeze_planning_challenge,
    freeze_planning_spec,
    protected_approval_policy,
    record_planning_approval,
    validate_approval_artifact,
    validate_approval_authority,
)
from stagemesh.stage_dispatch import stage_dispatch_lock

CONTRACT = "c" * 64
BASELINE = "b" * 40
TASK = "task-1"
AUTHORITY_PAYLOAD = {"version": 1, "authority_id": "project-owner:example", "approver_id": "owner:example"}


def _spec() -> dict:
    return {
        "behavior": ["do the thing"],
        "affected_paths": ["src/x.py"],
        "acceptance_checks": ["pytest tests/test_x.py"],
        "contract_hash": CONTRACT,
        "baseline_sha": BASELINE,
    }


def _challenge(spec_hash: str, verdict: str = "accept") -> dict:
    return {
        "version": 1,
        "planning_spec_hash": spec_hash,
        "planner_provider": "planner",
        "challenger_provider": "challenger",
        "verdict": verdict,
        "findings": [{"kind": "scope-risk", "summary": "narrow it"}] if verdict == "revise" else [],
        "required_decisions": [],
    }


def _authority() -> ApprovalAuthority:
    authority, problems = validate_approval_authority(AUTHORITY_PAYLOAD)
    assert authority is not None, problems
    return authority


class Fixture:
    def __init__(self, *, challenge: bool = False) -> None:
        self.require_challenge = challenge
        self.authority = _authority()
        self.spec = _spec()
        spec_hash = freeze_planning_spec(self.spec).digest
        self.challenge = _challenge(spec_hash) if challenge else None
        self.approval = {
            "version": 2,
            "decision": "approve",
            "task_id": TASK,
            "planning_spec_hash": spec_hash,
            "planning_challenge_hash": freeze_planning_challenge(self.challenge).digest if challenge else None,
            "contract_hash": CONTRACT,
            "baseline_sha": BASELINE,
            "authority_id": self.authority.authority_id,
            "authority_hash": self.authority.digest,
            "approver_id": self.authority.approver_id,
            "receipt_id": "receipt-1",
        }
        self.receipt = {**self.approval, "event": "planning_approval_recorded", "created_at": "2026-01-01T00:00:00Z"}
        self.identities = ExecutionIdentities(
            planner="planner:a", challenger="challenger:b", implementers=("impl:c",), reviewer="reviewer:d"
        )

    def evaluate(self, **overrides):
        args = {
            "task_id": TASK,
            "authority": self.authority,
            "spec_payload": self.spec,
            "approval": self.approval,
            "receipts": [self.receipt],
            "contract_hash": CONTRACT,
            "baseline_sha": BASELINE,
            "require_challenge": self.require_challenge,
            "challenge_payload": self.challenge,
            "identities": self.identities,
            "required_roles": ("implementer", "reviewer"),
        }
        args.update(overrides)
        return evaluate_accountable_approval(**args)


def _blocked(decision, reason: str) -> None:
    assert not decision.allowed
    assert decision.reason == reason
    assert decision.next_action is not None
    assert "digest by hand" not in decision.detail


# --- policy -------------------------------------------------------------------------------------------------------
def test_authority_policy_accepts_exact_object_and_digest_is_canonical_sha256() -> None:
    authority = _authority()
    assert authority.authority_id == "project-owner:example"
    assert authority.digest == approval_authority_digest(authority)
    assert len(authority.digest) == 64
    assert ApprovalAuthority("project-owner:example", "other:owner").digest != authority.digest


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"version": 1, "authority_id": "a"},
        {"version": 1, "approver_id": "a"},
        {**AUTHORITY_PAYLOAD, "extra": "x"},
        {**AUTHORITY_PAYLOAD, "version": 2},
        {**AUTHORITY_PAYLOAD, "version": True},
        {**AUTHORITY_PAYLOAD, "version": "1"},
        {**AUTHORITY_PAYLOAD, "authority_id": "Owner"},
        {**AUTHORITY_PAYLOAD, "approver_id": " owner"},
        {**AUTHORITY_PAYLOAD, "approver_id": ""},
        {**AUTHORITY_PAYLOAD, "approver_id": "-owner"},
        {**AUTHORITY_PAYLOAD, "approver_id": "o" * 101},
        {**AUTHORITY_PAYLOAD, "approver_id": "ownér"},
        {**AUTHORITY_PAYLOAD, "approver_id": 7},
    ],
)
def test_authority_policy_is_strict(payload) -> None:
    authority, problems = validate_approval_authority(payload)
    assert authority is None
    assert problems


def test_authority_identity_accepts_full_grammar_and_max_length() -> None:
    payload = {**AUTHORITY_PAYLOAD, "approver_id": "a" + "b._:@/-"[0] * 99}
    assert validate_approval_authority(payload)[0] is not None
    payload = {**AUTHORITY_PAYLOAD, "approver_id": "0a._:@/-z"}
    assert validate_approval_authority(payload)[0] is not None


# --- artifact schema ----------------------------------------------------------------------------------------------
def test_version_two_artifact_validates_with_and_without_challenge() -> None:
    for challenge in (False, True):
        fixture = Fixture(challenge=challenge)
        artifact, problems = validate_approval_artifact(fixture.approval, require_challenge=challenge)
        assert problems == ()
        assert artifact == fixture.approval


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": 1},
        {"decision": "reject"},
        {"task_id": ""},
        {"planning_spec_hash": "A" * 64},
        {"contract_hash": "c" * 63},
        {"baseline_sha": "b" * 41},
        {"authority_hash": None},
        {"authority_id": "Bad Id"},
        {"approver_id": "Bad Id"},
        {"receipt_id": ""},
        {"receipt_id": 5},
        {"planning_challenge_hash": "d" * 64},  # not allowed when no challenge is required
        {"planning_challenge_hash": ""},
    ],
)
def test_artifact_rejects_malformed_fields(mutation) -> None:
    fixture = Fixture()
    artifact, problems = validate_approval_artifact({**fixture.approval, **mutation}, require_challenge=False)
    assert artifact is None
    assert problems


def test_artifact_rejects_missing_key_and_non_object_and_challenge_shape() -> None:
    fixture = Fixture()
    missing = {key: value for key, value in fixture.approval.items() if key != "receipt_id"}
    assert validate_approval_artifact(missing, require_challenge=False)[0] is None
    assert validate_approval_artifact([], require_challenge=False)[0] is None
    with_challenge = Fixture(challenge=True)
    for bad in (None, "", "short"):
        payload = {**with_challenge.approval, "planning_challenge_hash": bad}
        assert validate_approval_artifact(payload, require_challenge=True)[0] is None


# --- compliant path -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("challenge", [False, True])
def test_compliant_artifact_and_receipt_pass_with_distinct_actors(challenge: bool) -> None:
    fixture = Fixture(challenge=challenge)
    decision = fixture.evaluate()
    assert decision.allowed
    assert decision.receipt_id == "receipt-1"
    assert decision.approved_scope == ("src/x.py",)


def test_decision_does_not_mutate_inputs() -> None:
    fixture = Fixture(challenge=True)
    before = copy.deepcopy((fixture.approval, fixture.receipt, fixture.spec, fixture.challenge))
    fixture.evaluate()
    assert (fixture.approval, fixture.receipt, fixture.spec, fixture.challenge) == before


# --- approval and authority evidence ------------------------------------------------------------------------------
def test_missing_authority_approval_and_non_object_fail_closed() -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(authority=None), APPROVAL_AUTHORITY_MISSING)
    _blocked(fixture.evaluate(approval=None), APPROVAL_MISSING)
    _blocked(fixture.evaluate(approval=["x"]), APPROVAL_MALFORMED)


@pytest.mark.parametrize(
    "approval",
    [
        {"approved": True, "planning_spec_hash": "x"},  # legacy shape
        {"version": 1, "decision": "approve"},
        {"version": 3},
        {"version": "2"},
        {"version": True},
        {"version": 2.0},
    ],
)
def test_legacy_or_unknown_versions_are_unsupported_when_authority_configured(approval) -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(approval=approval), APPROVAL_VERSION_UNSUPPORTED)
    # a legacy file cannot be reinterpreted as v2 even if it is "complete" for the legacy gate
    complete_legacy = {"approved": True, "planning_spec_hash": fixture.approval["planning_spec_hash"]}
    _blocked(fixture.evaluate(approval=complete_legacy), APPROVAL_VERSION_UNSUPPORTED)


def test_missing_approver_identity_fails() -> None:
    fixture = Fixture()
    without = {key: value for key, value in fixture.approval.items() if key != "approver_id"}
    _blocked(fixture.evaluate(approval=without), APPROVAL_ACTOR_MISSING)
    _blocked(fixture.evaluate(approval={**fixture.approval, "approver_id": None}), APPROVAL_ACTOR_MISSING)
    _blocked(fixture.evaluate(approval={**fixture.approval, "approver_id": ""}), APPROVAL_ACTOR_MISSING)


def test_unconfigured_approver_fails() -> None:
    fixture = Fixture()
    forged = {**fixture.approval, "approver_id": "someone:else"}
    _blocked(fixture.evaluate(approval=forged), APPROVAL_ACTOR_UNAUTHORIZED)


def test_malformed_artifact_and_unknown_key_fail() -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(approval={**fixture.approval, "extra": 1}), APPROVAL_MALFORMED)
    _blocked(fixture.evaluate(approval={**fixture.approval, "planning_spec_hash": "nope"}), APPROVAL_MALFORMED)
    # null challenge hash is malformed when the policy requires a challenge
    with_challenge = Fixture(challenge=True)
    _blocked(
        with_challenge.evaluate(approval={**with_challenge.approval, "planning_challenge_hash": None}),
        APPROVAL_MALFORMED,
    )


def test_changed_authority_invalidates_existing_approval() -> None:
    fixture = Fixture()
    for payload in (
        {**AUTHORITY_PAYLOAD, "authority_id": "project-owner:other"},
        {**AUTHORITY_PAYLOAD, "approver_id": "owner:other"},
    ):
        changed, problems = validate_approval_authority(payload)
        assert changed is not None, problems
        _blocked(fixture.evaluate(authority=changed), APPROVAL_AUTHORITY_STALE)


# --- stale bindings -----------------------------------------------------------------------------------------------
def test_changed_contract_or_baseline_is_stale() -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(contract_hash="d" * 64), APPROVAL_BINDING_STALE)
    _blocked(fixture.evaluate(baseline_sha="e" * 40), APPROVAL_BINDING_STALE)


def test_changed_spec_is_stale() -> None:
    fixture = Fixture()
    changed = {**fixture.spec, "behavior": ["do something else"]}
    _blocked(fixture.evaluate(spec_payload=changed), APPROVAL_BINDING_STALE)


def test_approval_for_another_task_is_stale() -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(task_id="task-2"), APPROVAL_BINDING_STALE)


def test_changed_challenge_is_stale_or_unaccepted() -> None:
    fixture = Fixture(challenge=True)
    changed = {**fixture.challenge, "challenger_provider": "another"}
    assert fixture.evaluate(challenge_payload=changed).reason == APPROVAL_BINDING_STALE
    other_spec_hash = freeze_planning_spec({**fixture.spec, "behavior": ["x"]}).digest
    stale = _challenge(other_spec_hash)
    assert fixture.evaluate(challenge_payload=stale).reason == APPROVAL_BINDING_STALE
    revise = _challenge(fixture.approval["planning_spec_hash"], "revise")
    assert not fixture.evaluate(challenge_payload=revise).allowed
    assert not fixture.evaluate(challenge_payload=None).allowed


def test_required_challenge_cannot_be_skipped_by_null_hash() -> None:
    fixture = Fixture(challenge=True)
    assert not fixture.evaluate(challenge_payload=None).allowed
    assert not fixture.evaluate(require_challenge=False).allowed  # approval carries a hash the policy says is null


def test_invalid_planning_spec_blocks() -> None:
    fixture = Fixture()
    assert not fixture.evaluate(spec_payload=None).allowed
    assert not fixture.evaluate(spec_payload={"behavior": []}).allowed


# --- durable receipt identity bindings ----------------------------------------------------------------------------
def test_copied_artifact_without_receipt_fails() -> None:
    fixture = Fixture()
    _blocked(fixture.evaluate(receipts=[]), APPROVAL_RECEIPT_MISSING)
    other = {**fixture.receipt, "receipt_id": "receipt-2"}
    _blocked(fixture.evaluate(receipts=[other, "junk", None]), APPROVAL_RECEIPT_MISSING)


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", 1),
        ("decision", "reject"),
        ("task_id", "task-2"),
        ("planning_spec_hash", "1" * 64),
        ("planning_challenge_hash", "2" * 64),
        ("contract_hash", "3" * 64),
        ("baseline_sha", "4" * 40),
        ("authority_id", "project-owner:other"),
        ("authority_hash", "5" * 64),
        ("approver_id", "owner:other"),
    ],
)
def test_receipt_with_any_mismatched_binding_fails(key: str, value) -> None:
    fixture = Fixture()
    receipt = {**fixture.receipt, key: value}
    _blocked(fixture.evaluate(receipts=[receipt]), APPROVAL_RECEIPT_MISMATCH)


def test_receipt_missing_binding_wrong_event_no_time_or_duplicate_fails() -> None:
    fixture = Fixture()
    missing = {key: value for key, value in fixture.receipt.items() if key != "baseline_sha"}
    _blocked(fixture.evaluate(receipts=[missing]), APPROVAL_RECEIPT_MISMATCH)
    _blocked(fixture.evaluate(receipts=[{**fixture.receipt, "event": "other_event"}]), APPROVAL_RECEIPT_MISMATCH)
    no_time = {key: value for key, value in fixture.receipt.items() if key != "created_at"}
    _blocked(fixture.evaluate(receipts=[no_time]), APPROVAL_RECEIPT_MISMATCH)
    _blocked(fixture.evaluate(receipts=[fixture.receipt, dict(fixture.receipt)]), APPROVAL_RECEIPT_MISMATCH)


def test_challenge_receipt_binds_challenge_hash() -> None:
    fixture = Fixture(challenge=True)
    assert fixture.evaluate().allowed
    _blocked(fixture.evaluate(receipts=[{**fixture.receipt, "planning_challenge_hash": None}]), APPROVAL_RECEIPT_MISMATCH)


# --- role separation ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "role,identities",
    [
        ("planner", ExecutionIdentities("owner:example", "challenger:b", ("impl:c",), "reviewer:d")),
        ("challenger", ExecutionIdentities("planner:a", "owner:example", ("impl:c",), "reviewer:d")),
        ("implementer", ExecutionIdentities("planner:a", "challenger:b", ("owner:example",), "reviewer:d")),
        ("implementer", ExecutionIdentities("planner:a", "challenger:b", ("impl:c", "owner:example"), "reviewer:d")),
        ("reviewer", ExecutionIdentities("planner:a", "challenger:b", ("impl:c",), "owner:example")),
    ],
)
def test_approver_equal_to_any_actor_is_a_role_conflict(role: str, identities: ExecutionIdentities) -> None:
    fixture = Fixture(challenge=True)
    decision = fixture.evaluate(identities=identities)
    _blocked(decision, APPROVAL_ROLE_CONFLICT)
    assert role in decision.detail


def test_inherited_implementer_identity_cannot_be_erased_by_replacement() -> None:
    fixture = Fixture()
    identities = ExecutionIdentities("planner:a", None, ("owner:example", "impl:replacement"), "reviewer:d")
    _blocked(fixture.evaluate(identities=identities), APPROVAL_ROLE_CONFLICT)


def test_missing_required_execution_identity_blocks() -> None:
    fixture = Fixture(challenge=True)
    _blocked(
        fixture.evaluate(identities=ExecutionIdentities(None, "challenger:b", ("impl:c",), "reviewer:d")),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )
    _blocked(
        fixture.evaluate(identities=ExecutionIdentities("planner:a", None, ("impl:c",), "reviewer:d")),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )
    _blocked(
        fixture.evaluate(identities=ExecutionIdentities("planner:a", "challenger:b", (), "reviewer:d")),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )
    _blocked(
        fixture.evaluate(identities=ExecutionIdentities("planner:a", "challenger:b", ("impl:c",), None)),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )
    _blocked(fixture.evaluate(identities=None), APPROVAL_EXECUTION_IDENTITY_MISSING)


def test_non_canonical_execution_identity_is_treated_as_missing() -> None:
    fixture = Fixture()
    for identities in (
        ExecutionIdentities("Planner Display Name", None, ("impl:c",), "reviewer:d"),
        ExecutionIdentities("planner:a", None, ("Impl C",), "reviewer:d"),
        ExecutionIdentities("planner:a", None, ("impl:c",), ""),
    ):
        _blocked(fixture.evaluate(identities=identities), APPROVAL_EXECUTION_IDENTITY_MISSING)


def test_roles_not_yet_known_are_not_required_at_earlier_boundaries() -> None:
    fixture = Fixture()
    pickup = fixture.evaluate(identities=ExecutionIdentities(planner="planner:a"), required_roles=())
    assert pickup.allowed
    dispatch = fixture.evaluate(
        identities=ExecutionIdentities(planner="planner:a", implementers=("impl:c",)), required_roles=("implementer",)
    )
    assert dispatch.allowed
    review = fixture.evaluate(
        identities=ExecutionIdentities(planner="planner:a", implementers=("impl:c",)), required_roles=("reviewer",)
    )
    _blocked(review, APPROVAL_EXECUTION_IDENTITY_MISSING)


def test_role_conflict_is_still_detected_for_optional_identities_when_supplied() -> None:
    fixture = Fixture()
    identities = ExecutionIdentities(planner="planner:a", reviewer="owner:example")
    _blocked(fixture.evaluate(identities=identities, required_roles=()), APPROVAL_ROLE_CONFLICT)


def test_authority_from_authoritative_input_not_from_artifact() -> None:
    # An artifact that names a different authority than the protected policy is rejected even if self-consistent.
    fixture = Fixture()
    forged_authority = ApprovalAuthority("project-owner:example", "attacker:self")
    forged = {
        **fixture.approval,
        "authority_hash": forged_authority.digest,
        "approver_id": "attacker:self",
    }
    receipt = {**fixture.receipt, **forged}
    _blocked(fixture.evaluate(approval=forged, receipts=[receipt]), APPROVAL_AUTHORITY_STALE)


# --- legacy behavior ----------------------------------------------------------------------------------------------
def test_absent_approval_authority_is_legacy() -> None:
    assert approval_authority_from_gate_config({"enabled": True}) == (None, ())
    assert approval_authority_from_gate_config({}) == (None, ())
    assert approval_authority_from_gate_config(None) == (None, ())


def test_present_approval_authority_is_validated() -> None:
    authority, problems = approval_authority_from_gate_config({"approval_authority": AUTHORITY_PAYLOAD})
    assert authority == _authority()
    assert problems == ()
    for bad in (None, {}, {**AUTHORITY_PAYLOAD, "extra": 1}):
        authority, problems = approval_authority_from_gate_config({"approval_authority": bad})
        assert authority is None
        assert problems


def test_legacy_gate_is_unchanged_without_authority() -> None:
    spec = _spec()
    digest = freeze_planning_spec(spec).digest
    legacy = {"approved": True, "planning_spec_hash": digest}
    decision = evaluate_planning_gate(
        enabled=True, spec_payload=spec, approval=legacy, contract_hash=CONTRACT, baseline_sha=BASELINE
    )
    assert decision.allowed
    assert decision.approved_scope == ("src/x.py",)
    assert evaluate_planning_gate(
        enabled=False, spec_payload=None, approval=None, contract_hash=CONTRACT, baseline_sha=BASELINE
    ).allowed


def test_legacy_gate_never_treats_a_version_two_artifact_as_approval() -> None:
    fixture = Fixture()
    decision = evaluate_planning_gate(
        enabled=True,
        spec_payload=fixture.spec,
        approval=fixture.approval,
        contract_hash=CONTRACT,
        baseline_sha=BASELINE,
    )
    assert not decision.allowed


# --- protected configuration --------------------------------------------------------------------------------------
def _write_config(project, planning_gate: dict) -> None:
    (project / ".stagemesh").mkdir(parents=True, exist_ok=True)
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"planning_gate": planning_gate}), encoding="utf-8")


def test_config_parses_authority_and_absent_key_is_legacy(tmp_path) -> None:
    _write_config(tmp_path, {"require_approved_spec": True, "approval_authority": AUTHORITY_PAYLOAD})
    assert load_config(tmp_path).approval_authority == _authority()
    _write_config(tmp_path, {"require_approved_spec": True})
    config = load_config(tmp_path)
    assert config.approval_authority is None
    assert config.require_approved_spec is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {**AUTHORITY_PAYLOAD, "extra": 1},
        {"version": 1, "authority_id": "project-owner:example"},
        {**AUTHORITY_PAYLOAD, "version": 2},
        {**AUTHORITY_PAYLOAD, "approver_id": "Owner"},
    ],
)
def test_config_rejects_present_but_invalid_authority(tmp_path, bad) -> None:
    _write_config(tmp_path, {"require_approved_spec": True, "approval_authority": bad})
    with pytest.raises(ConfigValidationError, match="approval_authority"):
        load_config(tmp_path)


def test_config_still_rejects_other_unknown_planning_gate_keys(tmp_path) -> None:
    _write_config(tmp_path, {"approval_authority": AUTHORITY_PAYLOAD, "approver_id": "owner:example"})
    with pytest.raises(ConfigValidationError, match="unsupported keys"):
        load_config(tmp_path)


# --- durable receipt store ----------------------------------------------------------------------------------------
class Project:
    """A synthetic project: a migrated store with a bound task, plus a spec (and optional accepted challenge) on disk."""

    def __init__(self, tmp_path, *, challenge: bool = False) -> None:
        self.root = tmp_path / "project"
        self.root.mkdir()
        self.store = Store(cli.db_path(self.root))
        self.store.migrate()
        self.task_id = self.store.upsert_task("synthetic")
        self.store.set_task_baseline(self.task_id, BASELINE)
        self.store.bind_task_contract(self.task_id, BASELINE, 1, CONTRACT, "{}")
        self.challenge = challenge
        self.write_spec(_spec())

    def write_spec(self, spec: dict) -> None:
        planning = self.root / ".stagemesh" / "planning"
        planning.mkdir(parents=True, exist_ok=True)
        (planning / f"{self.task_id}.spec.json").write_text(json.dumps(spec), encoding="utf-8")
        if self.challenge:
            payload = _challenge(freeze_planning_spec(spec).digest)
            (planning / f"{self.task_id}.challenge.json").write_text(json.dumps(payload), encoding="utf-8")

    def approve(self, **overrides):
        args = {"authority": _authority(), "require_challenge": self.challenge}
        args.update(overrides)
        return record_planning_approval(self.store, self.root, self.task_id, **args)

    def artifact(self) -> dict:
        path = self.root / ".stagemesh" / "planning" / f"{self.task_id}.approval.json"
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path):
    synthetic = Project(tmp_path)
    yield synthetic
    synthetic.store.close()


def test_approval_waits_for_governed_planning_to_release_task_lock(project) -> None:
    with stage_dispatch_lock(project.store.db_path, project.task_id).hold("planning"):
        with pytest.raises(PlanningApprovalRefusal) as raised:
            project.approve()
        assert raised.value.code == "stage_dispatch_active"
        assert project.store.planning_approval_receipts(project.task_id) == []
    assert project.approve()["artifact_written"] is True



def test_receipt_event_name_matches_the_evaluator_constant() -> None:
    assert PLANNING_APPROVAL_EVENT == APPROVAL_RECEIPT_EVENT


def test_store_receipts_are_append_only_and_scoped_to_the_task(project) -> None:
    receipt = {"task_id": project.task_id, "receipt_id": "r-1", "note": "x"}
    project.store.record_planning_approval(receipt)
    with pytest.raises(StoreValidationError):
        project.store.record_planning_approval(receipt)
    with pytest.raises(StoreValidationError):
        project.store.record_planning_approval({**receipt, "receipt_id": "r-2", "event": "forged"})
    with pytest.raises(StoreValidationError):
        project.store.record_planning_approval({"task_id": project.task_id})
    stored = project.store.planning_approval_receipts(project.task_id)
    assert [item["receipt_id"] for item in stored] == ["r-1"]
    assert stored[0]["event"] == APPROVAL_RECEIPT_EVENT
    assert stored[0]["created_at"].endswith("Z")
    assert project.store.planning_approval_receipts("another-task") == []


def test_active_execution_kinds_track_running_and_unknown_only(project) -> None:
    assert project.store.active_execution_kinds(project.task_id) == set()
    execution = project.store.start_execution(task_id=project.task_id, claim_id=None, kind=ExecutionKind.REVIEW)
    assert project.store.active_execution_kinds(project.task_id) == {"REVIEW"}
    project.store.finish_execution(execution, ExecutionStatus.SUCCEEDED)
    assert project.store.active_execution_kinds(project.task_id) == set()


# --- operator approval action -------------------------------------------------------------------------------------
@pytest.mark.parametrize("challenge", [False, True])
def test_approval_records_receipt_then_artifact_and_satisfies_the_evaluator(tmp_path, challenge: bool) -> None:
    fixture = Project(tmp_path, challenge=challenge)
    try:
        report = fixture.approve()
        artifact = fixture.artifact()
        receipts = fixture.store.planning_approval_receipts(fixture.task_id)
        assert len(receipts) == 1 and report["receipt_recorded"] and report["artifact_written"]
        assert report["receipt_id"] == artifact["receipt_id"] == receipts[0]["receipt_id"]
        assert report["approver_id"] == _authority().approver_id == artifact["approver_id"]
        assert report["authority_hash"] == _authority().digest
        assert report["baseline_sha"] == BASELINE and report["contract_hash"] == CONTRACT
        assert (report["planning_challenge_hash"] is not None) is challenge
        assert validate_approval_artifact(artifact, require_challenge=challenge)[1] == ()
        decision = evaluate_accountable_approval(
            task_id=fixture.task_id,
            authority=_authority(),
            spec_payload=_spec(),
            approval=artifact,
            receipts=receipts,
            contract_hash=CONTRACT,
            baseline_sha=BASELINE,
            require_challenge=challenge,
            challenge_payload=json.loads(
                (fixture.root / ".stagemesh" / "planning" / f"{fixture.task_id}.challenge.json").read_text("utf-8")
            )
            if challenge
            else None,
            identities=ExecutionIdentities("planner:a", "challenger:b", ("impl:c",), "reviewer:d"),
            required_roles=("implementer", "reviewer"),
        )
        assert decision.allowed, decision
        leftovers = [path.name for path in (fixture.root / ".stagemesh" / "planning").iterdir() if path.name.endswith(".tmp")]
        assert leftovers == []
    finally:
        fixture.store.close()


def test_identical_repeat_returns_the_existing_receipt(project) -> None:
    first = project.approve()
    second = project.approve()
    assert second["receipt_id"] == first["receipt_id"]
    assert second["created_at"] == first["created_at"]
    assert not second["receipt_recorded"] and not second["artifact_written"]
    assert len(project.store.planning_approval_receipts(project.task_id)) == 1


def test_repeat_restores_a_missing_artifact_without_a_new_receipt(project) -> None:
    first = project.approve()
    (project.root / ".stagemesh" / "planning" / f"{project.task_id}.approval.json").unlink()
    again = project.approve()
    assert again["receipt_id"] == first["receipt_id"] and again["artifact_written"] and not again["receipt_recorded"]
    assert project.artifact()["receipt_id"] == first["receipt_id"]


def test_changed_input_creates_a_new_receipt_and_leaves_the_old_one(project) -> None:
    first = project.approve()
    project.write_spec({**_spec(), "behavior": ["do a different thing"]})
    second = project.approve()
    receipts = project.store.planning_approval_receipts(project.task_id)
    assert second["receipt_id"] != first["receipt_id"]
    assert [item["receipt_id"] for item in receipts] == [first["receipt_id"], second["receipt_id"]]
    assert receipts[0]["planning_spec_hash"] != receipts[1]["planning_spec_hash"]
    assert project.artifact()["receipt_id"] == second["receipt_id"]


def test_receipt_is_durable_before_the_artifact_is_written(project, monkeypatch) -> None:
    def fail(path, artifact):
        raise OSError("disk full")

    monkeypatch.setattr(spec_planning, "_write_artifact_atomic", fail)
    with pytest.raises(OSError):
        project.approve()
    assert len(project.store.planning_approval_receipts(project.task_id)) == 1
    assert not (project.root / ".stagemesh" / "planning" / f"{project.task_id}.approval.json").exists()
    monkeypatch.undo()
    recovered = project.approve()  # the orphaned receipt is reused, not duplicated
    assert not recovered["receipt_recorded"] and recovered["artifact_written"]
    assert len(project.store.planning_approval_receipts(project.task_id)) == 1


def _assert_refused(project, code: str, **overrides) -> None:
    with pytest.raises(PlanningApprovalRefusal) as excinfo:
        project.approve(**overrides)
    assert excinfo.value.code == code
    assert project.store.planning_approval_receipts(project.task_id) == []
    assert not (project.root / ".stagemesh" / "planning" / f"{project.task_id}.approval.json").exists()


def test_refuses_inside_a_provider_process(project, monkeypatch) -> None:
    monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
    _assert_refused(project, "operator_decision")


def test_refuses_without_authority_or_with_an_unsafe_task_id(project) -> None:
    _assert_refused(project, APPROVAL_AUTHORITY_MISSING, authority=None)
    for bad in ("", "..", "../escape", "a/b", "a\\b"):
        with pytest.raises(PlanningApprovalRefusal) as excinfo:
            record_planning_approval(project.store, project.root, bad, authority=_authority(), require_challenge=False)
        assert excinfo.value.code == "invalid_task"


def test_refuses_an_unbound_task(tmp_path) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    store = Store(cli.db_path(root))
    store.migrate()
    try:
        task_id = store.upsert_task("unbound")
        with pytest.raises(PlanningApprovalRefusal) as excinfo:
            record_planning_approval(store, root, task_id, authority=_authority(), require_challenge=False)
        assert excinfo.value.code == "task_identity_unbound"
    finally:
        store.close()


@pytest.mark.parametrize("kind", [ExecutionKind.IMPLEMENTATION, ExecutionKind.REVIEW, ExecutionKind.INTEGRATION])
def test_refuses_while_implementation_review_or_integration_is_active(project, kind) -> None:
    execution = project.store.start_execution(task_id=project.task_id, claim_id=None, kind=kind)
    _assert_refused(project, "execution_active")
    project.store.finish_execution(execution, ExecutionStatus.FAILED)
    assert project.approve()["receipt_recorded"]


def test_validation_execution_does_not_block_approval(project) -> None:
    project.store.start_execution(task_id=project.task_id, claim_id=None, kind=ExecutionKind.VALIDATION)
    assert project.approve()["receipt_recorded"]


def test_refuses_stale_or_invalid_planning_inputs(project) -> None:
    project.write_spec({**_spec(), "baseline_sha": "e" * 40})
    _assert_refused(project, APPROVAL_BINDING_STALE)
    project.write_spec({**_spec(), "contract_hash": "d" * 64})
    _assert_refused(project, APPROVAL_BINDING_STALE)
    project.write_spec({"behavior": []})
    _assert_refused(project, PLANNING_INPUTS_INVALID)
    (project.root / ".stagemesh" / "planning" / f"{project.task_id}.spec.json").unlink()
    _assert_refused(project, PLANNING_INPUTS_INVALID)


def test_required_challenge_must_exist_match_and_be_accepted(tmp_path) -> None:
    fixture = Project(tmp_path, challenge=True)
    try:
        challenge_path = fixture.root / ".stagemesh" / "planning" / f"{fixture.task_id}.challenge.json"
        spec_hash = freeze_planning_spec(_spec()).digest
        challenge_path.write_text(json.dumps(_challenge(spec_hash, "revise")), encoding="utf-8")
        _assert_refused(fixture, PLANNING_INPUTS_INVALID)
        challenge_path.write_text(json.dumps(_challenge("1" * 64)), encoding="utf-8")
        _assert_refused(fixture, APPROVAL_BINDING_STALE)
        challenge_path.unlink()
        _assert_refused(fixture, PLANNING_INPUTS_INVALID)
    finally:
        fixture.store.close()


def test_action_does_not_modify_plan_inputs_or_task_bindings(project) -> None:
    planning = project.root / ".stagemesh" / "planning"
    spec_before = (planning / f"{project.task_id}.spec.json").read_bytes()
    project.approve()
    assert (planning / f"{project.task_id}.spec.json").read_bytes() == spec_before
    assert project.store.task_baseline(project.task_id) == BASELINE
    assert project.store.task_contract(project.task_id)["digest"] == CONTRACT


# --- lifecycle boundaries with a real store and protected project configuration -----------------------------------
CANDIDATE = "a" * 40
DERIVED = "f" * 40
APPROVER = AUTHORITY_PAYLOAD["approver_id"]
PLANNER, CHALLENGER, IMPLEMENTER, REVIEWER = "planner:a", "challenger:b", "impl:c", "reviewer:d"


def _gate_config(approver: str = APPROVER) -> dict:
    return {
        "require_approved_spec": True,
        "require_plan_challenge": True,
        "approval_authority": {**AUTHORITY_PAYLOAD, "approver_id": approver},
    }


class Lifecycle:
    """A synthetic accountable task: protected config, frozen plan, executed planner/challenger, and an approval."""

    def __init__(self, tmp_path) -> None:
        self.root = tmp_path / "lifecycle"
        self.root.mkdir()
        _write_config(self.root, _gate_config())
        self.store = Store(cli.db_path(self.root))
        self.store.migrate()
        self.task_id = self.store.upsert_task("synthetic lifecycle")
        self.store.set_task_baseline(self.task_id, BASELINE)
        self.store.bind_task_contract(self.task_id, BASELINE, 1, CONTRACT, "{}")
        planning = self.root / ".stagemesh" / "planning"
        planning.mkdir(parents=True, exist_ok=True)
        self.write_plan(_spec())
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()
        (self.worktree / "candidate.txt").write_text("candidate evidence", encoding="utf-8")

    def write_plan(self, spec: dict) -> None:
        planning = self.root / ".stagemesh" / "planning"
        digest = freeze_planning_spec(spec).digest
        challenge = _challenge(digest)
        (planning / f"{self.task_id}.spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (planning / f"{self.task_id}.challenge.json").write_text(json.dumps(challenge), encoding="utf-8")
        self._receipt("planning.spec_produced", PLANNER, digest)
        self._receipt("planning.challenge_produced", CHALLENGER, freeze_planning_challenge(challenge).digest)

    def _receipt(self, event: str, provider: str, digest: str) -> None:
        execution = self.run(ExecutionKind.REVIEW, provider)
        self.store.add_audit_event(
            event,
            {
                "task_id": self.task_id,
                "artifact_digest": digest,
                "provider_invoked": True,
                "execution_id": execution,
                "provider": provider,
            },
        )

    def run(self, kind: ExecutionKind, actor: str, candidate_sha: str | None = None, status=ExecutionStatus.SUCCEEDED) -> str:
        execution = self.store.start_execution(task_id=self.task_id, claim_id=None, kind=kind, candidate_sha=candidate_sha)
        self.store.finish_execution(execution, status, candidate_sha, actor=actor)
        return execution

    def approve(self) -> dict:
        authority, require_challenge, refusal = protected_approval_policy(self.root)[0:3]
        assert refusal is None and authority is not None and require_challenge
        return record_planning_approval(
            self.store, self.root, self.task_id, authority=authority, require_challenge=require_challenge
        )

    def artifact_receipt(self) -> str:
        path = self.root / ".stagemesh" / "planning" / f"{self.task_id}.approval.json"
        return json.loads(path.read_text(encoding="utf-8"))["receipt_id"]

    def add_candidate(self, sha: str = CANDIDATE) -> None:
        self.store.add_candidate(self.task_id, sha, IMPLEMENTER, True)
        self.store.bind_contract(self.task_id, sha, BASELINE, 1, CONTRACT, "{}")

    def implement_and_review(self, sha: str = CANDIDATE, implementer: str = IMPLEMENTER, reviewer: str = REVIEWER) -> None:
        self.add_candidate(sha)
        self.run(ExecutionKind.IMPLEMENTATION, implementer, sha)
        self.run(ExecutionKind.REVIEW, reviewer, sha)

    def check(self, boundary: str, **kwargs):
        return check_accountable_approval(self.store, self.root, self.task_id, boundary, **kwargs)

    def snapshot(self) -> dict:
        conn = self.store.conn
        tables = ("tasks", "candidates", "executions", "contract_bindings", "task_contracts", "task_baselines")
        rows = {name: [dict(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")] for name in tables}
        rows["worktree"] = sorted(path.name for path in self.worktree.iterdir())
        rows["worktree_bytes"] = (self.worktree / "candidate.txt").read_bytes()
        return rows

    def events(self, event_type: str) -> list[dict]:
        return [json.loads(row["payload"]) for row in self.store.task_audit_events(event_type, self.task_id)]


@pytest.fixture
def lifecycle(tmp_path):
    synthetic = Lifecycle(tmp_path)
    synthetic.approve()
    yield synthetic
    synthetic.store.close()


def test_pickup_and_baseline_lifecycle_pass_with_current_approval(lifecycle) -> None:
    decision = lifecycle.check(BOUNDARY_PICKUP)
    assert decision is not None and decision.allowed
    assert lifecycle.events(APPROVAL_REFUSED_EVENT) == []


# required case 6
def test_initial_implementer_conflict_is_blocked_before_process_start(lifecycle) -> None:
    before = lifecycle.snapshot()
    for implementers in ((APPROVER,), (IMPLEMENTER, APPROVER)):
        decision = lifecycle.check(BOUNDARY_IMPLEMENTATION, implementers=implementers)
        _blocked(decision, APPROVAL_ROLE_CONFLICT)
        assert "implementer" in decision.detail
    assert lifecycle.check(BOUNDARY_IMPLEMENTATION, implementers=(IMPLEMENTER,)).allowed
    assert lifecycle.snapshot() == before  # no execution row exists: nothing was started
    assert lifecycle.store.active_execution_kinds(lifecycle.task_id) == set()


# required case 7
def test_fallback_and_resumed_implementer_conflicts_are_blocked_before_process_start(lifecycle) -> None:
    lifecycle.run(ExecutionKind.IMPLEMENTATION, IMPLEMENTER, status=ExecutionStatus.FAILED)  # the original attempt died
    before = lifecycle.snapshot()
    for boundary in (BOUNDARY_RECOVERY, BOUNDARY_IMPLEMENTATION):
        for pool in ((IMPLEMENTER, APPROVER), (APPROVER,)):  # fallback pool offers the approver as a replacement
            _blocked(lifecycle.check(boundary, implementers=pool), APPROVAL_ROLE_CONFLICT)
        assert lifecycle.check(boundary, implementers=(IMPLEMENTER,)).allowed
    assert lifecycle.snapshot() == before


# required case 8
def test_conflicting_reviewer_is_blocked_before_review_process(lifecycle) -> None:
    lifecycle.add_candidate()
    lifecycle.run(ExecutionKind.IMPLEMENTATION, IMPLEMENTER, CANDIDATE)
    before = lifecycle.snapshot()
    decision = lifecycle.check(BOUNDARY_REVIEW, candidate_sha=CANDIDATE, reviewers=(APPROVER,))
    _blocked(decision, APPROVAL_ROLE_CONFLICT)
    assert "reviewer" in decision.detail
    assert lifecycle.check(BOUNDARY_REVIEW, candidate_sha=CANDIDATE, reviewers=(REVIEWER,)).allowed
    assert lifecycle.snapshot() == before
    assert lifecycle.store.active_execution_kinds(lifecycle.task_id) == set()


class _CountingReviewAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def review(self, prompt: str) -> str:
        self.calls.append(prompt)
        return '{"decision":"PASS"}'


class _CountingFallbackReviewAdapter:
    records_provider_calls = True

    def __init__(self, *adapters: _CountingReviewAdapter) -> None:
        self.adapters = adapters
        self.name = adapters[0].name
        self.calls: list[str] = []

    def review_candidate(self, prompt: str, _project: Path, _candidate: str) -> str:
        self.calls.append(prompt)
        return '{"decision":"PASS"}'


def _accountable_review_project(tmp_path: Path) -> tuple[Path, Store, str]:
    project, store, candidate = _project_with_candidate(tmp_path)
    bound = contract_for_candidate(store, REVIEW_TASK, candidate, project)
    store.set_task_baseline(REVIEW_TASK, bound.baseline_sha)
    store.bind_contract(REVIEW_TASK, candidate, bound.baseline_sha, bound.version, bound.digest, bound.canonical_json)
    _write_config(project, _gate_config())
    planning = project / ".stagemesh" / "planning"
    planning.mkdir(parents=True, exist_ok=True)
    spec = {
        "behavior": ["review the candidate"],
        "non_goals": [],
        "affected_paths": ["README.md"],
        "acceptance_checks": ["synthetic review"],
        "decisions": [],
        "spec_refs": [],
        "contract_hash": bound.digest,
        "baseline_sha": bound.baseline_sha,
    }
    digest = freeze_planning_spec(spec).digest
    challenge = _challenge(digest)
    (planning / f"{REVIEW_TASK}.spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (planning / f"{REVIEW_TASK}.challenge.json").write_text(json.dumps(challenge), encoding="utf-8")
    for event, actor, artifact_digest in (
        ("planning.spec_produced", PLANNER, digest),
        ("planning.challenge_produced", CHALLENGER, freeze_planning_challenge(challenge).digest),
    ):
        execution = store.start_execution(task_id=REVIEW_TASK, claim_id=None, kind=ExecutionKind.REVIEW)
        store.finish_execution(execution, ExecutionStatus.SUCCEEDED, actor=actor)
        store.add_audit_event(
            event,
            {
                "task_id": REVIEW_TASK,
                "artifact_digest": artifact_digest,
                "provider_invoked": True,
                "execution_id": execution,
                "provider": actor,
            },
        )
    authority, require_challenge, refusal = protected_approval_policy(project)
    assert authority is not None and require_challenge and refusal is None
    record_planning_approval(store, project, REVIEW_TASK, authority=authority, require_challenge=True)
    implementation = store.start_execution(
        task_id=REVIEW_TASK,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        candidate_sha=candidate,
        actor=IMPLEMENTER,
    )
    store.finish_execution(implementation, ExecutionStatus.SUCCEEDED, candidate, actor=IMPLEMENTER)
    return project, store, candidate


def _review_side_effect_counts(store: Store) -> tuple[int, int, int, int]:
    executions = store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    evidence = store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    findings = store.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    provider_calls = store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='provider.call'").fetchone()[0]
    return executions, evidence, findings, provider_calls


def test_reviewer_runtime_refuses_conflicting_initial_provider_without_side_effects(tmp_path) -> None:
    project, store, candidate = _accountable_review_project(tmp_path)
    adapter = _CountingReviewAdapter(APPROVER)
    before = _review_side_effect_counts(store)
    try:
        assert Reviewer(adapter=adapter, require_independent=True).review(store, REVIEW_TASK, candidate, project) == EvidenceStatus.CAPACITY
        assert _review_side_effect_counts(store) == before
        assert adapter.calls == []
    finally:
        store.close()


def test_reviewer_runtime_prechecks_every_fallback_without_side_effects(tmp_path) -> None:
    project, store, candidate = _accountable_review_project(tmp_path)
    first = _CountingReviewAdapter(REVIEWER)
    conflicting = _CountingReviewAdapter(APPROVER)
    fallback = ScopedReviewAdapter(_CountingFallbackReviewAdapter(first, conflicting), object())
    before = _review_side_effect_counts(store)
    try:
        assert Reviewer(adapter=fallback, require_independent=True).review(store, REVIEW_TASK, candidate, project) == EvidenceStatus.CAPACITY
        assert _review_side_effect_counts(store) == before
        assert fallback.inner.calls == [] and first.calls == [] and conflicting.calls == []
    finally:
        store.close()


def test_missing_reviewer_identity_blocks_review_dispatch(lifecycle) -> None:
    lifecycle.implement_and_review()
    reviewless = lifecycle.check(BOUNDARY_REVIEW, candidate_sha=CANDIDATE, required_roles=("implementer", "reviewer"))
    assert reviewless is not None and reviewless.allowed  # the candidate's own successful review identifies the reviewer
    lifecycle.add_candidate(DERIVED)
    lifecycle.run(ExecutionKind.IMPLEMENTATION, IMPLEMENTER, DERIVED)
    _blocked(
        lifecycle.check(BOUNDARY_REVIEW, candidate_sha=DERIVED, required_roles=("implementer", "reviewer")),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )


# required case 11
def _mutate_spec(lifecycle) -> None:
    lifecycle.write_plan({**_spec(), "behavior": ["a different frozen behavior"]})


def _mutate_challenge(lifecycle) -> None:
    path = lifecycle.root / ".stagemesh" / "planning" / f"{lifecycle.task_id}.challenge.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["challenger_provider"] = "another-challenger"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _mutate_authority(lifecycle) -> None:
    _write_config(lifecycle.root, _gate_config("owner:replacement"))


STALE_REASONS = {APPROVAL_BINDING_STALE, APPROVAL_EXECUTION_IDENTITY_MISSING}


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (_mutate_spec, STALE_REASONS),
        (_mutate_challenge, STALE_REASONS),
        (_mutate_authority, {APPROVAL_AUTHORITY_STALE}),
    ],
)
@pytest.mark.parametrize("boundary", [BOUNDARY_RECOVERY, BOUNDARY_PROMOTION])
def test_changed_plan_challenge_or_authority_blocks_recovery_and_promotion(lifecycle, mutation, expected, boundary) -> None:
    lifecycle.implement_and_review()
    kwargs = {"candidate_sha": CANDIDATE} if boundary == BOUNDARY_PROMOTION else {"implementers": (IMPLEMENTER,)}
    assert lifecycle.check(boundary, **kwargs).allowed
    mutation(lifecycle)
    decision = lifecycle.check(boundary, **kwargs)
    assert not decision.allowed
    assert decision.reason in expected
    assert decision.next_action is not None and "digest by hand" not in decision.detail


@pytest.mark.parametrize("boundary", [BOUNDARY_RECOVERY, BOUNDARY_PROMOTION])
def test_changed_contract_or_baseline_blocks_recovery_and_promotion(lifecycle, boundary) -> None:
    lifecycle.implement_and_review()
    kwargs = {"candidate_sha": CANDIDATE} if boundary == BOUNDARY_PROMOTION else {"implementers": (IMPLEMENTER,)}
    for override in ({"contract_hash": "d" * 64}, {"baseline_sha": "e" * 40}):
        _blocked(lifecycle.check(boundary, **kwargs, **override), APPROVAL_BINDING_STALE)


def test_promotion_with_rebound_candidate_contract_is_stale(lifecycle) -> None:
    lifecycle.implement_and_review()
    lifecycle.store.conn.execute(
        "UPDATE contract_bindings SET digest=? WHERE task_id=? AND candidate_sha=?",
        ("d" * 64, lifecycle.task_id, CANDIDATE),
    )
    lifecycle.store.conn.commit()
    _blocked(lifecycle.check(BOUNDARY_PROMOTION, candidate_sha=CANDIDATE), APPROVAL_BINDING_STALE)
    assert promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root) == APPROVAL_BINDING_STALE


# required case 12
def test_candidate_configuration_cannot_change_the_evaluating_authority(lifecycle) -> None:
    lifecycle.implement_and_review()
    candidate_config = lifecycle.worktree / ".stagemesh"
    candidate_config.mkdir()
    (candidate_config / "config.json").write_text(
        json.dumps({"planning_gate": _gate_config("owner:candidate-chosen")}), encoding="utf-8"
    )
    protected = protected_approval_policy(lifecycle.root)[0]
    assert protected == _authority()
    assert load_config(lifecycle.worktree).approval_authority.approver_id == "owner:candidate-chosen"
    # No boundary reads the candidate's copy: the protected project root still evaluates every approval.
    for boundary, kwargs in (
        (BOUNDARY_INTEGRATION, {"candidate_sha": CANDIDATE}),
        (BOUNDARY_PROMOTION, {"candidate_sha": CANDIDATE}),
    ):
        decision = lifecycle.check(boundary, **kwargs)
        assert decision.allowed and decision.receipt_id == lifecycle.artifact_receipt()
    assert protected_approval_policy(lifecycle.root)[0] == _authority()
    assert promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root) is None


# required case 13
def test_provider_execution_cannot_invoke_approval_or_forge_it(tmp_path, monkeypatch) -> None:
    fixture = Lifecycle(tmp_path)
    try:
        before = fixture.snapshot()
        events_before = len(fixture.store.planning_approval_receipts(fixture.task_id))
        monkeypatch.setenv("STAGEMESH_PROVIDER_PROCESS", "1")
        with pytest.raises(PlanningApprovalRefusal) as excinfo:
            fixture.approve()
        assert excinfo.value.code == "operator_decision"
        monkeypatch.delenv("STAGEMESH_PROVIDER_PROCESS")
        assert len(fixture.store.planning_approval_receipts(fixture.task_id)) == events_before == 0
        approval_path = fixture.root / ".stagemesh" / "planning" / f"{fixture.task_id}.approval.json"
        assert not approval_path.exists()
        assert fixture.snapshot() == before
        # A provider that hand-writes a self-consistent artifact still has no durable receipt.
        authority = _authority()
        forged = {
            "version": 2,
            "decision": "approve",
            "task_id": fixture.task_id,
            "planning_spec_hash": freeze_planning_spec(_spec()).digest,
            "planning_challenge_hash": freeze_planning_challenge(
                json.loads((fixture.root / ".stagemesh" / "planning" / f"{fixture.task_id}.challenge.json").read_text("utf-8"))
            ).digest,
            "contract_hash": CONTRACT,
            "baseline_sha": BASELINE,
            "authority_id": authority.authority_id,
            "authority_hash": authority.digest,
            "approver_id": authority.approver_id,
            "receipt_id": "approval-forged",
        }
        approval_path.write_text(json.dumps(forged), encoding="utf-8")
        for boundary in (BOUNDARY_PICKUP, BOUNDARY_IMPLEMENTATION):
            _blocked(fixture.check(boundary, implementers=(IMPLEMENTER,)), APPROVAL_RECEIPT_MISSING)
    finally:
        fixture.store.close()


# required case 14
@pytest.mark.parametrize(
    "boundary,kwargs",
    [
        (BOUNDARY_IMPLEMENTATION, {"implementers": (APPROVER,)}),
        (BOUNDARY_RECOVERY, {"implementers": (APPROVER,)}),
        (BOUNDARY_REVIEW, {"candidate_sha": CANDIDATE, "reviewers": (APPROVER,)}),
        (BOUNDARY_INTEGRATION, {"candidate_sha": CANDIDATE, "implementers": (APPROVER,)}),
        (BOUNDARY_PROMOTION, {"candidate_sha": CANDIDATE, "implementers": (APPROVER,)}),
    ],
)
def test_refusal_preserves_task_candidate_execution_and_worktree_evidence(lifecycle, boundary, kwargs) -> None:
    lifecycle.implement_and_review()
    lifecycle.run(ExecutionKind.IMPLEMENTATION, IMPLEMENTER, status=ExecutionStatus.FAILED)
    before = lifecycle.snapshot()
    receipts_before = lifecycle.store.planning_approval_receipts(lifecycle.task_id)
    decision = lifecycle.check(boundary, **kwargs)
    _blocked(decision, APPROVAL_ROLE_CONFLICT)
    assert lifecycle.snapshot() == before
    assert lifecycle.store.planning_approval_receipts(lifecycle.task_id) == receipts_before
    assert lifecycle.events(APPROVAL_PROMOTION_EVENT) == []
    refusals = lifecycle.events(APPROVAL_REFUSED_EVENT)
    assert refusals and refusals[-1]["boundary"] == boundary and refusals[-1]["reason"] == APPROVAL_ROLE_CONFLICT
    lifecycle.check(boundary, **kwargs)  # an identical standing refusal is not duplicated
    assert len(lifecycle.events(APPROVAL_REFUSED_EVENT)) == len(refusals)
    assert lifecycle.snapshot() == before


def test_promotion_refusal_returns_reason_and_records_no_evidence(lifecycle) -> None:
    lifecycle.implement_and_review(reviewer=APPROVER)
    before = lifecycle.snapshot()
    problem = promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root, record=True)
    assert problem == APPROVAL_ROLE_CONFLICT
    assert lifecycle.events(APPROVAL_PROMOTION_EVENT) == []
    assert lifecycle.snapshot() == before


# inherited implementer lineage
def test_inherited_implementer_lineage_survives_candidate_derivation(lifecycle) -> None:
    lifecycle.implement_and_review(CANDIDATE, implementer=APPROVER)
    lifecycle.implement_and_review(DERIVED, implementer=IMPLEMENTER)
    for boundary in (BOUNDARY_REVIEW, BOUNDARY_INTEGRATION, BOUNDARY_PROMOTION):
        assert lifecycle.check(boundary, candidate_sha=DERIVED).allowed  # unrelated candidates are independent
    lifecycle.store.add_audit_event(
        "candidate.implementation_identity_derived",
        {"task_id": lifecycle.task_id, "kind": "rebase", "candidate_sha": DERIVED, "from_candidate": CANDIDATE},
    )
    for boundary in (BOUNDARY_REVIEW, BOUNDARY_INTEGRATION, BOUNDARY_PROMOTION):
        decision = lifecycle.check(boundary, candidate_sha=DERIVED)
        _blocked(decision, APPROVAL_ROLE_CONFLICT)
        assert "implementer" in decision.detail
    assert promotion_approval_problem(lifecycle.store, lifecycle.task_id, DERIVED, lifecycle.root) == APPROVAL_ROLE_CONFLICT


@pytest.mark.parametrize("event_type", ["integration.rebased", "integration.reattributed", "integration.reconstructed"])
def test_inherited_implementer_lineage_survives_trusted_integration_event(lifecycle, event_type) -> None:
    lifecycle.implement_and_review(CANDIDATE, implementer=APPROVER)
    lifecycle.implement_and_review(DERIVED, implementer=IMPLEMENTER)
    lifecycle.store.add_audit_event(
        event_type,
        {"task_id": lifecycle.task_id, "candidate_sha": DERIVED, "from_candidate": CANDIDATE},
    )
    _blocked(lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=DERIVED), APPROVAL_ROLE_CONFLICT)


def test_malformed_and_ambiguous_trusted_lineage_edges_fail_closed(lifecycle) -> None:
    lifecycle.implement_and_review(CANDIDATE, implementer=APPROVER)
    lifecycle.implement_and_review(DERIVED, implementer=IMPLEMENTER)
    lifecycle.store.add_audit_event(
        "integration.rebased",
        {"task_id": lifecycle.task_id, "candidate_sha": DERIVED, "from_candidate": "not-a-sha"},
    )
    assert lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=DERIVED).allowed
    lifecycle.store.add_audit_event(
        "integration.rebased",
        {"task_id": lifecycle.task_id, "candidate_sha": DERIVED, "from_candidate": CANDIDATE},
    )
    lifecycle.store.add_audit_event(
        "integration.reconstructed",
        {"task_id": lifecycle.task_id, "candidate_sha": DERIVED, "from_candidate": "e" * 40},
    )
    _blocked(
        lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=DERIVED),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )


def test_cyclic_trusted_lineage_fails_closed(lifecycle) -> None:
    lifecycle.implement_and_review(CANDIDATE, implementer=IMPLEMENTER)
    lifecycle.implement_and_review(DERIVED, implementer=IMPLEMENTER)
    for child, parent in ((DERIVED, CANDIDATE), (CANDIDATE, DERIVED)):
        lifecycle.store.add_audit_event(
            "integration.rebased",
            {"task_id": lifecycle.task_id, "candidate_sha": child, "from_candidate": parent},
        )
    _blocked(
        lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=DERIVED),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )


def test_excessive_trusted_lineage_fails_closed(lifecycle) -> None:
    lifecycle.implement_and_review(DERIVED, implementer=IMPLEMENTER)
    child = DERIVED
    for number in range(33):
        parent = f"{number:040x}"
        lifecycle.store.add_audit_event(
            "integration.reconstructed",
            {"task_id": lifecycle.task_id, "candidate_sha": child, "from_candidate": parent},
        )
        child = parent
    _blocked(
        lifecycle.check(BOUNDARY_PROMOTION, candidate_sha=DERIVED),
        APPROVAL_EXECUTION_IDENTITY_MISSING,
    )


def test_derivation_through_a_chain_of_candidates_keeps_the_original_implementer(lifecycle) -> None:
    middle = "e" * 40
    lifecycle.implement_and_review(CANDIDATE, implementer=APPROVER)
    lifecycle.implement_and_review(middle)
    lifecycle.implement_and_review(DERIVED)
    for child, parent in ((middle, CANDIDATE), (DERIVED, middle)):
        lifecycle.store.add_audit_event(
            "candidate.implementation_identity_derived",
            {"task_id": lifecycle.task_id, "kind": "reconstruction", "candidate_sha": child, "from_candidate": parent},
        )
    _blocked(lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=DERIVED), APPROVAL_ROLE_CONFLICT)


# required case 16
def test_complete_distinct_actor_flow_reaches_every_gate_with_accepted_receipt_and_exact_sha(lifecycle) -> None:
    receipt_id = lifecycle.artifact_receipt()
    assert lifecycle.check(BOUNDARY_PICKUP).allowed
    assert lifecycle.check(BOUNDARY_IMPLEMENTATION, implementers=(IMPLEMENTER,)).allowed
    lifecycle.add_candidate()
    lifecycle.run(ExecutionKind.IMPLEMENTATION, IMPLEMENTER, CANDIDATE)
    assert lifecycle.check(BOUNDARY_REVIEW, candidate_sha=CANDIDATE, reviewers=(REVIEWER,)).allowed
    lifecycle.run(ExecutionKind.REVIEW, REVIEWER, CANDIDATE)
    integration = lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=CANDIDATE)
    assert integration.allowed and integration.receipt_id == receipt_id
    assert promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root, record=True) is None
    assert lifecycle.events(APPROVAL_PROMOTION_EVENT) == [
        {"task_id": lifecycle.task_id, "candidate_sha": CANDIDATE, "receipt_id": receipt_id, "boundary": BOUNDARY_PROMOTION}
    ]
    promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root, record=True)
    assert len(lifecycle.events(APPROVAL_PROMOTION_EVENT)) == 1  # identical evidence is not duplicated
    assert lifecycle.events(APPROVAL_REFUSED_EVENT) == []
    # No bypass: withdrawing the artifact or its receipt re-blocks the very next gate.
    approval_path = lifecycle.root / ".stagemesh" / "planning" / f"{lifecycle.task_id}.approval.json"
    approval_path.unlink()
    assert promotion_approval_problem(lifecycle.store, lifecycle.task_id, CANDIDATE, lifecycle.root) == APPROVAL_MISSING
    _blocked(lifecycle.check(BOUNDARY_INTEGRATION, candidate_sha=CANDIDATE), APPROVAL_MISSING)


def test_legacy_project_without_authority_is_never_refused_at_any_boundary(tmp_path) -> None:
    fixture = Lifecycle(tmp_path)
    try:
        _write_config(fixture.root, {"require_approved_spec": True})
        fixture.implement_and_review(implementer=APPROVER, reviewer=APPROVER)
        for boundary in (BOUNDARY_PICKUP, BOUNDARY_IMPLEMENTATION, BOUNDARY_REVIEW, BOUNDARY_INTEGRATION, BOUNDARY_PROMOTION):
            assert fixture.check(boundary, candidate_sha=CANDIDATE, implementers=(APPROVER,)) is None
        assert promotion_approval_problem(fixture.store, fixture.task_id, CANDIDATE, fixture.root, record=True) is None
        assert fixture.events(APPROVAL_PROMOTION_EVENT) == [] and fixture.events(APPROVAL_REFUSED_EVENT) == []
    finally:
        fixture.store.close()
