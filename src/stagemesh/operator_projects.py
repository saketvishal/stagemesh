"""In-process navigation between isolated loopback project servers.

The projection never exposes another project's bearer token or opens another
project's database.  A short-lived, one-use launch ticket is redeemed only by
the target server, which then places its own bearer token in its own URL
fragment.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

PROJECTS_CONTRACT = "stagemesh.operator-projects/1"
PROJECT_LAUNCH_TTL_SECONDS = 30
MAX_PROJECT_TARGETS = 50
MAX_LAUNCH_TICKETS = 200

_PROJECT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/")


class OperatorProjectError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class ProjectLaunchTickets:
    """Issue and consume target-local one-use launch tickets."""

    def __init__(
        self,
        bearer_token: str,
        *,
        now: Callable[[], float] = time.time,
        ttl_seconds: int = PROJECT_LAUNCH_TTL_SECONDS,
    ):
        if not bearer_token or len(bearer_token) > 512:
            raise ValueError("project launch requires a bounded bearer token")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
            raise ValueError("project launch ttl must be between 1 and 120 seconds")
        self._bearer_token = bearer_token
        self._now = now
        self.ttl_seconds = ttl_seconds
        self._tickets: dict[str, float] = {}

    def issue(self) -> str:
        self._prune()
        if len(self._tickets) >= MAX_LAUNCH_TICKETS:
            oldest = min(self._tickets, key=self._tickets.get)  # type: ignore[arg-type]
            self._tickets.pop(oldest, None)
        ticket = secrets.token_urlsafe(32)
        self._tickets[_digest(ticket)] = self._now() + self.ttl_seconds
        return ticket

    def redeem(self, ticket: object) -> str:
        if not isinstance(ticket, str) or not 20 <= len(ticket) <= 100 or not ticket.isascii():
            raise OperatorProjectError("invalid_launch", "project launch ticket is invalid", status=403)
        expiry = self._tickets.pop(_digest(ticket), None)
        if expiry is None or self._now() > expiry:
            raise OperatorProjectError("invalid_launch", "project launch ticket is invalid", status=403)
        self._prune()
        return "/#token=" + quote(self._bearer_token, safe="")

    def _prune(self) -> None:
        now = self._now()
        self._tickets = {key: expiry for key, expiry in self._tickets.items() if expiry >= now}


@dataclass(frozen=True)
class ProjectTarget:
    name: str
    project_id: str
    base_url: str
    issue_launch: Callable[[], str]


class ProjectProjection:
    """Validated, read-only metadata for one set of already-running servers."""

    def __init__(self, current_project_id: str, targets: tuple[ProjectTarget, ...]):
        if not 1 <= len(targets) <= MAX_PROJECT_TARGETS:
            raise ValueError("project projection requires between 1 and 50 targets")
        names: set[str] = set()
        ids: set[str] = set()
        urls: set[str] = set()
        normalized: list[ProjectTarget] = []
        for target in targets:
            name = target.name.strip()
            match = _LOOPBACK_URL.fullmatch(target.base_url)
            if not name or len(name) > 100 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
                raise ValueError("project projection names must be bounded display text")
            if not _PROJECT_ID.fullmatch(target.project_id):
                raise ValueError("project projection ids must be bounded identifiers")
            if match is None or not 1 <= int(match.group(1)) <= 65535:
                raise ValueError("project projection targets must be loopback HTTP servers")
            folded_name = name.casefold()
            if folded_name in names or target.project_id in ids or target.base_url in urls:
                raise ValueError("project projection contains duplicate identity")
            names.add(folded_name)
            ids.add(target.project_id)
            urls.add(target.base_url)
            normalized.append(ProjectTarget(name, target.project_id, target.base_url, target.issue_launch))
        if current_project_id not in ids:
            raise ValueError("current project is not present in the project projection")
        self.current_project_id = current_project_id
        self.targets = tuple(sorted(normalized, key=lambda item: (item.name.casefold(), item.project_id)))

    def serialize(self) -> dict[str, object]:
        return {
            "current_project_id": self.current_project_id,
            "projects": [
                {
                    "name": target.name,
                    "project_id": target.project_id,
                    "current": target.project_id == self.current_project_id,
                    "launch_url": None
                    if target.project_id == self.current_project_id
                    else target.base_url + "project-launch?ticket=" + quote(target.issue_launch(), safe=""),
                }
                for target in self.targets
            ],
            "isolated": True,
            "aggregated": False,
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()
