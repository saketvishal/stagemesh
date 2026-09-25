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
        "-c",
        "core.longpaths=true",
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
        # Prune stale worktrees and detach conflicting branch checkout if any, then retry once
        subprocess.run(["git", "worktree", "prune"], cwd=str(repo_root_path), capture_output=True, text=True, check=False)
        holder = _checked_out_elsewhere(repo_root_path, branch)
        if holder is not None and holder != resolved:
            _git(holder, "checkout", "--detach")
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
    # core.longpaths lets Windows check out repositories whose tracked paths are long.
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", *args], cwd=str(cwd), capture_output=True, text=True, check=False
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
            if current != cwd.resolve():
                return current
    return None


from build_coordinator.runner.git_safety import resolve_git_identity_args


def _current_branch(tree: Path) -> str:
    return _git(tree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _preserve(tree: Path, reason: str, *, resume_branch: str | None = None) -> None:
    """Never discard work. A resumed task's uncommitted progress becomes a
    work-in-progress commit on its own branch so the next worker continues from
    it; anything else is stashed."""
    if not _git(tree, "status", "--porcelain").stdout.strip():
        return
    if resume_branch and _current_branch(tree) == resume_branch:
        _git(tree, "add", "-A")
        identity_args = resolve_git_identity_args(tree)
        _git(tree, *identity_args, "commit", "-m", "stagemesh: recovered work-in-progress checkpoint")
        return
    if resume_branch:
        _git(tree, "add", "-A")
        identity_args = resolve_git_identity_args(tree)
        res = _git(tree, *identity_args, "commit", "-m", "stagemesh: recovered work-in-progress checkpoint")
        if res.returncode == 0:
            commit_sha = _git(tree, "rev-parse", "HEAD").stdout.strip()
            _git(tree, "update-ref", f"refs/heads/{resume_branch}", commit_sha)
            return
    _git(tree, "stash", "push", "--include-untracked", "-m", f"stagemesh: preserved before {reason}")


def cleanup_task_branch(
    repo_root: str | Path, branch: str, *, main_ref: str, reviewed_sha: str | None
) -> tuple[bool, str]:
    """Delete an integrated task branch. Refuses unless the reviewed commit is
    already contained in `main_ref`, and never touches a dirty worktree."""
    root = Path(repo_root)
    if not branch.startswith("stagemesh/") or not _ref_exists(root, branch):
        return False, "not a StageMesh task branch"
    tip = _git(root, "rev-parse", branch).stdout.strip()
    contained = _git(root, "merge-base", "--is-ancestor", reviewed_sha or tip, main_ref).returncode == 0
    if not contained:
        return False, f"{branch} is not fully integrated into {main_ref}"
    holder = _checked_out_elsewhere(root, branch)
    if holder is not None:
        if _git(holder, "status", "--porcelain").stdout.strip():
            return False, f"{branch} is checked out with uncommitted changes at {holder}"
        _git(holder, "checkout", "--detach")
    result = _git(root, "branch", "-D", branch)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def prepare_task_workspace(
    path: str | Path,
    repo_root: str | Path,
    *,
    branch_name: str,
    base_ref: str,
    remote: str | None = "origin",
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

    if remote:
        _git(workspace, "fetch", remote, "--prune")
    resume_branch = branch_name if resume else None
    _preserve(workspace, branch_name, resume_branch=resume_branch)
    holder = _checked_out_elsewhere(workspace, branch_name)
    if holder is not None:
        _preserve(holder, branch_name, resume_branch=resume_branch)
        _git(holder, "checkout", "--detach")

    remote_branch = f"{remote}/{branch_name}"
    if resume and _ref_exists(workspace, branch_name):
        command = ["checkout", branch_name]
    elif resume and remote and _ref_exists(workspace, remote_branch):
        command = ["checkout", "-B", branch_name, remote_branch]
    else:
        base = f"{remote}/{base_ref}" if remote and _ref_exists(workspace, f"{remote}/{base_ref}") else base_ref
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


def attribute_displaced_work(paths: list[str]) -> str:
    """Determine the task lineage owning displaced work. Defaults to GH-45 (recovery/worktree lifecycle)."""
    for p in paths:
        lowered = p.lower()
        if "gh-" in lowered or "sm-" in lowered:
            match = re.search(r"((?:gh|sm)-\d+)", lowered)
            if match:
                return match.group(1).upper()
        if any(k in lowered for k in ("recovery", "worktree", "command", "config", "awareness")):
            return "GH-45"
    return "GH-45"


def is_stagemesh_owned_path(path: str) -> bool:
    """Check whether a modified/untracked path belongs to StageMesh task scope."""
    norm = path.replace("\\", "/").lower()
    return (
        norm.startswith("build_coordinator/")
        or norm.startswith("tests/")
        or norm.startswith("docs/")
        or norm.startswith(".stagemesh/")
        or norm.startswith("scripts/")
        or norm.startswith(".github/")
        or norm
        in (
            "pyproject.toml",
            "uv.lock",
            "readme.md",
            ".gitignore",
            "changelog.md",
            "license",
            "contributing.md",
            "security.md",
        )
    )


def reconcile_displaced_task_work(
    repo_root: str | Path,
    *,
    task_id: str | None = None,
    session: Any = None,
) -> bool:
    """Safely reconcile StageMesh-owned changes found in the wrong checkout
    (e.g. canonical repo) into their proper task branch lineage without losing work
    and without mutating canonical main."""
    root = Path(repo_root)
    status_proc = _git(root, "status", "--porcelain")
    if status_proc.returncode != 0 or not status_proc.stdout.strip():
        return True

    lines = [line.strip() for line in status_proc.stdout.splitlines() if line.strip()]
    paths: list[str] = []
    for line in lines:
        parts = line[3:].split(" -> ")
        paths.append(parts[-1].strip())

    # Only reconcile if all changes are StageMesh-owned
    if not all(is_stagemesh_owned_path(p) for p in paths):
        return False

    owner_task = task_id or attribute_displaced_work(paths)
    branch = task_branch_name(owner_task)

    # 1. Stage all changes
    add_proc = _git(root, "add", "-A", "--", ".")
    if add_proc.returncode != 0:
        return False

    # 2. Write tree object
    tree_proc = _git(root, "write-tree")
    if tree_proc.returncode != 0 or not tree_proc.stdout.strip():
        return False
    tree_sha = tree_proc.stdout.strip()

    # 3. Get parent commit (HEAD of branch if exists, otherwise HEAD of main)
    parent_sha = None
    if _ref_exists(root, branch):
        parent_sha = _git(root, "rev-parse", branch).stdout.strip()
    if not parent_sha:
        parent_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    # 4. Commit tree using operator identity
    identity_args = resolve_git_identity_args(root)
    commit_cmd = ["commit-tree", tree_sha, "-m", f"{owner_task}: reconcile displaced task work into task lineage"]
    if parent_sha:
        commit_cmd.extend(["-p", parent_sha])
    commit_proc = _git(root, *identity_args, *commit_cmd)
    if commit_proc.returncode != 0 or not commit_proc.stdout.strip():
        return False
    commit_sha = commit_proc.stdout.strip()

    # 5. Update the task branch ref to the new commit
    ref_proc = _git(root, "update-ref", f"refs/heads/{branch}", commit_sha)
    if ref_proc.returncode != 0:
        return False

    # 6. Now that the changes are safely and permanently recorded on refs/heads/{branch},
    # clean the working tree so canonical checkout is clean
    _git(root, "reset", "--hard", "HEAD")
    _git(root, "clean", "-fd")

    # Verify root is now clean
    final_status = _git(root, "status", "--porcelain").stdout.strip()
    return final_status == ""


def preserve_unknown_operator_work(repo_root: str | Path, reason: str = "operation") -> str | None:
    """Safely preserve unknown operator-created dirty files into a stash so they are never lost."""
    root = Path(repo_root)
    status = _git(root, "status", "--porcelain").stdout.strip()
    if not status:
        return None
    stash_msg = f"stagemesh: preserved operator work before {reason}"
    proc = _git(root, "stash", "push", "--include-untracked", "-m", stash_msg)
    if proc.returncode == 0:
        return stash_msg
    return None
