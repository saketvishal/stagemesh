from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .attribution import GitAttribution


class GitError(RuntimeError):
    pass


class GitWorkspace:
    def __init__(self, path: Path):
        self.path = Path(path)

    def run(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            text=True,
            capture_output=True,
            check=False,
            env=merged_env,
        )
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
        return self.run("rev-parse", "HEAD").stdout.strip()

    def head_or_synthetic(self) -> str:
        result = self.run("rev-parse", "HEAD", check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        digest = hashlib.sha1(str(self.path).encode("utf-8")).hexdigest()
        return f"synthetic-{digest}"

    def create_worktree(self, target: Path, ref: str = "HEAD") -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", str(target), ref)
