"""Git worktree auto-provisioning, workspace isolation, and cleanup lifecycle for StageMesh vNext.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


class WorktreeValidationError(ValueError):
    """Raised when a worktree path or branch operations fail validation bounds."""


def is_git_worktree(path: Path) -> bool:
    git_entry = path / ".git"
    if git_entry.is_dir():
        return True
    if git_entry.is_file():
        try:
            contents = git_entry.read_text(encoding="utf-8", errors="replace")[:240]
        except OSError:
            return False
        return contents.lower().startswith("gitdir:")
    return False


def _is_under(path: Path, root: str) -> bool:
    try:
        path.relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False


def validate_worktree_path(
    path: str | Path | None,
    *,
    allowed_roots: tuple[str, ...] = (),
    require_git: bool = True,
) -> Path | None:
    if path is None or path == "":
        return None
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise WorktreeValidationError(f"worktree path does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorktreeValidationError(f"worktree path is not a directory: {resolved}")
    if require_git and not is_git_worktree(resolved):
        raise WorktreeValidationError(f"worktree path is not a Git repository or worktree: {resolved}")
    if allowed_roots:
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise WorktreeValidationError(f"worktree path is outside allowed workspace roots: {resolved}")
    return resolved


def task_branch_name(task_id: str) -> str:
    """Ref-safe deterministic branch name for a task."""
    safe = re.sub(r"[^A-Za-z0-9._/-]", "-", task_id).strip("./-") or "task"
    safe = safe.replace("..", "-")
    if safe.endswith(".lock"):
        safe = safe[: -len(".lock")] + "-lock"
    return f"stagemesh/{safe}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _ref_exists(cwd: Path, ref: str) -> bool:
    return _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _checked_out_elsewhere(cwd: Path, branch: str) -> Path | None:
    listing = _git(cwd, "worktree", "list", "--porcelain").stdout
    current: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
        elif line == f"branch refs/heads/{branch}" and current is not None:
            return current
    return None


def ensure_worktree(
    path: str | Path | None,
    repo_root: str | Path,
    *,
    branch_name: str | None = None,
    base_sha: str | None = None,
    allowed_roots: tuple[str, ...] = (),
) -> Path | None:
    """Ensure an isolated Git worktree exists, provisioning it automatically if absent."""
    if path is None or path == "":
        return None
    resolved = Path(path).expanduser().resolve()
    repo_root_path = Path(repo_root).expanduser().resolve()

    if allowed_roots:
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise WorktreeValidationError(f"worktree path is outside allowed workspace roots: {resolved}")

    if resolved.exists():
        if is_git_worktree(resolved):
            return resolved
        raise WorktreeValidationError(f"path exists but is not a valid git worktree: {resolved}")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_name or f"stagemesh/{resolved.name}"
    target_base = "HEAD"
    if base_sha:
        probe = _git(repo_root_path, "rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}")
        if probe.returncode == 0:
            target_base = base_sha

    cmd = ["git", "-c", "core.longpaths=true", "worktree", "add", "-B", branch, str(resolved), target_base]
    res = subprocess.run(cmd, cwd=str(repo_root_path), capture_output=True, text=True, check=False)
    if res.returncode != 0:
        _git(repo_root_path, "worktree", "prune")
        res = subprocess.run(cmd, cwd=str(repo_root_path), capture_output=True, text=True, check=False)

    if res.returncode != 0:
        raise WorktreeValidationError(
            f"failed to automatically provision worktree at {resolved}: {res.stderr.strip() or res.stdout.strip()}"
        )
    return resolved


def prepare_task_workspace(
    path: str | Path,
    repo_root: str | Path,
    *,
    branch_name: str,
    base_ref: str = "master",
    allowed_roots: tuple[str, ...] = (),
) -> Path:
    """Prepare a task's isolated working checkout inside a worktree."""
    workspace = ensure_worktree(
        path,
        repo_root=repo_root,
        branch_name=f"stagemesh/workspace/{Path(path).name}",
        base_sha=base_ref,
        allowed_roots=allowed_roots,
    )
    if workspace is None:
        raise WorktreeValidationError("a task workspace path is required")

    base = base_ref if _ref_exists(workspace, base_ref) else "HEAD"
    result = _git(workspace, "checkout", "-B", branch_name, base)
    if result.returncode != 0:
        raise WorktreeValidationError(
            f"could not prepare workspace {workspace} for {branch_name}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return workspace


def cleanup_task_branch(
    repo_root: str | Path,
    branch: str,
    *,
    main_ref: str = "master",
    reviewed_sha: str | None = None,
) -> tuple[bool, str]:
    """Safely delete an integrated StageMesh task branch. Refuses if branch is unmerged or not a stagemesh/ branch."""
    root = Path(repo_root).expanduser().resolve()
    if not branch.startswith("stagemesh/") or not _ref_exists(root, branch):
        return False, "not a StageMesh task branch"

    tip = _git(root, "rev-parse", branch).stdout.strip()
    target_sha = reviewed_sha or tip
    contained = _git(root, "merge-base", "--is-ancestor", target_sha, main_ref).returncode == 0
    if not contained:
        return False, f"{branch} is not fully integrated into {main_ref}"

    holder = _checked_out_elsewhere(root, branch)
    if holder is not None:
        if _git(holder, "status", "--porcelain").stdout.strip():
            return False, f"{branch} is checked out with uncommitted changes at {holder}"
        _git(holder, "checkout", "--detach")

    res = _git(root, "branch", "-D", branch)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def cleanup_worktree(
    repo_root: str | Path,
    worktree_path: str | Path,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Remove a StageMesh-managed worktree cleanly."""
    root = Path(repo_root).expanduser().resolve()
    target = Path(worktree_path).expanduser().resolve()

    if not target.exists():
        return True, "worktree path already removed"

    if _git(target, "status", "--porcelain").stdout.strip() and not force:
        return False, "worktree has uncommitted changes"

    res = _git(root, "worktree", "remove", "--force" if force else "", str(target))
    if res.returncode == 0 or not target.exists():
        _git(root, "worktree", "prune")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return True, "worktree removed"
    return False, (res.stderr or res.stdout).strip()
