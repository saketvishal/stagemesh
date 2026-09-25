"""Repository-scoped standalone clone pools for task workers.

Ports the clone-pool design proven in caventra-orchestrator. A linked
`git worktree add` checkout shares `.git/worktrees` administrative metadata
with the source checkout; on Windows that shared metadata routinely wedges
(locked index/HEAD files, orphaned worktree records) once several workers
churn through task branches concurrently. A standalone clone owns its own
`.git` directory and has no such coupling, at the cost of disk space and an
initial `git clone`. Clones are pooled per repository identity and reused
across tasks by worker slot instead of being recreated every run.

Because a pooled clone can outlive many tasks and repositories can be
reconfigured, every reuse re-verifies the clone's `origin` remote against the
expected repository URL and refuses (fails closed) rather than silently
running a task -- or pushing its result -- against the wrong repository.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from build_coordinator.runner.worktree import (
    WorktreeValidationError,
    _preserve,
    _ref_exists,
    _is_under,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def normalize_remote_url(url: str) -> str:
    """Normalize a remote URL for equality comparison.

    Trailing slashes, a trailing `.git`, and case in scheme/host differences
    from platform-generated URLs are common cosmetic variance, not a real
    difference in target repository.
    """
    trimmed = url.strip().rstrip("/")
    if trimmed.lower().endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    return trimmed.lower()


def repo_identity(repo_root: str | Path, *, remote: str = "origin") -> str:
    """Stable slug identifying a repository, used to key its clone pool.

    Derived from the repository's remote URL when one is configured (so two
    different checkouts of the same repository share a pool); falls back to
    the resolved local path for remote-less repositories such as test
    fixtures.
    """
    root = Path(repo_root).expanduser().resolve()
    result = _git(root, "remote", "get-url", remote)
    basis = result.stdout.strip() if result.returncode == 0 else str(root)
    normalized = normalize_remote_url(basis)
    name = re.split(r"[\\/]", normalized)[-1] or "repo"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "repo"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def expected_remote_url(repo_root: str | Path, *, remote: str = "origin") -> str:
    """The remote URL a clone of `repo_root` must match. Raises if `repo_root`
    itself has no such remote configured -- there is nothing safe to verify
    against, so callers must not proceed."""
    result = _git(Path(repo_root).expanduser().resolve(), "remote", "get-url", remote)
    if result.returncode != 0:
        raise WorktreeValidationError(
            f"source repository has no '{remote}' remote configured: {repo_root}"
        )
    return result.stdout.strip()


def _clone_remote_url(clone_path: Path, *, remote: str = "origin") -> str | None:
    result = _git(clone_path, "remote", "get-url", remote)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_standalone_clone(path: Path) -> bool:
    """True only for a full clone with its own `.git` directory -- never a
    linked worktree (`.git` file pointing at another repo's metadata)."""
    return (path / ".git").is_dir()


def verify_clone_remote(clone_path: Path, expected_url: str, *, remote: str = "origin") -> None:
    """Fail-closed remote verification: refuse to reuse or push through a
    clone whose `remote` doesn't match `expected_url`."""
    actual = _clone_remote_url(clone_path, remote=remote)
    if actual is None:
        raise WorktreeValidationError(
            f"clone at {clone_path} has no '{remote}' remote configured; "
            "refusing to reuse it for a task"
        )
    if normalize_remote_url(actual) != normalize_remote_url(expected_url):
        raise WorktreeValidationError(
            f"clone at {clone_path} is bound to '{actual}', not the expected "
            f"'{expected_url}'; refusing to run a task against a mismatched repository"
        )


def ensure_repo_clone(
    pool_root: str | Path,
    repo_root: str | Path,
    slot_id: str,
    *,
    remote: str = "origin",
    allowed_roots: tuple[str, ...] = (),
) -> Path:
    """Return the standalone clone for `slot_id` in `repo_root`'s pool,
    cloning it if absent. An existing path is reused only after it passes
    both the standalone-clone shape check and fail-closed remote
    verification."""
    repo_root_path = Path(repo_root).expanduser().resolve()
    expected_url = expected_remote_url(repo_root_path, remote=remote)
    pool_dir = Path(pool_root).expanduser().resolve() / repo_identity(repo_root_path, remote=remote)
    clone_path = pool_dir / slot_id

    if allowed_roots and not any(_is_under(clone_path, root) for root in allowed_roots):
        raise WorktreeValidationError(
            f"clone pool path is outside allowed workspace roots: {clone_path}"
        )

    if clone_path.exists():
        if not is_standalone_clone(clone_path):
            raise WorktreeValidationError(
                f"pool slot {clone_path} exists but is not a standalone git clone; "
                "refusing to reuse a linked worktree or non-git directory as a pool slot"
            )
        verify_clone_remote(clone_path, expected_url, remote=remote)
        return clone_path

    pool_dir.mkdir(parents=True, exist_ok=True)
    result = _git(pool_dir, "clone", "--origin", remote, expected_url, str(clone_path))
    if result.returncode != 0:
        raise WorktreeValidationError(
            f"failed to provision standalone clone at {clone_path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return clone_path


def sync_task_branch(
    clone_path: str | Path,
    *,
    branch_name: str,
    base_ref: str,
    remote: str = "origin",
    resume: bool = False,
) -> Path:
    """Bring a pooled clone onto `branch_name`, ready for a task to run.

    Always fetches first: a pool slot is reused across tasks, so any local
    branch state is not authoritative. A resumed task prefers the existing
    *remote* task branch (the durable record of a prior worker's progress)
    over a same-named local branch, since a different worker -- with no
    local history of that branch -- may have picked up this slot.
    """
    clone_path = Path(clone_path)
    fetched = _git(clone_path, "fetch", remote, "--prune")
    if fetched.returncode != 0:
        raise WorktreeValidationError(
            f"failed to fetch '{remote}' into clone at {clone_path}: "
            f"{fetched.stderr.strip() or fetched.stdout.strip()}"
        )

    _preserve(clone_path, branch_name, resume_branch=branch_name if resume else None)

    remote_branch = f"{remote}/{branch_name}"
    if resume and _ref_exists(clone_path, remote_branch):
        command = ["checkout", "-B", branch_name, remote_branch]
    elif resume and _ref_exists(clone_path, branch_name):
        command = ["checkout", branch_name]
    else:
        base = f"{remote}/{base_ref}" if _ref_exists(clone_path, f"{remote}/{base_ref}") else base_ref
        if not _ref_exists(clone_path, base):
            base = "HEAD"
        command = ["checkout", "-B", branch_name, base]

    result = _git(clone_path, *command)
    if result.returncode != 0:
        raise WorktreeValidationError(
            f"could not prepare clone {clone_path} for branch {branch_name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return clone_path
