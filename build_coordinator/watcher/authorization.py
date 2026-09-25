"""Repository authorization for the persistent watcher.

Fail-closed boundary (SDD-001 section 4.4): no GitHub mutation, label
provisioning, remote push, or unattended execution loop may run against a
repository unless it is both explicitly configured in the operator's
coordinator config AND proven, at validation time, to be a real git
worktree whose remote actually resolves to the configured slug. There is no
implicit trust in the caller's current working directory.

This builds on top of `tooling.build_coordinator.github.sanitizer`'s
existing global allowlist (`AUTHORIZED_REPOSITORIES` / `AUTHORIZED_OWNERS`)
rather than replacing it: a repository must pass both checks. The sanitizer
allowlist answers "is this repository ever allowed to participate in
Caventra automation"; this module answers "is this specific operator
config entry, pointing at this specific path, safe to run an unattended
loop against right now".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from build_coordinator.coordinator_config import (
    CoordinatorConfigError,
    config_file_path,
)
from build_coordinator.github.sanitizer import SecurityBoundaryError, validate_repository_name
from build_coordinator.runner.worker_pool import remote_matches_repo


class RepositoryAuthorizationError(ValueError):
    """Raised when a repository fails authorization validation. Callers
    must treat this as fail-closed: no GitHub mutation or unattended
    execution loop may proceed for the offending repository."""


@dataclass(frozen=True)
class AuthorizedRepository:
    slug: str  # "owner/repo", already validated against the global allowlist
    control_repo_root: Path
    default_branch: str = "main"
    remote: str = "origin"
    labels: bool = True


def _config_json() -> dict:
    path = config_file_path()
    if path is None:
        return {}
    import json

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CoordinatorConfigError(f"could not read coordinator config {path}: {exc}") from exc
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CoordinatorConfigError(f"coordinator config {path} is not valid JSON: {exc}") from exc


def load_authorized_repositories() -> tuple[AuthorizedRepository, ...]:
    """Load the configured `authorized_repositories` allowlist from the
    coordinator config file. Does not validate paths or remotes -- call
    `validate_repository` (or `authorize`) before using an entry for any
    GitHub mutation or unattended execution loop. A slug that fails the
    global sanitizer allowlist is dropped rather than raised here, since an
    operator config listing an unauthorized repo should not crash every
    other configured repo's resolution."""
    data = _config_json()
    entries = data.get("authorized_repositories") or []
    path = config_file_path()
    base_dir = path.parent if path is not None else Path.home()

    repos: list[AuthorizedRepository] = []
    for entry in entries:
        raw_slug = str(entry.get("slug", "")).strip()
        root_raw = entry.get("control_repo_root")
        if not raw_slug or not root_raw:
            continue
        try:
            from build_coordinator.github.sanitizer import full_repo_slug

            slug = full_repo_slug(raw_slug)
        except SecurityBoundaryError:
            continue
        root = Path(str(root_raw)).expanduser()
        if not root.is_absolute():
            root = (base_dir / root).resolve()
        repos.append(
            AuthorizedRepository(
                slug=slug,
                control_repo_root=root,
                default_branch=str(entry.get("default_branch", "main")),
                remote=str(entry.get("remote", "origin")),
                labels=bool(entry.get("labels", True)),
            )
        )
    return tuple(repos)


def _git_remote_url(repo_root: Path, remote: str) -> str:
    safe_directory = repo_root.resolve().as_posix()
    result = subprocess.run(
        ["git", "-c", f"safe.directory={safe_directory}", "-C", str(repo_root), "remote", "get-url", remote],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RepositoryAuthorizationError(
            f"{repo_root} has no readable {remote!r} remote: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def validate_repository(repo: AuthorizedRepository) -> None:
    """Validate that `repo` is safe to use for GitHub mutation or an
    unattended execution loop. Fails closed with
    `RepositoryAuthorizationError` -- never returns a partial pass."""
    try:
        validate_repository_name(repo.slug)
    except SecurityBoundaryError as exc:
        raise RepositoryAuthorizationError(str(exc)) from exc
    if not repo.control_repo_root.is_dir():
        raise RepositoryAuthorizationError(
            f"authorized repository {repo.slug!r}: control_repo_root "
            f"{repo.control_repo_root} does not exist"
        )
    if not (repo.control_repo_root / ".git").exists():
        raise RepositoryAuthorizationError(
            f"authorized repository {repo.slug!r}: {repo.control_repo_root} "
            "is not a git worktree"
        )
    remote_url = _git_remote_url(repo.control_repo_root, repo.remote)
    if not remote_matches_repo(remote_url, repo.slug.split("/", 1)[-1]):
        raise RepositoryAuthorizationError(
            f"authorized repository {repo.slug!r}: remote {repo.remote!r} "
            f"({remote_url!r}) does not resolve to the configured slug"
        )


def authorize(slug: str) -> AuthorizedRepository:
    """Resolve and validate the authorized repository for `slug` (either
    `owner/repo` or a bare repo name). Fails closed if the slug is not
    configured in `authorized_repositories` or fails validation."""
    try:
        normalized = validate_repository_name(slug)
        from build_coordinator.github.sanitizer import full_repo_slug

        full_slug = full_repo_slug(normalized)
    except SecurityBoundaryError as exc:
        raise RepositoryAuthorizationError(str(exc)) from exc
    for repo in load_authorized_repositories():
        if repo.slug == full_slug:
            validate_repository(repo)
            return repo
    raise RepositoryAuthorizationError(
        f"repository {full_slug!r} is not in the operator's authorized_repositories config"
    )
