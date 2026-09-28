from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .domain import TaskStatus
from .persistence import Store
from .github import GitHubClient
from .audit import record_audit
from .retry import RetryRegistry


class TaskSourceValidationError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveredTask:
    source: str
    source_id: str
    title: str
    eligible: bool = True
    state: str = "OPEN"
    dependencies: tuple[str, ...] = ()


class LocalBacklogSource:
    name = "local-backlog"

    def __init__(self, path: Path):
        self.path = Path(path)

    def discover(self) -> list[DiscoveredTask]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TaskSourceValidationError("local backlog root must be an object")
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise TaskSourceValidationError("local backlog tasks must be a list")
        discovered: list[DiscoveredTask] = []
        seen: set[str] = set()
        valid_states = {str(status) for status in TaskStatus}
        for item in tasks:
            if not isinstance(item, dict):
                raise TaskSourceValidationError("local backlog task entries must be objects")
            source_id = item.get("id")
            title = item.get("title")
            if not isinstance(source_id, str) or not source_id:
                raise TaskSourceValidationError("local backlog task id must be a non-empty string")
            if source_id in seen:
                raise TaskSourceValidationError(f"duplicate local backlog task id: {source_id}")
            if not isinstance(title, str) or not title:
                raise TaskSourceValidationError(f"local backlog task {source_id} title must be a non-empty string")
            eligible = item.get("eligible", True)
            if not isinstance(eligible, bool):
                raise TaskSourceValidationError(f"local backlog task {source_id} eligible must be a boolean")
            state = item.get("state", "OPEN")
            if not isinstance(state, str) or state not in valid_states:
                raise TaskSourceValidationError(f"local backlog task {source_id} state is unsupported: {state}")
            dependencies = item.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
                raise TaskSourceValidationError(f"local backlog task {source_id} dependencies must be a list of strings")
            seen.add(source_id)
            discovered.append(
                DiscoveredTask(
                    source=self.name,
                    source_id=source_id,
                    title=title,
                    eligible=eligible,
                    state=state,
                    dependencies=tuple(dependencies),
                )
            )
        return discovered


class GitHubIssueSource:
    name = "github"

    def __init__(self, cached_issues: list[dict[str, object]] | None = None, error: str | None = None):
        self.cached_issues = cached_issues or []
        self.error = error

    def discover(self) -> tuple[list[DiscoveredTask], str]:
        if self.error:
            return [], "UNKNOWN" if self.error in {"rate-limit", "capacity"} else "STALE"
        return (
            [
                DiscoveredTask(
                    source=self.name,
                    source_id=str(issue["number"]),
                    title=str(issue["title"]),
                    eligible=not bool(issue.get("deferred", False)),
                    state=str(issue.get("state", "OPEN")),
                )
                for issue in self.cached_issues
            ],
            "OK",
        )


class GitHubApiIssueSource:
    name = "github"

    def __init__(self, owner: str, repo: str, token: str | None = None, now: float | None = None):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.now = time.time() if now is None else now

    def discover(self) -> tuple[list[DiscoveredTask], str, float | None]:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/issues?state=open",
            headers={
                "Accept": "application/vnd.github+json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                issues = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                retry_after = exc.headers.get("Retry-After")
                return [], "UNKNOWN", self.now + float(retry_after or 60)
            if exc.code in {401, 404}:
                return [], "STALE", None
            raise
        return (
            [
                DiscoveredTask(
                    source=self.name,
                    source_id=str(issue["number"]),
                    title=str(issue["title"]),
                    eligible="stagemesh:deferred" not in [label.get("name") for label in issue.get("labels", [])],
                    state="OPEN",
                )
                for issue in issues
                if "pull_request" not in issue
            ],
            "OK",
            None,
        )


def sync_source(store: Store, tasks: list[DiscoveredTask]) -> list[str]:
    ids: list[str] = []
    for task in tasks:
        store.cache_source(task.source, task.source_id, {"eligible": task.eligible, "state": task.state}, task.state)
        if task.eligible and task.state == "OPEN":
            ids.append(store.upsert_task(task.title, task.source, task.source_id))
            for dependency in task.dependencies:
                store.add_dependency(task.source_id, dependency)
    return ids


class OutboundSync:
    """Records outbound lifecycle synchronization without trusting it as lifecycle truth."""

    def __init__(self, store: Store):
        self.store = store

    def publish(self, source: str, source_id: str, status: str, payload: dict[str, object] | None = None) -> str:
        event_id = self.store.add_source_event(source, source_id, "outbound", status, payload or {})
        record_audit(
            self.store,
            "source.outbound",
            {"source": source, "source_id": source_id, "status": status, **(payload or {})},
        )
        return event_id


class GitHubOutboundSync(OutboundSync):
    def __init__(self, store: Store, client: GitHubClient):
        super().__init__(store)
        self.client = client

    def publish_done(self, issue_number: str, candidate_sha: str) -> str:
        retry = RetryRegistry(self.store)
        key = f"github:{issue_number}:outbound"
        decision = retry.decision(key)
        if not decision.allowed:
            return self.publish(
                "github",
                issue_number,
                "BACKOFF",
                {"candidate_sha": candidate_sha, "next_attempt_at": decision.next_attempt_at},
            )
        comment = self.client.comment_issue(
            issue_number, f"StageMesh integrated candidate `{candidate_sha}`."
        )
        close = self.client.close_issue(issue_number) if comment.status == "OK" else comment
        status = "OK" if comment.status == "OK" and close.status == "OK" else close.status
        if status == "OK":
            retry.record_success(key)
        else:
            retry.record_failure(key, status)
        return self.publish(
            "github",
            issue_number,
            status,
            {"candidate_sha": candidate_sha, "comment": comment.status, "close": close.status},
        )
