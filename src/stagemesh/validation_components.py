"""Immutable audited component boundaries shared by local and hosted validation.

The catalog is deliberately repository-specific.  Exact roots may opt into a
focused fast path only when their direct and dependent contracts have been
reviewed.  Unknown, mixed, or cross-cutting scope remains broad by default.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class AuditedRoot:
    path: str
    component: str
    family: str
    direct_tests: tuple[str, ...]
    dependent_tests: tuple[str, ...]
    required_os: tuple[str, ...] = ("windows", "linux")
    fast_path: bool = False
    exclusive_family: bool = False
    browser_required: bool = False
    windows_installer_required: bool = False


@dataclass(frozen=True)
class ComponentImpact:
    roots: tuple[AuditedRoot, ...]
    components: tuple[str, ...]
    families: tuple[str, ...]
    direct_tests: tuple[str, ...]
    dependent_tests: tuple[str, ...]
    required_os: tuple[str, ...]
    complete: bool
    fast_path_eligible: bool
    browser_required: bool
    windows_installer_required: bool
    unknown_paths: tuple[str, ...]


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


def _family_roots(
    *,
    family: str,
    roots: tuple[tuple[str, str], ...],
    direct: tuple[str, ...],
    dependent: tuple[str, ...],
    exclusive: bool = False,
    browser: bool = False,
    windows_installer: bool = False,
) -> tuple[AuditedRoot, ...]:
    return tuple(
        AuditedRoot(
            path=path,
            component=component,
            family=family,
            direct_tests=direct,
            dependent_tests=dependent,
            fast_path=True,
            exclusive_family=exclusive,
            browser_required=browser,
            windows_installer_required=windows_installer,
        )
        for path, component in roots
    )


AUDITED_ROOTS = (
    AuditedRoot(
        "src/stagemesh/contract_binding.py",
        "candidate_contract_binding",
        "validation-core",
        (
            "tests/test_candidate_binding_hardening.py",
            "tests/test_contract_admission.py",
            "tests/test_contract_fail_closed.py",
        ),
        (
            "tests/test_candidate_isolation_integration.py",
            "tests/test_completion_integrity.py",
            "tests/test_task_recovery.py",
        ),
    ),
    AuditedRoot(
        "src/stagemesh/integration.py",
        "exact_integration_evidence",
        "validation-core",
        (
            "tests/test_candidate_isolation_integration.py",
            "tests/test_integration_recovery.py",
            "tests/test_review_reliability.py",
        ),
        (
            "tests/test_completion_integrity.py",
            "tests/test_landing_recovery.py",
            "tests/test_reliable_delivery_end_to_end.py",
        ),
    ),
    AuditedRoot(
        "src/stagemesh/validation_plan.py",
        "planning",
        "validation-core",
        ("tests/test_validation_plan.py",),
        (
            "tests/test_contract_fail_closed.py",
            "tests/test_invariant_semantics.py",
            "tests/test_stagemesh_validation_profile.py",
        ),
    ),
    AuditedRoot(
        "src/stagemesh/validation.py",
        "validation_execution",
        "validation-core",
        ("tests/test_contract_fail_closed.py", "tests/test_validation_plan.py"),
        (
            "tests/test_builtin_execution_ownership.py",
            "tests/test_candidate_binding_hardening.py",
            "tests/test_invariant_semantics.py",
        ),
    ),
    AuditedRoot(
        "src/stagemesh/review.py",
        "review",
        "validation-core",
        ("tests/test_review_reliability.py", "tests/test_review_terminalization.py"),
        (
            "tests/test_autonomy_review.py",
            "tests/test_builtin_execution_ownership.py",
            "tests/test_invariant_semantics.py",
        ),
    ),
    *_family_roots(
        family="operator",
        roots=(
            ("src/stagemesh/activity.py", "activity_read_model"),
            ("src/stagemesh/activity_periods.py", "activity_read_model"),
            ("src/stagemesh/operator.py", "operator_projection"),
            ("src/stagemesh/api_server.py", "loopback_operator_api"),
            ("src/stagemesh/ui/app.js", "packaged_operator_ui"),
            ("src/stagemesh/ui/index.html", "packaged_operator_ui"),
            ("src/stagemesh/ui/app.css", "packaged_operator_ui"),
        ),
        direct=OPERATOR_DIRECT_TESTS,
        dependent=OPERATOR_DEPENDENT_TESTS,
        exclusive=True,
        browser=True,
        windows_installer=True,
    ),
    *_family_roots(
        family="capabilities",
        roots=(
            ("src/stagemesh/agent_skills.py", "bundled_agent_skills"),
            ("src/stagemesh/capabilities.py", "capability_registry"),
            ("src/stagemesh/capability_contracts.py", "capability_contracts"),
            ("src/stagemesh/capability_discovery.py", "capability_discovery"),
            ("src/stagemesh/capability_vendor.py", "capability_vendoring"),
            ("src/stagemesh/claude_plugin_capabilities.py", "claude_plugin_capabilities"),
            ("src/stagemesh/mcp_capabilities.py", "mcp_capabilities"),
            ("src/stagemesh/provider_events.py", "capability_evidence"),
        ),
        direct=CAPABILITY_DIRECT_TESTS,
        dependent=CAPABILITY_DEPENDENT_TESTS,
    ),
    *_family_roots(
        family="task-sources",
        roots=(("src/stagemesh/task_sources.py", "task_source_sync"),),
        direct=TASK_SOURCE_DIRECT_TESTS,
        dependent=TASK_SOURCE_DEPENDENT_TESTS,
    ),
)

_ROOTS_BY_PATH = {root.path: root for root in AUDITED_ROOTS}
if len(_ROOTS_BY_PATH) != len(AUDITED_ROOTS):  # pragma: no cover - import-time invariant
    raise RuntimeError("audited validation roots must be unique")


def resolve_component_impact(
    paths: tuple[str, ...], *, neutral_test_paths: bool = False
) -> ComponentImpact:
    """Resolve exact audited roots; ambiguity never creates a fast path."""
    normalized = tuple(sorted({_normalize(path) for path in paths}))
    selected = tuple(_ROOTS_BY_PATH[path] for path in normalized if path in _ROOTS_BY_PATH)
    selected_support = {
        target for root in selected for target in (*root.direct_tests, *root.dependent_tests)
    }
    unknown = tuple(
        path
        for path in normalized
        if path not in _ROOTS_BY_PATH
        and path not in selected_support
        and not _is_neutral_support(path, neutral_test_paths=neutral_test_paths)
    )
    components = tuple(sorted({root.component for root in selected}))
    families = tuple(sorted({root.family for root in selected}))
    direct = tuple(sorted({target for root in selected for target in root.direct_tests}))
    dependent = tuple(
        sorted({target for root in selected for target in root.dependent_tests})
    )
    complete = bool(selected) and not unknown
    exclusive_conflict = len(families) > 1 and any(root.exclusive_family for root in selected)
    fast = complete and not exclusive_conflict and all(root.fast_path for root in selected)
    return ComponentImpact(
        roots=selected,
        components=components,
        families=families,
        direct_tests=direct,
        dependent_tests=dependent,
        required_os=_ordered_os(os_name for root in selected for os_name in root.required_os),
        complete=complete,
        fast_path_eligible=fast,
        browser_required=any(root.browser_required for root in selected),
        windows_installer_required=any(root.windows_installer_required for root in selected),
        unknown_paths=unknown,
    )


def fast_path_roots() -> tuple[AuditedRoot, ...]:
    return tuple(root for root in AUDITED_ROOTS if root.fast_path)


def fast_path_pytest_targets() -> frozenset[str]:
    return frozenset(
        target
        for root in fast_path_roots()
        for target in (*root.direct_tests, *root.dependent_tests)
    )


def component_catalog_fingerprint() -> str:
    payload = [
        {
            "path": root.path,
            "component": root.component,
            "family": root.family,
            "direct_tests": root.direct_tests,
            "dependent_tests": root.dependent_tests,
            "required_os": root.required_os,
            "fast_path": root.fast_path,
            "exclusive_family": root.exclusive_family,
            "browser_required": root.browser_required,
            "windows_installer_required": root.windows_installer_required,
        }
        for root in AUDITED_ROOTS
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize(path: str) -> str:
    if not isinstance(path, str):
        raise TypeError("paths must be strings")
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise ValueError("path must be non-empty and repository-relative")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("path must remain inside the repository")
    return PurePosixPath(*parts).as_posix().casefold()


def _is_neutral_support(path: str, *, neutral_test_paths: bool) -> bool:
    return path.startswith("docs/") or neutral_test_paths and path.startswith("tests/")


def _ordered_os(values: Iterable[str]) -> tuple[str, ...]:
    selected = set(values)
    return tuple(name for name in ("windows", "linux", "portable") if name in selected)
