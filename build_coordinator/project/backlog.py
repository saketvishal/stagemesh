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
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.events import record_event
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.project.definition import (
    ProjectDefinition,
    ProjectError,
    read_yaml,
)
from build_coordinator.service import upsert_task
from build_coordinator.task_source.base import SyncResult
from build_coordinator.types import EventInput, TaskSpec

SYNC_EVENT = "task.definition_synced"
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_REVIEW_POLICIES = ("NONE", "SELF", "INDEPENDENT", "TWO_REVIEWERS")
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
    }
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
    source: str = ""

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
        )

    def content_hash(self) -> str:
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
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return tuple(str(item).strip() for item in value if str(item).strip())
    problems.append(f"{where} must be a list of strings")
    return ()


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
    review = str(data.get("review") or project.default_review_policy).upper()
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
        source=source,
    )
    if task_id in definition.dependencies:
        local.append("a task cannot depend on itself")
    problems.extend(f"{where}: {issue}" for issue in local)
    return definition


def load_backlog(project: ProjectDefinition) -> list[TaskDefinition]:
    """Load and validate every definition under `.stagemesh/tasks/`, sorted by id."""
    tasks_dir = project.tasks_dir
    if not tasks_dir.is_dir():
        return []
    problems: list[str] = []
    definitions: dict[str, TaskDefinition] = {}
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
                problems.append(
                    f"duplicate task id {definition.task_id!r} in {rel} "
                    f"and {definitions[definition.task_id].source}"
                )
                continue
            definitions[definition.task_id] = definition

    for definition in definitions.values():
        if len(definition.dependencies) != len(set(definition.dependencies)):
            problems.append(f"{definition.source}:{definition.task_id}: duplicate dependencies")
    problems.extend(_dependency_cycles(definitions))
    if problems:
        raise BacklogError(problems)
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
        digest = definition.content_hash()
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
