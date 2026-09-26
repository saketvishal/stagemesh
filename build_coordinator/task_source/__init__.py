"""StageMesh Task Source subsystem."""

from __future__ import annotations

import os
from typing import Any

from build_coordinator.task_source.base import SyncResult, TaskSource, TaskSourceConfig
from build_coordinator.task_source.azure_devops import AzureDevOpsTaskSource
from build_coordinator.task_source.github import GitHubTaskSource


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def get_task_source(config: dict[str, Any] | TaskSourceConfig | None = None) -> TaskSource | None:
    if config is None:
        return None
    if isinstance(config, TaskSourceConfig):
        cfg = config
    elif isinstance(config, dict):
        cfg = TaskSourceConfig(
            source_type=config.get("type", "github"),
            repo=config.get("repo"),
            labels=tuple(config.get("labels", ())),
            dry_run=config.get("dry_run", False),
            options=config.get("options", {}),
        )
    else:
        raise TypeError(f"expected TaskSourceConfig or dict, got {type(config).__name__}")

    if cfg.source_type.lower() == "github":
        repo = cfg.repo or os.getenv("BUILD_COORDINATOR_GITHUB_REPO")
        if not repo:
            raise ValueError(
                "missing repository identity: 'repo' must be specified in task_sources.github "
                "or BUILD_COORDINATOR_GITHUB_REPO environment variable"
            )
        raw_config = config if isinstance(config, dict) else {}
        include_labels = (
            cfg.options.get("eligibility_include_labels")
            or cfg.options.get("include_labels")
            or raw_config.get("eligibility_include_labels", ())
            or raw_config.get("include_labels", ())
        )
        exclude_labels = (
            cfg.options.get("eligibility_exclude_labels")
            or cfg.options.get("exclude_labels")
            or cfg.options.get("deferred_labels")
            or raw_config.get("eligibility_exclude_labels", ())
            or raw_config.get("exclude_labels", ())
            or raw_config.get("deferred_labels", ())
        )
        return GitHubTaskSource(
            repo=repo,
            labels=cfg.labels,
            dry_run=cfg.dry_run,
            eligibility_include_labels=_as_tuple(include_labels),
            eligibility_exclude_labels=_as_tuple(exclude_labels),
        )
    if cfg.source_type.lower() in {"azure_devops", "azure-devops", "azdo"}:
        organization = cfg.options.get("organization") or cfg.options.get("org")
        project = cfg.options.get("project")
        query = cfg.options.get("query")
        if not (organization or os.getenv("BUILD_COORDINATOR_AZDO_ORG")):
            raise ValueError(
                "missing Azure DevOps organization: 'organization' must be specified in "
                "task_sources.azure_devops or BUILD_COORDINATOR_AZDO_ORG environment variable"
            )
        if not (project or os.getenv("BUILD_COORDINATOR_AZDO_PROJECT")):
            raise ValueError(
                "missing Azure DevOps project: 'project' must be specified in "
                "task_sources.azure_devops or BUILD_COORDINATOR_AZDO_PROJECT environment variable"
            )
        return AzureDevOpsTaskSource(
            organization=organization,
            project=project,
            query=query,
            dry_run=cfg.dry_run,
        )
    raise ValueError(f"unsupported task source type: '{cfg.source_type}'")
