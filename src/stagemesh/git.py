from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from .attribution import GitAttribution
from .git_identity import identity_env_for, sanitize_commit_message


_TRANSIENT_RETRIES = 4

# Subcommands that create commits (and so need an author/committer). Their identity is supplied per command through the environment
# by the project identity policy (see git_identity); StageMesh never writes user.name / user.email into any git config.
_HISTORY_WRITING = frozenset({"commit", "rebase", "merge", "cherry-pick", "revert", "am", "stash", "pull", "tag"})


def _subcommand(args: tuple[str, ...]) -> str | None:
    """The git subcommand in `args`, skipping global options such as `-c key=value` and `-C path`."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in {"-c", "-C", "--git-dir", "--work-tree"}:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def _sets_identity_inline(args: tuple[str, ...]) -> bool:
    return any(a == "-c" and i + 1 < len(args) and args[i + 1].startswith(("user.name=", "user.email=")) for i, a in enumerate(args))


def _transient_file_error(stderr: str) -> bool:
    """Windows briefly denies access to .git files another git process (or a scanner) has open; the same command succeeds a moment later."""
    return "permission denied" in stderr.lower()


class GitError(RuntimeError):
    pass


class GitValidationError(ValueError):
    pass


def _validate_non_empty_string(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise GitValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise GitValidationError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise GitValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


class GitWorkspace:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def run(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None, encoding: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        if not args:
            raise GitValidationError("git command must include at least one argument")
        validated_args = tuple(
            _validate_non_empty_string(arg, "git command argument", 1000)
            for arg in args
        )
        if env:
            for key, value in env.items():
                _validate_non_empty_string(key, "git environment key", 200)
                _validate_non_empty_string(value, f"git environment value for {key}", 1000)
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        if _subcommand(validated_args) in _HISTORY_WRITING and not _sets_identity_inline(validated_args):
            merged_env.update(identity_env_for(self.path, merged_env))  # fills only what is unset; raises on a placeholder identity
        for attempt in range(_TRANSIENT_RETRIES + 1):
            result = subprocess.run(
                ["git", *validated_args],
                cwd=self.path,
                text=True,
                encoding=encoding,  # None keeps the process locale; callers that need exact paths pass "utf-8" (git writes UTF-8)
                capture_output=True,
                check=False,
                env=merged_env,
            )
            if result.returncode == 0 or attempt == _TRANSIENT_RETRIES or not _transient_file_error(result.stderr):
                break
            time.sleep(0.1 * (attempt + 1))
        if check and result.returncode != 0:
            raise GitError(result.stderr.strip() or result.stdout.strip())
        return result

    def init_if_needed(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        if not (self.path / ".git").exists():
            self.run("init")  # no identity is written: commits resolve it per command (git_identity)

    def commit_all(self, message: str, attribution: GitAttribution | None = None) -> str:
        """Commit everything as the project identity (the repository owner), never as the provider that produced the change.

        `attribution` names the producing worker for StageMesh's own execution metadata; it is deliberately not turned into a commit
        author, because a fabricated author or Co-authored-by shows up on GitHub as a contributor.
        """
        message = sanitize_commit_message(_validate_non_empty_string(message, "commit message", 200))
        self.run("add", "-A")
        diff = self.run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            return self.head_or_synthetic()
        self.run("commit", "-m", message)
        return self.head()

    def head(self) -> str:
        sha = self.run("rev-parse", "HEAD").stdout.strip()
        if not sha:
            raise GitError("git returned an empty HEAD")
        return sha

    def head_or_synthetic(self) -> str:
        result = self.run("rev-parse", "HEAD", check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        digest = hashlib.sha1(str(self.path).encode("utf-8")).hexdigest()
        return f"synthetic-{digest}"

    def create_worktree(self, target: Path, ref: str = "HEAD") -> None:
        ref = _validate_non_empty_string(ref, "worktree ref", 200)
        target = Path(target).resolve()
        if target == self.path:
            raise GitValidationError("worktree target must differ from workspace path")
        if target in self.path.parents:
            raise GitValidationError("worktree target must not contain workspace path")
        if self.path in target.parents:
            raise GitValidationError("worktree target must not be inside workspace path")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", str(target), ref)
