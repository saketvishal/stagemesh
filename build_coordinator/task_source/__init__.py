"""StageMesh Task Source subsystem."""

from __future__ import annotations

from typing import Any

from build_coordinator.task_source.base import SyncResult, TaskSource, TaskSourceConfig
from build_coordinator.task_source.github import GitHubTaskSource


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
        return None

    if cfg.source_type.lower() == "github":
        return GitHubTaskSource(
            repo=cfg.repo,
            labels=cfg.labels,
            dry_run=cfg.dry_run,
        )
    return None
