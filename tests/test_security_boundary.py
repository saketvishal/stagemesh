"""Explicit security-boundary checks for the location-independent
controller CLI. Most of these invariants are proven end-to-end elsewhere
(`test_operator_location_independence.py`, `test_coordinator_config.py`,
`test_objective_run_workspace_routing.py`); this module makes each
individual invariant checkable on its own so a future change that breaks
just one of them fails a narrowly-named test instead of only a broad
integration test.
"""

from __future__ import annotations

import json

import pytest

from build_coordinator import cli as cli_module
from build_coordinator.coordinator_config import CoordinatorConfigError


def test_objective_create_exposes_no_workspace_path_arguments():
    """A task's own fields (title/description/acceptance criteria/...) can
    never carry a workspace path -- `objective create` has no argument that
    could set cwd, a repo root, or a config path. This is what makes "task
    text cannot choose cwd" true by construction rather than by convention."""
    parser = cli_module._build_parser()
    forbidden_flags = (
        "--cwd",
        "--worktree",
        "--worktree-path",
        "--control-repo-root",
        "--repo-root",
        "--config",
        "--database-url",
    )
    for flag in forbidden_flags:
        with pytest.raises(SystemExit):
            parser.parse_args(["objective", "create", "task-1", "--title", "x", flag, "value"])


def test_missing_explicit_config_fails_closed_rather_than_choosing_a_default(monkeypatch, tmp_path):
    """An operator who explicitly names a config file that doesn't resolve
    must get an error, never a silent fall-through to whatever the
    imported checkout happens to default to."""
    from build_coordinator.coordinator_config import config_file_path

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(CoordinatorConfigError):
        config_file_path()


def test_relative_explicit_config_fails_closed_instead_of_resolving_against_cwd(monkeypatch, tmp_path):
    """A relative BUILD_COORDINATOR_CONFIG would resolve against the caller's
    cwd -- exactly the dependency this feature exists to remove. It must be
    rejected outright, not silently interpreted."""
    from build_coordinator.coordinator_config import config_file_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", "config.json")
    with pytest.raises(CoordinatorConfigError, match="absolute"):
        config_file_path()


def test_controller_source_reports_the_running_package_not_a_claim():
    """`_controller_source()` reports where the running interpreter actually
    loaded tooling.build_coordinator from -- it is derived from the live
    `tooling.build_coordinator` module object, not from any config value an
    operator or task could set, so it cannot be spoofed by configuration."""
    source = cli_module._controller_source()
    assert source["cli_module_file"].endswith("cli.py")
    payload = json.dumps(source)  # must be JSON-serializable for `objective status`
    assert "package_root" in json.loads(payload)
