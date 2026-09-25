"""Tests for repository-scoped standalone clone pools (GH-56)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from build_coordinator.runner.clone_pool import (
    ensure_repo_clone,
    expected_remote_url,
    is_standalone_clone,
    repo_identity,
    sync_task_branch,
    verify_clone_remote,
)
from build_coordinator.runner.worktree import WorktreeValidationError


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), capture_output=True, check=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], path)
    _run(["git", "config", "user.name", "Test"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("# Repo", encoding="utf-8")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-m", "initial commit"], path)
    return path


def _init_bare_remote(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}.git"
    bare.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--bare"], bare)
    return bare


def _repo_with_origin(tmp_path: Path, name: str) -> tuple[Path, Path]:
    bare = _init_bare_remote(tmp_path, name)
    repo = _init_repo(tmp_path / name)
    _run(["git", "remote", "add", "origin", str(bare)], repo)
    _run(["git", "push", "-u", "origin", "master"], repo)
    return repo, bare


def test_ensure_repo_clone_creates_standalone_clone_not_linked_worktree(tmp_path: Path):
    repo, _bare = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"

    clone_path = ensure_repo_clone(pool_root, repo, "builder-a")

    assert clone_path.exists()
    assert is_standalone_clone(clone_path)
    # A linked worktree would register itself in the source repo's
    # .git/worktrees metadata; a standalone clone must not.
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert str(clone_path) not in listing


def test_ensure_repo_clone_is_reused_across_calls(tmp_path: Path):
    repo, _bare = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"

    first = ensure_repo_clone(pool_root, repo, "builder-a")
    second = ensure_repo_clone(pool_root, repo, "builder-a")

    assert first == second


def test_different_repos_get_separate_pool_slots(tmp_path: Path):
    repo_a, _ = _repo_with_origin(tmp_path, "repo-a")
    repo_b, _ = _repo_with_origin(tmp_path, "repo-b")
    pool_root = tmp_path / "pool"

    clone_a = ensure_repo_clone(pool_root, repo_a, "builder-a")
    clone_b = ensure_repo_clone(pool_root, repo_b, "builder-a")

    assert clone_a != clone_b
    assert repo_identity(repo_a) != repo_identity(repo_b)


def test_ensure_repo_clone_fails_closed_on_remote_mismatch(tmp_path: Path):
    repo_a, _ = _repo_with_origin(tmp_path, "repo-a")
    repo_b, _ = _repo_with_origin(tmp_path, "repo-b")
    pool_root = tmp_path / "pool"

    clone_path = ensure_repo_clone(pool_root, repo_a, "builder-a")
    # Simulate repository reconfiguration by repointing the pooled clone's
    # remote away from what its slot is keyed on.
    _run(["git", "remote", "set-url", "origin", str(repo_b)], clone_path)

    with pytest.raises(WorktreeValidationError, match="not the expected"):
        ensure_repo_clone(pool_root, repo_a, "builder-a")


def test_ensure_repo_clone_fails_closed_with_no_remote(tmp_path: Path):
    repo, _ = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"

    clone_path = ensure_repo_clone(pool_root, repo, "builder-a")
    _run(["git", "remote", "remove", "origin"], clone_path)

    with pytest.raises(WorktreeValidationError, match="no 'origin' remote"):
        ensure_repo_clone(pool_root, repo, "builder-a")


def test_ensure_repo_clone_rejects_non_clone_slot(tmp_path: Path):
    repo, _ = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"
    slot = pool_root / repo_identity(repo) / "builder-a"
    slot.mkdir(parents=True)
    (slot / "not_a_repo.txt").write_text("hi", encoding="utf-8")

    with pytest.raises(WorktreeValidationError, match="not a standalone git clone"):
        ensure_repo_clone(pool_root, repo, "builder-a")


def test_verify_clone_remote_accepts_matching_url(tmp_path: Path):
    repo, bare = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"
    clone_path = ensure_repo_clone(pool_root, repo, "builder-a")

    verify_clone_remote(clone_path, expected_remote_url(repo))


def test_sync_task_branch_resumes_from_remote_task_branch(tmp_path: Path):
    repo, bare = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"

    # A prior worker run pushed progress on the task branch.
    prior_clone = ensure_repo_clone(pool_root, repo, "builder-a")
    sync_task_branch(prior_clone, branch_name="stagemesh/task-1", base_ref="master", resume=False)
    (prior_clone / "progress.txt").write_text("wip", encoding="utf-8")
    _run(["git", "add", "progress.txt"], prior_clone)
    _run(["git", "commit", "-m", "progress"], prior_clone)
    _run(["git", "push", "origin", "stagemesh/task-1"], prior_clone)

    # A different worker slot (fresh clone, no local knowledge of the branch)
    # resumes the task and must pick up the remote progress.
    fresh_clone = ensure_repo_clone(pool_root, repo, "builder-b")
    sync_task_branch(fresh_clone, branch_name="stagemesh/task-1", base_ref="master", resume=True)

    assert (fresh_clone / "progress.txt").exists()


def test_sync_task_branch_starts_fresh_from_base_ref_when_not_resuming(tmp_path: Path):
    repo, _bare = _repo_with_origin(tmp_path, "repo-a")
    pool_root = tmp_path / "pool"
    clone_path = ensure_repo_clone(pool_root, repo, "builder-a")

    sync_task_branch(clone_path, branch_name="stagemesh/task-2", base_ref="master", resume=False)

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(clone_path), capture_output=True, text=True
    ).stdout.strip()
    assert branch == "stagemesh/task-2"
