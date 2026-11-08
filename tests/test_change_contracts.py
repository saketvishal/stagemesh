from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from stagemesh.agent_policy import (
    AgentPolicyError,
    agent_policy_prompt,
    load_effective_agent_policy,
)
from stagemesh.candidate_inspection import inspect_candidate_diff
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.contracts import (
    ChangeContract,
    ContractError,
    GateCommand,
    _gate_runtime_command,
    canonical_contract_json,
    contract_admission_problem,
    contract_prompt,
    evaluate_contract,
    parse_contract,
    run_gate,
    scope_classification,
    scope_policy_hash,
)
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import ExecutionResult, Executor, SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.providers import _build_task_prompt
from stagemesh.remediation import RemediationPolicy
from stagemesh.review import Reviewer
from stagemesh.validation import Validator
from stagemesh.workspaces import task_workspace


def _repo(path: Path) -> GitWorkspace:
    path.mkdir()
    workspace = GitWorkspace(path)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    workspace.commit_all("initial")
    return workspace


def _store(path: Path) -> Store:
    store = Store(path / "state.sqlite3")
    store.migrate()
    return store


class SequencedExecutor(Executor):
    name = "implementer"

    def __init__(self, project: Path, values: list[str]):
        self.project = project
        self.values = values
        self.calls = 0

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        self.calls += 1
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        value = self.values[min(self.calls - 1, len(self.values) - 1)]
        (self.project / "src" / "app.py").write_text(value, encoding="utf-8")
        sha = GitWorkspace(self.project).commit_all(f"candidate {self.calls}")
        store.add_candidate(task_id, sha, self.name, True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class RecordingReviewAdapter:
    def __init__(self, name: str = "reviewer", response: str = '{"decision":"PASS"}') -> None:
        self.name = name
        self.response = response
        self.calls = 0
        self.prompts: list[str] = []

    def review(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return self.response


def test_python_gate_uses_recorded_path_runtime_and_candidate_src(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / "src").mkdir(parents=True)
    (project / "src" / "probe_runtime.py").write_text('VALUE = "candidate-src"\n', encoding="utf-8")

    gate = run_gate(
        project,
        GateCommand(
            "python-runtime",
            [
                "python",
                "-c",
                "import probe_runtime, sys; print(sys.executable); print(probe_runtime.VALUE)",
            ],
            timeout_seconds=5,
        ),
    )

    assert gate.status == "PASSED", gate.stderr
    assert gate.resolution is not None
    assert gate.resolution["method"] == "path-lookup"
    assert Path(str(gate.resolution["executed"])).resolve() == Path(gate.stdout.splitlines()[0]).resolve()
    assert "candidate-src" in gate.stdout


def test_python_gate_resolves_from_path_not_stagemesh_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        "stagemesh.contracts.shutil.which",
        lambda command: "C:/project/.venv/Scripts/python.exe" if command == "python" else None,
    )

    command = _gate_runtime_command(["python", "-m", "pytest"])

    assert command == ["C:/project/.venv/Scripts/python.exe", "-m", "pytest"]


def test_contract_rejects_forbidden_and_out_of_scope_files(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "README.md").write_text("changed\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    contract = ChangeContract(
        objective="Only change application code",
        allowed_files=("src/**",),
        forbidden_files=("README.md",),
    )
    result = evaluate_contract(project, sha, contract, run_gates=False)

    assert result.status == "FAILED"
    assert "README.md" in result.changed_files
    assert {finding["code"] for finding in result.findings} >= {
        "outside_allowed_files",
        "forbidden_file_changed",
    }
    assert scope_classification(contract, result.changed_files) == {
        "dependency_manifest_without_gate": [],
        "excluded": [],
        "forbidden": ["README.md"],
        "in_scope": ["src/app.py"],
        "noise": [],
        "outside_allowed": ["README.md"],
        "protected": [],
    }


def _atomic_scope_repo(path: Path) -> tuple[GitWorkspace, str]:
    workspace = _repo(path)
    (path / "src" / "app.py").write_text(
        "VALUE = 1\n\ndef target(value: int) -> int:\n    return value + 1\n\ndef untouched() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    return workspace, workspace.commit_all("atomic scope baseline")


def _strict_scope(**changes: object) -> ChangeContract:
    base = ChangeContract(
        objective="Change one function",
        allowed_files=("src/app.py",),
        scope_mode="STRICT",
        allowed_read_files=("src/**", "tests/**"),
        allowed_symbols=(("src/app.py", ("target",)),),
        scope_owner="operator",
        scope_revision=1,
    )
    return replace(base, **changes)


def test_strict_atomic_scope_allows_only_the_authorized_python_symbol(tmp_path: Path) -> None:
    workspace, baseline = _atomic_scope_repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text(
        "VALUE = 1\n\ndef target(value: int) -> int:\n    return value + 2\n\ndef untouched() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    candidate = workspace.commit_all("authorized symbol")
    contract = _strict_scope()

    result = evaluate_contract(project, candidate, contract, baseline_sha=baseline, run_gates=False)

    assert result.passed
    assert scope_policy_hash(contract) in contract_prompt(contract)
    assert "Allowed read paths: src/**, tests/**" in contract_prompt(contract)


def test_strict_atomic_scope_blocks_an_edit_outside_the_authorized_symbol(tmp_path: Path) -> None:
    workspace, baseline = _atomic_scope_repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text(
        "VALUE = 9\n\ndef target(value: int) -> int:\n    return value + 2\n\ndef untouched() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    candidate = workspace.commit_all("scope bleed")

    result = evaluate_contract(project, candidate, _strict_scope(), baseline_sha=baseline, run_gates=False)

    assert result.status == "FAILED"
    assert any(item["code"] == "scope_symbol_verification_required" for item in result.findings)


def test_atomic_scope_classifies_generated_files_and_approved_expansion(tmp_path: Path) -> None:
    contract = _strict_scope(
        allowed_generated_files=("build/report.json",),
        approved_scope_expansions=("tests/test_app.py",),
        scope_exceptions=("revision 2 approved the focused regression test",),
        scope_revision=2,
    )

    assert scope_classification(contract, ["src/app.py", "build/report.json", "tests/test_app.py", "README.md"]) == {
        "approved_expansion": ["tests/test_app.py"],
        "dependency_manifest_without_gate": [],
        "excluded": [],
        "forbidden": [],
        "generated": ["build/report.json"],
        "in_scope": ["build/report.json", "src/app.py", "tests/test_app.py"],
        "noise": [],
        "outside_allowed": ["README.md"],
        "protected": [],
    }


def test_atomic_scope_can_explicitly_forbid_new_python_imports(tmp_path: Path) -> None:
    workspace, baseline = _atomic_scope_repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text(
        "VALUE = 1\n\ndef target(value: int) -> int:\n    import math\n    return value + math.floor(2.2)\n\ndef untouched() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    candidate = workspace.commit_all("unapproved import")

    result = evaluate_contract(
        project,
        candidate,
        _strict_scope(forbid_new_imports=True),
        baseline_sha=baseline,
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(item["code"] == "scope_new_import_forbidden" for item in result.findings)


def test_atomic_scope_read_only_mode_blocks_all_candidate_writes(tmp_path: Path) -> None:
    workspace, baseline = _atomic_scope_repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    candidate = workspace.commit_all("write in read-only scope")

    result = evaluate_contract(
        project,
        candidate,
        _strict_scope(scope_mode="EXPLORATORY", scope_write_enabled=False, allowed_symbols=()),
        baseline_sha=baseline,
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(item["code"] == "scope_write_not_authorized" for item in result.findings)


def test_atomic_scope_schema_round_trips_without_changing_legacy_contracts() -> None:
    legacy = ChangeContract(objective="legacy")
    assert "scope_mode" not in json.loads(canonical_contract_json(legacy))
    parsed = parse_contract(
        {
            "objective": "bounded",
            "allowed_files": ["src/**"],
            "scope_mode": "bounded",
            "scope_owner": "operator",
            "scope_revision": 1,
            "allowed_symbols": {"src/app.py": ["target"]},
        }
    )
    assert parsed.scope_mode == "BOUNDED"
    assert parsed.allowed_symbols == (("src/app.py", ("target",)),)
    assert contract_admission_problem(parsed) is None
    assert "STRICT atomic scope requires explicit writable files" in str(
        contract_admission_problem(_strict_scope(allowed_files=("src/**",)))
    )


def test_validation_evidence_binds_atomic_scope_to_task_baseline_and_candidate(tmp_path: Path) -> None:
    workspace, baseline = _atomic_scope_repo(tmp_path / "repo")
    project = workspace.path
    contract = _strict_scope(explicit=True, required_tests=(GateCommand("smoke", [sys.executable, "-c", "pass"]),))
    (project / "stagemesh.contract.json").write_text(canonical_contract_json(contract), encoding="utf-8")
    baseline = workspace.commit_all("atomic scope contract")
    (project / "src" / "app.py").write_text(
        "VALUE = 1\n\ndef target(value: int) -> int:\n    return value + 2\n\ndef untouched() -> int:\n    return VALUE\n",
        encoding="utf-8",
    )
    candidate = workspace.commit_all("atomic candidate")
    store = _store(tmp_path)
    task_id = store.upsert_task("atomic validation")
    store.set_task_baseline(task_id, baseline)
    store.add_candidate(task_id, candidate, "implementer", True)
    try:
        assert Validator().validate(store, task_id, candidate, project) is EvidenceStatus.PASSED
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=?",
            (task_id, candidate, EvidenceKind.VALIDATION),
        ).fetchone()
        policy = json.loads(row["payload"])["scope_policy"]
        assert policy["task_id"] == task_id
        assert policy["baseline_sha"] == baseline
        assert policy["candidate_sha"] == candidate
        assert policy["policy_hash"] == scope_policy_hash(contract)
        assert policy["mode"] == "STRICT"
    finally:
        store.close()


def _agent_policy_repo(path: Path) -> tuple[GitWorkspace, str]:
    workspace = _repo(path)
    policy_path = path / ".stagemesh" / "agent-policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text(
        json.dumps(
            {
                "schema": "stagemesh.agent-coding-policy/1",
                "policy_id": "test-policy",
                "approved_by": "test-owner",
                "constraints": {
                    "no_unrequested_abstractions": True,
                    "no_speculative_code": True,
                    "no_filler_comments": True,
                    "no_unsolicited_documentation_churn": True,
                    "no_policy_evasion": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return workspace, workspace.commit_all("agent policy baseline")


def test_agent_policy_is_provider_neutral_and_hash_bound_in_every_prompt(tmp_path: Path) -> None:
    workspace, baseline = _agent_policy_repo(tmp_path / "repo")
    project = workspace.path
    contract = ChangeContract(objective="small function", allowed_files=("src/app.py",))
    policy = load_effective_agent_policy(project, contract, baseline)

    prompts = [
        _build_task_prompt(
            "T-1",
            object(),
            project,
            contract=contract,
            planning_project=project,
            planning_baseline_sha=baseline,
            provider_name=provider,
            effective_agent_policy=policy,
        )
        for provider in ("codex", "claude", "grok")
    ]

    assert all(agent_policy_prompt(policy) in prompt for prompt in prompts)
    assert all(policy.digest in prompt for prompt in prompts)
    assert policy.source == f"baseline:{baseline}:.stagemesh/agent-policy.json"


def test_agent_policy_tampering_is_quarantined_without_owner_exception(tmp_path: Path) -> None:
    workspace, baseline = _agent_policy_repo(tmp_path / "repo")
    project = workspace.path
    policy_path = project / ".stagemesh" / "agent-policy.json"
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["constraints"]["no_policy_evasion"] = False
    policy_path.write_text(json.dumps(payload), encoding="utf-8")
    candidate = workspace.commit_all("weaken policy")
    contract = ChangeContract(objective="change policy", allowed_files=(".stagemesh/agent-policy.json",))

    report = inspect_candidate_diff(project, baseline, candidate, contract)

    assert report["disposition"] == "QUARANTINED"
    assert any(item["code"] == "agent_policy_tampered" for item in report["agent_policy_findings"])
    assert report["agent_policy"]["policy_hash"]


def test_agent_policy_exception_is_frozen_owned_and_still_requires_review(tmp_path: Path) -> None:
    workspace, baseline = _agent_policy_repo(tmp_path / "repo")
    project = workspace.path
    policy_path = project / ".stagemesh" / "agent-policy.json"
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    payload["constraints"]["no_policy_evasion"] = False
    policy_path.write_text(json.dumps(payload), encoding="utf-8")
    candidate = workspace.commit_all("owner-approved policy change")
    contract = ChangeContract(
        objective="change policy",
        allowed_files=(".stagemesh/agent-policy.json",),
        agent_policy_exceptions=("allow_policy_change",),
        agent_policy_owner="operator",
    )

    report = inspect_candidate_diff(project, baseline, candidate, contract)

    assert report["disposition"] == "REVIEW_REQUIRED"
    assert not any(item["severity"] == "error" for item in report["agent_policy_findings"])
    assert report["agent_policy"]["exceptions"] == ["allow_policy_change"]
    assert contract_admission_problem(contract) is None


def test_agent_policy_escalates_gratuitous_abstraction_and_documentation_churn(tmp_path: Path) -> None:
    workspace, baseline = _agent_policy_repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "helpers.py").write_text("class WidgetWrapper:\n    pass\n", encoding="utf-8")
    (project / "README.md").write_text("unrequested narrative\n", encoding="utf-8")
    candidate = workspace.commit_all("broad generated change")

    report = inspect_candidate_diff(
        project,
        baseline,
        candidate,
        ChangeContract(objective="fix widget result", allowed_files=("**",)),
    )

    codes = {item["code"] for item in report["agent_policy_findings"]}
    assert {"agent_policy_abstraction_review", "agent_policy_documentation_review"} <= codes
    assert report["disposition"] == "REVIEW_REQUIRED"


def test_invalid_baseline_agent_policy_fails_closed(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    path = project / ".stagemesh" / "agent-policy.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    baseline = workspace.commit_all("invalid policy")

    with pytest.raises(AgentPolicyError, match="schema"):
        load_effective_agent_policy(project, ChangeContract(objective="task"), baseline)


def test_validation_evidence_records_scope_classification_for_out_of_scope_paths(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    store = _store(tmp_path)
    task_id = store.upsert_task("scope task")
    (project / "stagemesh.contract.json").write_text(
        json.dumps({"objective": "Only source", "allowed_files": ["src/**", "stagemesh.contract.json"], "forbidden_files": ["README.md"]}),
        encoding="utf-8",
    )
    (project / "README.md").write_text("scope bleed\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    store.add_candidate(task_id, sha, "implementer", True)

    status = Validator().validate(store, task_id, sha, project)
    evidence = store.conn.execute("SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=?", (task_id, sha)).fetchone()
    payload = json.loads(evidence["payload"])

    assert status is EvidenceStatus.FAILED
    assert payload["scope"]["outside_allowed"] == ["README.md"]
    assert payload["scope"]["forbidden"] == ["README.md"]
    assert payload["scope"]["in_scope"] == ["stagemesh.contract.json"]
    assert payload["diff_inspection"]["disposition"] == "QUARANTINED"
    quarantine_ref = payload["diff_inspection"]["quarantine_ref"]
    assert workspace.run("rev-parse", quarantine_ref).stdout.strip() == sha
    event = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='candidate.diff_quarantined' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert json.loads(event["payload"])["inspection"]["candidate_sha"] == sha
    store.close()


def test_diff_first_inspection_accounts_for_both_sides_of_a_rename(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    baseline = workspace.head()
    workspace.run("mv", "src/app.py", "src/renamed.py")
    candidate = workspace.commit_all("rename")
    contract = ChangeContract(objective="rename target only", allowed_files=("src/renamed.py",))

    report = inspect_candidate_diff(project, baseline, candidate, contract, workspace=project)

    assert report["entries"] == [{"status": "R100", "paths": ["src/app.py", "src/renamed.py"]}]
    assert report["scope"]["outside_allowed"] == ["src/app.py"]
    assert report["disposition"] == "QUARANTINED"


def test_diff_first_inspection_accounts_for_dirty_untracked_and_ignored_files(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / ".gitignore").write_text("debug.log\n", encoding="utf-8")
    baseline = workspace.commit_all("ignore policy")
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    candidate = workspace.commit_all("candidate")
    (project / "scratch.txt").write_text("not committed\n", encoding="utf-8")
    (project / "debug.log").write_text("ignored but accounted\n", encoding="utf-8")

    report = inspect_candidate_diff(
        project,
        baseline,
        candidate,
        ChangeContract(objective="source only", allowed_files=("src/app.py",)),
        workspace=project,
    )

    assert report["workspace_state"]["untracked"] == ["scratch.txt"]
    assert report["workspace_state"]["ignored_not_in_candidate"] == ["debug.log"]
    assert report["disposition"] == "QUARANTINED"


def test_contract_runs_required_gates_and_blocks_failures(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    contract = ChangeContract(
        objective="Run deterministic tests",
        allowed_files=("src/**",),
        required_tests=(GateCommand("failing-test", [sys.executable, "-c", "import sys; sys.exit(7)"]),),
    )
    result = evaluate_contract(project, sha, contract, run_gates=True)

    assert result.status == "FAILED"
    assert result.gates[0].name == "failing-test"
    assert result.gates[0].returncode == 7
    assert any(finding["code"] == "gate_failed" for finding in result.findings)


def test_contract_gate_decodes_utf8_output_on_failing_gate(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    contract = ChangeContract(
        objective="Decode gate output",
        allowed_files=("src/**",),
        required_tests=(
            GateCommand(
                "utf8-failure",
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'vitest says \\xe2\\x9c\\x93\\n'); sys.exit(7)",
                ],
            ),
        ),
    )

    result = evaluate_contract(project, sha, contract, run_gates=True)

    assert result.status == "FAILED"
    assert result.gates[0].status == "FAILED"
    assert result.gates[0].stdout == "vitest says ✓\n"
    assert any(finding["code"] == "gate_failed" for finding in result.findings)


def test_contract_gate_none_output_records_failure_without_type_error(tmp_path: Path, monkeypatch) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    def fake_run(command, cwd, env, timeout):
        del cwd, env, timeout
        return subprocess.CompletedProcess(command, 9, stdout=None, stderr=None)

    monkeypatch.setattr("stagemesh.contracts._run_gate_process", fake_run)

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="Handle missing gate output",
            allowed_files=("src/**",),
            required_tests=(GateCommand("none-output", [sys.executable, "-c", "pass"]),),
        ),
        run_gates=True,
    )

    assert result.status == "FAILED"
    assert result.gates[0].stdout == ""
    assert result.gates[0].stderr == ""
    assert any(finding["code"] == "gate_failed" for finding in result.findings)


def test_contract_gate_timeout_output_is_normalized_to_strings(tmp_path: Path, monkeypatch) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 6\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    def fake_run(command, cwd, env, timeout):
        del cwd, env, timeout
        raise subprocess.TimeoutExpired(command, timeout=1, output=b"partial \xe2\x9c\x93", stderr=None)

    monkeypatch.setattr("stagemesh.contracts._run_gate_process", fake_run)

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="Handle timeout gate output",
            allowed_files=("src/**",),
            required_tests=(GateCommand("timeout-output", [sys.executable, "-c", "pass"], timeout_seconds=1),),
        ),
        run_gates=True,
    )

    assert result.status == "FAILED"
    assert result.gates[0].stdout == "partial ✓"
    assert isinstance(result.gates[0].stderr, str)
    assert any(finding["code"] == "gate_failed" for finding in result.findings)


def test_contract_gates_run_from_exact_candidate_workspace(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    (project / "src" / "app.py").write_text("VALUE = 'shared checkout only'\n", encoding="utf-8")

    contract = ChangeContract(
        objective="Validate candidate contents",
        allowed_files=("src/**",),
        required_tests=(
            GateCommand(
                "candidate-workspace",
                [
                    sys.executable,
                    "-c",
                    "import pathlib, sys; sys.exit(0 if 'candidate' in pathlib.Path('src/app.py').read_text() else 9)",
                ],
            ),
        ),
    )

    result = evaluate_contract(project, sha, contract, run_gates=True)

    assert result.status == "PASSED"
    assert result.gates[0].status == "PASSED"


def test_validator_records_failed_contract_evidence_for_canary_violation(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / "TASK-1.json").write_text(
        json.dumps(
            {
                "objective": "Keep documentation untouched",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text("canary violation\n", encoding="utf-8")
    sha = workspace.commit_all("bad candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("contract task", source_id="TASK-1")
    store.add_candidate(task_id, sha, "test", True)

    status = Validator().validate(store, task_id, sha, project)

    assert status is EvidenceStatus.FAILED
    assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED)
    assert not store.has_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.close()


def test_reviewer_independently_creates_structured_findings(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "stagemesh.contract.json").write_text(
        json.dumps(
            {
                "objective": "Source only",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    (project / "README.md").write_text("review canary\n", encoding="utf-8")
    sha = workspace.commit_all("review candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review task")
    store.add_candidate(task_id, sha, "test", True)

    status = Reviewer().review(store, task_id, sha, project)

    assert status is EvidenceStatus.FAILED
    findings = store.open_findings_for_candidate(task_id, sha)
    assert findings
    assert any("forbidden" in finding["message"] for finding in findings)
    store.close()


def test_bound_contract_survives_filesystem_mutation_after_validation(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    store = _store(tmp_path)
    task_id = store.upsert_task("mutation task")
    contract_path = project / "stagemesh.contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "objective": "Source only",
                "allowed_files": ["src/**", "stagemesh.contract.json"],
                "forbidden_files": ["README.md"],
                "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
            }
        ),
        encoding="utf-8",
    )
    (project / "src" / "app.py").write_text("VALUE = 7\n", encoding="utf-8")
    sha = workspace.commit_all("source candidate")

    store.add_candidate(task_id, sha, "implementer", True)

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    binding = store.contract_binding(task_id, sha)
    assert binding is not None
    bound_hash = binding["digest"]

    contract_path.write_text(
        json.dumps({"objective": "Mutated later", "allowed_files": ["docs/**"], "forbidden_files": ["src/**"]}),
        encoding="utf-8",
    )

    assert Reviewer(provider_name="reviewer").review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["contract_hash"] == bound_hash
    assert payload["objective"] == "Source only"
    assert payload["independent_reviewer"] is False
    assert payload["review_execution_invoked"] is False
    store.close()


def test_reviewer_provider_identity_must_be_distinct_from_implementer(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review identity")
    store.add_candidate(task_id, sha, "implementer", True)

    assert Reviewer(provider_name="implementer").review(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert store.open_findings_for_candidate(task_id, sha)

    assert Reviewer(provider_name="reviewer").review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["review_provider"] == "reviewer"
    assert payload["implementer_provider"] == "implementer"
    assert payload["independent_reviewer"] is False
    assert payload["review_execution_invoked"] is False
    assert payload["deterministic_contract_gate"] is True
    store.close()


def test_distinct_reviewer_adapter_executes_independent_review(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review execution")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="reviewer")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    ).fetchone()
    payload = json.loads(row["payload"])
    assert adapter.calls == 1
    assert payload["review_provider"] == "reviewer"
    assert payload["review_execution_provider"] == "reviewer"
    assert payload["independent_reviewer"] is True
    assert payload["review_execution_invoked"] is True
    assert payload["deterministic_contract_gate"] is True
    store.close()


def test_reviewer_adapter_matching_implementer_is_rejected(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 11\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("review same provider")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="implementer")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert adapter.calls == 0
    assert store.open_findings_for_candidate(task_id, sha)
    store.close()


def test_malformed_independent_review_output_is_review_capacity_not_candidate_finding(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 12\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("malformed review")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RecordingReviewAdapter(name="reviewer", response="PASS")

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.CAPACITY
    assert store.open_findings_for_candidate(task_id, sha) == []
    payload = json.loads(
        store.conn.execute("SELECT payload FROM evidence WHERE kind=?", (EvidenceKind.REVIEW,)).fetchone()["payload"]
    )
    assert payload["review_infrastructure_failure"] == "malformed_review_output"
    store.close()


def test_command_review_adapter_rejects_workspace_mutation(tmp_path: Path) -> None:
    from stagemesh.providers import RuntimeCommandAdapter

    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 13\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    script = tmp_path / "mutating_review.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('src/app.py').write_text('VALUE = 99\\n', encoding='utf-8')\n"
        "print('{\"decision\":\"PASS\"}')\n",
        encoding="utf-8",
    )

    store = _store(tmp_path)
    task_id = store.upsert_task("mutating review")
    store.add_candidate(task_id, sha, "implementer", True)
    adapter = RuntimeCommandAdapter(name="reviewer", command=(sys.executable, str(script)))

    assert Reviewer(adapter=adapter).review(store, task_id, sha, project) is EvidenceStatus.FAILED
    payload = json.loads(
        store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
            (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED),
        ).fetchone()["payload"]
    )
    assert "mutated candidate workspace" in payload["review_response"]
    assert workspace.head() == sha
    assert not (project / "src" / "app.py").read_text(encoding="utf-8").endswith("99\n")
    store.close()


def test_contract_parser_accepts_named_command_objects() -> None:
    contract = parse_contract(
        {
            "objective": "Ship safely",
            "acceptance_criteria": ["tests pass"],
            "spec_refs": ["docs/specs/voice-intake.md"],
            "skill_refs": ["agent-skills/genui-architect/SKILL.md"],
            "allowed_files": ["src/**"],
            "required_tests": [{"name": "unit", "command": [sys.executable, "--version"], "timeout_seconds": 5}],
        }
    )

    assert contract.objective == "Ship safely"
    assert contract.acceptance_criteria == ("tests pass",)
    assert contract.spec_refs == ("docs/specs/voice-intake.md",)
    assert contract.skill_refs == ("agent-skills/genui-architect/SKILL.md",)
    assert contract.required_tests[0].name == "unit"


@pytest.mark.parametrize("ref", ["../outside.md", "/tmp/spec.md", "C:/tmp/spec.md"])
def test_contract_parser_rejects_spec_refs_outside_project(ref: str) -> None:
    with pytest.raises(ContractError, match="spec_refs entries must be relative paths inside the project"):
        parse_contract({"objective": "Ship safely", "spec_refs": [ref]})


@pytest.mark.parametrize("ref", ["../outside/SKILL.md", "/tmp/SKILL.md", "C:/tmp/SKILL.md"])
def test_contract_parser_rejects_skill_refs_outside_project(ref: str) -> None:
    with pytest.raises(ContractError, match="skill_refs entries must be relative paths inside the project"):
        parse_contract({"objective": "Ship safely", "skill_refs": [ref]})


def test_dependency_manifest_change_requires_dependency_gate(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "pyproject.toml").write_text("[project]\nname = 'changed'\n", encoding="utf-8")
    sha = workspace.commit_all("change manifest")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(objective="Do not smuggle dependency changes", allowed_files=("**",)),
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(finding["code"] == "dependency_manifest_changed_without_gate" for finding in result.findings)


def test_dependency_manifest_detection_is_case_insensitive(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "PyProject.TOML").write_text("[project]\nname = 'changed'\n", encoding="utf-8")
    sha = workspace.commit_all("case variant manifest")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(objective="Catch Windows manifest variants", allowed_files=("**",)),
        run_gates=False,
    )

    assert result.status == "FAILED"
    assert any(finding["code"] == "dependency_manifest_changed_without_gate" for finding in result.findings)


def test_rename_source_and_target_are_checked_against_scope(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "secrets.txt").write_text("secret\n", encoding="utf-8")
    workspace.commit_all("add protected file")
    workspace.run("mv", "secrets.txt", "src/secrets.py")
    sha = workspace.commit_all("rename protected file into scope")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="Do not touch secrets",
            allowed_files=("src/**",),
            forbidden_files=("secrets.txt",),
        ),
        run_gates=False,
    )

    assert "secrets.txt" in result.changed_files
    assert "src/secrets.py" in result.changed_files
    assert result.status == "FAILED"
    assert any(finding["code"] == "forbidden_file_changed" for finding in result.findings)


def test_change_size_limits_reject_unrelated_refactor(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 2\nOTHER = 3\n", encoding="utf-8")
    (project / "src" / "extra.py").write_text("EXTRA = 1\n", encoding="utf-8")
    sha = workspace.commit_all("too broad")

    result = evaluate_contract(
        project,
        sha,
        ChangeContract(
            objective="One small edit",
            allowed_files=("src/**",),
            max_changed_files=1,
            max_diff_lines=1,
        ),
        run_gates=False,
    )

    codes = {finding["code"] for finding in result.findings}
    assert {"change_size_files_exceeded", "change_size_lines_exceeded"} <= codes


def test_integration_requires_validation_and_review_for_exact_sha(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 42\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("integration task")
    store.add_candidate(task_id, sha, "test", True)

    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED
    assert not store.has_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)

    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    assert Reviewer(adapter=RecordingReviewAdapter()).review(store, task_id, sha, project) is EvidenceStatus.PASSED
    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.PASSED
    store.close()


def test_integration_rejects_contract_hash_mismatch(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "src" / "app.py").write_text("VALUE = 43\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")

    store = _store(tmp_path)
    task_id = store.upsert_task("integration mismatch")
    store.add_candidate(task_id, sha, "test", True)
    bound = contract_for_candidate(store, task_id, sha, project)
    mismatched = {**bound.evidence_payload(), "contract_hash": "0" * 64}
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, mismatched)
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, bound.evidence_payload())

    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.FAILED
    store.close()


def test_provider_prompt_includes_change_contract(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    store = _store(tmp_path)
    task_id = store.upsert_task("prompt task")
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(
        json.dumps(
            {
                "objective": "Only touch source",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    store.advance_task(task_id, "IMPLEMENT")
    script = (
        "import pathlib, sys\n"
        "pathlib.Path('captured_prompt.txt').write_text(sys.stdin.read(), encoding='utf-8')\n"
    )

    Coordinator(store, project, executor=SubprocessExecutor([sys.executable, "-c", script], name="prompt-capture")).tick()

    prompt = (task_workspace(project, task_id) / "captured_prompt.txt").read_text(encoding="utf-8")
    assert "Change contract:" in prompt
    assert "Only touch source" in prompt
    assert "Forbidden files: README.md" in prompt
    assert "Allowed files constrain edits, not read-only investigation." in prompt
    assert "request an audited scope expansion" in prompt
    assert not (project / "captured_prompt.txt").exists()
    store.close()


def test_implementation_runs_in_isolated_worktree_not_shared_project(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    store = _store(tmp_path)
    task_id = store.upsert_task("isolated task")
    coord = Coordinator(store, project, remediation_policy=RemediationPolicy(max_attempts=1))
    assert coord.tick() == 1
    assert coord.tick() == 1

    assert not (project / f"stagemesh-task-{task_id}.txt").exists()
    assert (task_workspace(project, task_id) / f"stagemesh-task-{task_id}.txt").exists()
    assert store.latest_candidate(task_id) is not None
    store.close()


def test_remediation_produces_new_candidate_and_does_not_reuse_stale_evidence(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    (project / "stagemesh.contract.json").write_text(
        json.dumps(
            {
                "objective": "Source value must be remediated",
                "allowed_files": ["src/**"],
                "required_tests": [
                    {
                        "name": "value-remediated",
                        "command": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; assert 'VALUE = 11' in Path('src/app.py').read_text()",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = _store(tmp_path)
    task_id = store.upsert_task("remediate task")
    coord = Coordinator(
        store,
        project,
        executor=SequencedExecutor(project, ["VALUE = 10\n", "VALUE = 11\n"]),
        reviewer=Reviewer(adapter=RecordingReviewAdapter()),
    )

    assert coord.tick() == 1  # PLAN -> IMPLEMENT
    assert coord.tick() == 1  # candidate 1
    first_sha = store.latest_candidate(task_id)["sha"]
    assert coord.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.IMPLEMENT
    assert coord.tick() == 1
    second_sha = store.latest_candidate(task_id)["sha"]

    assert second_sha != first_sha
    assert store.has_evidence(task_id, first_sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED)
    assert store.open_findings_for_candidate(task_id, first_sha)
    assert not store.has_evidence(task_id, second_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert coord.tick() == 1
    assert store.has_evidence(task_id, second_sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.close()


def test_remediation_exhaustion_blocks_task(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    store = _store(tmp_path)
    task_id = store.upsert_task("exhaust task")
    (project / "stagemesh.contract.json").write_text(
        json.dumps({"objective": "Source only", "allowed_files": ["src/**"], "forbidden_files": ["README.md"]}),
        encoding="utf-8",
    )
    (project / "README.md").write_text("break contract\n", encoding="utf-8")
    sha = workspace.commit_all("failing candidate")
    store.add_candidate(task_id, sha, "implementer", True)
    store.advance_task(task_id, Stage.VALIDATE)
    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
    store.add_task_remediation(task_id, "VALIDATE", sha)  # the one allowed remediation is already spent

    coord = Coordinator(store, project, remediation_policy=RemediationPolicy(max_attempts=1))
    assert coord.tick() == 1

    task = store.get_task(task_id)
    assert task["stage"] == Stage.VALIDATE
    assert task["status"] == TaskStatus.BLOCKED
    store.close()



def test_delivery_reentry_uses_exact_existing_evidence_without_provider_or_ref_changes(tmp_path: Path) -> None:
    from stagemesh.coordinator import Coordinator
    from stagemesh.domain import Stage

    workspace = _repo(tmp_path / "repo")
    project = workspace.path
    baseline = workspace.head()
    (project / "src" / "app.py").write_text("VALUE = 43\n", encoding="utf-8")
    sha = workspace.commit_all("verified candidate")
    workspace.run("branch", "wave/staging", sha)
    store = _store(tmp_path)
    task_id = store.upsert_task("delivery reentry")
    store.add_candidate(task_id, sha, "implementer", True)
    bound = contract_for_candidate(store, task_id, sha, project)
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        payload = {**bound.evidence_payload(), "independent_reviewer": True, "review_execution_invoked": True,
                   "review_execution_provider": "reviewer", "review_verdict": "PASS", "implementer_provider": "implementer"}
        store.add_evidence(task_id, sha, kind, EvidenceStatus.PASSED, payload)
    store.advance_task(task_id, Stage.INTEGRATE)
    before_candidates = [tuple(row) for row in store.conn.execute("SELECT * FROM candidates")]
    before_evidence = [tuple(row) for row in store.conn.execute("SELECT * FROM evidence")]

    class NeverCallProvider:
        def execute(self, *args, **kwargs):
            raise AssertionError("implementation must not be replayed")
        def validate(self, *args, **kwargs):
            raise AssertionError("passing validation must be preserved")
        def review(self, *args, **kwargs):
            raise AssertionError("passing independent review must be preserved")

    no_calls = NeverCallProvider()
    coordinator = Coordinator(store, project, executor=no_calls, validator=no_calls, reviewer=no_calls,
                              integrator=Integrator("refs/heads/wave/staging", require_independent_review=True),
                              require_independent_review=True)
    assert coordinator.tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.DONE
    assert store.latest_candidate(task_id)["sha"] == sha
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM candidates")] == before_candidates
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM evidence")] == before_evidence
    assert workspace.head() == sha and workspace.run("rev-parse", "wave/staging").stdout.strip() == sha
    assert baseline != sha
    store.close()
