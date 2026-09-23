"""Tests for automated workspace / worktree provisioning."""

from __future__ import annotations

import subprocess
import pytest
from pathlib import Path

from build_coordinator.runner.worktree import WorktreeValidationError, ensure_worktree, validate_worktree_path


def test_ensure_worktree_rejects_unauthorized_root(tmp_path: Path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    unauthorized = tmp_path / "rogue" / "worktree-1"

    with pytest.raises(WorktreeValidationError, match="outside allowed workspace roots"):
        ensure_worktree(
            unauthorized,
            repo_root=allowed_root,
            allowed_roots=(str(allowed_root),),
        )


def test_ensure_worktree_provisions_git_worktree_when_missing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("# Repo", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo), capture_output=True, check=True)

    target_worktree = tmp_path / "worktrees" / "builder-a"
    assert not target_worktree.exists()

    resolved = ensure_worktree(
        target_worktree,
        repo_root=repo,
        branch_name="feature/auto-worktree",
        allowed_roots=(str(tmp_path),),
    )

    assert resolved.exists()
    assert (resolved / ".git").exists()
    assert (resolved / "README.md").exists()

    # Re-running ensure_worktree returns existing worktree safely
    second = ensure_worktree(
        target_worktree,
        repo_root=repo,
        allowed_roots=(str(tmp_path),),
    )
    assert second == resolved
