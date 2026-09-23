"""CLI handlers for project discovery, backlog sync and `continue`."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from build_coordinator.db import DatabaseSchemaError, SessionLocal, configure_process_database
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskEvent
from build_coordinator.project.backlog import (
    TaskDefinition,
    load_backlog,
    sync_backlog,
    task_priorities,
)
from build_coordinator.project.definition import (
    ProjectDefinition,
    ProjectError,
    parse_continue_phrase,
    register_project,
    registered_projects,
    registry_path,
    resolve_project,
)
from build_coordinator.project.runtime import (
    apply_project_environment,
    build_runner_config,
    require_git_repo,
)
from build_coordinator.project.state_migration import migrate_state, sqlite_path_from_url
from build_coordinator.runner import BuildRunner
from build_coordinator.service import list_available_tasks

_LIVE_EXECUTION = ("LAUNCHED", "RUNNING")
_BUILD_ROLES = ("BUILDER", "REMEDIATION")


def add_project_commands(sub: argparse._SubParsersAction) -> None:
    project = sub.add_parser("project", help="Discover and operate project-owned .stagemesh/ backlogs")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("name", nargs="*", help="project name/alias (default: project above cwd)")
        parser.add_argument("--project-dir", help="explicit project directory")

    discover = project_sub.add_parser("discover", help="resolve a project and validate its backlog")
    common(discover)
    show = project_sub.add_parser("show", help="print the resolved project definition")
    common(show)
    listing = project_sub.add_parser("list", help="list registered projects")
    listing.add_argument("--project-dir", help=argparse.SUPPRESS)
    register = project_sub.add_parser("register", help="register a project root for name lookup")
    register.add_argument("path")
    sync = project_sub.add_parser("sync", help="reconcile .stagemesh/tasks/ into the durable queue")
    common(sync)
    sync.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    status = project_sub.add_parser("status", help="queue and execution state for the project")
    common(status)
    migrate = project_sub.add_parser(
        "migrate-state",
        help="explicitly migrate durable state written by an older coordinator (backup first)",
    )
    common(migrate)
    migrate.add_argument("--apply", action="store_true", help="perform the migration (default: report only)")


def add_continue_command(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "continue",
        help="Continue a project's development from its .stagemesh/ backlog",
        description='Also accepts the phrase form: stagemesh "Continue <project> development."',
    )
    p.add_argument("target", nargs="*", help="project name (optional inside a project directory)")
    p.add_argument("--project-dir", help="explicit project directory")
    p.add_argument("--dry-run", action="store_true", help="plan only: no writes, no execution")
    p.add_argument("--once", action="store_true", help="run a single orchestration cycle")
    p.add_argument("--max-cycles", type=int, default=2000)
    p.add_argument("--timeout", type=float, default=None, help="wall-clock limit in seconds")
    p.add_argument("--no-sync", action="store_true", help="skip project backlog synchronization")
    p.add_argument("--github", action="store_true", help="also run the optional GitHub task-source adapter")


def normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite "Continue <project> development." into `continue <project>`."""
    if len(argv) < 2:
        return argv
    head = argv[1].split()
    if head and head[0].rstrip(".!").lower() == "continue":
        return [argv[0], "continue", *head[1:], *argv[2:]]
    return argv


def _project_from_args(args: argparse.Namespace) -> ProjectDefinition:
    words = list(getattr(args, "name", None) or getattr(args, "target", None) or [])
    query = parse_continue_phrase(["continue", *words]) if words else ""
    return resolve_project(query or None, path=getattr(args, "project_dir", None))


def _open(project: ProjectDefinition):
    require_git_repo(project)
    apply_project_environment(project)
    lifecycle = configure_process_database()
    try:
        lifecycle.initialize_schema()
    except DatabaseSchemaError as exc:
        raise ProjectError(
            f"{exc} Run `stagemesh project migrate-state {project.project_id}` to review an "
            "explicit, backup-first migration of this project's durable history."
        ) from exc
    return lifecycle


def handle_project(args: argparse.Namespace) -> None:
    command = args.project_command
    if command == "list":
        _print(
            {
                "registry": str(registry_path()),
                "projects": [p.summary() for p in registered_projects()],
            }
        )
        return
    if command == "register":
        project = register_project(args.path)
        _print({"registered": project.summary(), "registry": str(registry_path())})
        return
    project = _project_from_args(args)
    if command == "show":
        _print(project.summary())
        return
    if command == "discover":
        definitions = load_backlog(project)
        _print({**project.summary(), "task_definitions": [_definition_summary(d) for d in definitions]})
        return
    if command == "migrate-state":
        require_git_repo(project)
        apply_project_environment(project)
        from build_coordinator.config import get_settings

        report = migrate_state(sqlite_path_from_url(get_settings().database_url), apply=args.apply)
        _print(report.as_dict())
        return
    lifecycle = _open(project)
    with lifecycle.session() as session:
        if command == "sync":
            report = sync_backlog(session, project, load_backlog(project), dry_run=args.dry_run)
            session.commit()
            _print(report.as_dict())
        elif command == "status":
            _print(project_status(session, project))


def _definition_summary(definition: TaskDefinition) -> dict[str, Any]:
    return {
        "id": definition.task_id,
        "title": definition.title,
        "priority": definition.priority,
        "review": definition.review_policy,
        "dependencies": list(definition.dependencies),
        "source": definition.source,
    }


def project_status(session, project: ProjectDefinition) -> dict[str, Any]:
    tasks = session.scalars(select(BuildTask).order_by(BuildTask.task_id)).all()
    priorities = task_priorities(session, [t.task_id for t in tasks])
    available = {t.task_id for t in list_available_tasks(session)}
    executions = session.scalars(
        select(BuildRunnerExecution).order_by(BuildRunnerExecution.launched_at)
    ).all()
    routing_by_claim = {
        event.claim_id: (event.event_data or {}).get("routing")
        for event in session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.execution_launched")
        )
        if event.claim_id
    }
    by_state: dict[str, int] = {}
    for task in tasks:
        by_state[task.state] = by_state.get(task.state, 0) + 1
    return {
        "project_id": project.project_id,
        "state_dir": str(project.state_dir),
        "concurrency": project.concurrency,
        "tasks_by_state": dict(sorted(by_state.items())),
        "tasks": [
            {
                "task_id": t.task_id,
                "state": t.state,
                "priority": priorities.get(t.task_id),
                "review_policy": t.review_policy,
                "dependencies": list(t.dependencies or []),
                "claimable_now": t.task_id in available,
            }
            for t in tasks
        ],
        "executions": [
            {
                "execution_id": e.execution_id,
                "task_id": e.task_id,
                "role": e.role,
                "worker_id": e.worker_id,
                "provider": e.provider,
                "worktree_path": e.worktree_path,
                "status": e.status,
                "escalation": e.human_escalation_type,
                "routing": routing_by_claim.get(e.claim_id),
            }
            for e in executions
        ],
    }


def handle_continue(args: argparse.Namespace) -> None:
    project = _project_from_args(args)
    definitions = [] if args.no_sync else load_backlog(project)
    lifecycle = _open(project)

    sync_payload: dict[str, Any] | None = None
    adapter_payload: list[dict[str, Any]] = []
    task_source = _optional_task_source(project, force=args.github, dry_run=args.dry_run)
    with lifecycle.session() as session:
        if not args.no_sync:
            report = sync_backlog(session, project, definitions, dry_run=args.dry_run)
            sync_payload = report.as_dict()
        if task_source is not None:
            try:
                adapter_payload = [r.as_dict() for r in task_source.discover_tasks(session)]
            except Exception as exc:  # optional adapter: never blocks local execution
                adapter_payload = [{"action": "ERROR", "details": str(exc)}]
        session.commit()
        if args.dry_run:
            _print(_dry_run_plan(session, project, definitions, sync_payload))
            return

    config = build_runner_config(project, dry_run=False)
    runner = BuildRunner(SessionLocal, config)
    started = time.monotonic()
    cycles: list[dict[str, Any]] = []
    peak_parallel = 0
    idle_cycles = 0
    for number in range(1, max(1, args.max_cycles) + 1):
        result = runner.run_once()
        with lifecycle.session() as session:
            live = session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.status.in_(_LIVE_EXECUTION))
            ).all()
            live_builders = [e for e in live if e.role in _BUILD_ROLES]
            peak_parallel = max(peak_parallel, len(live_builders))
            launched = [
                _execution_row(session.get(BuildRunnerExecution, execution_id))
                for execution_id in result.launched
            ]
        cycles.append(
            {
                "cycle": number,
                "launched": launched,
                "observed": list(result.observed),
                "recovered": list(result.recovered),
                "escalations": list(result.escalations),
                "live_builders": len(live_builders),
                "live_executions": len(live),
            }
        )
        if args.once:
            break
        if not live and not result.launched:
            idle_cycles += 1
            if idle_cycles >= 2:
                break
        else:
            idle_cycles = 0
        if args.timeout is not None and time.monotonic() - started > args.timeout:
            break
        time.sleep(config.poll_seconds)

    with lifecycle.session() as session:
        final = project_status(session, project)
    _print(
        {
            "project": project.summary(),
            "backlog_sync": sync_payload,
            "task_source_adapters": adapter_payload,
            "cycles_run": len(cycles),
            "peak_parallel_builders": peak_parallel,
            "cycles": cycles,
            "final": final,
        }
    )


def _optional_task_source(project: ProjectDefinition, *, force: bool, dry_run: bool):
    github = project.task_sources.get("github")
    if github is None and not force:
        return None
    if github is not None and not github.get("enabled", False) and not force:
        return None
    from build_coordinator.task_source import get_task_source

    return get_task_source({"type": "github", **(github or {}), "dry_run": dry_run})


def _execution_row(execution: BuildRunnerExecution | None) -> dict[str, Any]:
    if execution is None:
        return {}
    return {
        "execution_id": execution.execution_id,
        "task_id": execution.task_id,
        "role": execution.role,
        "worker_id": execution.worker_id,
        "provider": execution.provider,
        "worktree_path": execution.worktree_path,
    }


def _dry_run_plan(
    session,
    project: ProjectDefinition,
    definitions: list[TaskDefinition],
    sync_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    ranked: dict[str, tuple[int, str]] = {}
    declared = {d.task_id: d for d in definitions}
    stored = task_priorities(session, [t.task_id for t in list_available_tasks(session)])
    for task in list_available_tasks(session):
        ranked[task.task_id] = (declared[task.task_id].priority if task.task_id in declared else stored.get(task.task_id, 100), task.task_id)
    for definition in definitions:
        if session.get(BuildTask, definition.task_id) is not None:
            continue
        deps_done = all(
            (dep_task := session.get(BuildTask, dep)) is not None and dep_task.state == "DONE"
            for dep in definition.dependencies
        )
        if deps_done:
            ranked[definition.task_id] = (definition.priority, definition.task_id)
    ordered = [task_id for _, task_id in sorted(ranked.values())]
    return {
        "dry_run": True,
        "project": project.summary(),
        "backlog_sync": sync_payload,
        "eligible_now": ordered,
        "would_run_in_parallel": ordered[: project.concurrency],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


__all__ = [
    "add_continue_command",
    "add_project_commands",
    "handle_continue",
    "handle_project",
    "normalize_argv",
]
