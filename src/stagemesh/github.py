from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class GitHubTransport(Protocol):
    def request(self, method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, str], object]:
        ...


@dataclass(frozen=True)
class GitHubResult:
    status: str
    payload: object
    retry_after: float | None = None


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    repo: str
    remote: str


class UrlLibGitHubTransport:
    def __init__(self, token: str | None = None, api_root: str = "https://api.github.com"):
        self.token = token
        self.api_root = api_root.rstrip("/")

    def request(self, method: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, str], object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.api_root}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                return response.status, dict(response.headers), json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            payload = json.loads(raw) if raw else {"message": exc.reason}
            return exc.code, dict(exc.headers), payload


class GitHubClient:
    def __init__(self, owner: str, repo: str, transport: GitHubTransport):
        self.owner = owner
        self.repo = repo
        self.transport = transport

    def list_open_issues(self) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "GET", f"/repos/{self.owner}/{self.repo}/issues?state=open"
        )
        return self._result(code, headers, payload)

    def comment_issue(self, number: str, body: str) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments", {"body": body}
        )
        return self._result(code, headers, payload)

    def close_issue(self, number: str) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{number}", {"state": "closed"}
        )
        return self._result(code, headers, payload)

    @staticmethod
    def _result(code: int, headers: dict[str, str], payload: object) -> GitHubResult:
        if code in {200, 201}:
            return GitHubResult("OK", payload)
        if code in {403, 429}:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            return GitHubResult("UNKNOWN", payload, float(retry_after or 60))
        if code in {401, 404}:
            return GitHubResult("STALE", payload)
        return GitHubResult("UNKNOWN", payload)


def detect_github_repository(project: Path, remote: str = "origin") -> GitHubRepository | None:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={project.resolve().as_posix()}", "remote", "get-url", remote],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return parse_github_remote(result.stdout.strip(), remote)


def parse_github_remote(url: str, remote: str = "origin") -> GitHubRepository | None:
    patterns = [
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?/?$",
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?/?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return GitHubRepository(match.group("owner"), match.group("repo"), remote)
    return None
