"""Project-owned backlog: load `.stagemesh/tasks/` and reconcile it into the queue.

Task *definitions* (objective, requirements, acceptance criteria, dependencies,
priority, review requirements) are version-controlled files. The durable queue
keeps owning *runtime* state; synchronization only creates missing tasks and
refreshes definition fields, and never touches lifecycle state, claims,
leases, executions, checkpoints or evidence.

Synchronization is deterministic (sorted by task id) and idempotent (a task
whose definition already matches is left untouched and emits no event).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.events import record_event
from build_coordinator.execution.git_integrator import push_branch
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.project.definition import (
    ProjectDefinition,
    ProjectError,
    read_yaml,
)
from build_coordinator.policy import normalize_review_policy
from build_coordinator.runner.git_safety import resolve_git_identity_args
from build_coordinator.service import transition_task, upsert_task
from build_coordinator.task_source.base import SyncResult, source_identity_metadata
from build_coordinator.types import EventInput, TaskSpec

SYNC_EVENT = "task.definition_synced"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_REVIEW_POLICIES = (
    "NONE",
    "SELF",
    "INDEPENDENT",
    "INDEPENDENT_WORKER",
    "INDEPENDENT_PROVIDER",
    "TWO_REVIEWERS",
    "TWO_PROVIDERS",
)
_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_KNOWN_KEYS = frozenset(
    {
        "id",
        "title",
        "objective",
        "requirements",
        "acceptance_criteria",
        "dependencies",
        "priority",
        "review",
        "risk",
        "scope",
        "validation",
        "notes",
        "program",
        "migration_allowed",
        "ownership",
        "delivered_by",
        "metadata",
    }
)
_METADATA_MAX_BYTES = 16 * 1024
_SECRET_METADATA_KEY = re.compile(
    r"(secret|token|credential|password|passwd|api[_-]?key|private[_-]?key)", re.IGNORECASE
)
# Definition refreshes are only applied while the task has no live or
# finished work attached; anything else is deferred, never disturbed.
_MUTABLE_STATES = frozenset({"READY", "BLOCKED", "FAILED"})
_FINISHED_STATES = frozenset({"DONE"})


class BacklogError(ProjectError):
    """Raised when the project backlog is structurally invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid project backlog: " + "; ".join(problems))


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    title: str
    objective: str
    requirements: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    priority: int
    review_policy: str
    risk_level: str
    permitted_scope: tuple[str, ...]
    required_validation: tuple[str, ...]
    notes: str | None
    program_key: str
    migration_allowed: bool
    ownership: dict[str, Any] | None
    metadata: dict[str, Any]
    source: str = ""
    source_owner: str = ""
    delivered_by: Any = None

    @property
    def delivered_sha(self) -> str | None:
        if isinstance(self.delivered_by, dict):
            value = self.delivered_by.get("sha") or self.delivered_by.get("integrated_sha")
            return str(value).strip() if value else None
        return None

    def description(self) -> str:
        parts = [self.objective.strip()]
        if self.requirements:
            parts.append("Requirements:\n" + "\n".join(f"- {item}" for item in self.requirements))
        return "\n\n".join(part for part in parts if part)

    def to_spec(self) -> TaskSpec:
        return TaskSpec(
            task_id=self.task_id,
            title=self.title,
            description=self.description(),
            acceptance_criteria=list(self.acceptance_criteria),
            dependencies=list(self.dependencies),
            risk_level=self.risk_level,
            review_policy=self.review_policy,
            permitted_scope=list(self.permitted_scope),
            required_validation=list(self.required_validation),
            implementation_notes=self.notes,
            program_key=self.program_key,
            migration_allowed=self.migration_allowed,
            ownership_scope=self.ownership,  # type: ignore[arg-type]
            definition_metadata=self.source_metadata(),
        )

    def source_metadata(self) -> dict[str, Any]:
        return {
            **self.metadata,
            **source_identity_metadata(
                source_type="local",
                source_owner=self.source_owner,
                source_ref=f"{self.source}:{self.task_id}",
                source_url=self.source,
            ),
        }

    def content_hash(self) -> str:
        metadata = self.source_metadata()
        payload = {
            "spec": {
                "title": self.title,
                "description": self.description(),
                "acceptance_criteria": list(self.acceptance_criteria),
                "dependencies": list(self.dependencies),
                "risk_level": self.risk_level,
                "review_policy": self.review_policy,
                "permitted_scope": list(self.permitted_scope),
                "required_validation": list(self.required_validation),
                "notes": self.notes,
                "program_key": self.program_key,
                "migration_allowed": self.migration_allowed,
                "ownership": self.ownership,
                "metadata": metadata,
                "delivered_by": self.delivered_by,
            },
            "priority": self.priority,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _str_list(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, (str, int, float)):
                out.append(str(item).strip())
            elif isinstance(item, dict) and len(item) == 1:
                k, v = next(iter(item.items()))
                out.append(f"{k}: {v}".strip())
            else:
                problems.append(f"{where} must be a list of strings")
                return ()
        return tuple(s for s in out if s)
    problems.append(f"{where} must be a list of strings")
    return ()


def _metadata(value: Any, where: str, problems: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} must be a mapping")
        return {}
    non_string_keys = _non_string_metadata_key_paths(value)
    if non_string_keys:
        problems.append(f"{where} keys must be strings: {non_string_keys}")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        problems.append(f"{where} must contain only JSON/YAML-safe values")
        return {}
    if len(encoded.encode("utf-8")) > _METADATA_MAX_BYTES:
        problems.append(f"{where} must be at most {_METADATA_MAX_BYTES} bytes when encoded as JSON")
    secret_keys = _secret_metadata_paths(value)
    if secret_keys:
        problems.append(f"{where} must not contain secret or credential keys: {secret_keys}")
    return value


def _non_string_metadata_key_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if not isinstance(key, str):
                paths.append(path)
            paths.extend(_non_string_metadata_key_paths(child, path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_non_string_metadata_key_paths(child, f"{prefix}[{index}]"))
        return paths
    return []


def _secret_metadata_paths(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if _SECRET_METADATA_KEY.search(key_text):
                paths.append(path)
            paths.extend(_secret_metadata_paths(child, path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, child in enumerate(value):
            paths.extend(_secret_metadata_paths(child, f"{prefix}[{index}]"))
        return paths
    return []


def _definition_from_mapping(
    data: dict[str, Any], project: ProjectDefinition, source: str, problems: list[str]
) -> TaskDefinition | None:
    task_id = str(data.get("id") or "").strip()
    where = f"{source}:{task_id or '<no id>'}"
    if not _TASK_ID.match(task_id):
        problems.append(f"{where}: `id` must match ^[A-Za-z0-9][A-Za-z0-9_.-]{{0,79}}$")
        return None
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        problems.append(f"{where}: unknown field(s) {unknown}")
    local: list[str] = []
    title = str(data.get("title") or "").strip()
    if not title or len(title) > 240:
        local.append("`title` is required (max 240 chars)")
    objective = str(data.get("objective") or "").strip()
    if not objective:
        local.append("`objective` is required")
    criteria = _str_list(data.get("acceptance_criteria"), "`acceptance_criteria`", local)
    if not criteria:
        local.append("`acceptance_criteria` needs at least one entry")
    priority = data.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        local.append("`priority` must be an integer (lower runs earlier)")
        priority = 100
    review = normalize_review_policy(str(data.get("review") or project.default_review_policy).upper())
    if review not in _REVIEW_POLICIES:
        local.append(f"`review` must be one of {_REVIEW_POLICIES}")
    risk = str(data.get("risk") or "MEDIUM").upper()
    if risk not in _RISK_LEVELS:
        local.append(f"`risk` must be one of {_RISK_LEVELS}")
    scope = data.get("scope") or {}
    if not isinstance(scope, dict):
        local.append("`scope` must be a mapping")
        scope = {}
    permitted = _str_list(scope.get("allowed_paths"), "`scope.allowed_paths`", local)
    ownership = data.get("ownership")
    if ownership is not None and not isinstance(ownership, dict):
        local.append("`ownership` must be a mapping")
        ownership = None
    notes = data.get("notes")
    metadata = _metadata(data.get("metadata"), "`metadata`", local)
    definition = TaskDefinition(
        task_id=task_id,
        title=title,
        objective=objective,
        requirements=_str_list(data.get("requirements"), "`requirements`", local),
        acceptance_criteria=criteria,
        dependencies=_str_list(data.get("dependencies"), "`dependencies`", local),
        priority=priority,
        review_policy=review,
        risk_level=risk,
        permitted_scope=permitted,
        required_validation=_str_list(data.get("validation"), "`validation`", local),
        notes=str(notes).strip() if notes else None,
        program_key=str(data.get("program") or project.project_id),
        migration_allowed=bool(data.get("migration_allowed", False)),
        ownership=ownership,
        metadata=metadata,
        source=source,
        source_owner=project.project_id,
        delivered_by=data.get("delivered_by"),
    )
    if task_id in definition.dependencies:
        local.append("a task cannot depend on itself")
    problems.extend(f"{where}: {issue}" for issue in local)
    return definition


def load_backlog(project: ProjectDefinition, *, allow_duplicates: bool = False) -> list[TaskDefinition]:
    """Load and validate every definition under `.stagemesh/tasks/`, sorted by id."""
    tasks_dir = project.tasks_dir
    if not tasks_dir.is_dir():
        return []
    problems: list[str] = []
    definitions: dict[str, TaskDefinition] = {}
    all_definitions: list[TaskDefinition] = []
    files = sorted(
        (p for p in tasks_dir.rglob("*") if p.suffix.lower() in {".yaml", ".yml"} and p.is_file()),
        key=lambda p: p.relative_to(tasks_dir).as_posix(),
    )
    for path in files:
        rel = path.relative_to(project.root).as_posix()
        try:
            data = read_yaml(path)
        except ProjectError as exc:
            problems.append(str(exc))
            continue
        entries = data["tasks"] if "tasks" in data and isinstance(data["tasks"], list) else [data]
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append(f"{rel}: each task must be a mapping")
                continue
            definition = _definition_from_mapping(entry, project, rel, problems)
            if definition is None:
                continue
            if definition.task_id in definitions:
                if not allow_duplicates:
                    problems.append(
                        f"duplicate task id {definition.task_id!r} in {rel} "
                        f"and {definitions[definition.task_id].source}"
                    )
                all_definitions.append(definition)
                continue
            definitions[definition.task_id] = definition
            all_definitions.append(definition)

    for definition in definitions.values():
        if len(definition.dependencies) != len(set(definition.dependencies)):
            problems.append(f"{definition.source}:{definition.task_id}: duplicate dependencies")
    problems.extend(_dependency_cycles(definitions))
    if problems:
        raise BacklogError(problems)
    if allow_duplicates:
        return sorted(all_definitions, key=lambda d: (d.task_id, d.source))
    return [definitions[key] for key in sorted(definitions)]


def _dependency_cycles(definitions: dict[str, TaskDefinition]) -> list[str]:
    problems: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        for dep in definitions[node].dependencies:
            if dep not in definitions:
                continue
            if state.get(dep) == 1:
                cycle = path[path.index(dep) :] + [dep] if dep in path else [node, dep]
                problems.append("dependency cycle: " + " -> ".join(cycle))
            elif state.get(dep) is None:
                visit(dep, path + [dep])
        state[node] = 2

    for key in sorted(definitions):
        if state.get(key) is None:
            visit(key, [key])
    return problems


def _matches_definition(task: BuildTask, definition: TaskDefinition) -> bool:
    spec = definition.to_spec()
    return (
        task.title == spec.title
        and task.description == spec.description
        and list(task.acceptance_criteria or []) == spec.acceptance_criteria
        and list(task.dependencies or []) == spec.dependencies
        and task.risk_level == spec.risk_level
        and task.review_policy == spec.review_policy
        and list(task.permitted_scope or []) == spec.permitted_scope
        and list(task.required_validation or []) == spec.required_validation
        and (task.implementation_notes or None) == spec.implementation_notes
        and task.program_key == spec.program_key
        and bool(task.migration_allowed) == spec.migration_allowed
        and dict(task.definition_metadata or {}) == spec.definition_metadata
    )


def _last_sync_event(session: Session, task_id: str) -> BuildTaskEvent | None:
    rows = session.scalars(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id == task_id)
        .where(BuildTaskEvent.event_type == SYNC_EVENT)
    ).all()
    return max(rows, key=_revision, default=None)


def _revision(row: BuildTaskEvent) -> int:
    value = (row.event_data or {}).get("revision")
    return value if isinstance(value, int) else 0


@dataclass
class SyncReport:
    project_id: str
    dry_run: bool
    results: list[SyncResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.action] = counts.get(result.action, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "dry_run": self.dry_run,
            "counts": self.counts(),
            "tasks": [result.as_dict() for result in self.results],
        }


def sync_backlog(
    session: Session,
    project: ProjectDefinition,
    definitions: list[TaskDefinition],
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Reconcile definitions into the durable queue by stable task id.

    Actions: CREATED, UPDATED, ADOPTED (pre-existing identical task now
    tracked), SKIPPED (already in sync), DEFERRED (definition drift on a task
    with live work), FINISHED (definition drift on a DONE task, left as is),
    ORPHANED (previously synced, definition since removed), ERROR.
    """
    report = SyncReport(project.project_id, dry_run)
    known = {d.task_id for d in definitions}

    for definition in sorted(definitions, key=lambda d: d.task_id):
        task = session.get(BuildTask, definition.task_id)
        missing = [
            dep
            for dep in definition.dependencies
            if dep not in known and session.get(BuildTask, dep) is None
        ]
        if missing:
            report.results.append(
                SyncResult(
                    definition.task_id,
                    definition.title,
                    "ERROR",
                    definition.source,
                    f"dependency not defined anywhere: {', '.join(missing)}",
                )
            )
            continue
        if definition.review_policy in {"TWO_REVIEWERS", "TWO_PROVIDERS"} and project.reviewers < 2:
            report.results.append(
                SyncResult(
                    definition.task_id,
                    definition.title,
                    "ERROR",
                    definition.source,
                    f"review {definition.review_policy} needs execution.reviewers >= 2 in project.yaml",
                )
            )
            continue
        digest = definition.content_hash()
        evidence = verify_delivery_evidence(project.root, definition)
        if definition.delivered_by and evidence["status"] == "STALE":
            report.results.append(
                SyncResult(
                    definition.task_id,
                    definition.title,
                    "ERROR",
                    definition.source,
                    evidence["detail"],
                )
            )
            continue
        if definition.delivered_by and (task is None or task.state == "READY"):
            if not dry_run:
                if task is None:
                    upsert_task(session, definition.to_spec())
                    session.flush()
                _reconcile_delivered(session, definition, digest, project)
            report.results.append(
                SyncResult(definition.task_id, definition.title, "RECONCILED", definition.source, f"delivered outside StageMesh: {definition.delivered_by}")
            )
            continue
        if task is None:
            if not dry_run:
                upsert_task(session, definition.to_spec())
                _record(session, definition, digest, "CREATED", project)
            report.results.append(
                SyncResult(definition.task_id, definition.title, "CREATED", definition.source, "queued as READY")
            )
            continue

        last = _last_sync_event(session, definition.task_id)
        in_sync = _matches_definition(task, definition)
        recorded = (last.event_data or {}).get("hash") if last else None
        if in_sync and recorded == digest:
            report.results.append(
                SyncResult(definition.task_id, definition.title, "SKIPPED", definition.source, f"in sync ({task.state})")
            )
            continue
        if in_sync:
            if not dry_run:
                _record(session, definition, digest, "ADOPTED" if last is None else "PRIORITY_UPDATED", project)
            report.results.append(
                SyncResult(
                    definition.task_id,
                    definition.title,
                    "ADOPTED" if last is None else "UPDATED",
                    definition.source,
                    f"tracked by project definition ({task.state})",
                )
            )
            continue
        if task.state in _FINISHED_STATES:
            report.results.append(
                SyncResult(
                    definition.task_id, definition.title, "FINISHED", definition.source,
                    "task already DONE; definition drift ignored",
                )
            )
            continue
        if task.state not in _MUTABLE_STATES:
            report.results.append(
                SyncResult(
                    definition.task_id, definition.title, "DEFERRED", definition.source,
                    f"task is {task.state}; definition refresh deferred to avoid disturbing active work",
                )
            )
            continue
        if not dry_run:
            upsert_task(session, definition.to_spec())
            _record(session, definition, digest, "UPDATED", project)
        report.results.append(
            SyncResult(definition.task_id, definition.title, "UPDATED", definition.source, f"definition refreshed ({task.state})")
        )

    for task_id in _previously_synced_ids(session, project.project_id) - known:
        task = session.get(BuildTask, task_id)
        if task is not None:
            report.results.append(
                SyncResult(task_id, task.title, "ORPHANED", "", f"no longer defined; left in {task.state}")
            )
    if not dry_run:
        session.flush()
    report.results.sort(key=lambda r: (r.task_id, r.action))
    return report


def _reconcile_delivered(session: Session, definition: TaskDefinition, digest: str, project: ProjectDefinition) -> None:
    """Close a task whose work landed outside StageMesh, without pretending it ran
    through the lifecycle: every step is attributed to the sync and carries the
    delivering reference, and no execution or review evidence is invented."""
    reason = f"DELIVERED_OUTSIDE_STAGEMESH: {definition.delivered_by}"
    _record(session, definition, digest, "RECONCILED", project)
    for state in ("CLAIMED", "IN_PROGRESS", "VALIDATING", "DONE"):
        transition_task(session, definition.task_id, state, actor="project-sync", reason=reason)


def _record(
    session: Session, definition: TaskDefinition, digest: str, action: str, project: ProjectDefinition
) -> None:
    session.flush()
    previous = _last_sync_event(session, definition.task_id)
    record_event(
        session,
        EventInput(
            task_id=definition.task_id,
            event_type=SYNC_EVENT,
            actor="project-sync",
            event_data={
                "revision": (_revision(previous) + 1) if previous is not None else 1,
                "project_id": project.project_id,
                "hash": digest,
                "priority": definition.priority,
                "source": definition.source,
                "action": action,
            },
        ),
    )


def _previously_synced_ids(session: Session, project_id: str) -> set[str]:
    rows = session.scalars(
        select(BuildTaskEvent).where(BuildTaskEvent.event_type == SYNC_EVENT)
    ).all()
    return {
        row.task_id
        for row in rows
        if row.task_id and (row.event_data or {}).get("project_id") == project_id
    }


def task_priorities(session: Session, task_ids: list[str]) -> dict[str, int]:
    """Declared priority (lower runs earlier) for tasks synced from a project."""
    if not task_ids:
        return {}
    rows = session.scalars(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.event_type == SYNC_EVENT)
        .where(BuildTaskEvent.task_id.in_(task_ids))
    ).all()
    priorities: dict[str, int] = {}
    for row in sorted(rows, key=_revision):
        value = (row.event_data or {}).get("priority")
        if row.task_id and isinstance(value, int):
            priorities[row.task_id] = value
    return priorities


def verify_delivery_evidence(repo_root: Path, definition: TaskDefinition) -> dict[str, Any]:
    """Verify structured delivery evidence against authoritative git history.

    Legacy string `delivered_by` declarations remain compatible. They are
    accepted only when the exact declaration is already present in committed
    repository history. They cannot verify a delivery SHA until migrated to the
    structured form written by `persist_delivery_evidence`.
    """
    if not definition.delivered_by:
        return {"status": "MISSING", "detail": "no delivered_by evidence"}
    committed = _committed_delivered_by(repo_root, definition)
    if committed != definition.delivered_by:
        return {
            "status": "STALE",
            "detail": "delivered_by evidence is not present in committed repository history",
        }
    if not isinstance(definition.delivered_by, dict):
        return {"status": "LEGACY", "detail": str(definition.delivered_by)}
    sha = definition.delivered_sha
    if not sha:
        return {"status": "STALE", "detail": "structured delivered_by is missing sha"}
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"status": "STALE", "detail": f"delivery sha {sha} is not present in repository history"}
    contains = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if contains.returncode != 0:
        return {"status": "STALE", "detail": f"delivery sha {sha} is not reachable from HEAD"}
    return {"status": "VERIFIED", "detail": f"delivered by {sha}", "sha": sha}


def _committed_delivered_by(repo_root: Path, definition: TaskDefinition) -> Any:
    if not definition.source:
        return None
    proc = subprocess.run(
        ["git", "show", f"HEAD:{definition.source}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        data = yaml.safe_load(proc.stdout) or {}
    except yaml.YAMLError:
        return None
    entries = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else [data]
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == definition.task_id:
            return entry.get("delivered_by")
    return None


def persist_delivery_evidence(
    repo_root: Path,
    definitions: list[TaskDefinition],
    task_id: str,
    *,
    sha: str,
    version: str | None = None,
) -> bool:
    definition = next((item for item in definitions if item.task_id == task_id), None)
    if definition is None or not definition.source:
        return False
    path = repo_root / definition.source
    if not path.is_file():
        return False
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else [data]
    changed = False
    for entry in tasks:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == task_id:
            current = entry.get("delivered_by")
            if isinstance(current, dict) and (current.get("sha") == sha or current.get("integrated_sha") == sha):
                return False
            evidence: dict[str, Any] = {"sha": sha}
            if version:
                evidence["version"] = version
            entry["delivered_by"] = evidence
            changed = True
            break
    if not changed:
        return False
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return True


def _matches_delivery_evidence(evidence: Any, sha: str, version: str | None = None) -> bool:
    if not isinstance(evidence, dict):
        return False
    if evidence.get("sha") != sha and evidence.get("integrated_sha") != sha:
        return False
    if version and evidence.get("version") != version:
        return False
    return True


def persist_delivery_evidence_in_history(
    repo_root: Path,
    definitions: list[TaskDefinition],
    task_id: str,
    *,
    sha: str,
    version: str | None = None,
    push_remote: str | None = None,
    push_branch_name: str = "main",
    expected_remote_url: str | None = None,
) -> dict[str, Any]:
    """Persist delivery evidence and commit it to repository history.

    `delivered_by` remains the single authoritative representation. This
    helper makes the YAML update durable across fresh coordinator databases,
    schema migrations, and clones by recording the update in git history.
    """
    definition = next((item for item in definitions if item.task_id == task_id), None)
    if definition is None or not definition.source:
        return {"status": "NOT_PROJECT_BACKLOG_TASK", "changed": False}

    committed = _committed_delivered_by(repo_root, definition)
    committed_durable = _matches_delivery_evidence(committed, sha, version)

    if committed_durable:
        evidence_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if push_remote:
            ok, detail = push_branch(
                repo_root,
                push_remote,
                evidence_commit,
                push_branch_name,
                expected_remote_url=expected_remote_url,
            )
            if not ok:
                return {
                    "status": "PUSH_FAILED",
                    "changed": False,
                    "sha": sha,
                    "evidence_commit": evidence_commit,
                    "source": definition.source,
                    "push_status": "FAILED",
                    "detail": detail,
                }
            is_up_to_date = "Everything up-to-date" in detail
            return {
                "status": "UNCHANGED" if is_up_to_date else "COMMITTED",
                "changed": not is_up_to_date,
                "sha": sha,
                "evidence_commit": evidence_commit,
                "source": definition.source,
                "push_status": "PUSHED",
            }
        return {
            "status": "UNCHANGED",
            "changed": False,
            "sha": sha,
            "evidence_commit": evidence_commit,
            "source": definition.source,
        }

    persist_delivery_evidence(repo_root, definitions, task_id, sha=sha, version=version)

    source_path = repo_root / definition.source
    rel_path = str(source_path.relative_to(repo_root))
    subprocess.run(["git", "add", "--", rel_path], cwd=str(repo_root), capture_output=True, text=True, check=True)
    identity_args = resolve_git_identity_args(repo_root)
    commit = subprocess.run(
        [
            "git",
            *identity_args,
            "commit",
            "-m",
            f"Record delivery evidence for {task_id}",
            "--",
            rel_path,
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        return {
            "status": "COMMIT_FAILED",
            "changed": True,
            "detail": (commit.stderr or commit.stdout).strip()[-400:],
        }
    evidence_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result: dict[str, Any] = {
        "status": "COMMITTED",
        "changed": True,
        "sha": sha,
        "evidence_commit": evidence_commit,
        "source": definition.source,
    }
    if push_remote:
        ok, detail = push_branch(
            repo_root,
            push_remote,
            evidence_commit,
            push_branch_name,
            expected_remote_url=expected_remote_url,
        )
        result["push_status"] = "PUSHED" if ok else "FAILED"
        if not ok:
            result["status"] = "PUSH_FAILED"
            result["detail"] = detail
    return result


def audit_delivery_evidence(session: Session, project: ProjectDefinition, definitions: list[TaskDefinition]) -> dict[str, Any]:
    seen: set[str] = set()
    tasks: list[dict[str, Any]] = []
    duplicates: set[str] = set()
    for definition in definitions:
        if definition.task_id in seen:
            duplicates.add(definition.task_id)
        seen.add(definition.task_id)
    for definition in definitions:
        task = session.get(BuildTask, definition.task_id)
        evidence = verify_delivery_evidence(project.root, definition)
        if definition.task_id in duplicates:
            status = "SUPERSEDED"
        elif evidence["status"] == "STALE":
            status = "SUPERSEDED"
        elif definition.delivered_by:
            status = "DELIVERED_WITH_EVIDENCE"
        elif task is not None and task.state == "DONE":
            status = "DELIVERED_MISSING_LEDGER_ENTRY"
        else:
            status = "STILL_OPEN"
        tasks.append(
            {
                "task_id": definition.task_id,
                "title": definition.title,
                "status": status,
                "source": definition.source,
                "runtime_state": task.state if task is not None else None,
                "evidence": evidence,
            }
        )
    counts: dict[str, int] = {}
    for row in tasks:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"project_id": project.project_id, "counts": dict(sorted(counts.items())), "tasks": tasks}
