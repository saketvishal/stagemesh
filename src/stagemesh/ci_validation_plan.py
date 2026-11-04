"""Deterministic, data-only hosted-CI validation planning.

The narrow plan is intentionally repository-specific and exact.  It records one
audited read-only operator boundary; everything else retains the complete hosted
regression.  This module does not inspect Git, execute commands, contact providers,
or make network calls.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

CI_VALIDATION_CONTRACT = "stagemesh.ci-validation/1"
FULL = "full"
OPERATOR = "operator"
DOCS = "docs"

Mode = Literal["full", "operator", "docs"]

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

_OPERATOR_COMPONENTS = {
    "src/stagemesh/activity.py": "activity_read_model",
    "src/stagemesh/activity_periods.py": "activity_read_model",
    "src/stagemesh/operator.py": "operator_projection",
    "src/stagemesh/api_server.py": "loopback_operator_api",
    "src/stagemesh/ui/app.js": "packaged_operator_ui",
    "src/stagemesh/ui/index.html": "packaged_operator_ui",
    "src/stagemesh/ui/app.css": "packaged_operator_ui",
}
_OPERATOR_SUPPORT = frozenset((*OPERATOR_DIRECT_TESTS, *OPERATOR_DEPENDENT_TESTS))

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
        "src/stagemesh/security.py",
        "src/stagemesh/validation.py",
        "src/stagemesh/validation_services.py",
        "src/stagemesh/validation_plan.py",
        "scripts/ci_validation_plan.py",
        "scripts/check_ci_range.py",
        "scripts/install-stagemesh.ps1",
        "tests/test_ci_validation_plan.py",
        "tests/test_ci_workflow_policy.py",
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

    operator_roots = tuple(path for path in paths if path in _OPERATOR_COMPONENTS)
    unknown = tuple(
        path
        for path in paths
        if path not in _OPERATOR_COMPONENTS and path not in _OPERATOR_SUPPORT and not _is_documentation(path)
    )
    if operator_roots and not unknown:
        components = tuple(sorted({_OPERATOR_COMPONENTS[path] for path in operator_roots}))
        return CiValidationPlan(
            mode=OPERATOR,
            changed_paths=paths,
            components=components,
            direct_test_targets=OPERATOR_DIRECT_TESTS,
            dependent_test_targets=OPERATOR_DEPENDENT_TESTS,
            mandatory_checks=MANDATORY_CHECKS,
            required_os=("windows", "linux"),
            browser_required=True,
            windows_installer_required=True,
            runtime_coverage=True,
            reasons=(
                "every executable path is an exact audited read-only operator root",
                "supporting paths are limited to the exact operator tests or documentation",
            ),
        )

    reasons = ["changed scope is mixed, unmapped, or lacks an exact audited operator root"]
    if unknown:
        reasons.append(f"unmapped paths: {', '.join(unknown)}")
    elif not operator_roots:
        reasons.append("no exact audited operator source root changed")
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
