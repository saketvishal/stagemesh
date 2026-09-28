from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .github import GitHubClient
from .persistence import Store
from .task_sources import DiscoveredTask, GitHubOutboundSync, sync_source


@dataclass(frozen=True)
class GitHubAcceptanceResult:
    status: str
    discovered: int
    deferred_skipped: bool
    outbound_status: str
    rate_limit_status: str


class FakeGitHubTransport:
    def __init__(self, rate_limited: bool = False):
        self.rate_limited = rate_limited
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []

    def request(self, method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, str], Any]:
        self.requests.append((method, path, body))
        if self.rate_limited:
            return 403, {"Retry-After": "60"}, {"message": "rate limit"}
        if method == "GET":
            return 200, {}, [
                {"number": 1, "title": "ready", "labels": []},
                {"number": 2, "title": "deferred", "labels": [{"name": "stagemesh:deferred"}]},
                {"number": 3, "title": "pull", "pull_request": {}, "labels": []},
            ]
        if method == "POST":
            return 201, {}, {"id": 10}
        if method == "PATCH":
            return 200, {}, {"state": "closed"}
        return 500, {}, {"message": "unexpected"}


def run_github_acceptance(store: Store) -> GitHubAcceptanceResult:
    client = GitHubClient("owner", "repo", FakeGitHubTransport())
    listed = client.list_open_issues()
    issues = listed.payload if isinstance(listed.payload, list) else []
    discovered = [
        DiscoveredTask(
            "github",
            str(issue["number"]),
            str(issue["title"]),
            eligible="stagemesh:deferred" not in [label.get("name") for label in issue.get("labels", [])],
            state="OPEN",
        )
        for issue in issues
        if "pull_request" not in issue
    ]
    task_ids = sync_source(store, discovered)
    outbound_id = GitHubOutboundSync(store, client).publish_done("1", "abc123")
    outbound = store.conn.execute("SELECT * FROM source_events WHERE id=?", (outbound_id,)).fetchone()
    rate_limited = GitHubClient("owner", "repo", FakeGitHubTransport(rate_limited=True)).list_open_issues()
    deferred_skipped = len(task_ids) == 1 and task_ids[0] == "1"
    status = "PASS" if listed.status == "OK" and deferred_skipped and outbound["status"] == "OK" and rate_limited.status == "UNKNOWN" else "FAIL"
    return GitHubAcceptanceResult(status, len(task_ids), deferred_skipped, outbound["status"], rate_limited.status)
