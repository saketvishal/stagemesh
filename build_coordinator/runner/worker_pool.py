"""Repository-scoped standalone worker clone pools.

Replaces the old shared, cross-repository `worktrees: {worker_id: path}`
map with a per-repository pool of full standalone Git clones, laid out as:

    <pool_root>/<repo>/worker-1/
    <pool_root>/<repo>/worker-2/
    ...

Each slot is a *full clone* (its own `.git` directory), never a linked Git
worktree -- linked worktrees share one `.git` across sibling checkouts,
which is exactly the Codex-on-Windows sandbox `index.lock` contention this
design avoids.

Repository selection, clone creation, and slot allocation are all
automatic: the caller names a repo and a busy-set, this module figures out
which slot (if any) is free, creates the clone on first use, and fails
closed if an existing directory at that slot turns out to be a clone of
the wrong repository -- it never silently repurposes a mismatched folder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from build_coordinator.github.sanitizer import full_repo_slug

DEFAULT_POOL_ROOT = Path("C:/caventra-workers")
DEFAULT_MAX_CLONES_PER_REPO = 2


class WorkerPoolError(RuntimeError):
    """Raised when a repo-scoped clone can't be allocated or verified."""


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL for comparison: strip trailing `.git`,
    a trailing slash, and lowercase it. Handles both
    `https://github.com/owner/repo(.git)` and `git@github.com:owner/repo(.git)`
    forms by reducing each to `<host>/<owner>/<repo>`."""
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    cleaned = cleaned.replace("git@github.com:", "github.com/")
    cleaned = cleaned.replace("https://github.com/", "github.com/")
    cleaned = cleaned.replace("http://github.com/", "github.com/")
    return cleaned.lower()


def remote_matches_repo(remote_url: str, repo: str) -> bool:
    """True if `remote_url` points at the authorized repo (owner/name)."""
    expected = full_repo_slug(repo)
    return _normalize_remote_url(remote_url) == _normalize_remote_url(
        f"github.com/{expected}"
    )


def repo_clone_url(repo: str) -> str:
    return f"https://github.com/{full_repo_slug(repo)}.git"


def repo_pool_dir(repo: str, *, root: Path) -> Path:
    return root / repo


def clone_slot_path(repo: str, slot: int, *, root: Path) -> Path:
    return repo_pool_dir(repo, root=root) / f"worker-{slot}"


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_clone(repo: str, path: Path) -> Path:
    """Ensure `path` is a full standalone clone of `repo`'s authorized
    remote, creating it if it doesn't exist yet. Fails closed: if `path`
    already exists but is not a git clone, or is a clone of a different
    repository, this raises rather than reusing or overwriting it."""
    expected_url = repo_clone_url(repo)
    if path.exists():
        if not (path / ".git").is_dir():
            raise WorkerPoolError(
                f"{path} exists and is not a standalone git clone -- refusing to "
                "reuse it as a worker slot"
            )
        remote = _run_git(["remote", "get-url", "origin"], cwd=path)
        if remote.returncode != 0:
            raise WorkerPoolError(
                f"{path} exists but has no readable 'origin' remote -- refusing "
                f"to reuse it: {remote.stderr.strip()}"
            )
        if not remote_matches_repo(remote.stdout.strip(), repo):
            raise WorkerPoolError(
                f"{path} is a clone of {remote.stdout.strip()!r}, expected a "
                f"clone of {expected_url!r} -- refusing to reuse a mismatched "
                "repository clone as a worker slot"
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", expected_url, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkerPoolError(
            f"failed to clone {expected_url} into {path}: {result.stderr.strip()}"
        )
    return path


def allocate_worker_clone(
    repo: str,
    *,
    root: Path,
    max_clones: int,
    busy_paths: set[str],
) -> Path:
    """Return a free clone path for `repo`, creating one (up to
    `max_clones` slots) if every existing slot is either busy or missing.
    Raises `WorkerPoolError` if all slots up to `max_clones` are busy."""
    for slot in range(1, max_clones + 1):
        path = clone_slot_path(repo, slot, root=root)
        if str(path) in busy_paths:
            continue
        return ensure_clone(repo, path)
    raise WorkerPoolError(
        f"no free worker clone available for {repo!r}: all {max_clones} pool "
        "slot(s) are currently active"
    )
