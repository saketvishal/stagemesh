from __future__ import annotations

import json
import sys
from pathlib import Path

from stagemesh.contracts import ChangeContract, GateCommand
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.validation import Validator
from stagemesh.validation_plan import (
    CONFIG_OR_PROVIDER_COMMAND,
    CORE_LIFECYCLE_OR_SCHEMA_SECURITY,
    DOCS_ONLY,
    LOCALIZED_CODE,
    derive_validation_plan,
    planned_contract,
)


def test_provider_command_config_task_schedules_only_smoke_runtime_checks() -> None:
    contract = ChangeContract(
        objective="Switch provider command",
        validation_classification=CONFIG_OR_PROVIDER_COMMAND,
        allowed_files=("src/stagemesh/providers.py",),
        required_tests=(
            GateCommand("provider-smoke", [sys.executable, "-c", "pass"]),
            GateCommand("focused-runtime", [sys.executable, "-c", "pass"]),
            GateCommand("full pytest", [sys.executable, "-m", "pytest"]),
            GateCommand("acceptance", [sys.executable, "scripts/acceptance.py"]),
            GateCommand("clean acceptance", [sys.executable, "scripts/live_acceptance.py"]),
            GateCommand("full ci", [sys.executable, "-m", "pytest", "tests"]),
        ),
        invariants=("python scripts/invariants.py",),
    )

    plan = derive_validation_plan(contract, ("src/stagemesh/providers.py",))
    scoped = planned_contract(contract, plan)

    assert plan.classification == CONFIG_OR_PROVIDER_COMMAND
    assert plan.debug_checks == ("provider-smoke", "focused-runtime")
    blocked = {"full pytest", "acceptance", "clean acceptance", "full ci"}
    assert blocked.isdisjoint(plan.planned_checks)
    assert [gate.name for gate in scoped.gates] == ["provider-smoke", "focused-runtime"]
    assert scoped.invariants == ()


def test_docs_only_schedules_no_application_suite() -> None:
    contract = ChangeContract(
        objective="Update docs",
        allowed_files=("docs/**",),
        required_tests=(
            GateCommand("docs-static", [sys.executable, "-c", "pass"]),
            GateCommand("pytest", [sys.executable, "-m", "pytest"]),
        ),
    )

    plan = derive_validation_plan(contract, ("docs/usage.md",))
    scoped = planned_contract(contract, plan)

    assert plan.classification == DOCS_ONLY
    assert plan.debug_checks == ("docs-static",)
    assert [gate.name for gate in scoped.gates] == ["docs-static"]


def test_localized_code_schedules_focused_checks_only() -> None:
    contract = ChangeContract(
        objective="Update a localized component",
        allowed_files=("src/widgets/**",),
        required_tests=(
            GateCommand("focused-widget-tests", [sys.executable, "-m", "pytest", "tests/test_widget.py"]),
            GateCommand("focused-widget-tests-direct", ["pytest", "tests/test_widget.py"]),
            GateCommand("full pytest", [sys.executable, "-m", "pytest"]),
            GateCommand("repo pytest", [sys.executable, "-m", "pytest", "tests"]),
        ),
        typecheck=(GateCommand("focused-typecheck", [sys.executable, "-c", "pass"]),),
    )

    plan = derive_validation_plan(contract, ("src/widgets/card.py",))
    scoped = planned_contract(contract, plan)

    assert plan.classification == LOCALIZED_CODE
    assert plan.debug_checks == ("focused-widget-tests", "focused-widget-tests-direct", "focused-typecheck")
    assert [gate.name for gate in scoped.gates] == [
        "focused-widget-tests",
        "focused-widget-tests-direct",
        "focused-typecheck",
    ]


def test_core_lifecycle_schema_security_records_escalation_reason() -> None:
    contract = ChangeContract(
        objective="Change lifecycle state machine",
        allowed_files=("src/stagemesh/coordinator.py",),
        required_tests=(GateCommand("full pytest", [sys.executable, "-m", "pytest"]),),
        invariants=("python scripts/invariants.py",),
    )

    plan = derive_validation_plan(contract, ("src/stagemesh/coordinator.py",))
    scoped = planned_contract(contract, plan)

    assert plan.classification == CORE_LIFECYCLE_OR_SCHEMA_SECURITY
    assert plan.escalation_reasons
    assert "full pytest" in plan.release_checks
    assert [gate.name for gate in scoped.gates] == ["full pytest"]
    assert scoped.invariants == ("python scripts/invariants.py",)


def test_localized_focused_pytest_executes_through_planned_contract(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / "src" / "widgets").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "widgets" / "card.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "tests" / "test_widget.py").write_text(
        "from src.widgets.card import VALUE\n\n\ndef test_value() -> None:\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    workspace.commit_all("initial")
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / "TASK-2.json").write_text(
        json.dumps(
            {
                "objective": "Localized widget",
                "allowed_files": ["src/widgets/**", ".stagemesh/contracts/**"],
                "required_tests": [
                    {"name": "focused-widget-pytest", "command": [sys.executable, "-m", "pytest", "tests/test_widget.py"]},
                    {"name": "full pytest", "command": [sys.executable, "-m", "pytest"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    (project / "src" / "widgets" / "card.py").write_text("VALUE = 2\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("localized widget", source_id="TASK-2")
    store.add_candidate(task_id, sha, "test", True)

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=?",
        (task_id, sha, EvidenceKind.VALIDATION),
    ).fetchone()
    payload = json.loads(row["payload"])

    assert payload["validation_plan"]["classification"] == LOCALIZED_CODE
    assert payload["validation_plan"]["debug_checks"] == ["focused-widget-pytest"]
    assert [gate["name"] for gate in payload["gates"]] == ["focused-widget-pytest"]
    assert payload["gates"][0]["returncode"] == 0
    store.close()


def test_validator_persists_plan_and_runs_only_planned_gates(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / "src").mkdir()
    (project / "src" / "provider.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace.commit_all("initial")
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / ".stagemesh" / "contracts" / "TASK-1.json").write_text(
        json.dumps(
            {
                "objective": "Provider command",
                "validation_classification": CONFIG_OR_PROVIDER_COMMAND,
                "allowed_files": ["src/**", ".stagemesh/contracts/**"],
                "required_tests": [
                    {"name": "provider-smoke", "command": [sys.executable, "-c", "pass"]},
                    {"name": "full pytest", "command": [sys.executable, "-m", "pytest"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    (project / "src" / "provider.py").write_text("VALUE = 2\n", encoding="utf-8")
    sha = workspace.commit_all("candidate")
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("provider command", source_id="TASK-1")
    store.add_candidate(task_id, sha, "test", True)

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.PASSED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=?",
        (task_id, sha, EvidenceKind.VALIDATION),
    ).fetchone()
    payload = json.loads(row["payload"])

    assert payload["validation_plan"]["classification"] == CONFIG_OR_PROVIDER_COMMAND
    assert [gate["name"] for gate in payload["gates"]] == ["provider-smoke"]
    store.close()
