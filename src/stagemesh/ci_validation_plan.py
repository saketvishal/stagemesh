"""Deterministic, data-only hosted-CI validation planning.

Focused pull-request plans are intentionally repository-specific and exact.
Post-merge smoke validation is bounded and fails safe to full regression whenever
CI-sensitive boundaries change.  This module does not inspect Git, execute commands,
contact providers, or make network calls.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

CI_VALIDATION_CONTRACT = "stagemesh.ci-validation/3"
FULL = "full"
OPERATOR = "operator"
COMPONENT = "component"
DOCS = "docs"
SMOKE = "smoke"

Mode = Literal["full", "operator", "component", "docs", "smoke"]

MANDATORY_CHECKS = (
    "attribution-and-static-range-policy",
    "focused-validation-policy",
    "declarative-and-executable-contract-invariants",
)

OPERATOR_DIRECT_TESTS = (
    "tests/test_activity.py",
    "tests/test_activity_cli.py",
    "tests/test_activity_evidence.py",
    "tests/test_activity_periods.py",
    "tests/test_execution_detail_api.py",
    "tests/test_operator_api.py",
    "tests/test_operator_report.py",
    "tests/test_task_detail_api.py",
)

OPERATOR_DEPENDENT_TESTS = (
    "tests/test_api_server.py",
    "tests/test_operator_ui_contract.py",
    "tests/test_ui_assets.py",
    "tests/test_ui_browser_behavior.py",
    "tests/test_ui_error_policy.py",
    "tests/test_ui_launcher.py",
    "tests/test_windows_user_installer.py",
)

CAPABILITY_DIRECT_TESTS = (
    "tests/test_agent_skills.py",
    "tests/test_capability_registry.py",
    "tests/test_codex_mcp_authorization.py",
    "tests/test_mcp_proxy.py",
    "tests/test_provider_events.py",
)

CAPABILITY_DEPENDENT_TESTS = (
    "tests/test_provider_acceptance.py",
    "tests/test_provider_authorization.py",
    "tests/test_provider_registry.py",
    "tests/test_run_window.py",
    "tests/test_task_recovery.py",
)

TASK_SOURCE_DIRECT_TESTS = (
    "tests/test_github_task_sources.py",
    "tests/test_invariants.py",
    "tests/test_outbound_lifecycle.py",
    "tests/test_source_sync_preserves_progress.py",
    "tests/test_zero_config_github.py",
)

TASK_SOURCE_DEPENDENT_TESTS = (
    "tests/test_admission_diagnosis_regression.py",
    "tests/test_objective_root_reconciliation.py",
    "tests/test_queue_reconcile.py",
    "tests/test_selection_scope.py",
    "tests/test_source_admission_publication.py",
)

_OPERATOR_COMPONENTS = {
    "src/stagemesh/activity.py": "activity_read_model",
    "src/stagemesh/activity_periods.py": "activity_read_model",
    "src/stagemesh/operator.py": "operator_projection",
    "src/stagemesh/api_server.py": "loopback_operator_api",
    "src/stagemesh/ui/app.js": "packaged_operator_ui",
    "src/stagemesh/ui/index.html": "packaged_operator_ui",
    "src/stagemesh/ui/app.css": "packaged_operator_ui",
}
@dataclass(frozen=True)
class AuditedComponent:
    family: str
    roots: tuple[tuple[str, str], ...]
    direct_tests: tuple[str, ...]
    dependent_tests: tuple[str, ...]
    required_os: tuple[str, ...] = ("windows", "linux")
    browser_required: bool = False
    windows_installer_required: bool = False

    @property
    def root_paths(self) -> frozenset[str]:
        return frozenset(path for path, _component in self.roots)

    @property
    def support_paths(self) -> frozenset[str]:
        return frozenset((*self.direct_tests, *self.dependent_tests))


_AUDITED_COMPONENTS = (
    AuditedComponent(
        "operator",
        tuple(_OPERATOR_COMPONENTS.items()),
        OPERATOR_DIRECT_TESTS,
        OPERATOR_DEPENDENT_TESTS,
        browser_required=True,
        windows_installer_required=True,
    ),
    AuditedComponent(
        "capabilities",
        (
            ("src/stagemesh/agent_skills.py", "bundled_agent_skills"),
            ("src/stagemesh/capabilities.py", "capability_registry"),
            ("src/stagemesh/capability_contracts.py", "capability_contracts"),
            ("src/stagemesh/capability_discovery.py", "capability_discovery"),
            ("src/stagemesh/capability_vendor.py", "capability_vendoring"),
            ("src/stagemesh/claude_plugin_capabilities.py", "claude_plugin_capabilities"),
            ("src/stagemesh/mcp_capabilities.py", "mcp_capabilities"),
            ("src/stagemesh/provider_events.py", "capability_evidence"),
        ),
        CAPABILITY_DIRECT_TESTS,
        CAPABILITY_DEPENDENT_TESTS,
    ),
    AuditedComponent(
        "task-sources",
        (("src/stagemesh/task_sources.py", "task_source_sync"),),
        TASK_SOURCE_DIRECT_TESTS,
        TASK_SOURCE_DEPENDENT_TESTS,
    ),
)

# These files define selection, protected jobs, developer admission, dependencies,
# packaging, or shared interfaces.  They can never select themselves into a narrow
# run.  Prefix checks below cover the corresponding directories.
_FULL_EXACT_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "agents.md",
        "build_backend.py",
        "pyproject.toml",
        "src/stagemesh/ci_validation_plan.py",
        "src/stagemesh/cli.py",
        "src/stagemesh/config.py",
        "src/stagemesh/contracts.py",
        "src/stagemesh/coordinator.py",
        "src/stagemesh/domain.py",
        "src/stagemesh/persistence.py",
        "src/stagemesh/postgres_store.py",
        "src/stagemesh/profile.py",
        "src/stagemesh/regression_orchestrator.py",
        "src/stagemesh/security.py",
        "src/stagemesh/test_isolation.py",
        "src/stagemesh/test_taxonomy.py",
        "src/stagemesh/validation.py",
        "src/stagemesh/validation_services.py",
        "src/stagemesh/validation_plan.py",
        "scripts/ci_validation_plan.py",
        "scripts/run_ci_component_tests.py",
        "scripts/check_ci_range.py",
        "scripts/install-stagemesh.ps1",
        "scripts/run_regression.py",
        "tests/test_ci_validation_plan.py",
        "tests/test_ci_component_runner.py",
        "tests/test_ci_workflow_policy.py",
        "tests/test_regression_architecture_acceptance.py",
        "docs/focused-validation.md",
        "docs/validation-components.md",
    }
)
_FULL_PREFIXES = (
    ".github/",
    ".stagemesh/",
    "migrations/",
    "schema/",
    "docs/profiles/",
    "src/stagemesh/migrations/",
)
_DEPENDENCY_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "poetry.lock",
        "pdm.lock",
        "uv.lock",
    }
)
_SHARED_BOUNDARY_NAMES = frozenset(
    {
        "authorization.py",
        "claims.py",
        "contract_binding.py",
        "execution.py",
        "integration.py",
        "lifecycle.py",
        "permissions.py",
        "provider_cooldown.py",
        "provider_pool.py",
        "providers.py",
        "redaction.py",
        "review.py",
    }
)


@dataclass(frozen=True)
class CiValidationPlan:
    mode: Mode
    changed_paths: tuple[str, ...]
    components: tuple[str, ...]
    direct_test_targets: tuple[str, ...]
    dependent_test_targets: tuple[str, ...]
    mandatory_checks: tuple[str, ...]
    required_os: tuple[str, ...]
    browser_required: bool
    windows_installer_required: bool
    runtime_coverage: bool
    reasons: tuple[str, ...]
    contract: str = CI_VALIDATION_CONTRACT

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "mode": self.mode,
            "changed_paths": list(self.changed_paths),
            "components": list(self.components),
            "direct_test_targets": list(self.direct_test_targets),
            "dependent_test_targets": list(self.dependent_test_targets),
            "mandatory_checks": list(self.mandatory_checks),
            "required_os": list(self.required_os),
            "browser_required": self.browser_required,
            "windows_installer_required": self.windows_installer_required,
            "runtime_coverage": self.runtime_coverage,
            "reasons": list(self.reasons),
        }


def derive_ci_validation_plan(changed_paths: Iterable[str]) -> CiValidationPlan:
    """Return a stable plan for exact changed paths; uncertainty always means full."""
    try:
        paths = tuple(sorted({_normalize_path(path) for path in changed_paths}))
    except (TypeError, ValueError) as exc:
        return full_ci_validation_plan((), f"invalid changed path: {exc}")
    if not paths:
        return full_ci_validation_plan((), "changed path set is empty or unavailable")

    protected = tuple(path for path in paths if _requires_full(path))
    if protected:
        return full_ci_validation_plan(
            paths,
            "selection, workflow, profile, dependency, schema, persistence, lifecycle, "
            "authorization, security, packaging, or shared-interface boundary changed",
            f"full-regression paths: {', '.join(protected)}",
        )

    if all(_is_documentation(path) for path in paths):
        return CiValidationPlan(
            mode=DOCS,
            changed_paths=paths,
            components=("documentation",),
            direct_test_targets=(),
            dependent_test_targets=(),
            mandatory_checks=MANDATORY_CHECKS,
            required_os=("portable",),
            browser_required=False,
            windows_installer_required=False,
            runtime_coverage=False,
            reasons=(
                "documentation-only paths use the portable policy path",
                "no application runtime coverage is claimed",
            ),
        )

    selected = tuple(
        component
        for component in _AUDITED_COMPONENTS
        if component.root_paths.intersection(paths)
    )
    selected_families = {component.family for component in selected}
    if "operator" in selected_families and len(selected_families) > 1:
        return full_ci_validation_plan(
            paths,
            "audited operator roots cannot be combined with another component fast path",
        )
    allowed = frozenset(
        path
        for component in selected
        for path in (*component.root_paths, *component.support_paths)
    )
    known_support = frozenset(
        path for component in _AUDITED_COMPONENTS for path in component.support_paths
    )
    unknown = tuple(
        path
        for path in paths
        if path not in allowed
        and not _is_documentation(path)
        and not (not selected and path in known_support)
    )
    if selected and not unknown:
        direct = tuple(
            sorted({target for component in selected for target in component.direct_tests})
        )
        dependent = tuple(
            sorted(
                {
                    target
                    for component in selected
                    for target in component.dependent_tests
                    if target not in direct
                }
            )
        )
        root_components = {
            path: name for component in selected for path, name in component.roots
        }
        return CiValidationPlan(
            mode=OPERATOR if selected_families == {"operator"} else COMPONENT,
            changed_paths=paths,
            components=tuple(
                sorted({root_components[path] for path in paths if path in root_components})
            ),
            direct_test_targets=direct,
            dependent_test_targets=dependent,
            mandatory_checks=MANDATORY_CHECKS,
            required_os=_ordered_os(
                os_name for component in selected for os_name in component.required_os
            ),
            browser_required=any(component.browser_required for component in selected),
            windows_installer_required=any(
                component.windows_installer_required for component in selected
            ),
            runtime_coverage=True,
            reasons=(
                "every executable path is an exact audited component root or support target",
                "direct and dependent tests are immutable repository policy",
            ),
        )

    reasons = ["changed scope is mixed, unmapped, or lacks an exact audited component root"]
    if unknown:
        reasons.append(f"unmapped paths: {', '.join(unknown)}")
    elif not selected:
        reasons.append("no exact audited component source root changed")
    return full_ci_validation_plan(paths, *reasons)


def full_ci_validation_plan(
    changed_paths: Iterable[str] = (), *reasons: str
) -> CiValidationPlan:
    """Construct explicit full-regression evidence for unavailable or forced scope."""
    try:
        paths = tuple(sorted({_normalize_path(path) for path in changed_paths}))
    except (TypeError, ValueError):
        paths = ()
    recorded = tuple(dict.fromkeys(reason for reason in reasons if reason)) or (
        "full regression explicitly required",
    )
    return CiValidationPlan(
        mode=FULL,
        changed_paths=paths,
        components=("complete_regression",),
        direct_test_targets=(),
        dependent_test_targets=(),
        mandatory_checks=MANDATORY_CHECKS,
        required_os=("windows", "linux"),
        browser_required=True,
        windows_installer_required=True,
        runtime_coverage=True,
        reasons=recorded,
    )


def post_merge_ci_validation_plan(
    changed_paths: Iterable[str], *, protected_merge: bool = False
) -> CiValidationPlan:
    """Return a bounded main-push plan, failing safe for CI-sensitive changes."""
    try:
        paths = tuple(sorted({_normalize_path(path) for path in changed_paths}))
    except (TypeError, ValueError) as exc:
        return full_ci_validation_plan((), f"invalid post-merge changed path: {exc}")
    if not paths:
        return full_ci_validation_plan(
            (), "post-merge changed path set is empty or unavailable"
        )
    protected = tuple(path for path in paths if _requires_full(path))
    if protected and not protected_merge:
        return full_ci_validation_plan(
            paths,
            "post-merge CI-sensitive boundary changed",
            f"full-regression paths: {', '.join(protected)}",
        )
    return CiValidationPlan(
        mode=SMOKE,
        changed_paths=paths,
        components=("protected_merge_smoke" if protected_merge else "post_merge_smoke",),
        direct_test_targets=(),
        dependent_test_targets=(),
        mandatory_checks=MANDATORY_CHECKS,
        required_os=("windows", "linux"),
        browser_required=False,
        windows_installer_required=False,
        runtime_coverage=False,
        reasons=(
            (
                "exact two-parent protected merge topology confirms pull-request validation covered the candidate"
                if protected_merge
                else "protected pull-request validation already covered the candidate"
            ),
            "main push retains exact range, bootstrap, focused policy, import, and invariants",
        ),
    )


def audited_pytest_targets() -> frozenset[str]:
    """Return the immutable target allowlist consumed by the component runner."""
    return frozenset(
        target
        for component in _AUDITED_COMPONENTS
        for target in (*component.direct_tests, *component.dependent_tests)
    )


def _ordered_os(values: Iterable[str]) -> tuple[str, ...]:
    selected = set(values)
    order = ("windows", "linux", "portable")
    return tuple(name for name in order if name in selected)


def _normalize_path(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("paths must be strings")
    value = raw.strip().replace("\\", "/")
    if not value or value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise ValueError("path must be non-empty and repository-relative")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path must remain inside the repository")
    return PurePosixPath(*parts).as_posix().casefold()


def _requires_full(path: str) -> bool:
    if path in _FULL_EXACT_PATHS or path.startswith(_FULL_PREFIXES):
        return True
    name = PurePosixPath(path).name
    if name in _DEPENDENCY_NAMES or name.startswith("requirements") and name.endswith(".txt"):
        return True
    if path.startswith("src/stagemesh/") and name in _SHARED_BOUNDARY_NAMES:
        return True
    return "schema" in PurePosixPath(path).parts or "security" in PurePosixPath(path).parts


def _is_documentation(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        name in {"readme", "readme.md", "readme.rst"}
        or path.startswith("docs/") and PurePosixPath(path).suffix in {".md", ".rst", ".txt"}
        or PurePosixPath(path).suffix in {".md", ".rst"} and "/" not in path
    )
