"""Narrow operator-config worktree validation and autonomous provisioning.

Task-provided text must never choose the executor cwd. Only trusted
WorkerConfig paths are validated and provisioned here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class WorktreeValidationError(ValueError):
    """Raised when a configured worktree is not safe to use as cwd."""


def validate_worktree_path(
    path: str | None,
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
        raise WorktreeValidationError(
            f"worktree path is not a Git repository or worktree: {resolved}"
        )
    if allowed_roots:
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise WorktreeValidationError(
                f"worktree path is outside allowed workspace roots: {resolved}"
            )
    return resolved


def ensure_worktree(
    path: str | Path | None,
    repo_root: str | Path,
    *,
    branch_name: str | None = None,
    base_sha: str | None = None,
    allowed_roots: tuple[str, ...] = (),
) -> Path | None:
    """Ensure an isolated worktree exists, provisioning it automatically if absent."""
    if path is None or path == "":
        return None
    resolved = Path(path).expanduser().resolve()
    repo_root_path = Path(repo_root).expanduser().resolve()

    if allowed_roots:
        if not any(_is_under(resolved, root) for root in allowed_roots):
            raise WorktreeValidationError(
                f"worktree path is outside allowed workspace roots: {resolved}"
            )

    if resolved.exists():
        if is_git_worktree(resolved):
            return resolved
        raise WorktreeValidationError(
            f"path exists but is not a valid git worktree: {resolved}"
        )

    resolved.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_name or f"stagemesh/{resolved.name}"
    target_base = "HEAD"
    if base_sha:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}"],
            cwd=str(repo_root_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            target_base = base_sha

    cmd = [
        "git",
        "worktree",
        "add",
        "-B",
        branch,
        str(resolved),
        target_base,
    ]
    res = subprocess.run(
        cmd,
        cwd=str(repo_root_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise WorktreeValidationError(
            f"failed to automatically provision worktree at {resolved}: {res.stderr.strip() or res.stdout.strip()}"
        )
    return resolved


def task_branch_name(task_id: str) -> str:
    """Deterministic, ref-safe feature branch for a task."""
    safe = re.sub(r"[^A-Za-z0-9._/-]", "-", task_id).strip("./-") or "task"
    safe = safe.replace("..", "-")
    if safe.endswith(".lock"):
        safe = safe[: -len(".lock")] + "-lock"
    return f"stagemesh/{safe}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _ref_exists(cwd: Path, ref: str) -> bool:
    return _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _checked_out_elsewhere(cwd: Path, branch: str) -> Path | None:
    listing = _git(cwd, "worktree", "list", "--porcelain").stdout
    current: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):]).resolve()
        elif line == f"branch refs/heads/{branch}" and current is not None:
            if current != cwd.resolve():
                return current
    return None


def _preserve(tree: Path, reason: str) -> None:
    """Never discard work: stash anything uncommitted."""
    if _git(tree, "status", "--porcelain").stdout.strip():
        _git(tree, "stash", "push", "--include-untracked", "-m", f"stagemesh: preserved before {reason}")


def prepare_task_workspace(
    path: str | Path,
    repo_root: str | Path,
    *,
    branch_name: str,
    base_ref: str,
    remote: str = "origin",
    resume: bool = False,
    allowed_roots: tuple[str, ...] = (),
) -> Path:
    """Give one task its own branch inside a StageMesh-managed worktree.

    A fresh task starts from the current `base_ref` (preferring the remote's
    copy), so no commit from another task, including a rejected one, can ride
    along. A resumed task (rework, recovery) re-attaches to its existing
    branch instead. Uncommitted leftovers are stashed, never discarded.
    """
    workspace = ensure_worktree(
        path,
        repo_root=repo_root,
        branch_name=f"stagemesh/workspace/{Path(path).name}",
        base_sha=base_ref,
        allowed_roots=allowed_roots,
    )
    if workspace is None:
        raise WorktreeValidationError("a task workspace path is required")

    _git(workspace, "fetch", remote, "--prune")
    _preserve(workspace, branch_name)
    holder = _checked_out_elsewhere(workspace, branch_name)
    if holder is not None:
        _preserve(holder, branch_name)
        _git(holder, "checkout", "--detach")

    remote_branch = f"{remote}/{branch_name}"
    if resume and _ref_exists(workspace, branch_name):
        command = ["checkout", branch_name]
    elif resume and _ref_exists(workspace, remote_branch):
        command = ["checkout", "-B", branch_name, remote_branch]
    else:
        base = f"{remote}/{base_ref}" if _ref_exists(workspace, f"{remote}/{base_ref}") else base_ref
        if not _ref_exists(workspace, base):
            base = "HEAD"
        command = ["checkout", "-B", branch_name, base]
    result = _git(workspace, *command)
    if result.returncode != 0:
        raise WorktreeValidationError(
            f"could not prepare workspace {workspace} for {branch_name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return workspace


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
