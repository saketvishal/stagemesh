from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit


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


def github_cli_token(timeout_seconds: float = 5.0) -> str | None:
    """Return the current GitHub CLI token without printing or persisting it."""
    kwargs: dict[str, object] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


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
                return response.status, dict(response.headers), _decode_json(raw, "invalid GitHub response JSON")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            payload = _decode_json(raw, str(exc.reason))
            return exc.code, dict(exc.headers), payload

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        """Download bounded binary/text content without forwarding GitHub credentials on redirects."""
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.api_root}{path}", headers=headers, method=method
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=20) as response:
                return _bounded_response(response, max_bytes)
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                return exc.code, dict(exc.headers), b""
            location = exc.headers.get("Location")
            if not _safe_download_url(location):
                return 502, {}, b""
            # This request intentionally has no Authorization header. GitHub's signed URL is the authority.
            redirected = urllib.request.Request(
                str(location), headers={"Accept": "text/plain"}, method="GET"
            )
            try:
                with urllib.request.urlopen(redirected, timeout=20) as response:
                    return _bounded_response(response, max_bytes)
            except urllib.error.HTTPError as redirected_error:
                return redirected_error.code, dict(redirected_error.headers), b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bounded_response(response, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    raw = response.read(max_bytes + 1)
    headers = dict(response.headers)
    if len(raw) > max_bytes:
        headers["X-StageMesh-Truncated"] = "true"
        return 206, headers, raw[:max_bytes]
    return int(response.status), headers, raw


def _safe_download_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


class GitHubClient:
    def __init__(self, owner: str, repo: str, transport: GitHubTransport):
        self.owner = owner
        self.repo = repo
        self.transport = transport

    def list_open_issues(self) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "GET", f"/repos/{self.owner}/{self.repo}/issues?state=open"
        )
        result = self._result(code, headers, payload)
        if result.status == "OK" and not isinstance(result.payload, list):
            return GitHubResult("UNKNOWN", result.payload)
        return result

    def comment_issue(self, number: str, body: str) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "POST", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments", {"body": body}
        )
        return self._result(code, headers, payload)

    def list_issue_comments(self, number: str, page: int = 1) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "GET", f"/repos/{self.owner}/{self.repo}/issues/{number}/comments?per_page=100&page={page}"
        )
        result = self._result(code, headers, payload)
        if result.status == "OK" and not isinstance(result.payload, list):
            return GitHubResult("UNKNOWN", result.payload)
        return result

    def close_issue(self, number: str) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{number}", {"state": "closed"}
        )
        return self._result(code, headers, payload)

    def ensure_label(self, name: str, color: str, description: str) -> GitHubResult:
        """Return an existing repository label or create it without replacing operator metadata."""
        path = f"/repos/{self.owner}/{self.repo}/labels/{quote(name, safe='')}"
        code, headers, payload = self.transport.request("GET", path)
        if code == 200:
            return GitHubResult("OK", payload)
        if code != 404:
            return self._result(code, headers, payload)
        code, headers, payload = self.transport.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/labels",
            {"name": name, "color": color, "description": description},
        )
        if code == 422:  # Another initializer may have created the label concurrently.
            code, headers, payload = self.transport.request("GET", path)
        return self._result(code, headers, payload)

    def add_issue_labels(self, number: str, labels: tuple[str, ...]) -> GitHubResult:
        code, headers, payload = self.transport.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{number}/labels",
            {"labels": list(labels)},
        )
        return self._result(code, headers, payload)

    @staticmethod
    def _result(code: int, headers: dict[str, str], payload: object) -> GitHubResult:
        if code in {200, 201}:
            return GitHubResult("OK", payload)
        if code in {403, 429}:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
            return GitHubResult("UNKNOWN", payload, parse_retry_after(retry_after))
        if code in {401, 404}:
            return GitHubResult("STALE", payload)
        return GitHubResult("UNKNOWN", payload)


def parse_retry_after(value: str | None, default: float = 60) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _repository_config_digest(project: Path) -> str | None:
    """Digest repository-local config so cached remote detection invalidates on change."""
    dotgit = project.resolve() / ".git"
    try:
        if dotgit.is_dir():
            gitdir = dotgit
        elif dotgit.is_file():
            prefix, raw_target = dotgit.read_text(encoding="utf-8").strip().split(":", 1)
            if prefix != "gitdir":
                return None
            target = Path(raw_target.strip())
            gitdir = (target if target.is_absolute() else dotgit.parent / target).resolve()
        else:
            return None
        commondir_file = gitdir / "commondir"
        if commondir_file.is_file():
            raw_common = Path(commondir_file.read_text(encoding="utf-8").strip())
            common = (raw_common if raw_common.is_absolute() else gitdir / raw_common).resolve()
        else:
            common = gitdir
        return hashlib.sha256((common / "config").read_bytes()).hexdigest()
    except (OSError, UnicodeError, ValueError):
        return None


@lru_cache(maxsize=256)
def _detect_github_repository_cached(
    project: str,
    remote: str,
    _config_digest: str,
) -> GitHubRepository | None:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={Path(project).as_posix()}", "remote", "get-url", remote],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return parse_github_remote(result.stdout.strip(), remote)


def detect_github_repository(project: Path, remote: str = "origin") -> GitHubRepository | None:
    resolved = Path(project).resolve()
    digest = _repository_config_digest(resolved)
    if digest is not None:
        return _detect_github_repository_cached(str(resolved), remote, digest)
    result = subprocess.run(
        ["git", "-c", f"safe.directory={resolved.as_posix()}", "remote", "get-url", remote],
        cwd=resolved,
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


def _decode_json(raw: str, fallback_message: str) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message": fallback_message}
