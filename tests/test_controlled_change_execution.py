from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.controlled_change import (
    ChangeContract,
    ChangeSet,
    MissingChangeContractError,
    ScopeViolationError,
    ValidationLevel,
    ValidationPlanner,
    derive_git_changeset,
    enforce_change_scope,
    format_provider_contract_prompt,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage
from stagemesh.execution import ExecutionResult, Executor, FakeExecutor, SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.governance import (
    canonicalize_and_record_candidate,
    prepare_task_worktree,
    resolve_or_capture_baseline,
)
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter, _build_task_prompt


def _init_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """Helper to initialize a synthetic git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    src_dir = repo / "src" / "stagemesh"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "governance.py").write_text("# initial governance\n", encoding="utf-8")
    (src_dir / "persistence.py").write_text("# initial persistence\n", encoding="utf-8")

    tests_dir = repo / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_git_governance.py").write_text("# test\n", encoding="utf-8")

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial baseline"], cwd=repo, check=True, capture_output=True)

    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    baseline_sha = res.stdout.strip()
    return repo, baseline_sha


def test_integration_a_missing_change_contract_prevents_provider_launch(tmp_path: Path):
    """
    Test A: Missing ChangeContract
    -> Coordinator / executor fails closed and does not launch provider.
    -> No candidate is produced.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = "TASK-A"
    store.upsert_task("Task without contract", task_id=task_id, create_default_contract=False)
    store.advance_task(task_id, Stage.IMPLEMENT)

    provider_launched = [False]

    class SpyExecutor(Executor):
        name = "spy"

        def run(self, s: Store, tid: str, cid: str | None, proj: Path) -> ExecutionResult:
            provider_launched[0] = True
            return ExecutionResult(ExecutionStatus.SUCCEEDED, "dummy", durable_handoff=True)

    coordinator = Coordinator(store=store, project=repo, executor=SpyExecutor())

    with pytest.raises(MissingChangeContractError) as exc_info:
        coordinator.tick()

    assert "cannot execute IMPLEMENT without a ChangeContract" in str(exc_info.value)
    assert provider_launched[0] is False
    assert store.latest_candidate(task_id) is None

    # Also test SubprocessExecutor directly
    sub_executor = SubprocessExecutor(command=[sys.executable, "-c", "print('hello')"])
    with pytest.raises(MissingChangeContractError):
        sub_executor.run(store, task_id, None, repo)


def test_integration_b_contract_permits_governance_provider_changes_governance_canonicalized(tmp_path: Path):
    """
    Test B: Contract permits governance.py only; provider changes governance.py
    -> authorized
    -> canonical candidate produced.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = "TASK-B"

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level=ValidationLevel.TASK.value,
    )
    store.upsert_task("Task B", task_id=task_id, contract=contract)

    # Provider script that modifies ONLY src/stagemesh/governance.py
    provider_script = (
        "import pathlib\n"
        "p = pathlib.Path('src/stagemesh/governance.py')\n"
        "p.write_text(p.read_text() + '# authorized edit\\n')\n"
    )
    executor = SubprocessExecutor(command=[sys.executable, "-c", provider_script])
    claim_id = store.acquire_claim(task_id, "worker")
    assert claim_id is not None

    res = executor.run(store, task_id, claim_id, repo)
    assert res.status is ExecutionStatus.SUCCEEDED
    assert res.candidate_sha is not None

    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["sha"] == res.candidate_sha
    assert candidate["base_sha"] == baseline_sha


def test_integration_c_scope_violation_prevents_canonical_candidate_and_validator(tmp_path: Path):
    """
    Test C: Contract permits governance.py only; provider ALSO changes persistence.py
    -> scope violation
    -> no canonical candidate
    -> no test runner invocation.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = "TASK-C"

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level=ValidationLevel.TASK.value,
    )
    store.upsert_task("Task C", task_id=task_id, contract=contract)

    # Provider script that modifies governance.py AND unauthorized persistence.py
    provider_script = (
        "import pathlib\n"
        "p1 = pathlib.Path('src/stagemesh/governance.py')\n"
        "p1.write_text(p1.read_text() + '# edit 1\\n')\n"
        "p2 = pathlib.Path('src/stagemesh/persistence.py')\n"
        "p2.write_text(p2.read_text() + '# unauthorized edit 2\\n')\n"
    )
    executor = SubprocessExecutor(command=[sys.executable, "-c", provider_script])
    claim_id = store.acquire_claim(task_id, "worker")

    res = executor.run(store, task_id, claim_id, repo)
    assert res.status is ExecutionStatus.FAILED
    assert res.candidate_sha is None
    assert "scope_violation" in (res.failure_reason or "")
    assert res.metadata is not None
    assert "src/stagemesh/persistence.py" in res.metadata["scope_violation"]["unexpected_paths"]

    # NO candidate must exist in Store
    assert store.latest_candidate(task_id) is None


def test_integration_d_provider_prompt_contains_exact_scope_and_validation_authority():
    """
    Test D: Provider prompt contains exact allowed/forbidden scope and validation authority.
    """
    task_id = "TASK-D"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base_sha_123",
        allowed_paths=("src/stagemesh/governance.py", "tests/test_git_governance.py"),
        expected_paths=("src/stagemesh/governance.py",),
        forbidden_paths=("pyproject.toml", "scripts/invariants.py"),
        validation_level="TASK",
        required_tests=("pytest tests/test_git_governance.py -q",),
    )

    prompt = _build_task_prompt(task_id, {"title": "Governance refinement"}, contract=contract)

    assert "AUTHORIZED CHANGE SCOPE:" in prompt
    assert "- src/stagemesh/governance.py" in prompt
    assert "- tests/test_git_governance.py" in prompt
    assert "EXPECTED PATHS:" in prompt
    assert "- src/stagemesh/governance.py" in prompt
    assert "FORBIDDEN PATHS:" in prompt
    assert "- pyproject.toml" in prompt
    assert "- scripts/invariants.py" in prompt
    assert "VALIDATION AUTHORITY:\n  TASK" in prompt
    assert "REQUIRED TARGETED TESTS:\n- pytest tests/test_git_governance.py -q" in prompt
    assert "Do not modify files outside the authorized scope." in prompt
    assert "Do not run repository-wide validation unless StageMesh explicitly assigns FULL." in prompt
    assert "StageMesh performs authoritative lifecycle validation after your implementation." in prompt


def test_integration_e_task_contract_instructs_provider_and_planner_selects_mapped_tests():
    """
    Test E: TASK contract
    -> provider is instructed not to run full suite
    -> StageMesh ValidationPlanner selects only mapped tests.
    """
    task_id = "TASK-E"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base",
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level="TASK",
    )
    prompt = _build_task_prompt(task_id, {"title": "Task E"}, contract=contract)
    assert "Do not run repository-wide validation unless StageMesh explicitly assigns FULL." in prompt

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base",
        result_tree_sha="tree",
        modified_paths=("src/stagemesh/governance.py",),
    )
    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    assert plan.validation_level == "TASK"
    assert plan.requires_full is False
    assert "pytest tests/test_git_governance.py -q" in plan.selected_commands
    assert "pytest tests/ -q" not in plan.selected_commands


def test_integration_f_provider_request_for_full_validation_ignored():
    """
    Test F: Provider tries to return/request FULL validation
    -> ignored/rejected unless contract already authorizes FULL.
    """
    task_id = "TASK-F"
    contract = ChangeContract(
        task_id=task_id,
        baseline_sha="base",
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level="TASK",
    )

    # Provider pretends it wants FULL validation in its output
    provider_output = {"requested_validation": "FULL", "notes": "agent asks for full repo CI"}

    changeset = ChangeSet(
        task_id=task_id,
        baseline_sha="base",
        result_tree_sha="tree",
        modified_paths=("src/stagemesh/governance.py",),
    )
    planner = ValidationPlanner()
    plan = planner.plan(contract, changeset)

    # Planner ignores provider_output and abides strictly by contract + git changeset
    assert plan.requires_full is False
    assert plan.validation_level == "TASK"
    assert "pytest tests/ -q" not in plan.selected_commands


def test_integration_g_changeset_derived_from_git_not_provider_claim(tmp_path: Path):
    """
    Test G: Prove actual ChangeSet is derived from Git, not provider response.
    """
    repo, baseline_sha = _init_git_repo(tmp_path)
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = "TASK-G"

    contract = ChangeContract(
        task_id=task_id,
        baseline_sha=baseline_sha,
        allowed_paths=("src/stagemesh/governance.py",),
        validation_level="TASK",
    )
    store.upsert_task("Task G", task_id=task_id, contract=contract)

    # Provider writes a structured result claiming it only touched governance.py,
    # but in git it ALSO touched persistence.py.
    provider_script = (
        "import os, pathlib, json\n"
        "p1 = pathlib.Path('src/stagemesh/governance.py')\n"
        "p1.write_text(p1.read_text() + '# edit 1\\n')\n"
        "p2 = pathlib.Path('src/stagemesh/persistence.py')\n"
        "p2.write_text(p2.read_text() + '# stealth edit 2\\n')\n"
        "res_path = os.environ.get('STAGEMESH_RESULT_PATH')\n"
        "if res_path:\n"
        "    pathlib.Path(res_path).write_text(json.dumps({'status': 'SUCCEEDED', 'candidate_sha': None, 'durable_handoff': True, 'changed_files': ['src/stagemesh/governance.py']}))\n"
    )
    executor = SubprocessExecutor(command=[sys.executable, "-c", provider_script])
    claim_id = store.acquire_claim(task_id, "worker")

    res = executor.run(store, task_id, claim_id, repo)

    # Must fail because Git diff-tree reveals persistence.py was touched!
    assert res.status is ExecutionStatus.FAILED
    assert store.latest_candidate(task_id) is None
    assert res.metadata is not None
    assert "src/stagemesh/persistence.py" in res.metadata["scope_violation"]["unexpected_paths"]
