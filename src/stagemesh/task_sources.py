from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .audit import record_audit
from .concurrency import IntegrationLock
from .config import StageMeshConfig
from .domain import TaskStatus
from .github import GitHubClient, GitHubResult, parse_retry_after
from .objective_roots import (
    DIRECT_EXECUTION_LABELS,
    folded_labels,
    objective_payload,
    source_contract_refusal_reason,
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
    eligibility_reason: str = ""
    selection_scope: dict | None = None
    satisfied_dependencies: tuple[tuple[str, str], ...] = ()


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
                    selection_scope=item.get("selection_scope"),
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
        self.last_status: str | None = None
        self.last_error: str | None = None

    def check_source(self) -> tuple[bool, str]:
        if self.last_status is None:
            self.discover()
        if self.last_status == "OK":
            return True, "OK"
        return False, self.last_error or f"source {self.name} unavailable ({self.last_status})"

    def discover(self) -> list[DiscoveredTask]:
        tasks, status, _retry_after = self.source.discover()
        self.last_status = status
        if status != "OK":
            self.last_error = f"github task source {self.name} unavailable ({status})"
            print(
                f"warning: github task source {self.name} unavailable ({status}); "
                "set STAGEMESH_GITHUB_TOKEN for private repositories",
                file=sys.stderr,
            )
            return []
        self.last_error = None
        if not self.labels and not self.excluded_labels:
            return tasks
        required = {label.casefold() for label in self.labels}
        excluded = {label.casefold() for label in self.excluded_labels}
        discovered: list[DiscoveredTask] = []
        for task in tasks:
            labels = {label.casefold() for label in task.labels}
            matches = required.issubset(labels) and not bool(excluded & labels)
            reason = task.eligibility_reason
            if not matches and task.eligible:
                if not required.issubset(labels):
                    missing = sorted(required - labels)
                    reason = f"missing required label(s): {', '.join(missing)}"
                elif bool(excluded & labels):
                    present_excluded = sorted(excluded & labels)
                    reason = f"has excluded label(s): {', '.join(present_excluded)}"
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
                    eligibility_reason=reason,
                    satisfied_dependencies=task.satisfied_dependencies,
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
    contract_refusal = source_contract_refusal_reason(body)
    blocked = {
        "stagemesh:deferred",
        "stagemesh:blocked",
        "status:blocked",
        "status:remediating",
    }.intersection(label.casefold() for label in label_names)
    eligible = not blocked and not objective_root and contract_refusal is None and state.lower() == "open"
    reason = ""
    if state.lower() != "open":
        reason = f"source issue is {state.lower()}"
    elif objective_root:
        reason = "source objective root"
    elif contract_refusal is not None:
        reason = contract_refusal
    elif blocked:
        reason = f"blocked by label: {', '.join(sorted(blocked))}"
    return DiscoveredTask(
        source=GitHubIssueSource.name,
        source_id=str(number),
        title=title,
        eligible=eligible,
        state="OPEN" if state.lower() == "open" else state.upper(),
        labels=tuple(label_names),
        body=body,
        dependencies=_github_dependencies(body),
        created_at=issue.get("created_at") if isinstance(issue.get("created_at"), str) else "",
        eligibility_reason=reason,
    )


def _github_dependencies(body: str) -> tuple[str, ...]:
    dependencies: list[str] = []
    for line in body.splitlines():
        if not re.match(r"^\s*(depends on|blocked by|requires)\s*:", line, re.IGNORECASE):
            continue
        issue_only = re.sub(r"\b(?:PR|pull\s+request)\s*#\d+", "", line, flags=re.IGNORECASE)
        dependencies.extend(match.group(1) for match in re.finditer(r"#(\d+)", issue_only))
    return tuple(dict.fromkeys(dependencies))


def reconcile_completed_spec_dependencies(
    tasks: list[DiscoveredTask], *, commit_is_merged: Callable[[str], bool]
) -> list[DiscoveredTask]:
    """Recognize a narrowly evidenced completed specification prerequisite.

    Closure alone is deliberately insufficient. The implementation and specification
    issues must bind to one another, both must name the same immutable commit, the
    specification must declare DONE and closed, and the caller must prove that commit
    is on the integration branch.
    """
    github = {
        task.source_id: task
        for task in tasks
        if task.source == GitHubIssueSource.name
    }
    reconciled: list[DiscoveredTask] = []
    for task in tasks:
        governing_spec = _governing_spec_dependency(task)
        if governing_spec is None:
            reconciled.append(task)
            continue
        evidence = _completed_spec_evidence(task, github, commit_is_merged)
        if evidence is None:
            dependencies = tuple(dict.fromkeys((*task.dependencies, governing_spec)))
            reconciled.append(replace(task, dependencies=dependencies))
            continue
        dependency_id, commit = evidence
        remaining = tuple(item for item in task.dependencies if item != dependency_id)
        satisfied = tuple(dict.fromkeys((*task.satisfied_dependencies, (dependency_id, commit))))
        reconciled.append(replace(task, dependencies=remaining, satisfied_dependencies=satisfied))
    return reconciled


def reconcile_project_spec_dependencies(
    tasks: list[DiscoveredTask], project: Path, integration_ref: str | None
) -> list[DiscoveredTask]:
    """Apply completed-spec reconciliation against a concrete integration ref."""
    if not integration_ref or not integration_ref.startswith("refs/heads/"):
        return tasks
    from .git import GitWorkspace

    git = GitWorkspace(project)
    tip = git.run("rev-parse", "--verify", "--quiet", f"{integration_ref}^{{commit}}", check=False)
    if tip.returncode != 0:
        return tasks
    integration_sha = tip.stdout.strip()
    merged_cache: dict[str, bool] = {}

    def commit_is_merged(commit: str) -> bool:
        if commit in merged_cache:
            return merged_cache[commit]
        resolved = git.run("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
        if resolved.returncode != 0 or resolved.stdout.strip().casefold() != commit.casefold():
            merged_cache[commit] = False
        else:
            merged_cache[commit] = (
                git.run("merge-base", "--is-ancestor", commit, integration_sha, check=False).returncode == 0
            )
        return merged_cache[commit]

    return reconcile_completed_spec_dependencies(tasks, commit_is_merged=commit_is_merged)


def _governing_spec_dependency(task: DiscoveredTask) -> str | None:
    if task.source != GitHubIssueSource.name or _normalized_state(task.state) != "OPEN":
        return None
    match = re.search(
        r"^\s*governing\s+specification\s+issue\s*:\s*#(\d+)\b",
        task.body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1) if match is not None else None


def _completed_spec_evidence(
    task: DiscoveredTask,
    github: dict[str, DiscoveredTask],
    commit_is_merged: Callable[[str], bool],
) -> tuple[str, str] | None:
    dependency_id = _governing_spec_dependency(task)
    pin_match = re.search(
        r"^\s*approved\s+specification\s+commit\s*:\s*`?([0-9a-f]{40})\b`?",
        task.body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if dependency_id is None or pin_match is None:
        return None
    commit = pin_match.group(1).lower()
    specification = github.get(dependency_id)
    if specification is None or _normalized_state(specification.state) != "CLOSED":
        return None
    implementation_match = re.search(
        r"^\s*specification\s+for\s+implementation\s*:\s*#(\d+)\b",
        specification.body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    done = re.search(
        r"^\s*stagemesh\s+status\s*:\s*DONE\b",
        specification.body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    merged_pin = re.search(
        rf"\bmerged\b[\s\S]{{0,600}}\b{re.escape(commit)}\b",
        specification.body,
        flags=re.IGNORECASE,
    )
    if implementation_match is None or implementation_match.group(1) != task.source_id:
        return None
    if done is None or merged_pin is None or not commit_is_merged(commit):
        return None
    return dependency_id, commit


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


def sync_source(store: Store, tasks: list[DiscoveredTask], *, preserve_existing_dependencies: bool = False) -> list[str]:
    """Publish source eligibility only after its dependency/retirement updates are complete."""
    if str(store.db_path) == ":memory:":  # a private SQLite connection cannot share a cross-process file lock
        return _sync_source(store, tasks, preserve_existing_dependencies=preserve_existing_dependencies)
    database = store.db_path.resolve()
    lock = IntegrationLock(database.with_name(database.name + ".source-sync.lock"), timeout_seconds=30)
    with lock.hold("source-sync"):
        return _sync_source(store, tasks, preserve_existing_dependencies=preserve_existing_dependencies)


def _sync_source(store: Store, tasks: list[DiscoveredTask], *, preserve_existing_dependencies: bool) -> list[str]:
    ids: list[str] = []
    publications: list[tuple[str, str, dict[str, object], str]] = []
    dependency_tasks: list[tuple[str, set[str], tuple[tuple[str, str], ...]]] = []
    # Only durable tasks can carry scheduler dependency edges. Cached issues alone (including CLOSED issues) are not completion proof.
    known_github_ids = {
        str(row[0])
        for row in store.conn.execute(
            "SELECT source_id FROM tasks WHERE source=? AND source_id IS NOT NULL",
            (GitHubIssueSource.name,),
        )
    }
    # Forward references are safe only when this scan will materialize a task row. Objective roots and closed issues do not.
    known_github_ids.update(
        task.source_id
        for task in tasks
        if task.source == GitHubIssueSource.name
        and _normalized_state(task.state) == "OPEN"
        and not source_issue_is_objective(task.labels)
        and (folded_labels(task.labels) & DIRECT_EXECUTION_LABELS or not _objective_exists(store, task.source_id))
    )
    github_validation_errors = _github_validation_errors(tasks, known_github_ids)
    for task in tasks:
        state = _normalized_state(task.state)
        is_github = task.source == GitHubIssueSource.name
        validation_errors = github_validation_errors.get(task.source_id, ()) if is_github else ()
        source_eligible = task.eligible and not validation_errors
        direct_execution = bool(folded_labels(task.labels) & DIRECT_EXECUTION_LABELS)
        source_objective = is_github and source_issue_is_objective(task.labels)
        historical_objective = is_github and not direct_execution and _objective_exists(store, task.source_id)
        cached: dict[str, object] = {"eligible": source_eligible, "state": state}
        if task.selection_scope is not None:
            cached["selection_scope"] = task.selection_scope
        if task.eligibility_reason:
            cached["eligibility_reason"] = task.eligibility_reason
        if validation_errors:
            cached["validation_errors"] = list(validation_errors)
            cached["eligibility_reason"] = f"source validation failed: {'; '.join(validation_errors)}"
        if source_objective or historical_objective:
            cached["objective_root"] = True
        previous_source_state = store.source_state(task.source, task.source_id) if is_github else {}
        retirement_reason = (
            f"source validation failed: {'; '.join(validation_errors)}"
            if validation_errors
            else _retirement_reason(task, state)
        )
        if is_github:
            _record_ready_label_drift(store, task, previous_source_state)
        if source_objective or historical_objective:
            retirement_reason = "source objective root"
            store.save_objective(
                task.source_id,
                task.title,
                objective_payload(task.source_id, task.labels, task.body, task.dependencies),
            )
        materialize_blocked_github_task = (
            is_github
            and state == "OPEN"
            and not source_objective
            and not historical_objective
            and retirement_reason is not None
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
        if task.satisfied_dependencies:
            cached["satisfied_dependencies"] = [
                {"task_id": dependency_id, "kind": "approved_merged_spec", "commit": commit}
                for dependency_id, commit in task.satisfied_dependencies
            ]
        publications.append((task.source, task.source_id, dict(cached), state))
        # Every Store helper can commit independently. Fence readers BEFORE materialization/restoration, and leave
        # the fence durable on failure. Only processed rows are published after the complete dependency phase.
        cached["admission_pending"] = True
        store.cache_source(task.source, task.source_id, cached, state)
        if materialize_blocked_github_task:
            task_id = store.upsert_task(task.title, task.source, task.source_id)
            dependency_tasks.append((task_id, set(task.dependencies), task.satisfied_dependencies))
        if retirement_reason is not None and is_github:
            store.retire_source_task(
                task.source,
                task.source_id,
                retirement_reason,
                {"source_state": state, "eligible": source_eligible, "objective_root": bool(source_objective or historical_objective)},
            )
        elif source_eligible and state == "OPEN":
            task_id = store.upsert_task(task.title, task.source, task.source_id)
            previous_retirement_reason = previous_source_state.get("retirement_reason")
            if not previous_retirement_reason and hasattr(store, "operator_retirement_reason"):
                previous_retirement_reason = store.operator_retirement_reason(task.source, task.source_id)
            if is_github and isinstance(previous_retirement_reason, str):
                store.restore_source_task(
                    task.source,
                    task.source_id,
                    previous_retirement_reason,
                    {"source_state": state, "eligible": task.eligible},
                )
            ids.append(task_id)
            dependency_tasks.append((task_id, set(task.dependencies), task.satisfied_dependencies))
    for task_id, declared_deps, satisfied_deps in dependency_tasks:
        for satisfied_dep, commit in satisfied_deps:
            removed = store.conn.execute(
                "DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?",
                (task_id, satisfied_dep),
            ).rowcount
            if removed:
                store.conn.commit()
                record_audit(
                    store,
                    "task.dependency_satisfied",
                    {
                        "task_id": task_id,
                        "dependency_task_id": satisfied_dep,
                        "kind": "approved_merged_spec",
                        "commit": commit,
                    },
                )
        if hasattr(store, "conn") and not preserve_existing_dependencies:
            rows = store.conn.execute("SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (task_id,)).fetchall()
            existing_deps = {str(r[0]) for r in rows}
            removed_deps = existing_deps - declared_deps
            if removed_deps:
                for removed_dep in removed_deps:
                    store.conn.execute("DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", (task_id, removed_dep))
                store.conn.commit()
        for dependency in declared_deps:
            if store.get_task(dependency) is not None:
                store.add_dependency(task_id, dependency)
    for source, source_id, published, state in publications:
        store.cache_source(source, source_id, published, state)
    return ids


def _github_validation_errors(tasks: list[DiscoveredTask], known_github_ids: set[str]) -> dict[str, tuple[str, ...]]:
    errors: dict[str, tuple[str, ...]] = {}
    blocked_labels = {"stagemesh:blocked", "stagemesh:deferred", "status:blocked", "status:remediating"}
    for task in tasks:
        if task.source != GitHubIssueSource.name:
            continue
        task_errors: list[str] = []
        labels = folded_labels(task.labels)
        if READY_LABEL in labels and labels & blocked_labels:
            task_errors.append("ready label conflicts with a blocked or deferred label")
        if READY_LABEL in labels and source_issue_is_objective(task.labels):
            task_errors.append("ready label conflicts with objective label")
        contract_refusal = source_contract_refusal_reason(task.body)
        if READY_LABEL in labels and contract_refusal is not None:
            task_errors.append(f"ready label conflicts with issue contract: {contract_refusal}")
        if task.source_id in task.dependencies:
            task_errors.append("task depends on itself")
        missing = sorted(set(task.dependencies) - known_github_ids, key=lambda value: int(value) if value.isdigit() else value)
        if missing:
            task_errors.append(f"unknown dependency issue(s): {', '.join('#' + value for value in missing)}")
        if task_errors:
            errors[task.source_id] = tuple(task_errors)
    return errors


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
        if task.eligibility_reason:
            return f"source no longer eligible ({task.eligibility_reason})"
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
    def __init__(self, store: Store, client: GitHubClient, *, approver_id: str | None = None):
        super().__init__(store)
        self.client = client
        self.approver_id = approver_id

    def reconcile_merged(self, task_id: str) -> bool:
        """Observe a StageMesh-published PR merged outside the landing guard.

        Exact published heads need only the already-recorded StageMesh evidence.
        A head changed after publication additionally needs the configured GitHub
        approver and a complete green hosted-check rollup. Every path proves that
        the candidate, merged head, merge commit, and current base form one ancestry
        chain before recording delivery.
        """
        from .delivery_state import (
            pr_url_repository,
            publication_record,
            record_observed_merge,
            task_delivery,
        )

        if task_delivery(self.store, task_id).state == "MERGED":
            return True
        candidate_row = self.store.latest_candidate(task_id)
        if candidate_row is None:
            return False
        candidate = str(candidate_row["sha"])
        repository = f"{self.client.owner}/{self.client.repo}".casefold()
        published = publication_record(self.store, task_id, candidate, repository)
        if published is None or pr_url_repository(str(published.get("pr_url") or "")) != repository:
            return False
        match = re.fullmatch(r"https?://[^/\s]+/[^/\s]+/[^/\s]+/pull/(\d+)(?:[/?#].*)?", str(published.get("pr_url") or ""), re.IGNORECASE)
        if match is None:
            return False
        number = int(match.group(1))
        repository_result = self.client.repository()
        pull_result = self.client.pull_request(number)
        if repository_result.status != "OK" or pull_result.status != "OK":
            return False
        repo, pull = repository_result.payload, pull_result.payload
        if not isinstance(repo, dict) or not isinstance(pull, dict):
            return False
        full_name = str(repo.get("full_name") or "").casefold()
        default_base = str(repo.get("default_branch") or "")
        head, base = pull.get("head") or {}, pull.get("base") or {}
        if (
            full_name != repository
            or pull.get("number") != number
            or pull.get("state") != "closed"
            or pull.get("merged") is not True
            or not pull.get("merged_at")
            or not isinstance(head, dict)
            or not isinstance(base, dict)
        ):
            return False
        for side in (head, base):
            side_repo = side.get("repo")
            if not isinstance(side_repo, dict):
                return False
            if str(side_repo.get("full_name") or "").casefold() != repository:
                return False
        published_base = str(published.get("base") or "")
        expected_base = published_base or default_base
        if not expected_base or str(base.get("ref") or "") != expected_base:
            return False
        merged_head = str(head.get("sha") or "")
        published_head = str(published.get("head_sha") or published.get("candidate_sha") or "")
        merge_commit = str(pull.get("merge_commit_sha") or "")
        if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (candidate, published_head, merged_head, merge_commit)):
            return False
        if not self._contains(candidate, merged_head) or not self._contains(merged_head, merge_commit):
            return False
        branch_result = self.client.branch(expected_base)
        branch_payload = branch_result.payload
        branch_commit = branch_payload.get("commit") if isinstance(branch_payload, dict) else None
        base_tip = str(branch_commit.get("sha") or "") if isinstance(branch_commit, dict) else ""
        if branch_result.status != "OK" or not re.fullmatch(r"[0-9a-f]{40}", base_tip) or not self._contains(merge_commit, base_tip):
            return False
        merged_by_payload = pull.get("merged_by")
        if not isinstance(merged_by_payload, dict):
            return False
        merged_by = str(merged_by_payload.get("login") or "")
        if not merged_by:
            return False
        if merged_head != published_head:
            expected_approver = self._github_approver()
            if not expected_approver or merged_by.casefold() != expected_approver.casefold() or not self._green_checks(merged_head):
                return False
        record_observed_merge(
            self.store,
            task_id,
            candidate,
            repository=repository,
            pr=number,
            published_head_sha=published_head,
            merged_head_sha=merged_head,
            merge_commit_sha=merge_commit,
            base=expected_base,
            merged_by=merged_by,
        )
        return True

    def _github_approver(self) -> str | None:
        prefix, separator, value = str(self.approver_id or "").partition(":")
        return value if separator and prefix.casefold() == "github" and value else None

    def _contains(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        result = self.client.compare_commits(ancestor, descendant)
        payload = result.payload
        if result.status != "OK" or not isinstance(payload, dict):
            return False
        merge_base = payload.get("merge_base_commit")
        return bool(
            isinstance(merge_base, dict)
            and payload.get("status") in {"ahead", "identical"}
            and str(merge_base.get("sha") or "") == ancestor
        )

    def _green_checks(self, head_sha: str) -> bool:
        result = self.client.check_runs(head_sha)
        payload = result.payload
        if result.status != "OK" or not isinstance(payload, dict):
            return False
        runs = payload.get("check_runs")
        total = payload.get("total_count")
        return bool(
            isinstance(runs, list)
            and runs
            and type(total) is int
            and total == len(runs)
            and total <= 100
            and all(
                isinstance(run, dict)
                and run.get("head_sha") == head_sha
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                for run in runs
            )
        )

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
        label = self.client.remove_issue_label(issue_number, BLOCKED_OUTBOUND_LABEL)
        if label.status != "OK":
            retry.record_failure(key, label.status)
            return self.publish(
                "github",
                issue_number,
                label.status,
                {"candidate_sha": candidate_sha, "label": label.status},
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
            {"candidate_sha": candidate_sha, "label": label.status, "comment": comment.status, "close": close.status},
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
        label = _add_issue_labels(self.client, issue_number, (BLOCKED_OUTBOUND_LABEL,))
        bodies: list[str] = []
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            comments = self.client.list_issue_comments(issue_number, page)
            if comments.status != "OK":
                status = label.status if label.status != "OK" else comments.status
                retry.record_failure(key, status)
                return self.publish(
                    "github",
                    issue_number,
                    status,
                    {"reason": bounded, "label": label.status, "comments": comments.status},
                )
            items = comments.payload if isinstance(comments.payload, list) else []
            bodies.extend(str(item.get("body", "")) for item in items if isinstance(item, dict))
            if len(items) < 100 or any(marker in body for body in bodies):
                break
        if any(marker in body for body in bodies):
            status = label.status
            if status == "OK":
                retry.record_success(key)
            else:
                retry.record_failure(key, status)
            return self.publish(
                "github",
                issue_number,
                status,
                {"reason": bounded, "label": label.status, "comment": "DUPLICATE"},
            )
        body = f"StageMesh blocked this task: `{bounded}`.\n\n{marker}"
        comment = self.client.comment_issue(issue_number, body)
        status = (
            "OK"
            if label.status == "OK" and comment.status == "OK"
            else (label.status if label.status != "OK" else comment.status)
        )
        if status == "OK":
            retry.record_success(key)
        else:
            retry.record_failure(key, status)
        return self.publish(
            "github",
            issue_number,
            status,
            {"reason": bounded, "label": label.status, "comment": comment.status},
        )

    def publish_unblocked(self, issue_number: str) -> str:
        """Clear only StageMesh's advisory blocked label when the local task is actionable again."""
        retry = RetryRegistry(self.store)
        key = f"github:{issue_number}:unblocked"
        decision = retry.decision(key)
        if not decision.allowed:
            return self.publish(
                "github",
                issue_number,
                "BACKOFF",
                {"outcome": "UNBLOCKED", "next_attempt_at": decision.next_attempt_at},
            )
        label = self.client.remove_issue_label(issue_number, BLOCKED_OUTBOUND_LABEL)
        if label.status == "OK":
            retry.record_success(key)
        else:
            retry.record_failure(key, label.status)
        return self.publish(
            "github",
            issue_number,
            label.status,
            {"outcome": "UNBLOCKED", "label": label.status},
        )


_MAX_COMMENT_PAGES = 10  # 1000 comments of dedupe history; beyond that a re-block may repeat once
BLOCKED_OUTBOUND_LABEL = "stagemesh:task-blocked"  # additive, and not a source-gating label


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


def _add_issue_labels(
    client: GitHubClient, issue_number: str, labels: tuple[str, ...]
) -> GitHubResult:
    code, headers, payload = client.transport.request(
        "POST",
        f"/repos/{client.owner}/{client.repo}/issues/{issue_number}/labels",
        {"labels": list(labels)},
    )
    return client._result(code, headers, payload)
