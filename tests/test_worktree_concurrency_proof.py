from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stagemesh.worktree import cleanup_task_branch, cleanup_worktree, ensure_worktree, prepare_task_workspace


def _init_git_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), capture_output=True, check=True)
    (repo_path / "README.md").write_text("# Test Repo", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_path), capture_output=True, check=True)


def test_two_task_worktree_isolation(tmp_path: Path):
    repo = tmp_path / "main_repo"
    _init_git_repo(repo)

    wt_a = tmp_path / "worktrees" / "task_a"
    wt_b = tmp_path / "worktrees" / "task_b"

    ws_a = prepare_task_workspace(
        wt_a,
        repo_root=repo,
        branch_name="stagemesh/task-A",
        base_ref="master",
        allowed_roots=(str(tmp_path),),
    )
    ws_b = prepare_task_workspace(
        wt_b,
        repo_root=repo,
        branch_name="stagemesh/task-B",
        base_ref="master",
        allowed_roots=(str(tmp_path),),
    )

    # Task A edits file A
    (ws_a / "file_a.txt").write_text("content A", encoding="utf-8")
    subprocess.run(["git", "add", "file_a.txt"], cwd=str(ws_a), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Task A work"], cwd=str(ws_a), capture_output=True, check=True)
    sha_a = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws_a), capture_output=True, text=True).stdout.strip()

    # Task B edits file B
    (ws_b / "file_b.txt").write_text("content B", encoding="utf-8")
    subprocess.run(["git", "add", "file_b.txt"], cwd=str(ws_b), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Task B work"], cwd=str(ws_b), capture_output=True, check=True)
    sha_b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ws_b), capture_output=True, text=True).stdout.strip()

    # Prove distinct SHAs and non-interference
    assert sha_a != sha_b
    assert (ws_a / "file_a.txt").exists()
    assert not (ws_a / "file_b.txt").exists()
    assert (ws_b / "file_b.txt").exists()
    assert not (ws_b / "file_a.txt").exists()

    # Merge Task A into master & cleanup
    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "merge", "stagemesh/task-A"], cwd=str(repo), capture_output=True, check=True)
    ok_a, _ = cleanup_task_branch(repo, "stagemesh/task-A", main_ref="master", reviewed_sha=sha_a)
    assert ok_a is True

    # Task B workspace remains intact
    assert (ws_b / "file_b.txt").exists()
