from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .persistence import Store


@dataclass(frozen=True)
class DiscoveredTask:
    source: str
    source_id: str
    title: str
    eligible: bool = True
    state: str = "OPEN"


class LocalBacklogSource:
    name = "local-backlog"

    def __init__(self, path: Path):
        self.path = Path(path)

    def discover(self) -> list[DiscoveredTask]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            DiscoveredTask(
                source=self.name,
                source_id=str(item["id"]),
                title=str(item["title"]),
                eligible=bool(item.get("eligible", True)),
                state=str(item.get("state", "OPEN")),
            )
            for item in data.get("tasks", [])
        ]


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
    return ids


class OutboundSync:
    """Records outbound lifecycle synchronization without trusting it as lifecycle truth."""

    def __init__(self, store: Store):
        self.store = store

    def publish(self, source: str, source_id: str, status: str, payload: dict[str, object] | None = None) -> str:
        return self.store.add_source_event(source, source_id, "outbound", status, payload or {})
