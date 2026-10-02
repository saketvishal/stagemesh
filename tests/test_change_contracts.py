from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contracts import ChangeContract, GateCommand, evaluate_contract, parse_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.review import Reviewer
from stagemesh.validation import Validator


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
        json.dumps({"objective": "Source only", "allowed_files": ["src/**"], "forbidden_files": ["README.md"]}),
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


def test_contract_parser_accepts_named_command_objects() -> None:
    contract = parse_contract(
        {
            "objective": "Ship safely",
            "acceptance_criteria": ["tests pass"],
            "allowed_files": ["src/**"],
            "required_tests": [{"name": "unit", "command": [sys.executable, "--version"], "timeout_seconds": 5}],
        }
    )

    assert contract.objective == "Ship safely"
    assert contract.acceptance_criteria == ("tests pass",)
    assert contract.required_tests[0].name == "unit"
