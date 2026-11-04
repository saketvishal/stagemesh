from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh.agent_skills import skill_prompt
from stagemesh.contracts import ChangeContract, ContractError, GateCommand, GateResult
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.focused_validation import (
    audited_component_gates,
    discover_focused_gates,
    focused_test_prompt,
    with_focused_gates,
)
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.validation import Validator
from stagemesh.validation_plan import derive_validation_plan


def write(root: Path, path: str, text: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def python_repo(root: Path, prefix: str = "") -> tuple[str, str]:
    write(root, prefix + "pytest.ini", "[pytest]\n")
    source, test = prefix + "app/widget.py", prefix + "tests/unit/test_widget.py"
    write(root, source, "VALUE = 1\n")
    write(root, test, "def test_widget(): pass\n")
    return source, test


@pytest.mark.parametrize("prefix", ["", "apps/api/"])
def test_python_discovery_is_automatic_and_package_local(tmp_path, prefix):
    source, test = python_repo(tmp_path, prefix)
    contract = ChangeContract("Fix widget", allowed_files=(source,))
    gates = discover_focused_gates(tmp_path, (source,), contract)
    assert len(gates) == 1
    assert gates[0].command == ["python", "-m", "pytest", "-q", "tests/unit/test_widget.py"]
    assert gates[0].cwd == (prefix.rstrip("/") or None)
    assert test in focused_test_prompt(tmp_path, contract) if not prefix else "cwd apps/api" in focused_test_prompt(tmp_path, contract)


def test_configured_interpreter_and_environment_are_preserved(tmp_path):
    source, _ = python_repo(tmp_path, "apps/api/")
    required = GateCommand("required-api", [".venv/Scripts/python.exe", "-m", "pytest", "-k", "billing"],
                           cwd="apps/api", env=(("TEST_DB", "scratch"),), timeout_seconds=90)
    contract = ChangeContract("Fix widget", required_tests=(required,))
    gates = discover_focused_gates(tmp_path, (source,), contract)
    assert gates[0].command[0] == ".venv/Scripts/python.exe"
    assert gates[0].env == required.env
    assert gates[0].timeout_seconds == 90
    assert with_focused_gates(contract, gates).required_tests[0] == required


@pytest.mark.parametrize("runner", ["vitest", "vitest run", "jest"])
def test_javascript_uses_declared_runner_and_exact_local_test(tmp_path, runner):
    write(tmp_path, "apps/web/package.json", json.dumps({"scripts": {"test": runner}}))
    write(tmp_path, "apps/web/src/Widget.tsx")
    write(tmp_path, "apps/web/src/Widget.test.tsx")
    gates = discover_focused_gates(tmp_path, ("apps/web/src/Widget.tsx",), ChangeContract("Fix UI"))
    assert len(gates) == 1
    assert gates[0].cwd == "apps/web"
    assert gates[0].command[:3] == ["npm", "test", "--"]
    assert gates[0].command[-1] == "src/Widget.test.tsx"
    assert "--runTestsByPath" in gates[0].command if runner == "jest" else "--run" in gates[0].command


def test_unknown_nested_runner_does_not_use_parent_runner(tmp_path):
    write(tmp_path, "package.json", '{"scripts":{"test":"vitest"}}')
    write(tmp_path, "apps/web/package.json", '{"scripts":{"test":"custom-runner"}}')
    write(tmp_path, "apps/web/src/Widget.tsx")
    write(tmp_path, "apps/web/src/Widget.test.tsx")
    assert discover_focused_gates(tmp_path, ("apps/web/src/Widget.tsx",), ChangeContract("UI")) == ()


def test_unknown_nested_python_package_does_not_use_parent_environment(tmp_path):
    write(tmp_path, "pytest.ini", "[pytest]\n")
    write(tmp_path, "apps/api/pyproject.toml", '[project]\nname="api"\n')
    write(tmp_path, "apps/api/app/widget.py")
    write(tmp_path, "apps/api/app/test_widget.py")
    assert discover_focused_gates(tmp_path, ("apps/api/app/widget.py",), ChangeContract("API")) == ()


def test_browser_spec_is_not_guessed_as_a_unit_test(tmp_path):
    write(tmp_path, "package.json", '{"scripts":{"test":"vitest"}}')
    write(tmp_path, "e2e/widget.spec.ts")
    assert discover_focused_gates(tmp_path, ("e2e/widget.spec.ts",), ChangeContract("UI")) == ()


def test_name_collision_cannot_silently_replace_discovered_gate(tmp_path):
    source, _ = python_repo(tmp_path)
    contract = ChangeContract("Task", required_tests=(GateCommand("auto-focused:pytest:.", ["echo", "pass"]),))
    with pytest.raises(ContractError, match="conflicts"):
        with_focused_gates(contract, discover_focused_gates(tmp_path, (source,), contract))


@pytest.mark.parametrize("paths", [("README.md",), ("../outside/test_widget.py",), ("**",), (".stagemesh/test_widget.py",)])
def test_docs_unbounded_and_unsafe_paths_do_not_schedule_application_tests(tmp_path, paths):
    python_repo(tmp_path)
    assert discover_focused_gates(tmp_path, paths, ChangeContract("Task")) == ()


def test_discovery_does_not_walk_the_repository(tmp_path, monkeypatch):
    source, _ = python_repo(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Repository traversal is not allowed")
    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    assert discover_focused_gates(tmp_path, (source,), ChangeContract("Task"))


@pytest.mark.parametrize("command", [["python", "-m", "pytest", "tests", "-q"],
                                    ["python", "-m", "pytest", "tests/unit/test_widget.py"]])
def test_declared_coverage_avoids_duplicate_checks_without_removing_required_gate(tmp_path, command):
    source, _ = python_repo(tmp_path)
    required = GateCommand("required-safety-gate", command)
    contract = ChangeContract("Task", required_tests=(required,), spec_refs=("spec.md",))
    result = with_focused_gates(contract, discover_focused_gates(tmp_path, (source,), contract))
    assert result == contract
    assert result.spec_refs == ("spec.md",)


def test_audited_component_targets_become_distinct_additive_gates() -> None:
    required = GateCommand(
        "required-review", [".venv/Scripts/python.exe", "-m", "pytest", "-k", "legacy"],
        env=(("REVIEW_DB", "scratch"),), timeout_seconds=91,
    )
    contract = ChangeContract(
        "Change review", allowed_files=("src/stagemesh/review.py",), required_tests=(required,),
    )
    plan = derive_validation_plan(contract, ("src\\stagemesh\\review.py",))

    gates = audited_component_gates(contract, plan)

    assert [gate.name for gate in gates] == [
        "auto-focused:audited-direct:.", "auto-focused:audited-dependent:.",
    ]
    assert gates[0].command == [
        ".venv/Scripts/python.exe", "-m", "pytest", "-q",
        "tests/test_review_reliability.py", "tests/test_review_terminalization.py",
    ]
    assert gates[1].command == [
        ".venv/Scripts/python.exe", "-m", "pytest", "-q",
        "tests/test_autonomy_review.py", "tests/test_builtin_execution_ownership.py",
        "tests/test_invariant_semantics.py",
    ]
    assert all(gate.env == required.env and gate.timeout_seconds == 91 for gate in gates)
    updated = with_focused_gates(contract, gates)
    assert updated.required_tests[0] == required
    assert updated.required_tests[1:] == gates


def test_audited_component_gates_fail_closed_for_unknown_and_conflicting_scope() -> None:
    unknown = ChangeContract("Unknown", allowed_files=("src/stagemesh/review.py", "src/unknown.py"))
    plan = derive_validation_plan(unknown, unknown.allowed_files)
    assert audited_component_gates(unknown, plan) == ()

    mapped = ChangeContract(
        "Mapped", allowed_files=("src/stagemesh/review.py",),
        required_tests=(GateCommand("auto-focused:audited-direct:.", ["echo", "spoof"]),),
    )
    gates = audited_component_gates(mapped, derive_validation_plan(mapped, mapped.allowed_files))
    with pytest.raises(ContractError, match="conflicts"):
        with_focused_gates(mapped, gates)


def test_declared_full_suite_covers_audited_component_gates_without_replacement() -> None:
    required = GateCommand("required-full", ["python", "-m", "pytest", "tests", "-q"])
    contract = ChangeContract(
        "Review", allowed_files=("src/stagemesh/review.py",), required_tests=(required,),
    )
    gates = audited_component_gates(contract, derive_validation_plan(contract, contract.allowed_files))

    assert gates
    assert with_focused_gates(contract, gates) == contract


def test_declared_exact_target_is_removed_from_audited_component_gate() -> None:
    required = GateCommand(
        "required-review-reliability",
        ["python", "-m", "pytest", "-q", "tests/test_review_reliability.py"],
    )
    contract = ChangeContract(
        "Review", allowed_files=("src/stagemesh/review.py",), required_tests=(required,),
    )

    gates = audited_component_gates(contract, derive_validation_plan(contract, contract.allowed_files))

    assert gates[0].name == "auto-focused:audited-direct:."
    assert "tests/test_review_reliability.py" not in gates[0].command
    assert "tests/test_review_terminalization.py" in gates[0].command
    assert with_focused_gates(contract, gates).required_tests[0] == required


def test_exact_candidate_discovery_ignores_dirty_metadata_and_test_files(tmp_path):
    source, test = python_repo(tmp_path)
    git = GitWorkspace(tmp_path)
    git.init_if_needed()
    git.run("config", "user.name", "Test Owner")
    git.run("config", "user.email", "test@example.invalid")
    sha = git.commit_all("base")
    (tmp_path / test).unlink()
    (tmp_path / "pytest.ini").unlink()
    gates = discover_focused_gates(tmp_path, (source,), ChangeContract("Task"), candidate_sha=sha)
    assert len(gates) == 1
    assert gates[0].command[-1] == "tests/unit/test_widget.py"


@pytest.mark.parametrize("command", [["python", "-m", "pytest", "-q"],
                                    ["python", "-m", "pytest", "-k", "other", "tests/unit/test_widget.py"]])
def test_default_testpaths_and_filtered_gates_do_not_suppress_focused_target(tmp_path, command):
    source, _ = python_repo(tmp_path)
    required = GateCommand("required-filtered", command)
    contract = ChangeContract("Task", required_tests=(required,))
    result = with_focused_gates(contract, discover_focused_gates(tmp_path, (source,), contract))
    assert result.required_tests[0] == required
    assert len(result.required_tests) == 2


@pytest.mark.parametrize("stage", ["implementation", "review"])
def test_policy_is_always_delivered_without_client_agents_file_or_profile(tmp_path, stage):
    prompt = skill_prompt("Fix widget", ChangeContract("Task", allowed_files=("app/widget.py",)), stage=stage, project=tmp_path)
    assert "default in every client repository" in prompt
    assert "Do not scan/read the whole repository" in prompt
    assert "plus required contract gates" in prompt


@pytest.mark.parametrize(("required", "focused_added"), [
    ({"name": "required-safety", "command": [sys.executable, "-c", "pass"]}, True),
    ({"name": "required-full-suite", "command": [sys.executable, "-m", "pytest"]}, False),
])
def test_validator_executes_discovered_failure_and_keeps_required_gate(
    tmp_path, monkeypatch, required, focused_added,
):
    project = tmp_path / "repo"
    source, _ = python_repo(project)
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.name", "Test Owner")
    git.run("config", "user.email", "test@example.invalid")
    git.commit_all("base")
    write(project, source, "VALUE = 2\n")
    sha = git.commit_all("candidate")
    write(project, ".stagemesh/contracts/T.json", json.dumps({
        "objective": "Fix widget", "allowed_files": ["app/**"],
        "required_tests": [required],
    }))
    calls = []
    def run_gate(root, gate):
        calls.append(gate.name)
        failed = gate.name.startswith("auto-focused:")
        return GateResult(gate.name, "FAILED" if failed else "PASSED", tuple(gate.command), 1 if failed else 0, "", "regression" if failed else "")
    monkeypatch.setattr("stagemesh.contracts.run_gate", run_gate)
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    try:
        task_id = store.upsert_task("Fix widget", source_id="T")
        store.add_candidate(task_id, sha, "test", True)
        expected = EvidenceStatus.FAILED if focused_added else EvidenceStatus.PASSED
        assert Validator().validate(store, task_id, sha, project) is expected
        assert required["name"] in calls
        assert ("auto-focused:pytest:." in calls) is focused_added
        payload = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind=?", (EvidenceKind.VALIDATION,)).fetchone()[0])
        assert payload["focused_validation"]["required_gates_replaced"] is False
        assert payload["validation_checks"]["missing"] == []
    finally:
        store.close()


@pytest.mark.parametrize("source", ["review.py", "contract_binding.py", "integration.py"])
def test_validator_records_and_executes_candidate_bound_audited_component_gates(
    tmp_path, monkeypatch, source,
):
    project = tmp_path / "repo"
    changed = f"src/stagemesh/{source}"
    write(project, changed, "VALUE = 1\n")
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.name", "Test Owner")
    git.run("config", "user.email", "test@example.invalid")
    git.commit_all("base")
    write(project, changed, "VALUE = 2\n")
    sha = git.commit_all("candidate")
    write(project, ".stagemesh/contracts/T.json", json.dumps({
        "objective": "Change audited boundary", "allowed_files": [changed],
        "required_tests": [{"name": "required-safety", "command": [sys.executable, "-c", "pass"]}],
    }))
    calls = []

    def run_gate(root, gate):
        calls.append(gate.name)
        return GateResult(gate.name, "PASSED", tuple(gate.command), 0, "", "")

    monkeypatch.setattr("stagemesh.contracts.run_gate", run_gate)
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    try:
        task_id = store.upsert_task("Change audited boundary", source_id="T")
        store.add_candidate(task_id, sha, "test", True)
        assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
        payload = json.loads(store.conn.execute(
            "SELECT payload FROM evidence WHERE kind=?", (EvidenceKind.VALIDATION,),
        ).fetchone()[0])
        expected = ["auto-focused:audited-direct:.", "auto-focused:audited-dependent:."]
        assert payload["focused_validation"]["conventional_discovered"] == []
        assert payload["focused_validation"]["audited_component_discovered"] == expected
        assert payload["focused_validation"]["discovered"] == expected
        assert calls == ["required-safety", *expected]
        assert payload["validation_checks"]["missing"] == []
    finally:
        store.close()
