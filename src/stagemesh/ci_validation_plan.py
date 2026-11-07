"""Deterministic, data-only hosted-CI validation planning.

Focused pull-request plans are intentionally repository-specific and exact.
Post-merge smoke validation is bounded and fails safe to full regression whenever
CI-sensitive boundaries change.  This module does not inspect Git, execute commands,
contact providers, or make network calls.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from . import validation_components as _validation_components
from .validation_components import (
    component_catalog_fingerprint,
    fast_path_pytest_targets,
    resolve_component_impact,
)

CAPABILITY_DEPENDENT_TESTS = _validation_components.CAPABILITY_DEPENDENT_TESTS
CAPABILITY_DIRECT_TESTS = _validation_components.CAPABILITY_DIRECT_TESTS
OPERATOR_DEPENDENT_TESTS = _validation_components.OPERATOR_DEPENDENT_TESTS
OPERATOR_DIRECT_TESTS = _validation_components.OPERATOR_DIRECT_TESTS
TASK_SOURCE_DEPENDENT_TESTS = _validation_components.TASK_SOURCE_DEPENDENT_TESTS
TASK_SOURCE_DIRECT_TESTS = _validation_components.TASK_SOURCE_DIRECT_TESTS

CI_VALIDATION_CONTRACT = "stagemesh.ci-validation/4"
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
        "src/stagemesh/validation_components.py",
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
        "tests/test_validation_component_catalog.py",
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


def ci_validation_policy_fingerprint() -> str:
    """Bind plan evidence to the exact selector and audited catalog policy."""
    payload = {
        "contract": CI_VALIDATION_CONTRACT,
        "catalog": component_catalog_fingerprint(),
        "mandatory_checks": MANDATORY_CHECKS,
        "full_exact_paths": sorted(_FULL_EXACT_PATHS),
        "full_prefixes": _FULL_PREFIXES,
        "dependency_names": sorted(_DEPENDENCY_NAMES),
        "shared_boundary_names": sorted(_SHARED_BOUNDARY_NAMES),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    policy_fingerprint: str = field(default_factory=ci_validation_policy_fingerprint)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "policy_fingerprint": self.policy_fingerprint,
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

    impact = resolve_component_impact(paths)
    selected_families = set(impact.families)
    if "operator" in selected_families and len(selected_families) > 1:
        return full_ci_validation_plan(
            paths,
            "audited operator roots cannot be combined with another component fast path",
        )
    if impact.fast_path_eligible:
        dependent = tuple(
            target for target in impact.dependent_tests if target not in impact.direct_tests
        )
        return CiValidationPlan(
            mode=OPERATOR if selected_families == {"operator"} else COMPONENT,
            changed_paths=paths,
            components=impact.components,
            direct_test_targets=impact.direct_tests,
            dependent_test_targets=dependent,
            mandatory_checks=MANDATORY_CHECKS,
            required_os=impact.required_os,
            browser_required=impact.browser_required,
            windows_installer_required=impact.windows_installer_required,
            runtime_coverage=True,
            reasons=(
                "every executable path is an exact audited component root or support target",
                "direct and dependent tests are immutable repository policy",
            ),
        )

    reasons = ["changed scope is mixed, unmapped, or lacks an exact audited component root"]
    if not impact.roots and all(path in fast_path_pytest_targets() for path in paths):
        reasons.append("no exact audited component source root changed")
    elif impact.unknown_paths:
        reasons.append(f"unmapped paths: {', '.join(impact.unknown_paths)}")
    elif not impact.roots:
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
    return fast_path_pytest_targets()


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
