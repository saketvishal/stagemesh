"""Coverage for coordinator config resolution semantics:

- BUILD_COORDINATOR_CONFIG fails closed (relative path, missing file, invalid
  JSON) instead of silently falling back to defaults.
- Relative paths inside the config file resolve against the config file's
  own directory, never the caller's cwd.
- UTF-8 BOM-encoded config files are read correctly.
- Precedence is environment > coordinator config file > built-in default.
- The reviewer worktree has a single source of truth: `worktrees[<id>]`,
  with no competing `reviewer_worktree` field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_coordinator import config as config_module
from build_coordinator.coordinator_config import (
    CoordinatorConfig,
    CoordinatorConfigError,
    config_file_path,
    load_coordinator_config,
)
from build_coordinator.runner.models import RunnerConfig


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fail-closed behavior for an explicit BUILD_COORDINATOR_CONFIG
# ---------------------------------------------------------------------------


def test_relative_explicit_config_path_fails_closed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", "relative-config.json")

    with pytest.raises(CoordinatorConfigError, match="absolute"):
        config_file_path()


def test_missing_explicit_config_file_fails_closed(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(missing))

    with pytest.raises(CoordinatorConfigError, match="does not exist"):
        config_file_path()


def test_invalid_json_explicit_config_fails_closed(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(bad))

    with pytest.raises(CoordinatorConfigError, match="not valid JSON"):
        load_coordinator_config()


def test_get_settings_propagates_fail_closed_error(monkeypatch, tmp_path):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(missing))

    with pytest.raises(CoordinatorConfigError):
        config_module.get_settings()


def test_missing_unset_config_falls_back_silently(monkeypatch, tmp_path):
    """No BUILD_COORDINATOR_CONFIG at all, and no file at the default path
    (redirected here via HOME) -- this is not an error, since no config was
    ever explicitly requested."""
    monkeypatch.delenv("BUILD_COORDINATOR_CONFIG", raising=False)
    monkeypatch.setattr(
        "build_coordinator.coordinator_config.DEFAULT_CONFIG_PATH",
        tmp_path / "nonexistent" / "build-coordinator.json",
    )
    assert load_coordinator_config() == CoordinatorConfig()


# ---------------------------------------------------------------------------
# Relative paths inside the config file resolve against the config file's
# own directory, never cwd.
# ---------------------------------------------------------------------------


def test_relative_workspace_paths_resolve_against_config_dir_not_cwd(monkeypatch, tmp_path):
    config_dir = tmp_path / "config-lives-here"
    config_dir.mkdir()
    config_path = config_dir / "build-coordinator.json"
    _write_config(
        config_path,
        {
            "control_repo_root": "../control-repo",
            "data_dir": "./data",
            "worktrees": {"builder-a": "../worktrees/builder-a"},
        },
    )
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))

    elsewhere = tmp_path / "some-unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved_from_elsewhere = load_coordinator_config()

    another_cwd = tmp_path / "another-totally-different-cwd"
    another_cwd.mkdir()
    monkeypatch.chdir(another_cwd)

    resolved_from_another_cwd = load_coordinator_config()

    expected_control_repo_root = (config_dir / "../control-repo").resolve()
    expected_data_dir = (config_dir / "./data").resolve()
    expected_builder_a = (config_dir / "../worktrees/builder-a").resolve()

    for resolved in (resolved_from_elsewhere, resolved_from_another_cwd):
        assert resolved.control_repo_root == expected_control_repo_root
        assert resolved.data_dir == expected_data_dir
        assert Path(resolved.worktrees["builder-a"]) == expected_builder_a


def test_absolute_workspace_paths_pass_through_unchanged(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    absolute_repo = tmp_path / "control-repo"
    _write_config(config_path, {"control_repo_root": str(absolute_repo)})
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))

    resolved = load_coordinator_config()
    assert resolved.control_repo_root == absolute_repo.resolve()


# ---------------------------------------------------------------------------
# UTF-8 BOM support
# ---------------------------------------------------------------------------


def test_utf8_bom_config_file_is_read_correctly(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    payload = json.dumps({"control_repo_root": str(tmp_path / "control-repo")})
    config_path.write_bytes(payload.encode("utf-8-sig"))
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))

    resolved = load_coordinator_config()
    assert resolved.control_repo_root == (tmp_path / "control-repo").resolve()


# ---------------------------------------------------------------------------
# Precedence: environment > coordinator config file > built-in default.
# ---------------------------------------------------------------------------


def test_database_url_precedence(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    _write_config(config_path, {"database_url": "sqlite:///from-config-file.sqlite3"})
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))
    monkeypatch.delenv("BUILD_COORDINATOR_DATABASE_URL", raising=False)

    assert config_module.get_settings().database_url == "sqlite:///from-config-file.sqlite3"

    monkeypatch.setenv("BUILD_COORDINATOR_DATABASE_URL", "sqlite:///from-env.sqlite3")
    assert config_module.get_settings().database_url == "sqlite:///from-env.sqlite3"


def test_control_repo_root_precedence(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    from_config = tmp_path / "from-config-repo"
    _write_config(config_path, {"control_repo_root": str(from_config)})
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))
    monkeypatch.delenv("BUILD_COORDINATOR_REPO_ROOT", raising=False)

    assert config_module.get_settings().repo_root == from_config.resolve()

    from_env = tmp_path / "from-env-repo"
    monkeypatch.setenv("BUILD_COORDINATOR_REPO_ROOT", str(from_env))
    assert config_module.get_settings().repo_root == from_env.resolve()


def test_worker_worktree_precedence(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    from_config = tmp_path / "builder-a-from-config"
    _write_config(config_path, {"worktrees": {"builder-a": str(from_config)}})
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))
    monkeypatch.delenv("BUILD_COORDINATOR_RUNNER_CONFIG", raising=False)

    runner_config = RunnerConfig.default(dry_run=True)
    builder = next(w for w in runner_config.workers if w.worker_id == "builder-a")
    assert builder.worktree_path == str(from_config.resolve())

    runner_config_json = tmp_path / "runner-config.json"
    runner_config_json.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "worker_id": "builder-a",
                        "role": "BUILDER",
                        "worktree_path": str(tmp_path / "builder-a-from-runner-config"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BUILD_COORDINATOR_RUNNER_CONFIG", str(runner_config_json))
    runner_config_override = RunnerConfig.default(dry_run=True)
    builder_override = next(
        w for w in runner_config_override.workers if w.worker_id == "builder-a"
    )
    assert builder_override.worktree_path == str(tmp_path / "builder-a-from-runner-config")


# ---------------------------------------------------------------------------
# Reviewer worktree: single source of truth.
# ---------------------------------------------------------------------------


def test_reviewer_worktree_has_no_dedicated_field():
    assert not hasattr(CoordinatorConfig(), "reviewer_worktree")


def test_reviewer_worktree_comes_from_worktrees_map(monkeypatch, tmp_path):
    config_path = tmp_path / "build-coordinator.json"
    reviewer_path = tmp_path / "reviewer-worktree"
    _write_config(config_path, {"worktrees": {"reviewer-1": str(reviewer_path)}})
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))
    monkeypatch.delenv("BUILD_COORDINATOR_RUNNER_CONFIG", raising=False)

    runner_config = RunnerConfig.default(dry_run=True)
    reviewer = next(w for w in runner_config.workers if w.worker_id == "reviewer-1")
    assert reviewer.worktree_path == str(reviewer_path.resolve())
