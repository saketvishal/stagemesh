from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
    contract_with_browser_e2e_gates,
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


def test_ci_inside_a_focused_test_path_does_not_make_gate_broad() -> None:
    contract = ChangeContract(
        objective="Focused API regression",
        allowed_files=("apps/api/tests/integration/test_trial_review_tenant_isolation.py",),
        required_tests=(
            GateCommand(
                "trial-review-tenant-isolation",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/integration/test_trial_review_tenant_isolation.py",
                    "tests/integration/test_authorization.py",
                    "tests/integration/test_auth_atomicity.py",
                    "-q",
                ],
                cwd="apps/api",
            ),
        ),
    )

    plan = derive_validation_plan(contract, ("apps/api/tests/integration/test_trial_review_tenant_isolation.py",))
    scoped = planned_contract(contract, plan)

    assert plan.debug_checks == ("trial-review-tenant-isolation",)
    assert [gate.name for gate in scoped.gates] == ["trial-review-tenant-isolation"]


def test_frontend_change_discovers_browser_e2e_script(tmp_path: Path) -> None:
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test smoke.spec.ts"}}),
        encoding="utf-8",
    )
    contract = ChangeContract(objective="Fix frontend flow", allowed_files=("apps/web/src/App.tsx",))

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/web/src/App.tsx",))
    plan = derive_validation_plan(updated, ("apps/web/src/App.tsx",))

    assert [gate.name for gate in updated.gates] == ["browser-e2e:apps/web"]
    assert updated.gates[0].command == ("npm", "run", "e2e:ui-gate")
    assert updated.gates[0].cwd == "apps/web"
    assert "browser-e2e:apps/web" in plan.debug_checks


def test_non_frontend_change_does_not_discover_browser_e2e_script(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}), encoding="utf-8")
    contract = ChangeContract(objective="Fix parser", allowed_files=("src/parser.py",))

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("src/parser.py",))

    assert updated.gates == ()


def test_localized_apps_web_task_excludes_forbidden_web_old(tmp_path: Path) -> None:
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test web.spec.ts"}}),
        encoding="utf-8",
    )
    web_old = tmp_path / "apps" / "web-old"
    web_old.mkdir(parents=True)
    (web_old / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test legacy.spec.ts"}}),
        encoding="utf-8",
    )
    contract = ChangeContract(
        objective="Fix new matter UI",
        allowed_files=("apps/web/src/pages/NewMatter.tsx",),
        forbidden_files=("apps/web-old/**", "apps/api/**"),
    )

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/web/src/pages/NewMatter.tsx",))

    assert [gate.name for gate in updated.gates] == ["browser-e2e:apps/web"]
    assert "browser-e2e:apps/web-old" not in [gate.name for gate in updated.gates]


def test_localized_apps_web_task_excludes_historical_checkout_scripts(tmp_path: Path) -> None:
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test web.spec.ts"}}),
        encoding="utf-8",
    )
    historical = tmp_path / ".historical-checkouts" / "snapshot" / "apps" / "web"
    historical.mkdir(parents=True)
    (historical / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test old.spec.ts"}}),
        encoding="utf-8",
    )
    contract = ChangeContract(
        objective="Fix frontend",
        allowed_files=("apps/web/src/App.tsx",),
    )

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/web/src/App.tsx",))

    assert [gate.name for gate in updated.gates] == ["browser-e2e:apps/web"]


def test_archived_package_exclusion_is_owned_by_the_contract(tmp_path: Path) -> None:
    web_old = tmp_path / "apps" / "web-old"
    web_old.mkdir(parents=True)
    (web_old / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test legacy.spec.ts"}}),
        encoding="utf-8",
    )

    contract_default = ChangeContract(objective="General frontend UI", allowed_files=("**",), forbidden_files=("apps/web-old/**",))
    updated_default = contract_with_browser_e2e_gates(tmp_path, contract_default, ())
    assert updated_default.gates == ()

    # Directory names alone do not establish a package's authority or status.
    contract_explicit = ChangeContract(objective="Legacy frontend migration", allowed_files=("apps/web-old/**",))
    updated_explicit = contract_with_browser_e2e_gates(tmp_path, contract_explicit, ())
    assert [gate.name for gate in updated_explicit.gates] == ["browser-e2e:apps/web-old"]


def test_localized_task_does_not_discover_root_package_gate(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test unrelated"}}), encoding="utf-8")
    (tmp_path / "apps/widget").mkdir(parents=True)
    (tmp_path / "apps/widget/package.json").write_text("{}", encoding="utf-8")
    contract = ChangeContract(objective="Fix frontend", allowed_files=("apps/widget/src/**",))
    assert contract_with_browser_e2e_gates(tmp_path, contract, ("apps/widget/src/main.tsx",)).gates == ()


@pytest.mark.parametrize("scope", [("apps/*/src/**",), ("**/*.tsx",)])
def test_wildcard_source_scope_keeps_owning_package_gate(tmp_path: Path, scope) -> None:
    package = tmp_path / "apps/customer"
    (package / "src").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}), encoding="utf-8")
    (package / "src/App.tsx").write_text("export default 1", encoding="utf-8")
    contract = ChangeContract(objective="Fix frontend", allowed_files=scope)
    result = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/customer/src/App.tsx",))
    assert [g.name for g in result.gates] == ["browser-e2e:apps/customer"]


def test_root_frontend_source_keeps_root_browser_gate(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}), encoding="utf-8")
    contract = ChangeContract(objective="Fix frontend", allowed_files=("src/App.tsx",))
    assert [g.name for g in contract_with_browser_e2e_gates(tmp_path, contract, ("src/App.tsx",)).gates] == ["browser-e2e"]


def test_deleted_source_keeps_owning_browser_gate(tmp_path: Path) -> None:
    package = tmp_path / "apps/customer"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}), encoding="utf-8")
    contract = ChangeContract(objective="Fix frontend", allowed_files=("**/*.tsx",))
    assert [g.name for g in contract_with_browser_e2e_gates(tmp_path, contract, ("apps/customer/src/deleted.tsx",)).gates] == ["browser-e2e:apps/customer"]


def test_excluded_package_does_not_assign_its_sources_to_root(tmp_path: Path) -> None:
    package = tmp_path / "apps/customer"
    package.mkdir(parents=True)
    for owner in (tmp_path, package):
        (owner / "package.json").write_text(json.dumps({"scripts": {"e2e": "playwright test"}}), encoding="utf-8")
    contract = ChangeContract(objective="Fix frontend", allowed_files=("apps/customer/src/App.tsx",), forbidden_files=("apps/customer/package.json",))
    assert contract_with_browser_e2e_gates(tmp_path, contract, ("apps/customer/src/App.tsx",)).gates == ()


def test_explicit_browser_gate_preserved(tmp_path: Path) -> None:
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(
        json.dumps({"scripts": {"e2e:ui-gate": "playwright test web.spec.ts"}}),
        encoding="utf-8",
    )
    explicit_gate = GateCommand("browser-e2e:apps/web", ["npm", "run", "custom-e2e"])
    contract = ChangeContract(
        objective="Fix frontend with explicit gate",
        allowed_files=("apps/web/src/App.tsx",),
        required_tests=(explicit_gate,),
    )

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/web/src/App.tsx",))

    assert len(updated.gates) == 1
    assert updated.gates[0].command == ["npm", "run", "custom-e2e"]


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
    assert [gate["name"] for gate in payload["gates"]] == ["focused-widget-pytest", "full pytest"]
    assert payload["validation_plan"]["release_checks"] == ["full pytest"]
    assert payload["gates"][0]["returncode"] == 0
    store.close()


def test_validator_preserves_required_gates_even_when_plan_prefers_focus(tmp_path: Path) -> None:
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

    assert Validator().validate(store, task_id, sha, project) is EvidenceStatus.FAILED
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=?",
        (task_id, sha, EvidenceKind.VALIDATION),
    ).fetchone()
    payload = json.loads(row["payload"])

    assert payload["validation_plan"]["classification"] == CONFIG_OR_PROVIDER_COMMAND
    assert [gate["name"] for gate in payload["gates"]] == ["provider-smoke", "full pytest"]
    assert payload["gates"][1]["status"] == "FAILED"  # The declared suite found no tests (pytest exit 5).
    assert payload["validation_checks"]["missing"] == []
    store.close()


def test_browser_discovery_ignores_unrelated_frontend_package(tmp_path: Path) -> None:
    for name in ("customer", "operator"):
        package = tmp_path / "apps" / name / "package.json"
        package.parent.mkdir(parents=True)
        package.write_text(json.dumps({"scripts": {"test:e2e": "playwright test"}}))
    contract = ChangeContract(
        objective="Fix customer frontend",
        allowed_files=("apps/customer/src/Card.tsx",),
    )

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/customer/src/Card.tsx",))

    assert [gate.cwd for gate in updated.gates] == ["apps/customer"]


def test_forbidden_browser_package_wins_but_explicit_gate_is_retained(tmp_path: Path) -> None:
    package = tmp_path / "apps" / "frontend" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"scripts": {"test:e2e": "playwright test"}}))
    explicit = GateCommand("explicit-api-browser", ("npm", "run", "approved-e2e"), cwd="apps/frontend")
    contract = ChangeContract(
        objective="Verify frontend API behavior",
        allowed_files=("apps/**",),
        forbidden_files=("apps/frontend/**",),
        required_tests=(explicit,),
    )

    updated = contract_with_browser_e2e_gates(tmp_path, contract, ("apps/other/Page.tsx",))

    assert updated.gates == (explicit,)
