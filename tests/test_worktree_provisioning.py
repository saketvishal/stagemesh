from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stagemesh.worktree import WorktreeValidationError, ensure_worktree, validate_worktree_path


def _init_git_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), capture_output=True, check=True)
    (repo_path / "README.md").write_text("# Test Repo", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_path), capture_output=True, check=True)


def test_ensure_worktree_provisions_isolated_workspace(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    target = tmp_path / "worktrees" / "task-1"
    resolved = ensure_worktree(
        target,
        repo_root=repo,
        branch_name="stagemesh/task-1",
        allowed_roots=(str(tmp_path),),
    )

    assert resolved.exists()
    assert (resolved / ".git").exists()
    assert (resolved / "README.md").exists()

    # Idempotent re-execution
    second = ensure_worktree(
        target,
        repo_root=repo,
        branch_name="stagemesh/task-1",
        allowed_roots=(str(tmp_path),),
    )
    assert second == resolved


def test_ensure_worktree_rejects_unauthorized_root(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    unauthorized = tmp_path / "rogue" / "worktree-1"

    with pytest.raises(WorktreeValidationError, match="outside allowed workspace roots"):
        ensure_worktree(
            unauthorized,
            repo_root=allowed,
            allowed_roots=(str(allowed),),
        )
