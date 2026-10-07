from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from .attribution import GitAttribution


_TRANSIENT_RETRIES = 4


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
            self.run("init")
            self.run("config", "user.email", "stagemesh@example.invalid")
            self.run("config", "user.name", "StageMesh")

    def commit_all(self, message: str, attribution: GitAttribution | None = None) -> str:
        message = _validate_non_empty_string(message, "commit message", 200)
        self.run("add", "-A")
        diff = self.run("diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            return self.head_or_synthetic()
        env = None
        if attribution:
            env = {
                "GIT_AUTHOR_NAME": attribution.author_name,
                "GIT_AUTHOR_EMAIL": attribution.author_email,
                "GIT_COMMITTER_NAME": attribution.committer_name,
                "GIT_COMMITTER_EMAIL": attribution.committer_email,
            }
        self.run("commit", "-m", message, env=env)
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
