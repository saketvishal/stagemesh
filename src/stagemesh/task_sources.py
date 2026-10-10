from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .audit import record_audit
from .config import StageMeshConfig
from .domain import TaskStatus
from .github import GitHubClient, parse_retry_after
from .objective_roots import (
    DIRECT_EXECUTION_LABELS,
    folded_labels,
    objective_payload,
    source_issue_is_objective,
)
from .persistence import Store
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
    labels: tuple[str, ...] = ()
    body: str = ""
    created_at: str = ""


class LocalBacklogSource:
    name = "local-backlog"

    def __init__(self, path: Path, name: str | None = None):
        self.path = Path(path)
        if name is not None:
            self.name = _validate_source_name(name)

    def discover(self) -> list[DiscoveredTask]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TaskSourceValidationError("local backlog must be valid JSON") from exc
        if not isinstance(data, dict):
            raise TaskSourceValidationError("local backlog root must be an object")
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise TaskSourceValidationError("local backlog tasks must be a list")
        discovered: list[DiscoveredTask] = []
        seen: set[str] = set()
        task_ids: set[str] = set()
        for item in tasks:
            if not isinstance(item, dict):
                raise TaskSourceValidationError("local backlog task entries must be objects")
            source_id = item.get("id")
            if not isinstance(source_id, str) or not source_id:
                raise TaskSourceValidationError("local backlog task id must be a non-empty string")
            if source_id in task_ids:
                raise TaskSourceValidationError(f"duplicate local backlog task id: {source_id}")
            task_ids.add(source_id)
        valid_states = {str(status) for status in TaskStatus}
        for item in tasks:
            source_id = item.get("id")
            title = item.get("title")
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
            for dependency in dependencies:
                if dependency not in task_ids:
                    raise TaskSourceValidationError(f"local backlog task {source_id} has unknown dependency: {dependency}")
            seen.add(source_id)
            discovered.append(
                DiscoveredTask(
                    source=self.name,
                    source_id=source_id,
                    title=title,
                    eligible=eligible,
                    state=state,
                    dependencies=tuple(dependencies),
                    body=_description(item),
                    labels=_local_labels(item),
                )
            )
        return discovered


class JsonFileTaskSource(LocalBacklogSource):
    """Configurable JSON source using the local backlog task schema."""


class GoogleAxTaskSource(LocalBacklogSource):
    """Google AX export source using the local backlog task schema."""


def task_sources_from_config(config: StageMeshConfig) -> list[object]:
    sources: list[object] = []
    for source in config.task_sources:
        if source.kind == "github":
            if not config.github.owner or not config.github.repo:
                raise TaskSourceValidationError(f"github task source {source.name} requires github.owner and github.repo")
            sources.append(
                ConfiguredGitHubTaskSource(
                    config.github.owner,
                    config.github.repo,
                    config.github.token,
                    labels=source.labels,
                    excluded_labels=source.excluded_labels,
                    name=source.name,
                )
            )
            continue
        if source.kind not in {"json", "google-ax"} or source.path is None:
            raise TaskSourceValidationError(f"unsupported configured task source: {source.name}")
        source_class = GoogleAxTaskSource if source.kind == "google-ax" else JsonFileTaskSource
        sources.append(source_class(source.path, source.name))
    return sources


class GitHubIssueSource:
    name = "github"

    def __init__(self, cached_issues: list[dict[str, object]] | None = None, error: str | None = None):
        self.cached_issues = cached_issues or []
        self.error = error

    def discover(self) -> tuple[list[DiscoveredTask], str]:
        if self.error:
            return [], "UNKNOWN" if self.error in {"rate-limit", "capacity"} else "STALE"
        discovered = [_github_issue_to_task(issue) for issue in self.cached_issues if "pull_request" not in issue]
        return ([task for task in discovered if task is not None], "OK")


class GitHubApiIssueSource:
    name = "github"

    def __init__(self, owner: str, repo: str, token: str | None = None, now: float | None = None):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.now = time.time() if now is None else now

    def discover(self) -> tuple[list[DiscoveredTask], str, float | None]:
        discovered = []
        page = 1
        while True:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{self.owner}/{self.repo}/issues?state=all&per_page=100&page={page}",
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
                    return [], "UNKNOWN", self.now + parse_retry_after(retry_after)
                if exc.code in {401, 404}:
                    return [], "STALE", None
                raise
            except (json.JSONDecodeError, UnicodeDecodeError):
                return [], "UNKNOWN", None
            if not isinstance(issues, list):
                raise TaskSourceValidationError("github issues response must be a list")
            for issue in issues:
                if not isinstance(issue, dict):
                    raise TaskSourceValidationError("github issue entries must be objects")
                if "pull_request" not in issue:
                    discovered.append(_github_issue_to_task(issue))
            if len(issues) < 100:
                break
            page += 1
        return ([task for task in discovered if task is not None], "OK", None)


class ConfiguredGitHubTaskSource:
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None,
        *,
        labels: tuple[str, ...] = (),
        excluded_labels: tuple[str, ...] = (),
        name: str = "github",
    ):
        self.name = _validate_source_name(name)
        self.labels = labels
        self.excluded_labels = excluded_labels
        self.source = GitHubApiIssueSource(owner, repo, token)

    def discover(self) -> list[DiscoveredTask]:
        tasks, status, _retry_after = self.source.discover()
        if status != "OK":
            # Never let an unreachable/unauthorized source look like an empty backlog.
            print(
                f"warning: github task source {self.name} unavailable ({status}); "
                "set STAGEMESH_GITHUB_TOKEN for private repositories",
                file=sys.stderr,
            )
            return []
        if not self.labels and not self.excluded_labels:
            return tasks
        required = {label.casefold() for label in self.labels}
        excluded = {label.casefold() for label in self.excluded_labels}
        discovered: list[DiscoveredTask] = []
        for task in tasks:
            labels = {label.casefold() for label in task.labels}
            matches = required.issubset(labels) and not bool(excluded & labels)
            discovered.append(
                DiscoveredTask(
                    task.source,
                    task.source_id,
                    task.title,
                    eligible=task.eligible and matches,
                    state=task.state,
                    dependencies=task.dependencies,
                    labels=task.labels,
                    body=task.body,
                    created_at=task.created_at,
                )
            )
        return discovered


def _github_issue_to_task(issue: dict[str, object]) -> DiscoveredTask:
    number = issue.get("number")
    title = issue.get("title")
    if not isinstance(number, int) or number <= 0:
        raise TaskSourceValidationError("github issue number must be a positive integer")
    if not isinstance(title, str) or not title:
        raise TaskSourceValidationError(f"github issue {number} title must be a non-empty string")
    labels = issue.get("labels", [])
    if not isinstance(labels, list):
        raise TaskSourceValidationError(f"github issue {number} labels must be a list")
    label_names: list[str] = []
    for label in labels:
        if not isinstance(label, dict):
            raise TaskSourceValidationError(f"github issue {number} labels must be objects")
        name = label.get("name")
        if isinstance(name, str):
            label_names.append(name)
    state = issue.get("state", "OPEN")
    if not isinstance(state, str):
        raise TaskSourceValidationError(f"github issue {number} state must be a string")
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    objective_root = source_issue_is_objective(tuple(label_names))
    blocked = {
        "stagemesh:deferred",
        "stagemesh:blocked",
        "status:blocked",
        "status:remediating",
    }.intersection(label.casefold() for label in label_names)
    return DiscoveredTask(
        source=GitHubIssueSource.name,
        source_id=str(number),
        title=title,
        eligible=not blocked and not objective_root and state.lower() == "open",
        state="OPEN" if state.lower() == "open" else state.upper(),
        labels=tuple(label_names),
        body=body,
        dependencies=_github_dependencies(body),
        created_at=issue.get("created_at") if isinstance(issue.get("created_at"), str) else "",
    )


def _github_dependencies(body: str) -> tuple[str, ...]:
    dependencies: list[str] = []
    for line in body.splitlines():
        if not re.match(r"^\s*(depends on|blocked by|requires)\s*:", line, re.IGNORECASE):
            continue
        dependencies.extend(match.group(1) for match in re.finditer(r"#(\d+)", line))
    return tuple(dict.fromkeys(dependencies))


def _validate_source_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskSourceValidationError("task source name must be a non-empty string")
    normalized = value.strip()
    if any(char.isspace() for char in normalized):
        raise TaskSourceValidationError("task source name must not contain whitespace")
    return normalized


def _local_labels(item: dict[str, object]) -> tuple[str, ...]:
    value = item.get("labels", ())
    if isinstance(value, list) and all(isinstance(label, str) for label in value):
        return tuple(value)
    return ()


def _description(item: dict[str, object]) -> str:
    for key in ("objective", "description", "body"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def sync_source(store: Store, tasks: list[DiscoveredTask]) -> list[str]:
    ids: list[str] = []
    for task in tasks:
        state = _normalized_state(task.state)
        is_github = task.source == GitHubIssueSource.name
        direct_execution = bool(folded_labels(task.labels) & DIRECT_EXECUTION_LABELS)
        source_objective = is_github and source_issue_is_objective(task.labels)
        historical_objective = is_github and not direct_execution and _objective_exists(store, task.source_id)
        cached: dict[str, object] = {"eligible": task.eligible, "state": state}
        if source_objective or historical_objective:
            cached["objective_root"] = True
        previous_source_state = store.source_state(task.source, task.source_id) if is_github else {}
        retirement_reason = _retirement_reason(task, state)
        if is_github:
            _record_ready_label_drift(store, task, previous_source_state)
        if source_objective or historical_objective:
            retirement_reason = "source objective root"
            store.save_objective(
                task.source_id,
                task.title,
                objective_payload(task.source_id, task.labels, task.body, task.dependencies),
            )
        if retirement_reason is not None and is_github:
            cached["retirement_reason"] = retirement_reason
        if task.labels or is_github:
            cached["labels"] = list(task.labels)
        if task.created_at:
            cached["created_at"] = task.created_at
        if task.body:
            # Lets auto-planning build a contract from the issue text.
            cached["objective"] = task.body[:6000]
        if task.dependencies:
            cached["dependencies"] = list(task.dependencies)
        store.cache_source(task.source, task.source_id, cached, state)
        if retirement_reason is not None and is_github:
            store.retire_source_task(
                task.source,
                task.source_id,
                retirement_reason,
                {"source_state": state, "eligible": task.eligible, "objective_root": bool(source_objective or historical_objective)},
            )
        elif task.eligible and state == "OPEN":
            task_id = store.upsert_task(task.title, task.source, task.source_id)
            previous_retirement_reason = previous_source_state.get("retirement_reason")
            if is_github and isinstance(previous_retirement_reason, str):
                store.restore_source_task(
                    task.source,
                    task.source_id,
                    previous_retirement_reason,
                    {"source_state": state, "eligible": task.eligible},
                )
            ids.append(task_id)
            for dependency in task.dependencies:
                if store.get_task(dependency) is not None:
                    store.add_dependency(task.source_id, dependency)
    return ids


READY_LABEL = "stagemesh:ready"


def _record_ready_label_drift(store: Store, task: DiscoveredTask, previous_state: dict[str, object]) -> None:
    """Record (as evidence) when the issue's ready label was added or removed since the last sync."""
    previous_labels = previous_state.get("labels")
    if not isinstance(previous_labels, list) or not all(isinstance(label, str) for label in previous_labels):
        return
    was_ready = READY_LABEL in folded_labels(tuple(previous_labels))
    is_ready = READY_LABEL in folded_labels(task.labels)
    if was_ready == is_ready:
        return
    status = "READY_RESTORED" if is_ready else "READY_REMOVED"
    payload = {"label": READY_LABEL, "previous_labels": previous_labels, "labels": list(task.labels)}
    store.add_source_event(task.source, task.source_id, "inbound", status, payload)
    record_audit(store, "source.ready_label_drift", {"source": task.source, "source_id": task.source_id, "status": status})


def _objective_exists(store: Store, source_id: str) -> bool:
    return store.conn.execute("SELECT 1 FROM objectives WHERE id IN (?, ?) LIMIT 1", (source_id, f"github:{source_id}")).fetchone() is not None


def _normalized_state(state: str) -> str:
    return state.upper()


def _retirement_reason(task: DiscoveredTask, state: str) -> str | None:
    if state != "OPEN":
        return "source closed"
    if not task.eligible:
        return "source no longer eligible"
    return None


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
            issue_number, f"StageMesh integrated candidate `{candidate_sha}`.")
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

    def publish_blocked(self, issue_number: str, reason: str) -> str:
        bounded = _bounded_block_reason(reason)
        marker = _blocked_marker(bounded)
        retry = RetryRegistry(self.store)
        key = f"github:{issue_number}:blocked"
        decision = retry.decision(key)
        if not decision.allowed:
            return self.publish(
                "github",
                issue_number,
                "BACKOFF",
                {"reason": bounded, "next_attempt_at": decision.next_attempt_at},
            )
        comments = self.client.list_issue_comments(issue_number)
        if comments.status != "OK":
            retry.record_failure(key, comments.status)
            return self.publish("github", issue_number, comments.status, {"reason": bounded, "comments": comments.status})
        bodies = [
            str(item.get("body", ""))
            for item in comments.payload
            if isinstance(item, dict)
        ] if isinstance(comments.payload, list) else []
        if any(marker in body for body in bodies):
            retry.record_success(key)
            return self.publish("github", issue_number, "OK", {"reason": bounded, "comment": "DUPLICATE"})
        body = f"StageMesh blocked this task: `{bounded}`.\n\n{marker}"
        comment = self.client.comment_issue(issue_number, body)
        if comment.status == "OK":
            retry.record_success(key)
        else:
            retry.record_failure(key, comment.status)
        return self.publish("github", issue_number, comment.status, {"reason": bounded, "comment": comment.status})


def _bounded_block_reason(reason: str, limit: int = 200) -> str:
    normalized = " ".join(str(reason or "blocked").split())
    if not normalized:
        normalized = "blocked"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _blocked_marker(reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    return f"<!-- stagemesh:blocked:{digest} -->"
