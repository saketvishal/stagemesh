"""Read-mostly git facts for the supervisor, plus the only two writes it needs: create-only refs and worktree-free transplants.

Nothing here can move, delete or rewrite an existing ref. `create_ref` refuses to overwrite; `transplant_commit` builds a new
commit object from a three-way merge of trees (`git merge-tree --write-tree`) without touching any checkout.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..git import GitError, GitWorkspace

STAGEMESH_COMMITTER = ("StageMesh", "stagemesh@stagemesh.invalid")
# No committer identity is trusted by default: the worktree's own configured identity is self-declared, so a second writer committing
# there would pass for StageMesh. StageMesh's own commits are *registered* (Supervisor.candidate_committed); a provider that commits
# by itself is trusted only through an explicit `trusted_committer_emails` entry.
TRUSTED_COMMITTER_EMAILS: tuple[str, ...] = ()
NULL_SHA = "0" * 40
_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitTooOldError(GitError):
    """`git merge-tree --write-tree` (git >= 2.38) is required for worktree-free three-way merges."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parents: tuple[str, ...]
    tree: str
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    message: str


@dataclass(frozen=True)
class TransplantResult:
    clean: bool
    tree: str | None = None
    conflicts: tuple[str, ...] = ()


class GitFacts:
    def __init__(self, path: Path):
        self.git = GitWorkspace(Path(path))
        self.path = self.git.path

    # --- reads ---------------------------------------------------------------------------------------------------------------

    def resolve(self, ref: str) -> str | None:
        result = self.git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and _SHA.match(value) else None

    def exists(self, sha: str) -> bool:
        return self.git.run("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def tree(self, rev: str) -> str | None:
        result = self.git.run("rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}", check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.git.run("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

    def merge_base(self, left: str, right: str) -> str | None:
        result = self.git.run("merge-base", left, right, check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def commits_between(self, base: str, tip: str) -> list[str]:
        """Commits reachable from `tip` but not `base`, oldest first (topological)."""
        out = self.git.run("rev-list", "--reverse", "--topo-order", f"{base}..{tip}").stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    def commit(self, sha: str) -> CommitInfo:
        fmt = "%H%x1f%P%x1f%T%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%B"
        out = self.git.run("show", "-s", f"--format={fmt}", sha).stdout.rstrip("\n")
        fields = out.split("\x1f", 8)
        if len(fields) != 9:
            raise GitError(f"unexpected commit format for {sha}")
        return CommitInfo(
            sha=fields[0],
            parents=tuple(fields[1].split()),
            tree=fields[2],
            author_name=fields[3],
            author_email=fields[4],
            author_date=fields[5],
            committer_name=fields[6],
            committer_email=fields[7],
            message=fields[8],
        )

    def changed_paths(self, base: str, tip: str) -> list[str]:
        out = self.git.run("diff", "--name-only", "-M", base, tip).stdout
        return sorted(line.replace("\\", "/") for line in out.splitlines() if line.strip())

    def patch_id(self, base: str, tip: str) -> str | None:
        """Stable patch-id of the diff `base..tip`; equal ids mean the same change regardless of surrounding history."""
        diff = self.git.run("diff", base, tip).stdout
        if not diff.strip():
            return None
        result = subprocess.run(
            ["git", "patch-id", "--stable"], cwd=self.path, input=diff, text=True, capture_output=True, check=False
        )
        parts = result.stdout.split()
        return parts[0] if result.returncode == 0 and parts else None

    def ref_exists(self, ref: str) -> bool:
        return self.git.run("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0

    def blob_at(self, rev: str, path: str) -> str | None:
        result = self.git.run("rev-parse", "--verify", "--quiet", f"{rev}:{path}", check=False)
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    # --- writes that cannot destroy anything -----------------------------------------------------------------------------------

    def create_ref(self, ref: str, sha: str) -> None:
        """Create `ref` -> `sha`; fails if the ref already exists (compare-and-swap against the null SHA)."""
        if not ref.startswith("refs/"):
            raise GitError(f"refusing to create non-namespaced ref {ref!r}")
        self.git.run("update-ref", ref, sha, NULL_SHA)

    def ensure_ref(self, ref: str, sha: str) -> None:
        """Create the ref, or accept it if it already points at exactly `sha`; never moves an existing ref."""
        if self.ref_exists(ref):
            current = self.resolve(ref)
            if current != sha:
                raise GitError(f"{ref} already exists at {current}, refusing to move it to {sha}")
            return
        self.create_ref(ref, sha)

    def transplant(self, onto: str, commit: str) -> TransplantResult:
        """Three-way merge of `commit`'s change (against its first parent) onto `onto`, without any checkout."""
        info = self.commit(commit)
        if len(info.parents) != 1:
            return TransplantResult(False, conflicts=("<non-linear commit: root or merge commit>",))
        result = self.git.run(
            "merge-tree", "--write-tree", "--no-messages", f"--merge-base={info.parents[0]}", onto, commit, check=False
        )
        if result.returncode == 129 or "unknown option" in result.stderr.lower():
            raise GitTooOldError("git >= 2.38 is required for `git merge-tree --write-tree`")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if result.returncode == 0 and lines and _SHA.match(lines[0]):
            return TransplantResult(True, tree=lines[0])
        if result.returncode == 1:
            conflicted = tuple(sorted({line.split("\t", 1)[-1] for line in lines[1:] if "\t" in line}))
            return TransplantResult(False, conflicts=conflicted or ("<unspecified conflict>",))
        raise GitError(result.stderr.strip() or result.stdout.strip() or "git merge-tree failed")

    def commit_tree(
        self,
        tree: str,
        parent: str,
        message: str,
        *,
        author: CommitInfo | None = None,
        committer: tuple[str, str] = STAGEMESH_COMMITTER,
    ) -> str:
        env = {"GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
        if author is not None:
            env.update(
                GIT_AUTHOR_NAME=author.author_name, GIT_AUTHOR_EMAIL=author.author_email, GIT_AUTHOR_DATE=author.author_date
            )
        else:
            env.update(GIT_AUTHOR_NAME=committer[0], GIT_AUTHOR_EMAIL=committer[1])
        return self._commit_tree_with_message(["commit-tree", tree, "-p", parent, "-F", "-"], message, env)

    def _commit_tree_with_message(self, args: list[str], message: str, env: dict[str, str]) -> str:
        """Run commit-tree with the message on stdin (GitWorkspace.run caps argument length, which would truncate messages)."""
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            input=message if message.endswith("\n") else message + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            env={**os.environ, **env},
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git commit-tree failed")
        return result.stdout.strip()

    def snapshot_worktree(self, worktree: Path, message: str) -> str | None:
        """Commit-object snapshot of everything in `worktree` (tracked changes and untracked files) without touching it.

        Used to preserve the exact state a second writer left, or uncommitted work, before any quarantine or reset. The worktree's
        real index is untouched: a temporary index file is used.
        """
        work = GitWorkspace(Path(worktree))
        head = work.run("rev-parse", "--verify", "--quiet", "HEAD", check=False).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="stagemesh-snapshot-") as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            if head:
                work.run("read-tree", "HEAD", env=env)
            work.run("add", "-A", env=env)
            tree = work.run("write-tree", env=env).stdout.strip()
        if head and tree == (work.run("rev-parse", "HEAD^{tree}").stdout.strip()):
            return head  # nothing beyond HEAD to preserve
        args = ["commit-tree", tree, "-F", "-"]
        if head:
            args += ["-p", head]
        env = {
            "GIT_AUTHOR_NAME": STAGEMESH_COMMITTER[0],
            "GIT_AUTHOR_EMAIL": STAGEMESH_COMMITTER[1],
            "GIT_COMMITTER_NAME": STAGEMESH_COMMITTER[0],
            "GIT_COMMITTER_EMAIL": STAGEMESH_COMMITTER[1],
        }
        return GitFacts(worktree)._commit_tree_with_message(args, message, env)


def same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))
