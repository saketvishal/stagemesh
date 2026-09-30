from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stagemesh.worktree import cleanup_task_branch, cleanup_worktree, ensure_worktree


def _init_git_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), capture_output=True, check=True)
    (repo_path / "README.md").write_text("# Test Repo", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_path), capture_output=True, check=True)


def test_cleanup_integrated_task_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    # Create task branch & commit
    subprocess.run(["git", "checkout", "-b", "stagemesh/task-100"], cwd=str(repo), capture_output=True, check=True)
    (repo / "task.txt").write_text("task 100", encoding="utf-8")
    subprocess.run(["git", "add", "task.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "task 100 implementation"], cwd=str(repo), capture_output=True, check=True)
    task_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()

    # Merge into main
    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "merge", "stagemesh/task-100"], cwd=str(repo), capture_output=True, check=True)
    main_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()

    ok, msg = cleanup_task_branch(repo, "stagemesh/task-100", main_ref="master", reviewed_sha=task_sha)
    assert ok is True

    # Branch is deleted
    probe = subprocess.run(["git", "rev-parse", "--verify", "stagemesh/task-100"], cwd=str(repo), capture_output=True)
    assert probe.returncode != 0


def test_cleanup_refuses_unmerged_task_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    # Create task branch & commit
    subprocess.run(["git", "checkout", "-b", "stagemesh/task-200"], cwd=str(repo), capture_output=True, check=True)
    (repo / "unmerged.txt").write_text("unmerged work", encoding="utf-8")
    subprocess.run(["git", "add", "unmerged.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "unmerged work"], cwd=str(repo), capture_output=True, check=True)
    task_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()

    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)

    ok, msg = cleanup_task_branch(repo, "stagemesh/task-200", main_ref="master", reviewed_sha=task_sha)
    assert ok is False
    assert "not fully integrated" in msg


def test_cleanup_refuses_user_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    subprocess.run(["git", "checkout", "-b", "user-feature"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)

    ok, msg = cleanup_task_branch(repo, "user-feature", main_ref="master", reviewed_sha=None)
    assert ok is False
    assert "not a StageMesh task branch" in msg
