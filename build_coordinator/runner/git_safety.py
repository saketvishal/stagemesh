"""Runner-owned mechanical Git compatibility checks.

Mechanical compatibility is deterministic infrastructure. Semantic
compatibility remains a reviewer/integrator/human judgment. Commands are
executed as argument arrays with shell=False.
"""

from __future__ import annotations

import fnmatch
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class GitSafetyError(RuntimeError):
    """Raised when a required Git fact cannot be determined safely."""


@dataclass(frozen=True)
class MechanicalMergeAssessment:
    current_main_sha: str
    feature_remote_sha: str
    merge_base: str
    reviewed_sha_matches: bool
    conflict: bool
    conflict_paths: tuple[str, ...] = ()
    remote_name: str = "origin"
    main_ref: str = "main"
    feature_ref: str | None = None


class GitBackend(Protocol):
    def fetch_prune(self, cwd: str | Path, remote: str = "origin") -> None: ...

    def rev_parse(self, cwd: str | Path, ref: str) -> str: ...

    def merge_base(self, cwd: str | Path, left: str, right: str) -> str: ...

    def merge_tree_conflicts(
        self, cwd: str | Path, ours: str, theirs: str
    ) -> tuple[str, ...]: ...

    def ls_files(self, cwd: str | Path, *patterns: str) -> tuple[str, ...]: ...


class RealGit:
    """Subprocess Git backend. Never uses shell interpolation."""

    def fetch_prune(self, cwd: str | Path, remote: str = "origin") -> None:
        # Sibling worktrees share one ref store; concurrent fetches can lose a
        # ref-lock race although nothing is wrong. Retry only that case.
        for attempt in range(_FETCH_ATTEMPTS):
            result = _git(cwd, "fetch", remote, "--prune")
            if result.returncode == 0:
                return
            detail = (result.stderr or "") + (result.stdout or "")
            if attempt + 1 < _FETCH_ATTEMPTS and _is_ref_lock_contention(detail):
                time.sleep(0.2 * (attempt + 1))
                continue
            break
        raise GitSafetyError(_git_error("fetch --prune", result))

    def rev_parse(self, cwd: str | Path, ref: str) -> str:
        result = _git(cwd, "rev-parse", ref)
        if result.returncode != 0:
            raise GitSafetyError(_git_error(f"rev-parse {ref}", result))
        sha = result.stdout.strip()
        if not sha:
            raise GitSafetyError(f"git rev-parse {ref} returned an empty SHA")
        return sha

    def merge_base(self, cwd: str | Path, left: str, right: str) -> str:
        result = _git(cwd, "merge-base", left, right)
        if result.returncode != 0:
            raise GitSafetyError(_git_error("merge-base", result))
        sha = result.stdout.strip()
        if not sha:
            raise GitSafetyError("git merge-base returned an empty SHA")
        return sha

    def merge_tree_conflicts(
        self, cwd: str | Path, ours: str, theirs: str
    ) -> tuple[str, ...]:
        modern = _git(cwd, "merge-tree", "--write-tree", "--name-only", ours, theirs)
        if modern.returncode == 0:
            return ()
        if modern.returncode == 1:
            paths = tuple(
                line.strip()
                for line in modern.stdout.splitlines()
                if line.strip() and not line.startswith("_")
            )
            return paths or ("conflict",)
        if "unknown option" not in (modern.stderr or "").lower() and modern.returncode not in {129}:
            # Unexpected failure of modern merge-tree; try legacy before giving up.
            if "not a valid" in (modern.stderr or "").lower():
                raise GitSafetyError(_git_error("merge-tree --write-tree", modern))
        base = self.merge_base(cwd, ours, theirs)
        legacy = _git(cwd, "merge-tree", base, ours, theirs)
        if legacy.returncode not in {0, 1}:
            raise GitSafetyError(_git_error("merge-tree", legacy))
        return _parse_legacy_merge_tree_conflicts(legacy.stdout)

    def ls_files(self, cwd: str | Path, *patterns: str) -> tuple[str, ...]:
        args = ["ls-files", "-z", "--"]
        args.extend(patterns or [])
        result = _git(cwd, *args)
        if result.returncode != 0:
            raise GitSafetyError(_git_error("ls-files", result))
        return tuple(item for item in result.stdout.split("\0") if item)


@dataclass
class FakeGit:
    feature_sha: str = "feature-sha"
    remote_feature_sha: str | None = None
    main_sha: str = "main-sha"
    merge_base_sha: str = "merge-base"
    conflict_paths: tuple[str, ...] = ()
    tracked_files: tuple[str, ...] = ()
    fetch_calls: int = 0
    refs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.remote_feature_sha is None:
            self.remote_feature_sha = self.feature_sha
        self.refs.setdefault("HEAD", self.feature_sha)
        self.refs.setdefault("origin/main", self.main_sha)
        self.refs.setdefault("main", self.main_sha)

    def fetch_prune(self, cwd: str | Path, remote: str = "origin") -> None:
        self.fetch_calls += 1

    def rev_parse(self, cwd: str | Path, ref: str) -> str:
        if ref in self.refs:
            return self.refs[ref]
        if ref.startswith("origin/") and ref != "origin/main":
            return self.remote_feature_sha or self.feature_sha
        if ref == "HEAD":
            return self.feature_sha
        raise GitSafetyError(f"unknown ref: {ref}")

    def merge_base(self, cwd: str | Path, left: str, right: str) -> str:
        return self.merge_base_sha

    def merge_tree_conflicts(
        self, cwd: str | Path, ours: str, theirs: str
    ) -> tuple[str, ...]:
        return self.conflict_paths

    def ls_files(self, cwd: str | Path, *patterns: str) -> tuple[str, ...]:
        if not patterns:
            return self.tracked_files
        matched: list[str] = []
        for path in self.tracked_files:
            normalized = path.replace("\\", "/")
            for pattern in patterns:
                if fnmatch.fnmatch(normalized, pattern.replace("\\", "/")):
                    matched.append(path)
                    break
        return tuple(matched)


def assess_mechanical_merge(
    git: GitBackend,
    *,
    cwd: str | Path,
    branch_name: str,
    reviewed_feature_sha: str,
    remote: str | None = "origin",
    main_ref: str = "main",
) -> MechanicalMergeAssessment:
    if remote:
        git.fetch_prune(cwd, remote)
    current_main_sha = git.rev_parse(cwd, f"{remote}/{main_ref}" if remote else main_ref)
    feature_ref = f"{remote}/{branch_name}" if remote else branch_name
    feature_remote_sha = git.rev_parse(cwd, feature_ref)
    merge_base = git.merge_base(cwd, current_main_sha, feature_remote_sha)
    reviewed_sha_matches = feature_remote_sha == reviewed_feature_sha
    conflict_paths: tuple[str, ...] = ()
    if reviewed_sha_matches:
        conflict_paths = git.merge_tree_conflicts(
            cwd, current_main_sha, feature_remote_sha
        )
    return MechanicalMergeAssessment(
        current_main_sha=current_main_sha,
        feature_remote_sha=feature_remote_sha,
        merge_base=merge_base,
        reviewed_sha_matches=reviewed_sha_matches,
        conflict=bool(conflict_paths),
        conflict_paths=conflict_paths,
        remote_name=remote or "",
        main_ref=main_ref,
        feature_ref=feature_ref,
    )


def capture_feature_sha(
    git: GitBackend,
    *,
    cwd: str | Path,
    branch_name: str | None,
    remote: str | None = "origin",
) -> str:
    if remote:
        git.fetch_prune(cwd, remote)
    if branch_name:
        return git.rev_parse(cwd, f"{remote}/{branch_name}" if remote else branch_name)
    return git.rev_parse(cwd, "HEAD")


_FETCH_ATTEMPTS = 5


def _is_ref_lock_contention(detail: str) -> bool:
    lowered = detail.lower()
    return "cannot lock ref" in lowered or ("unable to" in lowered and ".lock" in lowered)


def _git(cwd: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )


def _git_error(action: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    return f"git {action} failed ({result.returncode}): {detail}"


def _parse_legacy_merge_tree_conflicts(output: str) -> tuple[str, ...]:
    conflicts: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("changed in both") or stripped.startswith("CONFLICT"):
            parts = stripped.split()
            current = parts[-1] if parts else stripped
            if current and current not in conflicts:
                conflicts.append(current)
        elif "<<<<<<<" in line and current and current not in conflicts:
            conflicts.append(current)
    return tuple(conflicts)
