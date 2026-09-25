"""Per-task isolated workspaces: fresh from main, no cross-task leakage, resumable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from build_coordinator.runner.git_safety import RealGit, GitSafetyError
from build_coordinator.runner.worktree import (
    WorktreeValidationError,
    prepare_task_workspace,
    task_branch_name,
)

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    import os

    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env={**os.environ, **ENV}
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "origin", "main")
    return root


def commit(tree: Path, name: str) -> str:
    (tree / name).write_text(name, encoding="utf-8")
    git(tree, "add", name)
    git(tree, "commit", "-m", name)
    return git(tree, "rev-parse", "HEAD")


def test_task_branch_names_are_ref_safe():
    assert task_branch_name("CAV-122-01") == "stagemesh/CAV-122-01"
    assert task_branch_name("weird id..x.lock") == "stagemesh/weird-id-x-lock"
    assert task_branch_name("///") == "stagemesh/task"


def test_fresh_task_starts_from_main_and_never_inherits_another_tasks_commits(repo, tmp_path):
    roots = (str(tmp_path / "wt"),)
    path = tmp_path / "wt" / "builder-1"
    first = prepare_task_workspace(path, repo, branch_name="stagemesh/T-1", base_ref="main", allowed_roots=roots)
    rejected = commit(first, "rejected.txt")
    git(first, "push", "origin", "stagemesh/T-1")

    again = prepare_task_workspace(path, repo, branch_name="stagemesh/T-2", base_ref="main", allowed_roots=roots)
    assert again == first
    assert git(again, "rev-parse", "--abbrev-ref", "HEAD") == "stagemesh/T-2"
    assert not (again / "rejected.txt").exists()
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", rejected, "HEAD"], cwd=str(again)
    ).returncode == 1


def test_fresh_task_tracks_the_latest_remote_main(repo, tmp_path):
    roots = (str(tmp_path / "wt"),)
    path = tmp_path / "wt" / "builder-1"
    prepare_task_workspace(path, repo, branch_name="stagemesh/T-1", base_ref="main", allowed_roots=roots)
    newer = commit(repo, "landed.txt")
    git(repo, "push", "origin", "main")
    workspace = prepare_task_workspace(path, repo, branch_name="stagemesh/T-2", base_ref="main", allowed_roots=roots)
    assert git(workspace, "rev-parse", "HEAD") == newer


def test_resumed_task_keeps_its_commits_even_on_a_different_worker(repo, tmp_path):
    roots = (str(tmp_path / "wt"),)
    one, two = tmp_path / "wt" / "builder-1", tmp_path / "wt" / "builder-2"
    tree = prepare_task_workspace(one, repo, branch_name="stagemesh/T-9", base_ref="main", allowed_roots=roots)
    work = commit(tree, "work.txt")
    (tree / "dirty.txt").write_text("uncommitted", encoding="utf-8")

    moved = prepare_task_workspace(
        two, repo, branch_name="stagemesh/T-9", base_ref="main", resume=True, allowed_roots=roots
    )
    assert subprocess.run(["git", "merge-base", "--is-ancestor", work, "HEAD"], cwd=str(moved)).returncode == 0
    assert (moved / "work.txt").exists()
    assert (moved / "dirty.txt").read_text(encoding="utf-8") == "uncommitted"  # carried as a WIP commit
    assert "recovered work-in-progress" in git(moved, "log", "-1", "--format=%s")
    assert git(repo, "stash", "list") == ""
    assert git(one, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # branch freed, never deleted


def test_second_task_never_collides_with_a_branch_checked_out_elsewhere(repo, tmp_path):
    """Two distinct tasks (e.g. from different objectives) must never end up
    sharing one branch checkout: preparing task A's workspace while task B's
    branch is checked out (with uncommitted work) in a sibling worktree must
    detach that sibling and preserve its work rather than colliding with it."""
    roots = (str(tmp_path / "wt"),)
    holder = tmp_path / "wt" / "builder-1"
    mover = tmp_path / "wt" / "builder-2"
    branch = "stagemesh/T-collide"

    held = prepare_task_workspace(holder, repo, branch_name=branch, base_ref="main", allowed_roots=roots)
    (held / "dirty.txt").write_text("uncommitted on holder", encoding="utf-8")

    prepare_task_workspace(mover, repo, branch_name=branch, base_ref="main", resume=True, allowed_roots=roots)

    assert git(holder, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached, no longer holds the branch
    assert git(mover, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert (mover / "dirty.txt").read_text(encoding="utf-8") == "uncommitted on holder"
    assert "recovered work-in-progress" in git(mover, "log", "-1", "--format=%s")


def test_workspace_outside_allowed_roots_is_rejected(repo, tmp_path):
    with pytest.raises(WorktreeValidationError, match="outside allowed workspace roots"):
        prepare_task_workspace(
            tmp_path / "elsewhere" / "w", repo, branch_name="stagemesh/T-1", base_ref="main",
            allowed_roots=(str(tmp_path / "wt"),),
        )


def test_fetch_retries_ref_lock_contention_but_not_other_failures(monkeypatch, tmp_path):
    import build_coordinator.runner.git_safety as safety

    calls = {"n": 0}

    def flaky(cwd, *args):
        calls["n"] += 1
        stderr = "error: cannot lock ref 'refs/remotes/origin/x': is at A but expected B" if calls["n"] < 3 else ""
        return subprocess.CompletedProcess(args, 1 if stderr else 0, "", stderr)

    monkeypatch.setattr(safety, "_git", flaky)
    monkeypatch.setattr(safety.time, "sleep", lambda _s: None)
    RealGit().fetch_prune(tmp_path)
    assert calls["n"] == 3

    monkeypatch.setattr(
        safety, "_git", lambda cwd, *a: subprocess.CompletedProcess(a, 128, "", "fatal: not a git repository")
    )
    with pytest.raises(GitSafetyError, match="not a git repository"):
        RealGit().fetch_prune(tmp_path)
