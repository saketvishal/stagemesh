from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contracts import ChangeContract, GateCommand, evaluate_contract, parse_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
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
    assert Integrator().integrate(store, task_id, sha, project) is EvidenceStatus.PASSED
    store.close()


def test_provider_prompt_includes_change_contract(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "stagemesh.contract.json").write_text(
        json.dumps(
            {
                "objective": "Only touch source",
                "allowed_files": ["src/**"],
                "forbidden_files": ["README.md"],
            }
        ),
        encoding="utf-8",
    )
    store = _store(tmp_path)
    task_id = store.upsert_task("prompt task")
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
    assert not (project / "captured_prompt.txt").exists()
    store.close()


def test_implementation_runs_in_isolated_worktree_not_shared_project(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    store = _store(tmp_path)
    task_id = store.upsert_task("isolated task")
    coord = Coordinator(store, project)
    assert coord.tick() == 1
    assert coord.tick() == 1

    assert not (project / f"stagemesh-task-{task_id}.txt").exists()
    assert (task_workspace(project, task_id) / f"stagemesh-task-{task_id}.txt").exists()
    assert store.latest_candidate(task_id) is not None
    store.close()
