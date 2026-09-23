"""Configuration for the Build Coordinator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from build_coordinator.coordinator_config import load_coordinator_config


@dataclass(frozen=True)
class BuildCoordinatorSettings:
    repo_root: Path
    data_dir: Path
    database_url: str
    max_active_builders: int = 2
    project_roots: dict[str, str] | None = None


def _repo_root() -> Path:
    configured = (
        os.getenv("BUILD_COORDINATOR_REPO_ROOT")
        or os.getenv("REPO_ROOT")
    )
    if configured:
        return Path(configured).expanduser().resolve()
    coordinator = load_coordinator_config()
    if coordinator.control_repo_root:
        return coordinator.control_repo_root
    return Path(__file__).resolve().parents[2]


def get_settings() -> BuildCoordinatorSettings:
    coordinator = load_coordinator_config()
    repo_root = _repo_root()
    data_dir = Path(
        os.getenv(
            "BUILD_COORDINATOR_DATA_DIR",
            str(coordinator.data_dir or (repo_root / ".build-coordinator")),
        )
    ).expanduser()
    database_url = os.getenv(
        "BUILD_COORDINATOR_DATABASE_URL",
        coordinator.database_url or f"sqlite:///{(data_dir / 'coordinator.sqlite3').as_posix()}",
    )
    max_active_builders = int(
        os.getenv(
            "BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS",
            str(coordinator.max_active_builders or 2),
        )
    )
    return BuildCoordinatorSettings(
        repo_root=repo_root,
        data_dir=data_dir,
        database_url=database_url,
        max_active_builders=max_active_builders,
        project_roots={
            "api": os.getenv(
                "BUILD_COORDINATOR_PROJECT_ROOT_API",
                coordinator.project_roots.get("api", "apps/api"),
            ),
            "web": os.getenv(
                "BUILD_COORDINATOR_PROJECT_ROOT_WEB",
                coordinator.project_roots.get("web", "apps/web"),
            ),
            "contracts": os.getenv(
                "BUILD_COORDINATOR_PROJECT_ROOT_CONTRACTS",
                coordinator.project_roots.get("contracts", "packages/contracts"),
            ),
            "build_coordinator": os.getenv(
                "BUILD_COORDINATOR_PROJECT_ROOT_BUILD_COORDINATOR",
                coordinator.project_roots.get("build_coordinator", "build_coordinator"),
            ),
        },
    )
