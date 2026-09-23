"""Prove `objective run` performs real workspace routing -- not just an
empty-queue no-op -- and that the routing decision (which worktree a
claimed task is assigned to, which database the claim lands in) comes from
coordinator config, never from the invoking process's cwd.

Runs the CLI as a real subprocess (`objective create` to queue a task, then
`objective run --once --dry-run` to claim and dispatch it with the `fake`
executor adapter -- `--dry-run` only swaps the executor, the coordinator
claim/worktree-validation/dispatch logic in
`build_coordinator.runner.orchestrator` still runs in full), then
inspects the coordinator's own sqlite database directly to confirm the
claim actually landed with the configured worktree.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_LOCATION_ENV_VARS = (
    "LEGACY_PRODUCT_REPO_ROOT",
    "LEGACY_PRODUCT_BUILD_CONFIG",
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


def _run_cli(args: list[str], *, cwd: Path, config_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for var in _LOCATION_ENV_VARS:
        env.pop(var, None)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-P", "-m", "build_coordinator.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def routed_workspace(tmp_path: Path):
    control_repo_root = tmp_path / "control-repo"
    control_repo_root.mkdir()

    builder_a_worktree = tmp_path / "builder-a-worktree"
    builder_a_worktree.mkdir()
    (builder_a_worktree / ".git").mkdir()  # enough for is_git_worktree()

    db_path = tmp_path / "coordinator-data" / "coordinator.sqlite3"
    db_path.parent.mkdir(parents=True)

    result_dir = tmp_path / "results"
    result_dir.mkdir()

    config_path = tmp_path / "build-coordinator.json"
    config_path.write_text(
        json.dumps(
            {
                "control_repo_root": str(control_repo_root),
                "database_url": f"sqlite:///{db_path.as_posix()}",
                "data_dir": str(db_path.parent),
                "worktrees": {"builder-a": str(builder_a_worktree)},
            }
        ),
        encoding="utf-8",
    )
    return {
        "config_path": config_path,
        "db_path": db_path,
        "builder_a_worktree": builder_a_worktree,
        "result_dir": result_dir,
    }


@pytest.fixture
def unrelated_directory(tmp_path: Path) -> Path:
    unrelated = tmp_path / "totally-unrelated-cwd"
    unrelated.mkdir()
    return unrelated


def test_objective_run_routes_queued_task_to_configured_worktree_from_unrelated_cwd(
    routed_workspace, unrelated_directory
):
    config_path = routed_workspace["config_path"]
    db_path = routed_workspace["db_path"]
    builder_a_worktree = routed_workspace["builder_a_worktree"]

    create_result = _run_cli(
        [
            "objective",
            "create",
            "route-me",
            "--title",
            "Prove real workspace routing",
        ],
        cwd=unrelated_directory,
        config_path=config_path,
    )
    assert create_result.returncode == 0, create_result.stderr

    env_extra = {
        "BUILD_COORDINATOR_RESULT_DIR": str(routed_workspace["result_dir"]),
    }
    env = dict(os.environ)
    for var in _LOCATION_ENV_VARS:
        env.pop(var, None)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(env_extra)

    run_result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "build_coordinator.cli",
            "objective",
            "run",
            "--once",
            "--dry-run",
        ],
        cwd=str(unrelated_directory),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run_result.returncode == 0, run_result.stderr
    run_payload = json.loads(run_result.stdout)
    assert len(run_payload["launched"]) == 1, run_payload

    # The claim must have landed in the CONFIGURED canonical database (not
    # some cwd-relative default), with the CONFIGURED worktree -- proving
    # both DB and workspace routing came from coordinator config, not cwd.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT worker_id, worktree_path FROM build_task_claims WHERE task_id = ?",
            ("route-me",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "expected a claim for the routed task in the configured database"
    worker_id, worktree_path = row
    assert worker_id == "builder-a"
    assert worktree_path == str(builder_a_worktree)
