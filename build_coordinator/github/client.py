"""GitHub API client adapter.

Uses the authenticated `gh` CLI for all operations (zero-secret, keyring-backed).
Also provides clean interfaces that can be mocked or redirected to direct HTTP/REST.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from build_coordinator.policy import CoordinatorPolicyError

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "QUEUED": "status:QUEUED",
    "PLANNING": "status:PLANNING",
    "IN_PROGRESS": "status:IN_PROGRESS",
    "REVIEWING": "status:REVIEWING",
    "REMEDIATING": "status:REMEDIATING",
    "READY_FOR_INTEGRATION": "status:READY_FOR_INTEGRATION",
    "HUMAN_GATE": "status:HUMAN_GATE",
    "DONE": "status:DONE",
    "BLOCKED": "status:BLOCKED",
    "FAILED": "status:FAILED",
}

OBJECTIVE_LABEL = "caventra:objective"

REQUIRED_ORCHESTRATION_LABELS = (OBJECTIVE_LABEL, *STATUS_LABELS.values())


class GitHubClientError(CoordinatorPolicyError):
    """Raised on GitHub API or CLI failure."""


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    author: str
    state: str
    html_url: str


@dataclass(frozen=True)
class GitHubComment:
    id: str | int
    author: str
    body: str
    created_at: str


class GitHubClient:
    """Client for GitHub issues, PRs, and comments via `gh` CLI."""

    def __init__(self, *, gh_path: str = "gh") -> None:
        self._gh_path = gh_path

    def _run_gh(self, args: list[str], *, cwd: str | Path | None = None) -> str:
        cmd = [self._gh_path] + args
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                cwd=str(cwd) if cwd else None,
                shell=False,
            )
        except OSError as exc:
            raise GitHubClientError(f"failed to run gh command {cmd}: {exc}") from exc

        if completed.returncode != 0:
            err = completed.stderr.strip() or completed.stdout.strip()
            raise GitHubClientError(f"gh command {' '.join(cmd)} failed (code {completed.returncode}): {err}")
        return completed.stdout

    def get_authorized_issues(
        self,
        repo: str,
        *,
        label: str = "caventra:objective",
        state: str = "open",
    ) -> list[GitHubIssue]:
        """Fetch all open issues in repo having the authorized objective label."""
        fields = "number,title,body,labels,author,state,url"
        stdout = self._run_gh(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                state,
                "--label",
                label,
                "--json",
                fields,
            ]
        )
        try:
            raw_issues = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(f"invalid json from gh issue list: {exc}") from exc

        issues: list[GitHubIssue] = []
        for raw in raw_issues:
            label_names = tuple(l.get("name", "") for l in raw.get("labels", []))
            author_info = raw.get("author", {}) or {}
            issues.append(
                GitHubIssue(
                    number=int(raw["number"]),
                    title=str(raw.get("title", "")),
                    body=str(raw.get("body", "") or ""),
                    labels=label_names,
                    author=str(author_info.get("login", "")),
                    state=str(raw.get("state", "OPEN")),
                    html_url=str(raw.get("url", "")),
                )
            )
        return issues

    def ensure_orchestration_labels(self, repo: str) -> None:
        """Ensure Caventra-owned orchestration labels exist before syncing state."""
        stdout = self._run_gh(["label", "list", "--repo", repo, "--json", "name"])
        try:
            raw_labels = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubClientError(f"invalid json from gh label list: {exc}") from exc

        existing = {str(label.get("name", "")) for label in raw_labels}
        for label in REQUIRED_ORCHESTRATION_LABELS:
            if label in existing:
                continue
            self._run_gh(["label", "create", label, "--repo", repo])

    def get_issue(self, repo: str, issue_number: int) -> GitHubIssue:
        """Fetch a single issue by number."""
        fields = "number,title,body,labels,author,state,url"
        stdout = self._run_gh(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                fields,
            ]
        )
        raw = json.loads(stdout)
        label_names = tuple(l.get("name", "") for l in raw.get("labels", []))
        author_info = raw.get("author", {}) or {}
        return GitHubIssue(
            number=int(raw["number"]),
            title=str(raw.get("title", "")),
            body=str(raw.get("body", "") or ""),
            labels=label_names,
            author=str(author_info.get("login", "")),
            state=str(raw.get("state", "OPEN")),
            html_url=str(raw.get("url", "")),
        )

    def get_issue_comments(self, repo: str, issue_number: int) -> list[GitHubComment]:
        """Fetch all comments for an issue."""
        stdout = self._run_gh(
            [
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "comments",
            ]
        )
        raw = json.loads(stdout)
        comments: list[GitHubComment] = []
        for c in raw.get("comments", []):
            author_info = c.get("author", {}) or {}
            comments.append(
                GitHubComment(
                    id=str(c.get("id", "") or c.get("databaseId", "")),
                    author=str(author_info.get("login", "")),
                    body=str(c.get("body", "")),
                    created_at=str(c.get("createdAt", "")),
                )
            )
        return comments

    def add_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        """Post a comment to an issue."""
        self._run_gh(
            [
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo,
                "--body",
                body,
            ]
        )

    def set_issue_status_label(self, repo: str, issue_number: int, status: str) -> None:
        """Update the issue status label, removing previous status labels."""
        target_label = STATUS_LABELS.get(status)
        if not target_label:
            return

        current_issue = self.get_issue(repo, issue_number)
        to_remove = [lbl for lbl in current_issue.labels if lbl.startswith("status:") and lbl != target_label]

        args = ["issue", "edit", str(issue_number), "--repo", repo, "--add-label", target_label]
        for rem in to_remove:
            args.extend(["--remove-label", rem])
        self._run_gh(args)

    def get_pull_request(self, repo: str, head_branch: str) -> dict[str, Any] | None:
        """Find an existing PR for a head branch."""
        stdout = self._run_gh(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                head_branch,
                "--json",
                "number,url,state,title",
            ]
        )
        prs = json.loads(stdout)
        if prs and len(prs) > 0:
            return prs[0]
        return None

    def create_pull_request(
        self,
        repo: str,
        *,
        head_branch: str,
        base_branch: str = "main",
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Create a PR on GitHub."""
        existing = self.get_pull_request(repo, head_branch)
        if existing:
            return existing

        stdout = self._run_gh(
            [
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                base_branch,
                "--head",
                head_branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        url = stdout.strip()
        return {"url": url, "head": head_branch, "base": base_branch}

    def push_branch(
        self,
        local_repo_path: str | Path,
        branch: str,
        *,
        remote: str = "origin",
        expected_repo: str | None = None,
    ) -> None:
        """Push a local branch to the git remote.

        When `expected_repo` (an `owner/repo` slug) is given, this refuses
        to push -- fails closed -- unless `remote`'s URL actually points at
        that repository. This is the boundary that stops an objective from
        ever landing a branch on the wrong repository because its worktree
        happened to be a clone of something else.
        """
        if expected_repo is not None:
            from build_coordinator.runner.worker_pool import remote_matches_repo

            remote_check = subprocess.run(
                ["git", "-C", str(local_repo_path), "remote", "get-url", remote],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_check.returncode != 0:
                raise GitHubClientError(
                    f"cannot verify push target: {local_repo_path} has no readable "
                    f"{remote!r} remote: {remote_check.stderr.strip()}"
                )
            actual_url = remote_check.stdout.strip()
            if not remote_matches_repo(actual_url, expected_repo.split("/", 1)[-1]):
                raise GitHubClientError(
                    f"refusing to push: {local_repo_path}'s {remote!r} remote "
                    f"({actual_url!r}) does not match this task's resolved "
                    f"target repository ({expected_repo!r})"
                )
        cmd = ["git", "-C", str(local_repo_path), "push", "-u", remote, f"{branch}:{branch}"]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise GitHubClientError(f"failed to run git push: {exc}") from exc

        if completed.returncode != 0:
            err = completed.stderr.strip() or completed.stdout.strip()
            raise GitHubClientError(f"git push failed (code {completed.returncode}): {err}")
