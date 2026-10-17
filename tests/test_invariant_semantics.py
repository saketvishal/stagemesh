from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.contracts import (
    ChangeContract,
    ContractError,
    GateCommand,
    canonical_contract_json,
    contract_prompt,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.validation_plan import derive_validation_plan, planned_contract, with_required_checks

PROSE = (
    "review: Preserve the existing API envelope.",
    "review: Explain separate admission and execution states.",
    "review: Command examples include --help without changing runtime configuration.",
    "review: Do not change the user's authentication settings.",
    "review: Backward-compatible behavior must remain unchanged.",
    "review: keep public names stable",
)


def candidate(tmp_path: Path, invariants: tuple[str, ...]):
    project = tmp_path / "project"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test Owner")
    (project / ".gitignore").write_text(".stagemesh/\n", encoding="utf-8")
    (project / "README.md").write_text("original\n", encoding="utf-8")
    (project / "check_docs.py").write_text(
        "from pathlib import Path\nassert Path('README.md').read_text() == 'updated docs\\n'\n", encoding="utf-8",
    )
    (project / "fail_invariant.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    git.commit_all("baseline")
    store = Store(project / ".stagemesh/state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("Update supporting documentation", source_id="DOCS")
    contract_dir = project / ".stagemesh/contracts"
    contract_dir.mkdir(parents=True)
    payload = {
        "objective": "Update supporting documentation",
        "allowed_files": ["README.md"],
        "invariants": list(invariants),
        "required_tests": [{"name": "docs-static", "command": [sys.executable, "check_docs.py"]}],
    }
    (contract_dir / f"{task_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    (project / "README.md").write_text("updated docs\n", encoding="utf-8")
    sha = git.commit_all("documentation update")
    store.add_candidate(task_id, sha, "implementer", True)
    return project, store, task_id, sha


def evidence(store, task_id, sha, kind):
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC LIMIT 1",
        (task_id, sha, kind),
    ).fetchone()
    return json.loads(row["payload"])


def test_prose_invariants_are_not_commands_or_planned_checks():
    from stagemesh.contracts import declarative_invariants, invariant_gates

    contract = ChangeContract(objective="Docs", allowed_files=("README.md",), invariants=PROSE,
                              required_tests=(GateCommand("docs-static", ["python", "check_docs.py"]),))
    before = canonical_contract_json(contract)
    plan = with_required_checks(contract, derive_validation_plan(contract, ("README.md",)))
    assert plan.planned_checks == ("docs-static",)
    assert invariant_gates(contract) == ()
    assert declarative_invariants(contract) == PROSE
    assert planned_contract(contract, plan).invariants == PROSE
    assert canonical_contract_json(contract) == before
    assert all(text in contract_prompt(contract) for text in PROSE)


def test_validator_executes_real_gates_not_prose(tmp_path):
    project, store, task_id, sha = candidate(tmp_path, PROSE)
    try:
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
        data = evidence(store, task_id, sha, EvidenceKind.VALIDATION)
        assert data["review_invariants"] == list(PROSE)
        assert data["validation_checks"] == {"planned": ["docs-static"], "executed": ["docs-static"], "missing": []}
        assert data["gates"][0]["returncode"] == 0
    finally:
        store.close()


@pytest.mark.parametrize("command", ["python fail_invariant.py", "command: python fail_invariant.py", "missing-invariant-check --strict"])
def test_executable_invariant_failure_is_not_silently_dropped(tmp_path, command):
    project, store, task_id, sha = candidate(tmp_path, PROSE + (command,))
    try:
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
        data = evidence(store, task_id, sha, EvidenceKind.VALIDATION)
        assert data["validation_checks"]["missing"] == []
        gate = next(gate for gate in data["gates"] if gate["name"] == f"invariant:{command}")
        assert gate["status"] == "FAILED"
        assert gate["returncode"] == (None if command.startswith("missing") else 7)
    finally:
        store.close()


@pytest.mark.parametrize("text", ["command:", "review:"])
def test_empty_explicit_invariant_is_rejected(text):
    from stagemesh.contracts import invariant_gates

    with pytest.raises(ContractError):
        invariant_gates(ChangeContract(objective="Invalid", invariants=(text,)))


@pytest.mark.parametrize("text", [
    "check invariants", 'check "unterminated', "custom-validator positional", "Semgrep scan .",
    'Check "unterminated argument.', "Preserve the existing API envelope.",
    "Backward-compatible behavior must remain unchanged.",
])
def test_ambiguous_legacy_invariant_fails_closed(text):
    from stagemesh.contracts import invariant_gates

    with pytest.raises(ContractError, match="ambiguous invariant"):
        invariant_gates(ChangeContract(objective="Ambiguous", invariants=(text,)))


def test_ambiguous_frozen_prose_fails_as_contract_error_not_executable(tmp_path):
    project, store, task_id, sha = candidate(tmp_path, ("Preserve the existing API envelope.",))
    try:
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
        data = evidence(store, task_id, sha, EvidenceKind.VALIDATION)
        assert data["findings"][0]["code"] == "invalid_contract"
        assert "ambiguous invariant" in data["findings"][0]["message"]
        assert "gates" not in data
    finally:
        store.close()


def test_prose_constraints_require_actual_review_and_reach_reviewer(tmp_path):
    project, store, task_id, sha = candidate(tmp_path, PROSE)

    class Adapter:
        name = "independent-reviewer"
        prompt = ""

        def review(self, prompt):
            self.prompt = prompt
            return '{"decision":"PASS"}'

    try:
        assert Reviewer().review(store, task_id, sha, project) is EvidenceStatus.CAPACITY
        unavailable = evidence(store, task_id, sha, EvidenceKind.REVIEW)
        assert unavailable["review_infrastructure_failure"] == "invariant_review_unavailable"
        adapter = Adapter()
        assert Reviewer(adapter=adapter, require_independent=True).review(store, task_id, sha, project) is EvidenceStatus.PASSED
        assert all(text in adapter.prompt for text in PROSE)
        reviewed = evidence(store, task_id, sha, EvidenceKind.REVIEW)
        assert reviewed["review_invariants"] == list(PROSE)
        assert reviewed["independent_reviewer"] is True
        assert reviewed["review_execution_invoked"] is True
    finally:
        store.close()


@pytest.mark.parametrize("matching_history", [True, False])
def test_repaired_legacy_contract_needs_fresh_validation_and_independent_review(tmp_path, matching_history):
    from stagemesh.contracts import bound_contract_from_record
    from stagemesh.recovery import rebind_contract
    from stagemesh.remediation import finding_identity
    from stagemesh.domain import Stage

    original = "Preserve the existing public API behavior."
    project, store, task_id, sha = candidate(tmp_path, (original,))
    try:
        store.advance_task(task_id, Stage.VALIDATE)
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
        binding = dict(store.contract_binding(task_id, sha))
        store.set_task_baseline(task_id, binding["baseline_sha"])
        store.bind_task_contract(task_id, binding["baseline_sha"], binding["version"], binding["digest"], binding["canonical_json"])
        invalid_message = evidence(store, task_id, sha, EvidenceKind.VALIDATION)["findings"][0]["message"]
        invalid_id = finding_identity(sha, invalid_message)
        real_id = finding_identity(sha, "Real review finding must remain open")
        store.upsert_finding(real_id, task_id, sha, "error", "Real review finding must remain open")
        legacy_message = f"invariant:{original} failed"
        legacy_id = finding_identity(sha, legacy_message)
        store.upsert_finding(legacy_id, task_id, sha, "error", legacy_message)
        store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {
            "contract_hash": binding["digest"] if matching_history else "unrelated-contract",
            "findings": [{"code": "gate_failed", "message": legacy_message, "command": original.split(),
                          "returncode": None, "failure_kind": "environment"}],
        })
        report = rebind_contract(store, project, task_id, review_invariants=(1,), reason="Keep the legacy requirement under explicit review")
        assert store.get_finding(invalid_id)["status"] == "OPEN"
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
        assert store.get_finding(invalid_id)["status"] == "RESOLVED"
        assert store.get_finding(real_id)["status"] == "OPEN"
        assert Reviewer().review(store, task_id, sha, project) is EvidenceStatus.CAPACITY
        assert store.get_finding(legacy_id)["status"] == "OPEN"
        bound = bound_contract_from_record(dict(store.contract_binding(task_id, sha)))
        assert bound.digest == report["new_digest"] != binding["digest"]
        assert bound.contract.invariants == ("review: " + original,)
        assert evidence(store, task_id, sha, EvidenceKind.VALIDATION)["review_invariants"] == ["review: " + original]
        assert not store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.PASSED)

        class Adapter:
            name = "independent-reviewer"

            def review(self, prompt):
                assert "review: " + original in prompt
                return '{"decision":"PASS"}'

        assert Reviewer(adapter=Adapter(), require_independent=True).review(store, task_id, sha, project) is EvidenceStatus.PASSED
        assert store.get_finding(legacy_id)["status"] == ("RESOLVED" if matching_history else "OPEN")
        assert store.get_finding(real_id)["status"] == "OPEN"
    finally:
        store.close()
