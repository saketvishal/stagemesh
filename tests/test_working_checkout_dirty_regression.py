from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from build_coordinator.agents.wrapper import GENERATED_ARTIFACT_EXCLUDES
from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.git_integrator import GitIntegrationExecutor, IntegrationStop
from build_coordinator.execution.subprocess_executor import SubprocessExecutor


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def test_tmp_directory_is_durably_gitignored_at_repo_level():
    """Verify that tmp/ and pytest scratch like tmp/gh56-pytest are ignored by repo git rules."""
    repo_root = Path(__file__).resolve().parents[1]
    res = _git(repo_root, "check-ignore", "-v", "tmp/gh56-pytest/some_test_dir/file.txt")
    assert res.returncode == 0, f"tmp/ is not ignored: stdout={res.stdout}, stderr={res.stderr}"
    assert ".gitignore" in res.stdout, f"tmp/ should be ignored via .gitignore: {res.stdout}"


def test_wrapper_generated_artifact_excludes_contains_tmp():
    """Agent wrapper commits must never bundle disposable tmp scratch directories."""
    assert ":(exclude,glob)**/tmp/**" in GENERATED_ARTIFACT_EXCLUDES


def test_subprocess_executor_fallback_temp_does_not_create_stagemesh_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SubprocessExecutor with default args must use managed temp/data dir, never .stagemesh/tmp."""
    custom_data = tmp_path / "custom-data"
    custom_data.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_DATA_DIR", str(custom_data))

    executor = SubprocessExecutor(["python", "-c", "import sys; sys.exit(0)"])
    prep = executor._prepare_temp_dir("worker-1", "exec-1")
    assert prep.is_relative_to(custom_data / "tmp")
    assert not (repo_root := Path(__file__).resolve().parents[1] / ".stagemesh" / "tmp").exists()


def test_working_checkout_dirty_safety_gate_preservation(tmp_path: Path):
    """WORKING_CHECKOUT_DIRTY gate must remain strict for genuine changes, but not trip on ignored tmp/."""
    # Set up a test git repository with a main checkout and a worktree
    remote_repo = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote_repo))

    main_checkout = tmp_path / "main_repo"
    _git(tmp_path, "clone", str(remote_repo), str(main_checkout))
    _git(main_checkout, "checkout", "-b", "main")

    # Add .gitignore with tmp/
    (main_checkout / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    (main_checkout / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(main_checkout, "-c", "user.name=Test", "-c", "user.email=t@example.com", "add", ".gitignore", "tracked.txt")
    _git(main_checkout, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-m", "init")
    _git(main_checkout, "push", "origin", "main")

    # Create worktree for integration
    wt_path = tmp_path / "integration_wt"
    res = _git(main_checkout, "worktree", "add", "--detach", str(wt_path), "HEAD")
    assert res.returncode == 0, f"worktree add failed: {res.stderr}"

    # Create a feature commit to advance to
    _git(wt_path, "checkout", "-b", "feature")
    (wt_path / "feature.txt").write_text("feature content\n", encoding="utf-8")
    _git(wt_path, "-c", "user.name=Test", "-c", "user.email=t@example.com", "add", "feature.txt")
    _git(wt_path, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-m", "feature")
    feature_sha = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
    old_main_sha = _git(main_checkout, "rev-parse", "HEAD").stdout.strip()

    executor = GitIntegrationExecutor(main_ref="main")

    # 1. Normal state with disposable test artifacts under tmp/gh56-pytest:
    # Must NOT be considered dirty and must advance cleanly.
    test_scratch = main_checkout / "tmp" / "gh56-pytest" / "test_scratch0"
    test_scratch.mkdir(parents=True, exist_ok=True)
    (test_scratch / "output.log").write_text("pytest log", encoding="utf-8")

    status = _git(main_checkout, "status", "--porcelain").stdout.strip()
    assert status == "", f"git status should be clean despite tmp/ artifacts: {status}"

    # Advance should succeed
    executor._advance(wt_path, "main", feature_sha, old_main_sha)
    new_main_sha = _git(main_checkout, "rev-parse", "HEAD").stdout.strip()
    assert new_main_sha == feature_sha

    # 2. Genuine dirty state (untracked or modified file NOT in .gitignore):
    # The safety gate MUST trip with WORKING_CHECKOUT_DIRTY.
    (main_checkout / "uncommitted_user_work.py").write_text("# my work\n", encoding="utf-8")
    dirty_status = _git(main_checkout, "status", "--porcelain").stdout.strip()
    assert dirty_status != ""

    # Create another commit to advance to
    _git(wt_path, "checkout", "-b", "feature-2")
    (wt_path / "feature2.txt").write_text("feature 2\n", encoding="utf-8")
    _git(wt_path, "-c", "user.name=Test", "-c", "user.email=t@example.com", "add", "feature2.txt")
    _git(wt_path, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-m", "feature 2")
    feature2_sha = _git(wt_path, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(IntegrationStop) as exc_info:
        executor._advance(wt_path, "main", feature2_sha, new_main_sha)

    assert exc_info.value.escalation == "WORKING_CHECKOUT_DIRTY"
    assert "main is checked out with uncommitted changes" in exc_info.value.detail
