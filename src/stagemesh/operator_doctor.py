"""Privacy-bounded project readiness for the loopback operator UI.

This projection is intentionally separate from the CLI doctor.  It reloads project
configuration for every request, reads the already-open read-only store, performs no
provider or network calls, and exposes only an allowlisted posture summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import BUILTIN_PROVIDERS, PROVIDER_STAGES, StageMeshConfig, load_config
from .persistence import SCHEMA_VERSION, Store
from .runtime_info import runtime_info

DOCTOR_CONTRACT = "stagemesh.project-doctor/1"
PROVIDER_LIMIT = 100
SOURCE_KIND_LIMIT = 20


def _attention(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _schema_status(current: int) -> str:
    if current == SCHEMA_VERSION:
        return "CURRENT"
    if current < SCHEMA_VERSION:
        return "OUTDATED"
    return "AHEAD"


def _providers(config: StageMeshConfig) -> tuple[list[dict[str, Any]], int]:
    names = sorted(set(BUILTIN_PROVIDERS) | set(config.provider_commands) | set(config.provider_specs))
    authorized = set(config.authorized_providers or ())
    rows = []
    for name in names[:PROVIDER_LIMIT]:
        spec = config.provider_specs.get(name)
        rows.append(
            {
                "name": name[:100],
                "capabilities": sorted(spec.capabilities if spec else PROVIDER_STAGES),
                "command_source": (
                    "PROJECT_OR_ENV"
                    if name in config.provider_commands
                    else "BUILTIN"
                    if name in BUILTIN_PROVIDERS
                    else "UNAVAILABLE"
                ),
                "authorized_for_unattended": name in authorized,
            }
        )
    return rows, len(names)


def _runtime() -> dict[str, Any]:
    try:
        info = runtime_info()
    except Exception:  # noqa: BLE001 - local installation details must not escape
        return {
            "version": __version__,
            "mode": "UNKNOWN",
            "distribution_matches": None,
            "provenance": "UNKNOWN",
        }
    provenance = info.get("provenance") or {}
    verified = bool(provenance.get("source_commit")) and isinstance(
        provenance.get("source_dirty"), bool
    )
    return {
        "version": __version__,
        "mode": "DEVELOPMENT" if info.get("source_checkout") else "INSTALLED",
        "distribution_matches": info.get("dist_version") in {None, __version__},
        "provenance": "VERIFIED" if verified else "UNKNOWN",
    }


def _configuration(config: StageMeshConfig) -> dict[str, Any]:
    github_parts = (config.github.owner, config.github.repo, config.github.token)
    source_kinds = sorted({source.kind[:40] for source in config.task_sources})[:SOURCE_KIND_LIMIT]
    return {
        "status": "AVAILABLE",
        "source": "DEFAULTS" if config.source == "defaults" else "PROJECT",
        "routing": {
            "mode": config.routing_mode,
            "selection_policy": config.provider_selection_policy,
            "profile": config.provider_profile,
            "independent_review_required": config.require_independent_review,
        },
        "task_sources": {"count": len(config.task_sources), "kinds": source_kinds},
        "github": {
            "repository_configured": bool(config.github.owner and config.github.repo),
            "credential_configured": bool(config.github.token),
            "ready": config.github.configured,
            "partially_configured": any(github_parts) and not config.github.configured,
        },
    }


def _policies(config: StageMeshConfig) -> dict[str, Any]:
    driver = config.driver
    governance = config.spec_governance
    return {
        "authorization": {
            "allowlist_configured": config.authorized_providers is not None,
            "authorized_provider_count": len(config.authorized_providers or ()),
        },
        "planning": {
            "approved_spec_required": config.require_approved_spec,
            "challenge_required": config.require_plan_challenge,
            "planner_configured": config.planner_provider is not None,
            "challenger_configured": config.challenger_provider is not None,
        },
        "merging": {
            "mode": config.merging.mode,
            "required_check_count": len(config.merging.required_checks),
            "trusted_check_app_configured": config.merging.check_app_id is not None,
        },
        "research": {
            "enabled": config.research is not None,
            "scope_enforced": bool(config.research and config.research.enforce_scope),
        },
        "driver": {
            "configured": driver is not None,
            "strict_live": bool(driver and driver.strict_live),
            "auto_merge": bool(driver and driver.auto_merge_ff),
            "integration": None if driver is None else driver.integration,
        },
        "spec_governance": {
            "enabled": governance is not None,
            "adapter": None if governance is None else governance.adapter,
            "independent_review_classes": 0
            if governance is None
            else sum(policy.require_independent_review for policy in governance.risk_classes.values()),
        },
        "task_selection": {
            "automatic": config.task_selection.auto_select,
            "priority_label_count": len(config.task_selection.priority_labels),
            "preferred_label_count": len(config.task_selection.preferred_labels),
            "excluded_label_count": len(config.task_selection.excluded_labels),
            "tie_breaker": config.task_selection.tie_breaker,
        },
        "budgets": {
            name: value
            for name, value in vars(config.budgets).items()
        },
    }


class ProjectDoctor:
    """Reload and serialize one project's safe, local-only operator posture."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()

    def serialize(self, store: Store) -> dict[str, Any]:
        observed_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        current_schema = store.schema_version()
        schema_status = _schema_status(current_schema)
        attention: list[dict[str, str]] = []
        if schema_status != "CURRENT":
            attention.append(
                _attention(
                    "SCHEMA_" + schema_status,
                    "The activity store schema does not match this StageMesh runtime.",
                    "error",
                )
            )
        try:
            config = load_config(self.project)
        except Exception:  # noqa: BLE001 - configuration details and local paths must not escape
            attention.append(
                _attention(
                    "CONFIG_UNAVAILABLE",
                    "Project configuration could not be safely loaded.",
                    "error",
                )
            )
            return {
                "observed_at": observed_at,
                "verdict": "UNAVAILABLE",
                "attention": attention,
                "configuration": {"status": "UNAVAILABLE"},
                "database": {
                    "backend": "SQLITE",
                    "schema": schema_status,
                    "current_version": current_schema,
                    "expected_version": SCHEMA_VERSION,
                },
                "runtime": _runtime(),
                "workspace": {"isolation_configured": False, "unsafe_root_allowed": False},
                "providers": [],
                "provider_total": 0,
                "provider_limit": PROVIDER_LIMIT,
                "providers_truncated": False,
                "policies": None,
                "network_checked": False,
                "mutation_performed": False,
            }

        providers, provider_total = _providers(config)
        if not config.authorized_providers:
            attention.append(
                _attention(
                    "NO_UNATTENDED_PROVIDER_AUTHORIZATION",
                    "No providers are explicitly authorized for unattended work.",
                )
            )
        if config.runtime and config.runtime.allow_unsafe_worktree_root:
            attention.append(
                _attention(
                    "UNSAFE_WORKTREE_ROOT_ALLOWED",
                    "The project permits a worktree root outside the normal isolation boundary.",
                    "error",
                )
            )
        github_parts = (config.github.owner, config.github.repo, config.github.token)
        if any(github_parts) and not config.github.configured:
            attention.append(
                _attention(
                    "GITHUB_CONFIGURATION_INCOMPLETE",
                    "GitHub integration is only partially configured.",
                )
            )
        return {
            "observed_at": observed_at,
            "verdict": "ATTENTION" if attention else "READY",
            "attention": attention,
            "configuration": _configuration(config),
            "database": {
                "backend": "SQLITE",
                "schema": schema_status,
                "current_version": current_schema,
                "expected_version": SCHEMA_VERSION,
            },
            "runtime": _runtime(),
            "workspace": {
                "isolation_configured": config.runtime is not None,
                "unsafe_root_allowed": bool(
                    config.runtime and config.runtime.allow_unsafe_worktree_root
                ),
            },
            "providers": providers,
            "provider_total": provider_total,
            "provider_limit": PROVIDER_LIMIT,
            "providers_truncated": provider_total > PROVIDER_LIMIT,
            "policies": _policies(config),
            "network_checked": False,
            "mutation_performed": False,
        }
