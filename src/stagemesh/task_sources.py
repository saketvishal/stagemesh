from __future__ import annotations

import json
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


def sync_source(store: Store, tasks: list[DiscoveredTask]) -> list[str]:
    ids: list[str] = []
    for task in tasks:
        store.cache_source(task.source, task.source_id, {"eligible": task.eligible, "state": task.state}, task.state)
        if task.eligible and task.state == "OPEN":
            ids.append(store.upsert_task(task.title, task.source, task.source_id))
    return ids
