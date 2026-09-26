"""CLI handlers for project discovery, backlog sync and `continue`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
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
    find_project_root,
    parse_continue_phrase,
    machine_environment,
    register_project,
    registered_projects,
    set_machine_environment,
    unregister_project,
    registry_path,
    resolve_project,
)
from build_coordinator.project.runtime import (
    apply_project_environment,
    build_runner_config,
    require_git_repo,
)
from build_coordinator.project.state_migration import (
    migrate_state,
    reconcile_stale_executions,
    sqlite_path_from_url,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.upstream import retry_pending_pushes
from build_coordinator.service import ensure_state, list_available_tasks, set_mode

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
    register = project_sub.add_parser("register", aliases=["add"], help="register a project root for name lookup")
    register.add_argument("path", nargs="?", default=".")
    register.add_argument("--path-prepend", action="append", default=[], help="machine-local directory to put first on PATH for this project's runs (e.g. a virtualenv's Scripts/bin)")
    register.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="machine-local environment variable for this project's runs")
    remove = project_sub.add_parser("remove", help="forget a registered project (its repository is untouched)")
    remove.add_argument("project", help="project name, alias or path")
    sync = project_sub.add_parser("sync", help="reconcile .stagemesh/tasks/ into the durable queue")
    common(sync)
    sync.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    status = project_sub.add_parser("status", help="queue and execution state for the project")
    common(status)
    retry = project_sub.add_parser("retry-push", help="retry delivery of work integrated locally but not yet pushed")
    common(retry)
    migrate = project_sub.add_parser(
        "migrate-state",
        help="explicitly migrate durable state written by an older coordinator (backup first)",
    )
    common(migrate)
    migrate.add_argument("--apply", action="store_true", help="perform the migration (default: report only)")
    migrate.add_argument("--all", action="store_true", dest="all_projects", help="inventory/migrate every registered project")


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
    p.add_argument("--timeout", type=float, default=None, help="stop starting new work after this many seconds and let in-flight work finish")
    p.add_argument("--no-sync", action="store_true", help="skip project backlog synchronization")
    p.add_argument("--github", action="store_true", help="also run the optional GitHub task-source adapter")
    p.add_argument("--all", action="store_true", dest="all_projects", help="coordinate every registered project (default outside a project)")
    p.add_argument("--task", dest="task_id", help="run only this explicit task id; unrelated backlog is not claimable")
    p.add_argument("--json", action="store_true", help="print the complete structured result instead of the concise human summary")


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


def _apply_machine_environment(project: ProjectDefinition) -> None:
    extra = machine_environment(project.root)
    for key, value in extra["env"].items():
        if key.startswith("BUILD_COORDINATOR_") and key != "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED":
            continue
        os.environ[key] = value
    if extra["path_prepend"]:
        os.environ["PATH"] = os.pathsep.join([*extra["path_prepend"], os.environ.get("PATH", "")])


def _open(project: ProjectDefinition):
    require_git_repo(project)
    _apply_machine_environment(project)
    apply_project_environment(project)
    lifecycle = configure_process_database()
    try:
        lifecycle.initialize_schema()
    except DatabaseSchemaError as exc:
        db_path = _project_default_sqlite_path(project)
        migrated = _migrate_database(db_path, apply=True)
        if migrated.get("outcome") == "applied":
            lifecycle.initialize_schema()
        else:
            raise ProjectError(
                f"{exc} Run `stagemesh project migrate-state --all` to review an "
                "explicit, backup-first migration of registered durable history."
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
    if command == "remove":
        removed = unregister_project(args.project)
        _print({"removed": [str(p) for p in removed], "registry": str(registry_path())})
        return
    if command in ("register", "add"):
        project = register_project(args.path)
        if args.env or args.path_prepend:
            pairs = dict(item.split("=", 1) for item in args.env)
            set_machine_environment(project.root, env=pairs, path_prepend=[str(Path(p).resolve()) for p in args.path_prepend])
        _print({"registered": project.summary(), "registry": str(registry_path()), "machine_environment": machine_environment(project.root)})
        return
    if command == "migrate-state" and args.all_projects:
        _print(_migrate_registered_projects(apply=args.apply))
        return
    project = _project_from_args(args)
    if command == "show":
        _print(project.summary())
        return
    if command == "discover":
        definitions = load_backlog(project)
        _print({**project.summary(), "task_definitions": [_definition_summary(d) for d in definitions]})
        return
    if command == "retry-push":
        if not project.push_upstream:
            raise ProjectError(f"{project.project_id} has no upstream push configured (project.yaml: upstream)")
        lifecycle = _open(project)
        with lifecycle.session() as session:
            outcomes = retry_pending_pushes(
                session, repo_root=project.root, remote=project.upstream_remote, main_ref=project.main_ref, actor="human:retry-push"
            )
            session.commit()
        _print({"retried": outcomes})
        return
    if command == "migrate-state":
        require_git_repo(project)
        apply_project_environment(project)
        from build_coordinator.config import get_settings

        db_path = sqlite_path_from_url(get_settings().database_url)
        _print(_migrate_database(db_path, apply=args.apply))
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


def _migrate_database(db_path: Path, *, apply: bool) -> dict[str, Any]:
    report = migrate_state(db_path, apply=apply)
    reconciliation = None
    if report.refused and "live execution" in report.refused:
        reconciliation = reconcile_stale_executions(db_path, apply=apply)
        if apply and reconciliation.applied and not reconciliation.refused:
            report = migrate_state(db_path, apply=True)
    return {
        "migration": report.as_dict(),
        "stale_execution_reconciliation": reconciliation.as_dict() if reconciliation else report.stale_execution_reconciliation,
        "outcome": "refused" if report.refused else "applied" if report.applied else "ready" if not report.needed else "planned",
    }


def _project_default_sqlite_path(project: ProjectDefinition) -> Path:
    return project.state_dir / "coordinator.sqlite3"


def _migrate_registered_projects(*, apply: bool) -> dict[str, Any]:
    projects = registered_projects()
    results: dict[str, Any] = {}
    for project in projects:
        try:
            require_git_repo(project)
            db_path = _project_default_sqlite_path(project)
            results[project.project_id] = {
                "project": project.summary(),
                **_migrate_database(db_path, apply=apply),
            }
        except Exception as exc:
            results[project.project_id] = {
                "project": project.summary(),
                "outcome": "error",
                "error": str(exc),
            }
    return {
        "mode": "global",
        "apply": apply,
        "target_schema_version": __import__("build_coordinator.db", fromlist=["CURRENT_SCHEMA_VERSION"]).CURRENT_SCHEMA_VERSION,
        "projects": results,
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
        "blocked": _blocked_reasons(session),
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


def _blocked_reasons(session) -> list[dict[str, Any]]:
    """Tasks waiting for a human, with the reason StageMesh recorded."""
    rows = []
    for task in session.scalars(select(BuildTask).where(BuildTask.state == "BLOCKED").order_by(BuildTask.task_id)):
        event = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task.task_id)
            .where(BuildTaskEvent.to_state == "BLOCKED")
            .order_by(BuildTaskEvent.created_at.desc())
        ).first()
        rows.append({"task_id": task.task_id, "reason": (event.event_data or {}).get("reason") if event else None})
    return rows


def handle_continue(args: argparse.Namespace) -> None:
    if _is_global_mode(args):
        handle_global_continue(args)
        return
    target_task_ids = _target_task_ids(args)
    project = _project_from_args(args)
    definitions = [] if args.no_sync else load_backlog(project)
    lifecycle = _open(project)

    sync_payload: dict[str, Any] | None = None
    task_source, initial_diagnostics = _optional_task_source(project, force=args.github, dry_run=args.dry_run)
    adapter_payload: list[dict[str, Any]] = list(initial_diagnostics)
    with lifecycle.session() as session:
        if not args.no_sync:
            report = sync_backlog(session, project, definitions, dry_run=args.dry_run)
            sync_payload = report.as_dict()
        if task_source is not None:
            try:
                adapter_payload.extend([r.as_dict() for r in task_source.discover_tasks(session)])
            except Exception as exc:  # optional adapter: never blocks local execution
                adapter_payload.append({"source": "github", "action": "ERROR", "details": str(exc)})
        if args.dry_run:
            _print(
                _dry_run_plan(
                    session,
                    project,
                    definitions,
                    sync_payload,
                    target_task_ids=target_task_ids,
                    adapter_payload=adapter_payload,
                )
            )
            session.rollback()
            return
        session.commit()

    config = build_runner_config(project, dry_run=False)
    if not any(w.role == "BUILDER" and w.adapter != "unconfigured" for w in config.workers):
        print(
            f"[{project.project_id}] EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED: "
            f"No executable builders are configured for {project.project_id}. "
            f"Run 'stagemesh agent setup' to verify installed agent runtimes.",
            file=sys.stderr,
            flush=True,
        )
        return
    runner = BuildRunner(SessionLocal, config, task_source=task_source, target_task_ids=target_task_ids)
    started = time.monotonic()
    cycles: list[dict[str, Any]] = []
    peak_parallel = 0
    idle_cycles = 0
    drained_from: str | None = None
    last_escalations: dict[str, str] = {}
    for number in range(1, max(1, args.max_cycles) + 1):
        project, config = _reload_project_runtime(project, lifecycle, runner)
        if args.timeout is not None and drained_from is None and time.monotonic() - started > args.timeout:
            with lifecycle.session() as session:
                state = ensure_state(session)
                drained_from = state.mode
                if drained_from == "RUNNING":
                    set_mode(session, "DRAINING")
                session.commit()
            print(f"[{project.project_id}] time budget reached: finishing in-flight work, starting nothing new", file=sys.stderr, flush=True)
        if project.push_upstream and (number == 1 or number % 10 == 0):
            with lifecycle.session() as session:
                retry_pending_pushes(
                    session,
                    repo_root=project.root,
                    remote=project.upstream_remote,
                    main_ref=project.main_ref,
                )
                session.commit()
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
        for row in launched:
            print(f"[{project.project_id}] {row.get('role', '?').lower()} {row.get('task_id')} on {row.get('worker_id')}", file=sys.stderr, flush=True)
        for item in result.escalations:
            gate_id, _, reason = item.partition(":")
            if last_escalations.get(gate_id) != reason:
                print(f"[{project.project_id}] needs attention: {item}", file=sys.stderr, flush=True)
                last_escalations[gate_id] = reason
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
        time.sleep(config.poll_seconds)
    if drained_from == "RUNNING":
        with lifecycle.session() as session:
            set_mode(session, "RUNNING")
            session.commit()

    with lifecycle.session() as session:
        final = project_status(session, project)
        target_diagnostics = [
            _target_task_diagnostic(session, task_id) for task_id in sorted(target_task_ids)
        ]
    payload = {
        "project": project.summary(),
        "target_tasks": target_diagnostics,
        "backlog_sync": sync_payload,
        "task_source_adapters": adapter_payload,
        "cycles_run": len(cycles),
        "peak_parallel_builders": peak_parallel,
        "cycles": cycles,
        "final": final,
    }
    if getattr(args, "json", False):
        _print(payload)
    else:
        print(_continue_human_summary(project, cycles, final, last_escalations), flush=True)


def _reload_project_runtime(
    project: ProjectDefinition,
    lifecycle,
    runner: BuildRunner,
) -> tuple[ProjectDefinition, Any]:
    """Refresh project.yaml-derived runtime state between cycles only."""
    project = resolve_project(None, path=project.root)
    apply_project_environment(project)
    config = build_runner_config(project, dry_run=False)
    with lifecycle.session() as session:
        live_worker_ids = {
            row.worker_id
            for row in session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.status.in_(_LIVE_EXECUTION))
            )
        }
    runner.reload_config(config, live_worker_ids=live_worker_ids)
    return project, config


_ESCALATION_LABELS = {
    "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED": "External executor configuration required",
}


def _escalation_label(reason: str) -> str:
    return _ESCALATION_LABELS.get(reason, reason.replace("_", " ").capitalize())


def _continue_human_summary(
    project: ProjectDefinition,
    cycles: list[dict[str, Any]],
    final: dict[str, Any],
    outstanding_escalations: dict[str, str] | None = None,
) -> str:
    """A bounded, operator-facing summary: current state and what needs attention,
    never a dump of historical execution/routing evidence (that's `--json`)."""
    live_by_task = {
        e["task_id"]: e
        for e in final.get("executions", [])
        if e.get("status") in _LIVE_EXECUTION
    }
    by_state = final.get("tasks_by_state", {})
    ready_count = by_state.get("READY", 0)

    # Durable BLOCKED tasks plus same-run escalations (e.g. PLANNER configuration
    # gates) that never reach BLOCKED task state but still need an operator.
    attention: dict[str, str] = {
        item["task_id"]: item.get("reason") or "human action required"
        for item in final.get("blocked", [])
    }
    for gate_id, reason in (outstanding_escalations or {}).items():
        attention.setdefault(gate_id, reason)

    lines = [
        f"StageMesh - {project.project_id}",
        f"Cycle: {len(cycles)}",
        "",
        f"Active:       {len(live_by_task)}",
        f"Needs action: {len(attention)}",
        f"Ready:        {ready_count}",
    ]
    if live_by_task:
        lines += ["", "ACTIVE"]
        for task_id, execution in sorted(live_by_task.items()):
            role = execution.get("role", "?")
            worker = execution.get("worker_id", "?")
            lines.append(f"  {task_id}  {role:<12} {worker}")
    if attention:
        lines += ["", "ATTENTION"]
        for task_id, reason in sorted(attention.items()):
            lines.append(f"  {task_id}  {_escalation_label(reason)}")
    return "\n".join(lines)


def _is_global_mode(args: argparse.Namespace) -> bool:
    """Global mode: coordinate every registered project. It applies when asked for
    (`--all`) or when no project is named and the cwd is not inside one."""
    if getattr(args, "all_projects", False):
        return True
    if getattr(args, "project_dir", None) or list(getattr(args, "target", None) or []):
        return False
    return find_project_root(Path.cwd()) is None


def handle_global_continue(args: argparse.Namespace) -> None:
    """One process per project: a project's state, workspaces and failures can
    never leak into another, and a stuck project does not hold up the others."""
    import subprocess
    import sys
    import threading

    projects = registered_projects()
    if not projects:
        raise ProjectError(
            "no StageMesh projects are registered; register one with `stagemesh project add <path>` "
            "(or run `stagemesh init` inside a repository)"
        )
    flags = [flag for flag, on in (("--dry-run", args.dry_run), ("--once", args.once), ("--no-sync", args.no_sync), ("--github", args.github)) if on]
    flags.append("--json")
    if getattr(args, "task_id", None):
        flags += ["--task", args.task_id]
    flags += ["--max-cycles", str(args.max_cycles)] + (["--timeout", str(args.timeout)] if args.timeout is not None else [])
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUILD_COORDINATOR_")}
    for key in ("BUILD_COORDINATOR_AUTO_PUSH_ALLOWED", "BUILD_COORDINATOR_RUNNER_CONFIG"):
        if key in os.environ:
            env[key] = os.environ[key]
    runs: dict[str, dict[str, Any]] = {}

    def drive(project: ProjectDefinition) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-m", "build_coordinator", "continue", "--project-dir", str(project.root), *flags],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            stdin=subprocess.DEVNULL,
        )

        def relay() -> None:
            for line in proc.stderr:  # progress lines from the project's own run
                print(line.rstrip(), file=sys.stderr, flush=True)

        threading.Thread(target=relay, daemon=True).start()
        out, _ = proc.communicate()
        try:
            detail = json.loads(out)
        except (json.JSONDecodeError, TypeError):
            detail = {"error": (out or "").strip()[-400:]}
        runs[project.project_id] = {"returncode": proc.returncode, **_summarize_run(detail)}

    threads = [threading.Thread(target=drive, args=(project,)) for project in projects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ordered = {p.project_id: runs.get(p.project_id, {"returncode": None}) for p in projects}
    _print({"mode": "global", "projects": ordered})
    if any(run.get("returncode") not in (0, None) for run in ordered.values()):
        raise SystemExit(1)


def _summarize_run(detail: dict[str, Any]) -> dict[str, Any]:
    final = detail.get("final") or {}
    peak = detail.get("peak_parallel_builders")
    return {
        "concurrency": (detail.get("project") or {}).get("concurrency"),
        "cycles_run": detail.get("cycles_run"),
        "peak_parallel_builders": peak,
        "tasks_by_state": final.get("tasks_by_state"),
        "pending_human_action": final.get("blocked"),
        "error": detail.get("error") or detail.get("dry_run") and None,
        **({"plan": {"eligible_now": detail["eligible_now"], "would_run_in_parallel": detail["would_run_in_parallel"]}} if detail.get("dry_run") else {}),
    }


def _detect_repo_from_git(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            url = proc.stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _optional_task_source(
    project: ProjectDefinition, *, force: bool, dry_run: bool
) -> tuple[Any | None, list[dict[str, Any]]]:
    from build_coordinator.task_source import get_task_source

    diagnostics: list[dict[str, Any]] = []
    supported_sources = {"github", "azure_devops", "azure-devops", "azdo"}
    source_configs: list[tuple[str, dict[str, Any]]] = []

    for src_name, src_cfg in project.task_sources.items():
        if src_name not in supported_sources:
            diagnostics.append({
                "source": src_name,
                "action": "ERROR",
                "details": f"unsupported task source: '{src_name}'",
            })
            continue
        if src_cfg is not None:
            source_configs.append((src_name, dict(src_cfg)))

    if force and not any(src_name == "github" for src_name, _ in source_configs):
        source_configs.append(("github", {}))

    if not source_configs:
        return None, diagnostics

    enabled_sources = [
        (src_name, cfg)
        for src_name, cfg in source_configs
        if cfg.get("enabled", False) or (force and src_name == "github")
    ]
    for src_name, cfg in source_configs:
        if cfg.get("enabled", False) or (force and src_name == "github"):
            continue
        diagnostics.append({
            "source": src_name,
            "action": "DISABLED",
            "details": f"task source '{src_name}' is disabled in project configuration (enabled: false)",
        })

    if not enabled_sources:
        return None, diagnostics

    src_name, cfg = enabled_sources[0]
    source_type = cfg.pop("type", src_name)
    normalized_source_type = source_type.lower()
    options = dict(cfg.get("options") or {})
    options.update({key: value for key, value in cfg.items() if key not in {"enabled", "repo", "labels", "options"}})

    if normalized_source_type == "github" and not cfg.get("repo") and not os.getenv("BUILD_COORDINATOR_GITHUB_REPO"):
        detected_repo = _detect_repo_from_git(project.root)
        if detected_repo:
            cfg["repo"] = detected_repo
        else:
            diagnostics.append({
                "source": "github",
                "action": "ERROR",
                "details": "missing repo identity: 'repo' must be specified in task_sources.github or detectable from git remote",
            })
            return None, diagnostics

    try:
        source = get_task_source({"type": source_type, **cfg, "options": options, "dry_run": dry_run})
        return source, diagnostics
    except Exception as exc:
        diagnostics.append({
            "source": src_name,
            "action": "ERROR",
            "details": f"adapter construction failure: {exc}",
        })
        return None, diagnostics


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
    *,
    target_task_ids: frozenset[str] = frozenset(),
    adapter_payload: list[dict[str, Any]] | None = None,
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
    if target_task_ids:
        ordered = [task_id for task_id in ordered if task_id in target_task_ids]
    runner_config = build_runner_config(project, dry_run=True)
    runner = BuildRunner(lambda: session, runner_config, target_task_ids=target_task_ids)
    diag = runner.diagnostics(session)
    return {
        "dry_run": True,
        "project": project.summary(),
        "target_tasks": [
            _target_task_diagnostic(session, task_id, declared=declared) for task_id in sorted(target_task_ids)
        ],
        "backlog_sync": sync_payload,
        "task_source_adapters": adapter_payload if adapter_payload is not None else [],
        "diagnostics": diag,
        "eligible_now": ordered,
        "would_run_in_parallel": ordered[: project.concurrency],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _target_task_ids(args: argparse.Namespace) -> frozenset[str]:
    task_id = getattr(args, "task_id", None)
    return frozenset([str(task_id)]) if task_id else frozenset()


def _target_task_diagnostic(
    session, task_id: str, *, declared: dict[str, TaskDefinition] | None = None
) -> dict[str, Any]:
    task = session.get(BuildTask, task_id)
    if task is None:
        definition = (declared or {}).get(task_id)
        if definition is not None:
            unmet = [
                dep
                for dep in definition.dependencies
                if (dep_task := session.get(BuildTask, dep)) is None or dep_task.state != "DONE"
            ]
            claimable = not unmet
            return {
                "task_id": task_id,
                "exists": True,
                "state": "READY",
                "claimable_now": claimable,
                "reason": "claimable"
                if claimable
                else f"blocked_by_dependencies:{','.join(unmet)}",
            }
        return {
            "task_id": task_id,
            "exists": False,
            "claimable_now": False,
            "reason": "task_not_found",
        }
    available = {item.task_id for item in list_available_tasks(session)}
    if task.task_id in available:
        reason = "claimable"
    elif task.state != "READY":
        reason = f"state:{task.state}"
        latest = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task.task_id)
            .where(BuildTaskEvent.to_state == "BLOCKED")
            .order_by(BuildTaskEvent.created_at.desc())
        ).first()
        latest_reason = (latest.event_data or {}).get("reason") if latest else None
        if task.state == "BLOCKED" and latest_reason == "EXECUTION_RETRY_LIMIT_REACHED":
            reason = (
                "retry_exhausted: run `stagemesh recover-execution-retry "
                f"{task.task_id}` to open a new bounded retry generation"
            )
    else:
        unmet = [
            dep
            for dep in (task.dependencies or [])
            if (dep_task := session.get(BuildTask, dep)) is None or dep_task.state != "DONE"
        ]
        reason = f"blocked_by_dependencies:{','.join(unmet)}" if unmet else "not_claimable"
    return {
        "task_id": task.task_id,
        "exists": True,
        "state": task.state,
        "claimable_now": task.task_id in available,
        "reason": reason,
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
