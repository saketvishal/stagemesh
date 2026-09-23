"""Location-independent coordinator configuration resolution.

The Build Coordinator CLI must work when invoked from any current working
directory: the repository root, a build worktree, or an unrelated directory.
Nothing in this module may read `os.getcwd()` -- every path returned here is
resolved either as an absolute path taken verbatim, or relative to a fixed,
documented base that never varies with the calling process's cwd.

Canonical workspace locations (control/integration repo root, builder and
reviewer worktrees, and the coordinator database) are read from a single
coordinator config file so an operator only has to configure them once per
machine, instead of setting environment variables in every shell.

Resolution order (highest priority first):
1. Environment variables (see `build_coordinator.config` and
   `build_coordinator.runner.models` for the specific variable
   names) -- kept for CI and test isolation.
2. The coordinator config file, located by `BUILD_COORDINATOR_CONFIG` if set,
   otherwise the fixed per-user path `~/.build-coordinator/config.json`.
3. Built-in defaults.

Path semantics (deterministic, cwd-independent):
- `BUILD_COORDINATOR_CONFIG`, when set, MUST be an absolute path to a file that
  exists and parses as JSON. A relative value, a missing file, or invalid
  JSON all fail closed with `CoordinatorConfigError` -- there is no silent
  fallback to defaults once the operator has explicitly named a config
  file. This prevents a mistyped or stale env var from silently routing the
  controller at an unconfigured, imported-checkout default.
- When `BUILD_COORDINATOR_CONFIG` is unset, the fixed default path
  (`~/.build-coordinator/config.json`) is used opportunistically: if it
  does not exist, resolution falls through to built-in defaults with no
  error, since no config was ever explicitly requested.
- Every relative path *inside* the config file (`control_repo_root`,
  `data_dir`, and each `worktrees` value) is resolved against the
  directory containing the config file itself -- never against the
  process's cwd and never against this module's own source location.
  Operators should still prefer absolute paths in the config file; relative
  paths are supported only for portability of a config file that travels
  with a workspace layout.

Single source of truth for the reviewer worktree: there is no separate
`reviewer_worktree` field. The reviewer's location is just another entry in
`worktrees`, keyed by its worker id (e.g. `"reviewer-1"`), exactly like a
builder's. Keeping one map avoids two competing sources of truth.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_CONFIG_PATH = Path.home() / ".build-coordinator" / "config.json"


class CoordinatorConfigError(ValueError):
    """Raised when coordinator configuration is explicitly requested but
    cannot be resolved safely (missing file, relative override path,
    unparsable JSON). Fails closed rather than silently falling back."""


@dataclass(frozen=True)
class CoordinatorConfig:
    control_repo_root: Path | None = None
    database_url: str | None = None
    data_dir: Path | None = None
    max_active_builders: int | None = None
    project_roots: dict[str, str] = field(default_factory=dict)
    worktrees: dict[str, str] = field(default_factory=dict)
    source_path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "control_repo_root": str(self.control_repo_root) if self.control_repo_root else None,
            "database_url": self.database_url,
            "data_dir": str(self.data_dir) if self.data_dir else None,
            "max_active_builders": self.max_active_builders,
            "project_roots": dict(self.project_roots),
            "worktrees": dict(self.worktrees),
            "source_path": str(self.source_path) if self.source_path else None,
        }


def _explicit_config_path() -> Path:
    env_name = "BUILD_COORDINATOR_CONFIG"
    configured = os.getenv("BUILD_COORDINATOR_CONFIG", "")
    raw = Path(configured)
    if not raw.is_absolute():
        raise CoordinatorConfigError(
            f"{env_name} must be an absolute path; got a relative "
            f"path ({configured!r}), which would resolve against the "
            "process's current working directory and defeat location "
            "independence. Use an absolute path."
        )
    resolved = raw.expanduser()
    if not resolved.is_file():
        raise CoordinatorConfigError(
            f"{env_name} is set to {resolved}, but that file does "
            f"not exist. Fix the path or unset {env_name} to fall "
            "back to the default config location."
        )
    return resolved


def config_file_path() -> Path | None:
    """Resolve the coordinator config file location without touching cwd.

    Returns None when no config file is configured or present -- callers
    should treat that as "use built-in defaults", not an error. An
    explicitly-set `BUILD_COORDINATOR_CONFIG` that is invalid raises
    `CoordinatorConfigError` instead of returning None (fail closed).
    """
    if os.getenv("BUILD_COORDINATOR_CONFIG"):
        return _explicit_config_path()
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def _resolve_workspace_path(raw: str, *, base_dir: Path) -> Path:
    """Resolve a workspace path from the config file. Absolute paths are
    taken verbatim; relative paths resolve against `base_dir` (the config
    file's own directory), never against the caller's cwd."""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def load_coordinator_config() -> CoordinatorConfig:
    path = config_file_path()
    if path is None:
        return CoordinatorConfig()
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CoordinatorConfigError(f"could not read coordinator config {path}: {exc}") from exc
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CoordinatorConfigError(f"coordinator config {path} is not valid JSON: {exc}") from exc

    base_dir = path.parent
    control_repo_root = data.get("control_repo_root")
    data_dir = data.get("data_dir")
    worktrees = dict(data.get("worktrees") or {})
    return CoordinatorConfig(
        control_repo_root=(
            _resolve_workspace_path(control_repo_root, base_dir=base_dir)
            if control_repo_root
            else None
        ),
        database_url=data.get("database_url"),
        data_dir=_resolve_workspace_path(data_dir, base_dir=base_dir) if data_dir else None,
        max_active_builders=data.get("max_active_builders"),
        project_roots=dict(data.get("project_roots") or {}),
        worktrees={
            str(worker_id): str(_resolve_workspace_path(str(worktree_path), base_dir=base_dir))
            for worker_id, worktree_path in worktrees.items()
        },
        source_path=path,
    )
