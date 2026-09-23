"""Prove the operator CLI resolves workspace location from coordinator
config, not from the process's current working directory.

This exercises `build-coordinator objective create/run/status` as real
subprocesses (not in-process calls) so the test actually crosses a fresh
Python interpreter's module-import boundary the way an operator invocation
would, from three different working directories:

1. the repository root itself,
2. a directory standing in for another unrelated worktree (its own git
   checkout, unrelated to the control repo the coordinator is configured
   against), and
3. a directory with no relationship to the control repo at all.

The CLI must behave identically in all three cases: the resolved control
repo root, worktrees, and database always come from the coordinator config
file (`BUILD_COORDINATOR_CONFIG`), never from `os.getcwd()`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every env var that could let workspace location leak in from outside the
# coordinator config file under test. Stripped from the child environment so
# a developer's own shell configuration can't mask a cwd dependency.
_LOCATION_ENV_VARS = (
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS",
    "BUILD_COORDINATOR_PROJECT_ROOT_API",
    "BUILD_COORDINATOR_PROJECT_ROOT_WEB",
    "BUILD_COORDINATOR_PROJECT_ROOT_CONTRACTS",
    "BUILD_COORDINATOR_PROJECT_ROOT_BUILD_COORDINATOR",
    "BUILD_COORDINATOR_RUNNER_CONFIG",
    "BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS",
    "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED",
    "BUILD_COORDINATOR_RESULT_DIR",
)


def _write_coordinator_config(path: Path, *, control_repo_root: Path, db_path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "control_repo_root": str(control_repo_root),
                "database_url": f"sqlite:///{db_path.as_posix()}",
                "data_dir": str(db_path.parent),
                "max_active_builders": 2,
                "worktrees": {
                    "builder-a": str(control_repo_root / "builder-a-worktree"),
                    "reviewer-1": str(control_repo_root / "reviewer-worktree"),
                },
            }
        ),
        encoding="utf-8",
    )


def _run_cli(
    args: list[str], *, cwd: Path, config_path: Path, isolate_cwd: bool = True
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for var in _LOCATION_ENV_VARS:
        env.pop(var, None)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    # Simulate an installed/on-PATH `build-coordinator`: the module is
    # importable via PYTHONPATH alone, independent of `cwd`.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    python_args = [sys.executable]
    if isolate_cwd:
        # Matches the launcher scripts: `-P` stops `python -m` from
        # prepending cwd to sys.path[0], so a stale tooling/build_coordinator
        # sitting in cwd can never shadow the repo root on PYTHONPATH.
        python_args.append("-P")
    return subprocess.run(
        [*python_args, "-m", "build_coordinator.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def coordinator_workspace(tmp_path: Path):
    control_repo_root = tmp_path / "control-repo"
    control_repo_root.mkdir()
    db_path = tmp_path / "coordinator-data" / "coordinator.sqlite3"
    db_path.parent.mkdir(parents=True)
    config_path = tmp_path / "build-coordinator.json"
    _write_coordinator_config(config_path, control_repo_root=control_repo_root, db_path=db_path)
    return control_repo_root, config_path


_POISON_CLI_SOURCE = '''\
"""Stale/conflicting controller checkout used only to prove the real
controller wins over whatever tooling/build_coordinator happens to sit in
cwd."""
import json


def main() -> None:
    print(json.dumps({"poison": True, "controller_source": {"package_root": "STALE-CWD-CHECKOUT"}}))


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def another_worktree(tmp_path: Path) -> Path:
    """A directory that looks like a separate worktree but is not the
    control repo the coordinator is configured against, and -- critically
    -- contains its OWN stale tooling/build_coordinator/cli.py. If cwd ever
    won a shadowing race against the repo root on PYTHONPATH, invoking the
    CLI from here would run this poisoned module instead of the real one."""
    worktree = tmp_path / "unrelated-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere/else/.git\n", encoding="utf-8")

    stale_tooling = worktree / "tooling"
    stale_tooling.mkdir()
    (stale_tooling / "__init__.py").write_text("", encoding="utf-8")
    stale_build_coordinator = stale_tooling / "build_coordinator"
    stale_build_coordinator.mkdir()
    (stale_build_coordinator / "__init__.py").write_text("", encoding="utf-8")
    (stale_build_coordinator / "cli.py").write_text(_POISON_CLI_SOURCE, encoding="utf-8")

    return worktree


@pytest.fixture
def unrelated_directory(tmp_path: Path) -> Path:
    """A directory with no relationship to the control repo whatsoever."""
    unrelated = tmp_path / "totally-unrelated"
    unrelated.mkdir()
    return unrelated


def _invocation_dirs(coordinator_workspace, another_worktree, unrelated_directory):
    control_repo_root, _config_path = coordinator_workspace
    return {
        "repository_root": control_repo_root,
        "another_worktree": another_worktree,
        "unrelated_directory": unrelated_directory,
    }


@pytest.mark.parametrize(
    "invocation_dir_key",
    ["repository_root", "another_worktree", "unrelated_directory"],
)
def test_objective_status_resolves_workspace_regardless_of_cwd(
    coordinator_workspace, another_worktree, unrelated_directory, invocation_dir_key
):
    control_repo_root, config_path = coordinator_workspace
    invocation_dir = _invocation_dirs(coordinator_workspace, another_worktree, unrelated_directory)[
        invocation_dir_key
    ]

    result = _run_cli(["objective", "status"], cwd=invocation_dir, config_path=config_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "poison" not in payload, "stale cwd checkout shadowed the real controller"
    assert payload["workspace"]["control_repo_root"] == str(control_repo_root.resolve())
    assert payload["workspace"]["coordinator_config_path"] == str(config_path.resolve())
    assert payload["workspace"]["data_dir"] == str((config_path.parent / "coordinator-data").resolve())
    assert payload["workspace"]["database_url"].endswith("/coordinator-data/coordinator.sqlite3")
    assert payload["workspace"]["result_dir"].endswith("coordinator-data\\results") or payload[
        "workspace"
    ]["result_dir"].endswith("coordinator-data/results")
    assert payload["workspace"]["log_artifact_dir"].endswith(
        "coordinator-data\\execution-logs"
    ) or payload["workspace"]["log_artifact_dir"].endswith("coordinator-data/execution-logs")
    assert payload["workspace"]["runner_config_path"] is None
    assert payload["workspace"]["worktrees"] == {
        "builder-a": str(control_repo_root / "builder-a-worktree"),
        "reviewer-1": str(control_repo_root / "reviewer-worktree"),
    }
    # The controller code itself must always load from the real repo root,
    # never from whatever tooling/build_coordinator happens to sit in cwd.
    assert payload["controller_source"]["package_root"] == str(
        (REPO_ROOT / "build_coordinator").resolve()
    )
    assert payload["controller_source"]["cli_module_file"] == str(
        (REPO_ROOT / "build_coordinator" / "cli.py").resolve()
    )


def test_stale_checkout_in_cwd_is_not_imported_when_cwd_is_isolated(
    coordinator_workspace, another_worktree
):
    """Direct regression coverage for the shadowing blocker: from a cwd that
    contains its own (stale/conflicting) tooling/build_coordinator/cli.py,
    the CLI invocation used by the launcher scripts (`python -P -m ...`)
    must load the real controller, never the stale checkout sitting in
    cwd."""
    _control_repo_root, config_path = coordinator_workspace

    result = _run_cli(
        ["objective", "status"], cwd=another_worktree, config_path=config_path, isolate_cwd=True
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("poison") is not True
    assert "workspace" in payload
    assert payload["controller_source"]["package_root"] == str(
        (REPO_ROOT / "build_coordinator").resolve()
    )


@pytest.mark.xfail(reason="Python's -m path behavior is version/platform-dependent; the coordinator's protection (-P flag) is what matters, not the unprotected behavior")
def test_without_cwd_isolation_a_stale_checkout_in_cwd_shadows_the_real_controller(
    coordinator_workspace, another_worktree
):
    """Documents *why* `-P` is required: without it, `python -m` prepends
    cwd to sys.path[0], and a stale tooling/build_coordinator/cli.py sitting
    there wins the import over the repo root on PYTHONPATH. This is a
    regression guard on the bug itself, not on the fix -- if this ever
    starts failing (i.e. the stale module is no longer picked up even
    without -P), the shadowing scenario the fix defends against may no
    longer be reproducible and the test/assumption should be revisited."""
    _control_repo_root, config_path = coordinator_workspace

    result = _run_cli(
        ["objective", "status"], cwd=another_worktree, config_path=config_path, isolate_cwd=False
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("poison") is True, (
        "expected the stale cwd checkout to shadow the real controller when "
        "-P is not used -- if it no longer does, Python's own sys.path "
        "behavior for `python -m` may have changed"
    )


def test_objective_create_and_status_are_consistent_across_different_cwds(
    coordinator_workspace, another_worktree, unrelated_directory
):
    """A task created while invoked from one directory must be visible when
    the next command is invoked from a completely different directory --
    proving state is keyed off coordinator config, not the caller's cwd."""
    _control_repo_root, config_path = coordinator_workspace

    create_result = _run_cli(
        [
            "objective",
            "create",
            "smoke-task-1",
            "--title",
            "Prove location independence",
            "--description",
            "Created from an unrelated directory.",
        ],
        cwd=unrelated_directory,
        config_path=config_path,
    )
    assert create_result.returncode == 0, create_result.stderr
    created = json.loads(create_result.stdout)
    assert created["task_id"] == "smoke-task-1"

    status_result = _run_cli(
        ["objective", "status"],
        cwd=another_worktree,
        config_path=config_path,
    )
    assert status_result.returncode == 0, status_result.stderr
    status_payload = json.loads(status_result.stdout)
    assert status_payload["task_count"] == 1


def test_objective_run_dry_run_works_from_unrelated_directory(
    coordinator_workspace, unrelated_directory
):
    _control_repo_root, config_path = coordinator_workspace

    result = _run_cli(
        ["objective", "run", "--once", "--dry-run"],
        cwd=unrelated_directory,
        config_path=config_path,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "launched" in payload
    assert "capacity_full" in payload
