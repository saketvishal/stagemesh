from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from stagemesh.controlled_change import (
    ChangeContract,
    ChangeSet,
    ScopeViolationError,
    ValidationLevel,
    ValidationPlan,
    ValidationPlanner,
    compute_plan_hash,
    derive_git_changeset,
    enforce_change_scope,
    expand_contract_scope,
    format_validation_plan_explain,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.persistence import Store
from stagemesh.validation import Validator


def _init_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Helper to initialize a synthetic git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    # Initial structure
    src_dir = repo / "src" / "stagemesh"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "governance.py").write_text("# initial governance\n", encoding="utf-8")
    (src_dir / "coordinator.py").write_text("# initial coordinator\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_git_governance.py").write_text("# test\n", encoding="utf-8")

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial baseline"], cwd=repo, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    baseline_sha = res.stdout.strip()
    return repo, baseline_sha


def test_scenario_1_authorized_source_and_test_passed_mapped_tests_only(tmp_path: Path):
    """
    Scenario 1:
    One authorized source file + matching test file changed
    -> scope passes
    -> only mapped tests selected
    -> FULL not selected.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-001"

    # Modify authorized files
    (repo / "src" / "stagemesh" / "governance.py").write_text("# modified\n", encoding="utf-8")
    (repo / "tests" / "test_git_governance.py").write_text("# modified test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "authorized change"], cwd=repo, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py", "tests/test_git_governance.py"),
        validation_level=ValidationLevel.TASK.value,
    )

    changeset = derive_git_changeset(repo, task_id, baseline_sha, candidate_sha)
    assert set(changeset.all_changed_paths) == {"src/stagemesh/governance.py", "tests/test_git_governance.py"}

    scope_result = enforce_change_scope(contract, changeset)
    assert scope_result.is_authorized is True
    assert len(scope_result.unexpected_paths) == 0
    assert len(scope_result.forbidden_paths) == 0

    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert plan.requires_full is False
    assert plan.validation_level == ValidationLevel.TASK.value
    assert "pytest tests/test_git_governance.py -q" in plan.selected_commands
    assert "pytest tests/ -q" not in plan.selected_commands


def test_scenario_2_unauthorized_file_fails_closed_before_tests_run(tmp_path: Path):
    """
    Scenario 2:
    Agent changes an unauthorized file
    -> scope fails BEFORE test runner is called.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-002"

    # Modify authorized file AND unauthorized file
    (repo / "src" / "stagemesh" / "governance.py").write_text("# authorized change\n", encoding="utf-8")
    (repo / "src" / "stagemesh" / "coordinator.py").write_text("# unauthorized leak\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "scope violation commit"], cwd=repo, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    store = Store(tmp_path / "test.db")
    store.migrate()
    store.upsert_task("Test task 2", task_id=task_id)

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level=ValidationLevel.TASK.value,
    )
    store.save_change_contract(contract)

    test_runner_calls: list[str] = []

    def mock_runner(cmd: str, cwd: Path) -> tuple[bool, str]:
        test_runner_calls.append(cmd)
        return True, "ok"

    validator = Validator(command_runner=mock_runner)

    with pytest.raises(ScopeViolationError) as exc_info:
        validator.validate(store, task_id, candidate_sha, repo, base_sha=baseline_sha)

    # CRITICAL: test runner must NEVER have been called
    assert len(test_runner_calls) == 0

    violation = exc_info.value.result
    assert violation.is_authorized is False
    assert "src/stagemesh/coordinator.py" in violation.unexpected_paths

    evidence = store.latest_evidence(task_id, EvidenceKind.VALIDATION)
    assert evidence is not None
    assert evidence["status"] == EvidenceStatus.FAILED.value
    assert "scope_violation" in evidence["payload"]


def test_scenario_3_high_risk_file_escalates_to_full(tmp_path: Path):
    """
    Scenario 3:
    Agent modifies pyproject/config/core high-risk file
    -> deterministic escalation according to policy.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-003"

    # Add and modify pyproject.toml
    pyproject = repo / "pyproject.toml"
    pyproject.write_text("[tool.pytest]\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "modify pyproject"], cwd=repo, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("pyproject.toml",),
        validation_level=ValidationLevel.TASK.value,
    )

    changeset = derive_git_changeset(repo, task_id, baseline_sha, candidate_sha)
    scope_result = enforce_change_scope(contract, changeset)
    assert scope_result.is_authorized is True

    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert plan.requires_full is True
    assert plan.validation_level == ValidationLevel.FULL.value
    assert plan.escalation_reason is not None
    assert "high-risk" in plan.escalation_reason
    assert "pytest tests/ -q" in plan.selected_commands


def test_scenario_4_explicit_required_test_always_included(tmp_path: Path):
    """
    Scenario 4:
    Explicit required test is always included.
    """
    task_id = "TASK-004"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base123",
        allowed_paths=("src/stagemesh/governance.py",),
        required_tests=("pytest tests/test_special_invariant.py -q",),
        validation_level=ValidationLevel.TASK.value,
    )

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base123",
        result_tree_sha="tree123",
        modified_paths=("src/stagemesh/governance.py",),
    )

    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert "pytest tests/test_special_invariant.py -q" in plan.selected_commands
    assert plan.reasons["pytest tests/test_special_invariant.py -q"] == "explicit task-required validation"
    assert "pytest tests/test_git_governance.py -q" in plan.selected_commands


def test_scenario_5_unrelated_tests_not_selected():
    """
    Scenario 5:
    Unrelated tests are not selected.
    """
    task_id = "TASK-005"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base123",
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level=ValidationLevel.TASK.value,
    )

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base123",
        result_tree_sha="tree123",
        modified_paths=("src/stagemesh/governance.py",),
    )

    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    # Should only contain test_git_governance.py
    assert plan.selected_commands == ("pytest tests/test_git_governance.py -q",)
    assert "pytest tests/test_canary_regression.py -q" not in plan.selected_commands
    assert "pytest tests/test_sqlite_busy_retry.py -q" not in plan.selected_commands
    assert "pytest tests/ -q" not in plan.selected_commands


def test_scenario_6_evidence_reuse_same_tree_same_plan(tmp_path: Path):
    """
    Scenario 6:
    Same tree + same ValidationPlan
    -> evidence can be reused.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-006"

    (repo / "src" / "stagemesh" / "governance.py").write_text("# mod\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    store = Store(tmp_path / "test.db")
    store.migrate()
    store.upsert_task("Test task 6", task_id=task_id)

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
    )
    store.save_change_contract(contract)

    execution_counts = [0]

    def mock_runner(cmd: str, cwd: Path) -> tuple[bool, str]:
        execution_counts[0] += 1
        return True, "pass"

    validator = Validator(command_runner=mock_runner)

    # First validation run
    status1 = validator.validate(store, task_id, candidate_sha, repo, base_sha=baseline_sha)
    assert status1 == EvidenceStatus.PASSED
    assert execution_counts[0] == 1

    # Second candidate with identical tree (e.g. metadata/rebase change preserving exact tree)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "c2 same tree"], cwd=repo, check=True, capture_output=True)
    candidate_sha_2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    status2 = validator.validate(store, task_id, candidate_sha_2, repo, base_sha=baseline_sha)
    assert status2 == EvidenceStatus.PASSED
    # Execution count MUST NOT increase; cached evidence was reused
    assert execution_counts[0] == 1

    ev = store.latest_evidence(task_id, EvidenceKind.VALIDATION)
    assert ev is not None
    assert ev["payload"].get("reused") is True


def test_scenario_7_different_tree_reruns_validation(tmp_path: Path):
    """
    Scenario 7:
    Different tree
    -> validation reruns.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-007"

    (repo / "src" / "stagemesh" / "governance.py").write_text("# tree1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 1"], cwd=repo, check=True, capture_output=True)
    candidate1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    store = Store(tmp_path / "test.db")
    store.migrate()
    store.upsert_task("Test task 7", task_id=task_id)

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
    )
    store.save_change_contract(contract)

    execution_counts = [0]

    def mock_runner(cmd: str, cwd: Path) -> tuple[bool, str]:
        execution_counts[0] += 1
        return True, "pass"

    validator = Validator(command_runner=mock_runner)

    # First candidate validation
    validator.validate(store, task_id, candidate1, repo, base_sha=baseline_sha)
    assert execution_counts[0] == 1

    # Second candidate with a different tree
    (repo / "src" / "stagemesh" / "governance.py").write_text("# tree2 different\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=repo, check=True, capture_output=True)
    candidate2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    validator.validate(store, task_id, candidate2, repo, base_sha=baseline_sha)
    # Execution count MUST increase because tree differed
    assert execution_counts[0] == 2


def test_scenario_8_changed_validation_definition_reruns_validation(tmp_path: Path):
    """
    Scenario 8:
    Changed validation definition/version
    -> evidence reruns.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-008"

    (repo / "src" / "stagemesh" / "governance.py").write_text("# test8\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit"], cwd=repo, check=True, capture_output=True)
    candidate = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    store = Store(tmp_path / "test.db")
    store.migrate()
    store.upsert_task("Test task 8", task_id=task_id)

    contract1 = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
    )
    store.save_change_contract(contract1)

    execution_counts = [0]

    def mock_runner(cmd: str, cwd: Path) -> tuple[bool, str]:
        execution_counts[0] += 1
        return True, "pass"

    validator = Validator(command_runner=mock_runner)
    validator.validate(store, task_id, candidate, repo, base_sha=baseline_sha)
    assert execution_counts[0] == 1

    # Now update contract with an additional required test (changes validation definition & plan_hash)
    contract2 = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
        required_tests=("pytest tests/test_extra.py -q",),
    )
    store.save_change_contract(contract2)

    validator.validate(store, task_id, candidate, repo, base_sha=baseline_sha)
    # Validation must rerun for the new definition
    assert execution_counts[0] > 1


def test_scenario_9_agent_lies_about_file_list_git_diff_still_detects(tmp_path: Path):
    """
    Scenario 9:
    Agent-reported file list lies/omits a changed path
    -> Git-derived ChangeSet still detects it.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-009"

    # Provider secretly modifies an unauthorized file
    (repo / "src" / "stagemesh" / "governance.py").write_text("# authorized\n", encoding="utf-8")
    (repo / "secret_unauthorized.txt").write_text("backdoor\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "stealth commit"], cwd=repo, check=True, capture_output=True)
    candidate = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Provider reports to coordinator/executor only:
    agent_claimed_files = ["src/stagemesh/governance.py"]

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
    )

    # Git-derived ChangeSet is authoritative and ignores agent_claimed_files
    changeset = derive_git_changeset(repo, task_id, baseline_sha, candidate)
    assert "secret_unauthorized.txt" in changeset.all_changed_paths

    scope = enforce_change_scope(contract, changeset)
    assert scope.is_authorized is False
    assert "secret_unauthorized.txt" in scope.unexpected_paths


def test_scenario_10_full_validation_cannot_be_selected_by_provider_output():
    """
    Scenario 10:
    FULL validation cannot be selected by provider output.
    """
    task_id = "TASK-010"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base",
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level=ValidationLevel.TASK.value,
    )

    # Even if provider payload claims it wants "FULL"
    provider_output = {"requested_validation": "FULL", "notes": "run full suite please"}

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base",
        result_tree_sha="tree",
        modified_paths=("src/stagemesh/governance.py",),
    )

    # Planner only takes ChangeContract and ChangeSet, strictly ignoring provider output
    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert plan.requires_full is False
    assert plan.validation_level == ValidationLevel.TASK.value
    assert "pytest tests/ -q" not in plan.selected_commands


def test_scenario_11_validation_plan_records_reason_for_every_selected_test():
    """
    Scenario 11:
    ValidationPlan records a reason for every selected test.
    """
    task_id = "TASK-011"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base",
        allowed_paths=("src/stagemesh/governance.py", "tests/test_git_governance.py"),
        required_tests=("pytest tests/test_custom.py -q",),
        validation_level=ValidationLevel.TASK.value,
    )

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base",
        result_tree_sha="tree",
        modified_paths=("src/stagemesh/governance.py", "tests/test_git_governance.py"),
    )

    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert len(plan.selected_commands) > 0
    for cmd in plan.selected_commands:
        assert cmd in plan.reasons
        assert isinstance(plan.reasons[cmd], str)
        assert len(plan.reasons[cmd].strip()) > 0


def test_scenario_12_scope_expansion_does_not_silently_authorize_itself():
    """
    Scenario 12:
    Scope-expansion request does not silently authorize itself.
    """
    contract = ChangeContract(
        task_id="TASK-012",
        baseline_sha="base",
        allowed_paths=("src/stagemesh/governance.py",),
    )

    # Silent expansion without reason must fail
    with pytest.raises(ValueError, match="explicit reason"):
        expand_contract_scope(contract, ["src/stagemesh/coordinator.py"], reason="")

    with pytest.raises(ValueError, match="explicit reason"):
        expand_contract_scope(contract, ["src/stagemesh/coordinator.py"], reason="   ")

    # Valid expansion creates an audited record
    expanded = expand_contract_scope(
        contract,
        ["src/stagemesh/coordinator.py"],
        reason="Refactoring coordinator to support governance hook",
    )
    assert expanded.expanded is True
    assert expanded.expansion_reason == "Refactoring coordinator to support governance hook"
    assert "src/stagemesh/coordinator.py" in expanded.allowed_paths


def test_format_validation_plan_explain():
    """Test explainability formatting matching section I."""
    plan = ValidationPlan(
        task_id="TASK-013",
        validation_level="TASK",
        selected_commands=("pytest tests/test_git_governance.py -q",),
        reasons={"pytest tests/test_git_governance.py -q": "direct component mapping"},
        requires_full=False,
        escalation_reason=None,
        plan_hash="abc12345",
    )
    changeset = ChangeSet(
        task_id="TASK-013",
        baseline_sha="base",
        result_tree_sha="tree",
        modified_paths=("src/stagemesh/governance.py",),
    )
    from stagemesh.controlled_change import ScopeValidationResult
    scope_result = ScopeValidationResult(
        is_authorized=True,
        expected_paths=(),
        allowed_paths=("src/stagemesh/governance.py",),
        actual_paths=("src/stagemesh/governance.py",),
        unexpected_paths=(),
        forbidden_paths=(),
    )
    output = format_validation_plan_explain(plan, changeset, scope_result)
    assert "Changed:" in output
    assert "src/stagemesh/governance.py" in output
    assert "Authorized:" in output
    assert "yes" in output
    assert "Selected:" in output
    assert "pytest tests/test_git_governance.py -q" in output
    assert "reason: direct component mapping" in output
    assert "Skipped:" in output
    assert "full repository suite" in output
    assert "reason: impact bounded" in output
    assert "Validation:\n  TASK" in output


def test_cli_validation_plan_command(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Test CLI command 'stagemesh validation-plan' and '--json' output."""
    from stagemesh.cli import build_parser, db_path

    repo, baseline_sha = _init_git_repo(tmp_path)
    task_id = "TASK-CLI-01"

    # Make change
    (repo / "src" / "stagemesh" / "governance.py").write_text("# cli change\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "cli commit"], cwd=repo, check=True, capture_output=True)
    candidate_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    store = Store(db_path(repo))
    store.migrate()
    store.upsert_task("CLI Task", task_id=task_id)
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
    )
    store.save_change_contract(contract)
    store.add_candidate(task_id, candidate_sha, "agent", True)
    store.close()

    parser = build_parser()

    # 1. Plain text format
    args = parser.parse_args(["validation-plan", task_id, "--project", str(repo)])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Changed:\n  src/stagemesh/governance.py" in captured.out
    assert "Authorized:\n  yes" in captured.out
    assert "Selected:\n  pytest tests/test_git_governance.py -q" in captured.out
    assert "Validation:\n  TASK" in captured.out

    # 2. JSON format
    args_json = parser.parse_args(["validation-plan", task_id, "--project", str(repo), "--json"])
    rc_json = args_json.func(args_json)
    assert rc_json == 0
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert data["task_id"] == task_id
    assert data["scope"]["is_authorized"] is True
    assert data["plan"]["validation_level"] == "TASK"

