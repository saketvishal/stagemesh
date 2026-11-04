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
    with_required_checks,
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


@pytest.mark.parametrize("source, classification, components", [
    ("src/provider.py", CONFIG_OR_PROVIDER_COMMAND, []),
    ("src/stagemesh/review.py", CORE_LIFECYCLE_OR_SCHEMA_SECURITY, ["review"]),
])
def test_validator_preserves_required_gates_even_when_plan_prefers_focus(
    tmp_path: Path, source, classification, components,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / source).parent.mkdir(parents=True)
    (project / source).write_text("VALUE = 1\n", encoding="utf-8")
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
    # Keep the contract in the baseline so the candidate changes only the source root.
    workspace.commit_all("initial")
    (project / source).write_text("VALUE = 2\n", encoding="utf-8")
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

    assert payload["validation_plan"]["classification"] == classification
    assert payload["validation_plan"]["affected_components"] == components
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


def test_promoted_full_ui_quality_classified_as_broad_release_gate() -> None:
    contract = ChangeContract(
        objective="Deliver typed read-only adapter",
        allowed_files=("apps/web/src/api/timeline.ts",),
        required_tests=(
            GateCommand("focused-timeline-adapter", ("npm", "test", "timeline-api.test.ts")),
            GateCommand("promoted-full-ui-quality", ("npm", "run", "ui:quality")),
            GateCommand("spec-integrity", (sys.executable, "scripts/spec_integrity.py")),
        ),
    )

    plan = derive_validation_plan(contract, ("apps/web/src/api/timeline.ts",))
    scoped = planned_contract(contract, plan)
    required_plan = with_required_checks(contract, plan)

    assert plan.classification == LOCALIZED_CODE
    assert "focused-timeline-adapter" in plan.debug_checks
    assert "spec-integrity" in plan.debug_checks
    assert "promoted-full-ui-quality" not in plan.debug_checks
    assert "promoted-full-ui-quality" in required_plan.release_checks
    assert [gate.name for gate in scoped.gates] == ["focused-timeline-adapter", "spec-integrity"]



@pytest.mark.parametrize("source", ["validation_plan.py", "validation.py", "review.py"])
@pytest.mark.parametrize("declared", [None, DOCS_ONLY, CONFIG_OR_PROVIDER_COMMAND, LOCALIZED_CODE])
def test_validation_components_reject_narrow_classification(source, declared) -> None:
    path = f"src/stagemesh/{source}"
    contract = ChangeContract(
        objective="Change a validation boundary",
        allowed_files=(path,),
        validation_classification=declared,
        required_tests=(
            GateCommand("focused-tests", (sys.executable, "-m", "pytest", "tests/test_validation_plan.py")),
            GateCommand("full pytest", (sys.executable, "-m", "pytest")),
        ),
        invariants=("review: preserve independent review", "python scripts/invariants.py"),
    )

    # Both pre-candidate scope and actual changed files must retain broader checks.
    for changed in ((), (path,), (path.replace("/", "\\"), "docs/validation-components.md")):
        plan = derive_validation_plan(contract, changed)
        assert plan.classification == CORE_LIFECYCLE_OR_SCHEMA_SECURITY
        assert plan.risk_level == "HIGH"
        assert plan.allows_broad_validation
        assert "full pytest" in plan.release_checks
        scoped = planned_contract(contract, plan)
        assert scoped.gates == contract.gates
        assert scoped.invariants == contract.invariants


def test_unknown_component_impact_retains_explicit_broader_contract() -> None:
    contract = ChangeContract(
        objective="Change with unaudited dependent impact",
        allowed_files=("src/extension.py",),
        validation_escalation_reasons=("unknown impact: dependent callers not audited",),
        required_tests=(
            GateCommand("focused-extension", (sys.executable, "-m", "pytest", "tests/test_extension.py")),
            GateCommand("full pytest", (sys.executable, "-m", "pytest")),
        ),
        invariants=("review: preserve authorization", "python scripts/invariants.py"),
    )

    plan = derive_validation_plan(contract, ("src/extension.py",))
    assert plan.allows_broad_validation
    assert plan.to_dict()["escalation_reasons"] == ["unknown impact: dependent callers not audited"]
    assert planned_contract(contract, plan).gates == contract.gates
    assert planned_contract(contract, plan).invariants == contract.invariants
    required = with_required_checks(contract, plan)
    assert "full pytest" in required.release_checks
    assert required.escalation_reasons == plan.escalation_reasons


@pytest.mark.parametrize("path", ["src/customer/review.py", "src/stagemesh/review_helpers.py"])
def test_component_map_does_not_classify_client_or_similarly_named_files(path) -> None:
    contract = ChangeContract(objective="Localized change", allowed_files=(path,))

    plan = derive_validation_plan(contract, (path,))

    assert plan.classification == LOCALIZED_CODE
    assert not plan.allows_broad_validation


@pytest.mark.parametrize(
    ("source", "component", "direct", "dependent"),
    [
        (
            "contract_binding.py", "candidate_contract_binding",
            ["tests/test_candidate_binding_hardening.py", "tests/test_contract_admission.py",
             "tests/test_contract_fail_closed.py"],
            ["tests/test_candidate_isolation_integration.py", "tests/test_completion_integrity.py",
             "tests/test_task_recovery.py"],
        ),
        (
            "integration.py", "exact_integration_evidence",
            ["tests/test_candidate_isolation_integration.py", "tests/test_integration_recovery.py",
             "tests/test_review_reliability.py"],
            ["tests/test_completion_integrity.py", "tests/test_landing_recovery.py",
             "tests/test_reliable_delivery_end_to_end.py"],
        ),
        (
            "validation_plan.py", "planning",
            ["tests/test_validation_plan.py"],
            ["tests/test_contract_fail_closed.py", "tests/test_invariant_semantics.py",
             "tests/test_stagemesh_validation_profile.py"],
        ),
        (
            "validation.py", "validation_execution",
            ["tests/test_contract_fail_closed.py", "tests/test_validation_plan.py"],
            ["tests/test_builtin_execution_ownership.py", "tests/test_candidate_binding_hardening.py",
             "tests/test_invariant_semantics.py"],
        ),
        (
            "review.py", "review",
            ["tests/test_review_reliability.py", "tests/test_review_terminalization.py"],
            ["tests/test_autonomy_review.py", "tests/test_builtin_execution_ownership.py",
             "tests/test_invariant_semantics.py"],
        ),
    ],
)
@pytest.mark.parametrize("scope_kind", ["allowed", "changed", "windows"])
def test_audited_component_evidence_matches_exact_roots(
    source, component, direct, dependent, scope_kind,
) -> None:
    path = f"src/stagemesh/{source}"
    contract = ChangeContract(objective="Audit evidence", allowed_files=(path,))
    changed = () if scope_kind == "allowed" else (
        path.replace("/", "\\") if scope_kind == "windows" else path,
    )

    plan = derive_validation_plan(contract, changed)
    evidence = json.loads(json.dumps(plan.to_dict()))

    assert evidence["affected_components"] == [component]
    assert evidence["direct_test_targets"] == direct
    assert evidence["dependent_test_targets"] == dependent
    # Evidence does not manufacture executable gates from the documented targets.
    assert plan.debug_checks == ("focused-tests",)
    assert plan.release_checks == ()
    assert planned_contract(contract, plan).gates == ()


def test_audited_component_evidence_deduplicates_independently_of_path_order() -> None:
    paths = (
        "src/stagemesh/review.py", "src/stagemesh/validation.py",
        "src/stagemesh/validation_plan.py", "src\\stagemesh\\validation.py",
        "SRC/STAGEMESH/REVIEW.PY",
    )
    contract = ChangeContract(objective="Multiple audited roots", allowed_files=("**",))
    evidence = derive_validation_plan(contract, paths).to_dict()

    assert evidence == derive_validation_plan(contract, tuple(reversed(paths))).to_dict()
    assert evidence["affected_components"] == ["planning", "review", "validation_execution"]
    assert evidence["direct_test_targets"] == [
        "tests/test_contract_fail_closed.py", "tests/test_review_reliability.py",
        "tests/test_review_terminalization.py", "tests/test_validation_plan.py",
    ]
    assert evidence["dependent_test_targets"] == [
        "tests/test_autonomy_review.py", "tests/test_builtin_execution_ownership.py",
        "tests/test_candidate_binding_hardening.py", "tests/test_contract_fail_closed.py",
        "tests/test_invariant_semantics.py", "tests/test_stagemesh_validation_profile.py",
    ]


def test_contract_binding_and_integration_evidence_has_deterministic_union() -> None:
    paths = (
        "src/stagemesh/integration.py",
        "src\\stagemesh\\contract_binding.py",
        "SRC/STAGEMESH/INTEGRATION.PY",
    )
    contract = ChangeContract(objective="Exact evidence boundaries", allowed_files=("**",))

    evidence = derive_validation_plan(contract, paths).to_dict()

    assert evidence == derive_validation_plan(contract, tuple(reversed(paths))).to_dict()
    assert evidence["affected_components"] == [
        "candidate_contract_binding", "exact_integration_evidence",
    ]
    assert evidence["direct_test_targets"] == [
        "tests/test_candidate_binding_hardening.py",
        "tests/test_candidate_isolation_integration.py",
        "tests/test_contract_admission.py",
        "tests/test_contract_fail_closed.py",
        "tests/test_integration_recovery.py",
        "tests/test_review_reliability.py",
    ]
    assert evidence["dependent_test_targets"] == [
        "tests/test_candidate_isolation_integration.py",
        "tests/test_completion_integrity.py",
        "tests/test_landing_recovery.py",
        "tests/test_reliable_delivery_end_to_end.py",
        "tests/test_task_recovery.py",
    ]


@pytest.mark.parametrize("path", [
    "src/customer/contract_binding.py", "src/customer/integration.py", "src/customer/review.py",
    "src/customer/validation.py", "src/customer/validation_plan.py",
    "src/stagemesh/contract_binding_helpers.py", "src/stagemesh/integration_helpers.py",
    "src/stagemesh/review_helpers.py", "src/stagemesh/validation_helpers.py",
    "src/stagemesh/validation_plan_helpers.py", "src/stagemesh/review.py/child.py",
    "vendor/src/stagemesh/review.py", "src/stagemesh/contracts.py",
    "src/stagemesh/persistence.py", "src/stagemesh/execution.py", "src/unknown.py", "src/stagemesh/**",
    "tests/test_validation_plan.py", "docs/validation-components.md", "pyproject.toml",
])
def test_unaudited_paths_have_no_inferred_component_evidence(path) -> None:
    contract = ChangeContract(
        objective="Unknown impact",
        allowed_files=("src/stagemesh/review.py",),
        validation_escalation_reasons=("unknown impact: dependent callers not audited",),
        required_tests=(GateCommand("full pytest", (sys.executable, "-m", "pytest")),),
        invariants=("review: preserve authorization", "python scripts/invariants.py"),
    )

    # Actual changes override an audited allowed scope; filenames and globs do not infer edges.
    plan = derive_validation_plan(contract, (path,))
    evidence = plan.to_dict()

    assert evidence["affected_components"] == []
    assert evidence["direct_test_targets"] == []
    assert evidence["dependent_test_targets"] == []
    assert evidence["escalation_reasons"] == ["unknown impact: dependent callers not audited"]
    assert planned_contract(contract, plan).gates == contract.gates
    assert planned_contract(contract, plan).invariants == contract.invariants
    required = with_required_checks(contract, plan)
    assert "full pytest" in required.planned_checks
    assert required.to_dict()["affected_components"] == []


@pytest.mark.parametrize("other_paths", [
    ("src/stagemesh/contracts.py", "src/unknown.py"),
    ("src/customer/review.py",),
    ("src/stagemesh/review_helpers.py",),
    ("src/stagemesh/**",),
    ("pyproject.toml",),
    ("scripts/invariants.py",),
    ("README.md",),
    ("docs_extra/guide.md",),
    ("tests_extra/test_review.py",),
])
@pytest.mark.parametrize("scope_kind", ["allowed", "changed", "windows"])
def test_audited_component_evidence_preserves_required_checks_with_mixed_scope(
    other_paths, scope_kind,
) -> None:
    scope = ("src/stagemesh/review.py", *other_paths)
    if scope_kind == "windows":
        scope = tuple(path.replace("/", "\\") for path in scope)
    contract = ChangeContract(
        objective="Audited and unknown impact",
        allowed_files=scope if scope_kind == "allowed" else ("src/stagemesh/review.py",),
        validation_escalation_reasons=("unknown impact: shared contract callers",),
        required_tests=(
            GateCommand("focused-tests", (sys.executable, "-c", "pass")),
            GateCommand("full pytest", (sys.executable, "-m", "pytest")),
        ),
        invariants=("review: preserve independent review", "python scripts/invariants.py"),
    )
    known = derive_validation_plan(contract, ("src/stagemesh/review.py",))
    mixed = derive_validation_plan(
        contract, () if scope_kind == "allowed" else scope,
    )

    assert known.to_dict()["affected_components"] == ["review"]
    # An unaudited path outside docs/tests makes the mapping incomplete, so the evidence
    # fails closed instead of presenting a partial set as the affected test map.
    assert mixed.to_dict()["affected_components"] == []
    assert mixed.to_dict()["direct_test_targets"] == []
    assert mixed.to_dict()["dependent_test_targets"] == []
    required = with_required_checks(contract, mixed)
    assert required.to_dict()["affected_components"] == []
    assert required.direct_test_targets == mixed.direct_test_targets
    assert required.dependent_test_targets == mixed.dependent_test_targets
    assert required.escalation_reasons == ("unknown impact: shared contract callers",)
    assert required.debug_checks == ("focused-tests", "full pytest")
    assert required.release_checks == ("full pytest", "invariant:python scripts/invariants.py")
    assert planned_contract(contract, required).gates == contract.gates
    assert planned_contract(contract, required).invariants == contract.invariants


@pytest.mark.parametrize("scope_kind", ["allowed", "changed", "windows"])
def test_supporting_docs_and_tests_are_neutral_to_audited_evidence(scope_kind) -> None:
    scope = (
        "src/stagemesh/review.py", "docs/validation-components.md",
        "tests/test_validation_plan.py", "docs/**", "tests/**",
    )
    if scope_kind == "windows":
        scope = tuple(path.replace("/", "\\") for path in scope)
    contract = ChangeContract(objective="Supporting changes", allowed_files=scope)

    evidence = derive_validation_plan(contract, () if scope_kind == "allowed" else scope).to_dict()

    assert evidence["affected_components"] == ["review"]
    assert evidence["direct_test_targets"] == [
        "tests/test_review_reliability.py", "tests/test_review_terminalization.py",
    ]
    assert evidence["dependent_test_targets"] == [
        "tests/test_autonomy_review.py", "tests/test_builtin_execution_ownership.py",
        "tests/test_invariant_semantics.py",
    ]


@pytest.mark.parametrize(
    ("path", "required", "reason"),
    [
        ("README.md", False, "documentation-only scope"),
        ("stagemesh.toml", False, "configuration-only scope"),
        ("src/stagemesh/providers.py", True, "executable provider or configuration source"),
        ("src/widgets/card.py", True, "localized executable source"),
        ("src/stagemesh/contract_binding.py", True, "core lifecycle, schema, security"),
        ("src/stagemesh/integration.py", True, "core lifecycle, schema, security"),
        ("src/stagemesh/review.py", True, "core lifecycle, schema, security"),
        ("src\\stagemesh\\validation_plan.py", True, "test-selection policy changed"),
    ],
)
def test_full_regression_decision_is_explicit_and_conservative(path, required, reason) -> None:
    evidence = derive_validation_plan(ChangeContract("Plan", allowed_files=(path,)), (path,)).to_dict()

    assert evidence["full_regression_required"] is required
    assert any(reason in item for item in evidence["full_regression_reasons"])


def test_full_regression_decision_fails_safe_for_incomplete_or_empty_scope() -> None:
    mixed = derive_validation_plan(
        ChangeContract("Mixed", allowed_files=("src/stagemesh/review.py", "src/unknown.py")),
        ("src/stagemesh/review.py", "src/unknown.py"),
    ).to_dict()
    empty = derive_validation_plan(ChangeContract("Empty", allowed_files=()), ()).to_dict()

    assert mixed["affected_components"] == []
    assert mixed["full_regression_required"] is True
    assert "component impact map is incomplete for the changed scope" in mixed["full_regression_reasons"]
    assert empty["full_regression_required"] is True
    assert "change scope is empty or unavailable" in empty["full_regression_reasons"]


def test_required_check_enrichment_preserves_full_regression_decision() -> None:
    contract = ChangeContract(
        "Review", allowed_files=("src/stagemesh/review.py",),
        required_tests=(GateCommand("full pytest", [sys.executable, "-m", "pytest"]),),
    )
    plan = derive_validation_plan(contract, ("src/stagemesh/review.py",))

    enriched = with_required_checks(contract, plan)

    assert enriched.full_regression_required is True
    assert enriched.full_regression_reasons == plan.full_regression_reasons
