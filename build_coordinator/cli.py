"""CLI for the Build Coordinator."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from build_coordinator.config import get_settings
from build_coordinator.coordinator_config import (
    CoordinatorConfigError,
    load_coordinator_config,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.project.commands import add_continue_command, add_project_commands
from build_coordinator.db import DatabaseSchemaError, SessionLocal, configure_process_database
from build_coordinator.service import (
    CheckpointInput,
    ClaimRequest,
    claim_review,
    claim_task,
    checkpoint,
    ensure_state,
    heartbeat,
    list_available_tasks,
    provide_task_input,
    recover_expired,
    recover_execution_retry_exhausted,
    request_task_input,
    set_mode,
    transition_task,
    upsert_task,
)
from build_coordinator.models import (
    BuildObjective,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskEvent,
)
from build_coordinator.objectives import (
    ObjectiveError,
    create_objective,
    list_objectives,
    objective_tasks,
    open_gates,
    pause_objective,
    resolve_gate,
    resume_objective,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.runner.routing import StageRequirement, route_worker
from build_coordinator.runner.worker_health import derive_worker_health
from build_coordinator.types import ObjectiveSpec, PlannedChildTask, StructuredContractError, TaskSpec


def main() -> None:
    import sys

    from build_coordinator.project.commands import handle_continue, handle_project, normalize_argv
    from build_coordinator.project.extras import EXTRA_COMMANDS, handle_extra
    from build_coordinator.project.definition import ProjectError

    sys.argv = normalize_argv(sys.argv)
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command in EXTRA_COMMANDS:
            handle_extra(args)
            return
        if args.command == "project":
            handle_project(args)
            return
        if args.command == "continue":
            handle_continue(args)
            return
        lifecycle = configure_process_database()
        lifecycle.initialize_schema()
        with lifecycle.session() as session:
            _run(args, session)
            session.commit()
    except (
        CoordinatorPolicyError,
        CoordinatorConfigError,
        DatabaseSchemaError,
        StructuredContractError,
        ProjectError,
    ) as exc:
        raise SystemExit(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    from build_coordinator import __version__
    from build_coordinator.project.extras import add_extra_commands

    parser = argparse.ArgumentParser(prog="stagemesh")
    parser.add_argument("--version", action="version", version=f"stagemesh {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_simple_commands(sub)
    add_continue_command(sub)
    add_project_commands(sub)
    add_extra_commands(sub)
    _add_run_commands(sub)
    _add_claim_commands(sub)
    _add_checkpoint_commands(sub)
    _add_transition_commands(sub)
    _add_objective_commands(sub)
    _add_worker_commands(sub)
    _add_routing_commands(sub)
    _add_watcher_commands(sub)
    return parser


def _add_simple_commands(sub) -> None:
    for command in (
        "status",
        "list",
        "pause",
        "drain",
        "resume",
        "recover-expired",
    ):
        sub.add_parser(command)


def _add_run_commands(sub) -> None:
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")


def _add_claim_commands(sub) -> None:
    claim = sub.add_parser("claim")
    claim.add_argument("task_id")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--provider")
    claim.add_argument("--branch")
    claim.add_argument("--worktree")
    claim.add_argument("--lease-seconds", type=int, default=1800)

    review_claim = sub.add_parser("review-claim")
    review_claim.add_argument("task_id")
    review_claim.add_argument("--worker", required=True)
    review_claim.add_argument("--provider")
    review_claim.add_argument("--branch")
    review_claim.add_argument("--worktree")
    review_claim.add_argument("--lease-seconds", type=int, default=1800)


def _add_checkpoint_commands(sub) -> None:
    hb = sub.add_parser("heartbeat")
    hb.add_argument("claim_id", type=UUID)
    hb.add_argument("--worker", required=True)
    hb.add_argument("--lease-seconds", type=int, default=1800)

    cp = sub.add_parser("checkpoint")
    cp.add_argument("claim_id", type=UUID)
    cp.add_argument("--worker", required=True)
    cp.add_argument("--current-step", required=True)
    cp.add_argument("--current-head-sha")
    cp.add_argument("--completed", action="append", default=[])
    cp.add_argument("--remaining", action="append", default=[])
    cp.add_argument("--file", action="append", default=[])
    cp.add_argument("--commit", action="append", default=[])
    cp.add_argument("--test", action="append", default=[])
    cp.add_argument("--failure", action="append", default=[])
    cp.add_argument("--decision", action="append", default=[])
    cp.add_argument("--blocker", action="append", default=[])


def _add_transition_commands(sub) -> None:
    for name, state in {
        "in-progress": "IN_PROGRESS",
        "validating": "VALIDATING",
        "review-ready": "REVIEW_READY",
        "complete": "DONE",
    }.items():
        p = sub.add_parser(name)
        p.add_argument("task_id")
        p.set_defaults(to_state=state)

    block = sub.add_parser("block")
    block.add_argument("task_id")
    block.add_argument("--reason", required=True)
    block.set_defaults(to_state="BLOCKED")

    fail = sub.add_parser("fail")
    fail.add_argument("task_id")
    fail.add_argument("--reason", required=True)
    fail.set_defaults(to_state="FAILED")

    wait = sub.add_parser("request-input")
    wait.add_argument("task_id")
    wait.add_argument("--question", required=True)

    answer = sub.add_parser("provide-input")
    answer.add_argument("task_id")
    answer.add_argument("--response", required=True)

    recover_rev = sub.add_parser("recover-review-environment")
    recover_rev.add_argument("task_id")
    recover_rev.add_argument("--reason", default="operator recovery: review environment failure")

    recover_retry = sub.add_parser("recover-execution-retry")
    recover_retry.add_argument("task_id")
    recover_retry.add_argument("--reason", default="operator recovery: new execution retry generation")


def _add_objective_commands(sub) -> None:
    """`objective` is the operator-facing surface: create/run/status work
    identically from any current working directory because they resolve
    workspace location from coordinator config, never from cwd."""
    objective = sub.add_parser("objective")
    objective_sub = objective.add_subparsers(dest="objective_command", required=True)

    create = objective_sub.add_parser("create")
    create.add_argument("task_id", help="flat-task id, or the objective id when --goal is given")
    create.add_argument("--title")
    create.add_argument("--description", default="")
    create.add_argument(
        "--acceptance-criteria", action="append", default=[], dest="acceptance_criteria"
    )
    create.add_argument("--risk-level", default="MEDIUM")
    create.add_argument("--review-policy", default="SELF")
    create.add_argument("--program-key")
    create.add_argument("--base-sha")
    # Objective mode: a user submits ONE high-level goal. A bare free-text
    # goal is planned by the existing runner executor (PLANNER role) into a
    # validated structured ObjectivePlan; --plan-file remains the explicit
    # structured-plan input. The operator does not write child tasks by hand.
    create.add_argument("--goal", help="submit ONE high-level objective instead of a flat task")
    create.add_argument(
        "--plan-file",
        help="optional JSON ObjectivePlan (list or {tasks, requested_human_gates})",
    )
    create.add_argument("--constraint", action="append", default=[], dest="constraints")
    create.add_argument("--allowed-scope", action="append", default=[], dest="allowed_scope")
    create.add_argument("--prohibited-scope", action="append", default=[], dest="prohibited_scope")
    create.add_argument(
        "--completion-criteria", action="append", default=[], dest="objective_completion_criteria"
    )
    create.add_argument("--parallelism", type=int, default=2)
    create.add_argument("--max-auto-created-tasks", type=int, default=20)
    create.add_argument("--max-child-depth", type=int, default=4)

    run = objective_sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")

    objective_sub.add_parser("status")

    pause = objective_sub.add_parser("pause")
    pause.add_argument("objective_id")

    resume = objective_sub.add_parser("resume")
    resume.add_argument("objective_id")

    approve = objective_sub.add_parser("approve")
    approve.add_argument("gate_id")
    approve.add_argument("--resolved-by", default="operator")
    approve.add_argument("--note")


def _add_worker_commands(sub) -> None:
    workers = sub.add_parser("workers")
    workers_sub = workers.add_subparsers(dest="workers_command", required=True)

    workers_sub.add_parser("list")

    show = workers_sub.add_parser("show")
    show.add_argument("worker_id")

    eligible = workers_sub.add_parser("eligible")
    eligible.add_argument("--stage", required=True)
    eligible.add_argument("--task-id")


def _add_routing_commands(sub) -> None:
    routing = sub.add_parser("routing")
    routing_sub = routing.add_subparsers(dest="routing_command", required=True)
    explain = routing_sub.add_parser("explain")
    explain.add_argument("--stage", required=True)
    explain.add_argument("--task-id")


def _run(args: argparse.Namespace, session) -> None:
    if hasattr(args, "to_state"):
        _transition(args, session)
        return
    if args.command == "objective":
        _objective(args, session)
        return
    if args.command == "workers":
        _workers(args, session)
        return
    if args.command == "routing":
        _routing(args, session)
        return
    handlers = {
        "status": _status,
        "list": _list,
        "pause": _mode,
        "drain": _mode,
        "resume": _mode,
        "claim": _claim,
        "review-claim": _review_claim,
        "heartbeat": _heartbeat,
        "checkpoint": _checkpoint,
        "recover-expired": _recover_expired,
        "request-input": _request_input,
        "provide-input": _provide_input,
        "recover-review-environment": _recover_review_environment,
        "recover-execution-retry": _recover_execution_retry,
        "run": _runner,
    }
    handlers[args.command](args, session)


def _objective(args: argparse.Namespace, session) -> None:
    handlers = {
        "create": _objective_create,
        "run": _runner,
        "status": _status,
        "pause": _objective_pause,
        "resume": _objective_resume,
        "approve": _objective_approve,
    }
    handlers[args.objective_command](args, session)


def _workers(args: argparse.Namespace, session) -> None:
    handlers = {
        "list": _workers_list,
        "show": _workers_show,
        "eligible": _workers_eligible,
    }
    handlers[args.workers_command](args, session)


def _routing(args: argparse.Namespace, session) -> None:
    if args.routing_command == "explain":
        _routing_explain(args, session)
        return
    raise SystemExit(f"unknown routing command: {args.routing_command}")


def _objective_create(args: argparse.Namespace, session) -> None:
    if args.goal:
        _objective_create_goal(args, session)
        return
    if not args.title:
        raise SystemExit("objective create: --title is required for flat-task creation")
    spec = TaskSpec(
        task_id=args.task_id,
        title=args.title,
        description=args.description,
        acceptance_criteria=args.acceptance_criteria,
        risk_level=args.risk_level,
        review_policy=args.review_policy,
        program_key=args.program_key,
        base_sha=args.base_sha,
    )
    task = upsert_task(session, spec)
    _print({"task_id": task.task_id, "title": task.title, "state": task.state})


def _objective_create_goal(args: argparse.Namespace, session) -> None:
    child_tasks: tuple[PlannedChildTask, ...] = ()
    requested_human_gates: tuple[str, ...] = ()
    if args.plan_file:
        from build_coordinator.types import ObjectivePlan

        plan_data = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        plan = ObjectivePlan.from_mapping(plan_data)
        child_tasks = plan.tasks
        requested_human_gates = plan.requested_human_gates

    spec = ObjectiveSpec(
        objective_id=args.task_id,
        goal=args.goal,
        constraints=tuple(args.constraints),
        allowed_scope=tuple(args.allowed_scope),
        prohibited_scope=tuple(args.prohibited_scope),
        completion_criteria=tuple(args.objective_completion_criteria),
        parallelism=args.parallelism,
        max_auto_created_tasks=args.max_auto_created_tasks,
        max_child_depth=args.max_child_depth,
        child_tasks=child_tasks,
        requested_human_gates=requested_human_gates,
    )
    objective = create_objective(session, spec)
    _print(_objective_summary(session, objective))


def _latest_block_reason(session, task_id: str) -> str | None:
    event = session.scalar(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id == task_id)
        .where(BuildTaskEvent.event_type == "task.transitioned")
        .where(BuildTaskEvent.to_state == "BLOCKED")
        .order_by(BuildTaskEvent.created_at.desc())
        .limit(1)
    )
    if event is None:
        return None
    return (event.event_data or {}).get("reason")


def _objective_summary(session, objective: BuildObjective) -> dict:
    from build_coordinator.objectives import objective_work_tasks, planner_status

    tasks = objective_work_tasks(session, objective.objective_id)
    gates = open_gates(session, objective.objective_id)
    return {
        "objective_id": objective.objective_id,
        "state": objective.state,
        "goal": objective.goal,
        "task_count": len(tasks),
        "tasks_by_state": {
            state: len([t for t in tasks if t.state == state]) for state in sorted({t.state for t in tasks})
        },
        "auto_created_task_count": objective.auto_created_task_count,
        "planner_status": planner_status(session, objective),
        "manual_plan_required": False,
        "open_gates": [
            {"gate_id": g.gate_id, "gate_type": g.gate_type, "reason": g.reason, "source_task_id": g.source_task_id}
            for g in gates
        ],
    }


def _objective_pause(args: argparse.Namespace, session) -> None:
    objective = pause_objective(session, args.objective_id)
    _print(_objective_summary(session, objective))


def _objective_resume(args: argparse.Namespace, session) -> None:
    objective = resume_objective(session, args.objective_id)
    _print(_objective_summary(session, objective))


def _objective_approve(args: argparse.Namespace, session) -> None:
    gate = resolve_gate(session, args.gate_id, resolved_by=args.resolved_by, resolution_note=args.note)
    objective = get_objective_or_raise(session, gate.objective_id)
    _print(_objective_summary(session, objective))


def get_objective_or_raise(session, objective_id: str) -> BuildObjective:
    objective = session.get(BuildObjective, objective_id)
    if objective is None:
        raise ObjectiveError(f"unknown objective: {objective_id}")
    return objective


def _controller_source() -> dict[str, str]:
    """Report which `tooling.build_coordinator` checkout this running
    process actually loaded its code from. If a stale checkout on cwd (e.g.
    another worktree's own `tooling/build_coordinator/`) ever shadowed the
    intended controller, this is what would reveal it: `cli_module_file`
    would point outside the configured control repo root."""
    import build_coordinator as _package

    return {
        "cli_module_file": str(Path(__file__).resolve()),
        "package_root": str(Path(_package.__file__).resolve().parent),
    }


def _status(args: argparse.Namespace, session) -> None:
    state = ensure_state(session)
    tasks = session.scalars(select(BuildTask).order_by(BuildTask.task_id)).all()
    executions = session.scalars(
        select(BuildRunnerExecution).order_by(BuildRunnerExecution.launched_at.desc())
    ).all()
    by_state = dict(session.execute(select(BuildTask.state, func.count()).group_by(BuildTask.state)).all())
    events = session.scalar(
        select(BuildTaskEvent).order_by(BuildTaskEvent.created_at.desc()).limit(1)
    )
    settings = get_settings()
    coordinator_config = load_coordinator_config()
    runner_config = RunnerConfig.default(dry_run=True)
    result_dir = Path(runner_config.result_dir) if runner_config.result_dir else settings.data_dir / "results"
    log_dir = settings.data_dir / "execution-logs"
    _print(
        {
            "mode": state.mode,
            "workspace": {
                "control_repo_root": str(settings.repo_root),
                "data_dir": str(settings.data_dir),
                "database_url": settings.database_url,
                "result_dir": str(result_dir),
                "log_artifact_dir": str(log_dir),
                "project_roots": settings.project_roots,
                "worktrees": coordinator_config.worktrees,
                "runner_config_path": __import__("os").getenv("BUILD_COORDINATOR_RUNNER_CONFIG"),
                "coordinator_config_path": (
                    str(coordinator_config.source_path)
                    if coordinator_config.source_path
                    else None
                ),
            },
            "controller_source": _controller_source(),
            "task_count": len(tasks),
            "tasks_by_state": by_state,
            "available_count": len(list_available_tasks(session)),
            "active_executions": [
                {
                    "execution_id": row.execution_id,
                    "task_id": row.task_id,
                    "role": row.role,
                    "worker_id": row.worker_id,
                    "status": row.status,
                }
                for row in executions
                if row.status in {"LAUNCHED", "RUNNING"}
            ],
            "human_action_required": [
                {
                    "task_id": row.task_id,
                    "role": row.role,
                    "reason": row.human_escalation_type,
                }
                for row in executions
                if row.status == "HUMAN_ACTION_REQUIRED"
            ],
            "last_event": events.event_type if events else None,
            "objectives": [_objective_summary(session, objective) for objective in list_objectives(session)],
            "blocked_tasks": [
                {
                    "task_id": t.task_id,
                    "reason": _latest_block_reason(session, t.task_id),
                }
                for t in tasks
                if t.state == "BLOCKED"
            ],
        }
    )


def _list(args: argparse.Namespace, session) -> None:
    _print([
        {"task_id": task.task_id, "title": task.title, "state": task.state}
        for task in list_available_tasks(session)
    ])


def _workers_list(args: argparse.Namespace, session) -> None:
    config = RunnerConfig.default(dry_run=True)
    events = session.scalars(
        select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.provider_failure")
    ).all()
    health = derive_worker_health(
        config.workers,
        (row.event_data or {} for row in events),
        now=datetime.now(timezone.utc),
    )
    _print(
        [
            {
                "worker_id": worker.worker_id,
                "enabled": worker.enabled,
                "runtime": worker.runtime,
                "provider": worker.provider,
                "model": worker.model,
                "capabilities": list(worker.capability_names()),
                "stages": list(worker.stage_names()),
                "max_concurrency": worker.max_concurrency,
                "health": health[worker.worker_id].to_public_dict(),
            }
            for worker in config.workers
        ]
    )


def _workers_show(args: argparse.Namespace, session) -> None:
    config = RunnerConfig.default(dry_run=True)
    worker = next((item for item in config.workers if item.worker_id == args.worker_id), None)
    if worker is None:
        raise SystemExit(f"unknown worker: {args.worker_id}")
    _print(worker.public_summary())


def _workers_eligible(args: argparse.Namespace, session) -> None:
    _print(_routing_payload(args.stage, session, task_id=args.task_id))


def _routing_explain(args: argparse.Namespace, session) -> None:
    _print(_routing_payload(args.stage, session, task_id=args.task_id))


def _routing_payload(stage: str, session, *, task_id: str | None = None) -> dict:
    config = RunnerConfig.default(dry_run=True)
    requirement = config.stage_requirements.get(stage, StageRequirement(stage))
    decision = route_worker(
        config.workers,
        stage=stage,
        stage_requirement=requirement,
        providers=config.providers,
        runtimes=config.runtimes,
        routing_policy=config.routing_policy,
        session=session,
        task_id=task_id,
    )
    selected = next(
        (worker for worker in config.workers if worker.worker_id == decision.selected_worker_id),
        None,
    )
    return decision.to_audit_dict(selected)


def _mode(args: argparse.Namespace, session) -> None:
    mode = {"pause": "PAUSED", "drain": "DRAINING", "resume": "RUNNING"}[args.command]
    state = set_mode(session, mode)
    _print({"mode": state.mode})




def _claim(args: argparse.Namespace, session) -> None:
    claim = claim_task(session, _claim_request(args))
    _print({"claim_id": str(claim.claim_id), "task_id": claim.task_id})


def _review_claim(args: argparse.Namespace, session) -> None:
    claim = claim_review(session, _claim_request(args))
    _print({"claim_id": str(claim.claim_id), "task_id": claim.task_id})


def _heartbeat(args: argparse.Namespace, session) -> None:
    claim = heartbeat(
        session,
        args.claim_id,
        worker_id=args.worker,
        lease_seconds=args.lease_seconds,
    )
    _print({
        "claim_id": str(claim.claim_id),
        "lease_expires_at": claim.lease_expires_at.isoformat(),
    })


def _checkpoint(args: argparse.Namespace, session) -> None:
    row = checkpoint(
        session,
        args.claim_id,
        worker_id=args.worker,
        data=CheckpointInput(
            current_step=args.current_step,
            current_head_sha=args.current_head_sha,
            completed_work=args.completed,
            remaining_work=args.remaining,
            files_changed=args.file,
            commits_created=args.commit,
            last_successful_tests=args.test,
            known_failures=args.failure,
            decisions=args.decision,
            blockers=args.blocker,
        ),
    )
    _print({"checkpoint_id": str(row.checkpoint_id), "task_id": row.task_id})


def _recover_expired(args: argparse.Namespace, session) -> None:
    recovered = recover_expired(session)
    _print({"recovered": [task.task_id for task in recovered]})


def _request_input(args: argparse.Namespace, session) -> None:
    task = request_task_input(session, args.task_id, args.question, actor="cli")
    _print({"task_id": task.task_id, "state": task.state, "waiting_input": task.waiting_input})


def _provide_input(args: argparse.Namespace, session) -> None:
    task = provide_task_input(session, args.task_id, args.response, actor="cli")
    _print({"task_id": task.task_id, "state": task.state, "waiting_input": task.waiting_input})


def _recover_review_environment(args: argparse.Namespace, session) -> None:
    from build_coordinator.service import recover_review_environment_blocked

    task = recover_review_environment_blocked(
        session,
        args.task_id,
        reason=args.reason,
    )
    _print({"task_id": task.task_id, "state": task.state, "recovered": True})


def _recover_execution_retry(args: argparse.Namespace, session) -> None:
    task = recover_execution_retry_exhausted(
        session,
        args.task_id,
        reason=args.reason,
    )
    _print(
        {
            "task_id": task.task_id,
            "state": task.state,
            "retry_generation": task.retry_generation,
            "recovered": True,
        }
    )


def _runner(args: argparse.Namespace, session) -> None:
    session.commit()
    runner = BuildRunner(SessionLocal, RunnerConfig.default(dry_run=args.dry_run))
    if args.once:
        result = runner.run_once()
        _print(
            {
                "mode": result.mode,
                "recovered": result.recovered,
                "launched": result.launched,
                "observed": result.observed,
                "escalations": result.escalations,
                "capacity_full": result.capacity_full,
                "objectives_reconciled": result.objectives_reconciled,
                "objective_follow_ups_created": result.objective_follow_ups_created,
                "objective_unrelated_tasks_created": result.objective_unrelated_tasks_created,
                "objective_gates_raised": result.objective_gates_raised,
                "objectives_completed": result.objectives_completed,
            }
        )
        return
    runner.run_forever()
    _print({"runner": "stopped"})


def _transition(args: argparse.Namespace, session) -> None:
    task = transition_task(
        session,
        args.task_id,
        args.to_state,
        reason=getattr(args, "reason", None),
    )
    _print({"task_id": task.task_id, "state": task.state})


def _claim_request(args: argparse.Namespace) -> ClaimRequest:
    return ClaimRequest(
        task_id=args.task_id,
        worker_id=args.worker,
        provider=args.provider,
        branch_name=args.branch,
        worktree_path=args.worktree,
        lease_seconds=args.lease_seconds,
    )




def _print(payload) -> None:
    print(json.dumps(payload, indent=2))


def _add_watcher_commands(sub) -> None:
    watcher = sub.add_parser("watcher")
    watcher_sub = watcher.add_subparsers(dest="watcher_command", required=True)

    for name in ("install", "uninstall", "start", "stop", "status"):
        p = watcher_sub.add_parser(name)
        p.add_argument("--repo", help="authorized repository slug; required unless exactly one is configured")

    run = watcher_sub.add_parser("run")
    run.add_argument("--repo", help="authorized repository slug; required unless exactly one is configured")
    run.add_argument("--foreground", action="store_true", help="required; the watcher only ever runs in the foreground")
    run.add_argument("--once", action="store_true", help="run exactly one cycle instead of looping")

    labels = watcher_sub.add_parser("provision-labels")
    labels.add_argument("--repo", help="authorized repository slug; required unless exactly one is configured")
    labels.add_argument("--dry-run", action="store_true")


def _watcher_resolve_repo_slug(args: argparse.Namespace):
    from build_coordinator.watcher.authorization import load_authorized_repositories

    if args.repo:
        return args.repo
    configured = load_authorized_repositories()
    if len(configured) == 1:
        return configured[0].slug
    raise SystemExit(
        "watcher: --repo is required (0 or more than 1 authorized_repositories configured); "
        f"configured slugs: {[repo.slug for repo in configured]}"
    )


def _watcher(args: argparse.Namespace, session) -> None:
    handlers = {
        "install": _watcher_install,
        "uninstall": _watcher_uninstall,
        "start": _watcher_start,
        "stop": _watcher_stop,
        "status": _watcher_status,
        "run": _watcher_run,
        "provision-labels": _watcher_provision_labels,
    }
    handlers[args.watcher_command](args, session)


def _watcher_task_definition(repo, *, task_name: str):
    import sys

    from build_coordinator.watcher.windows_task_scheduler import TaskDefinition

    return TaskDefinition(
        task_name=task_name,
        command=sys.executable,
        arguments=f"-m build_coordinator.cli watcher run --foreground --repo {repo.slug}",
        working_directory=str(repo.control_repo_root),
    )


def _watcher_install(args: argparse.Namespace, session) -> None:
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.labels import provision_labels
    from build_coordinator.watcher.windows_task_scheduler import SchtasksAdapter, stable_task_name

    repo = authorize(_watcher_resolve_repo_slug(args))
    task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
    adapter = SchtasksAdapter()
    adapter.create_or_update(_watcher_task_definition(repo, task_name=task_name))
    label_result = provision_labels(repo) if repo.labels else None
    _print(
        {
            "task_name": task_name,
            "repository_slug": repo.slug,
            "installed": True,
            "labels_created": list(label_result.created) if label_result else [],
            "labels_updated": list(label_result.updated) if label_result else [],
        }
    )


def _watcher_uninstall(args: argparse.Namespace, session) -> None:
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.windows_task_scheduler import SchtasksAdapter, stable_task_name

    repo = authorize(_watcher_resolve_repo_slug(args))
    task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
    SchtasksAdapter().delete(task_name)
    _print({"task_name": task_name, "repository_slug": repo.slug, "installed": False})


def _watcher_start(args: argparse.Namespace, session) -> None:
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.windows_task_scheduler import SchtasksAdapter, stable_task_name

    repo = authorize(_watcher_resolve_repo_slug(args))
    task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
    adapter = SchtasksAdapter()
    if not adapter.task_exists(task_name):
        raise SystemExit(f"watcher start: task {task_name!r} is not installed; run 'watcher install' first")
    if not adapter.is_running(task_name):
        adapter.run(task_name)
    _print({"task_name": task_name, "repository_slug": repo.slug, "started": True})


def _watcher_stop(args: argparse.Namespace, session) -> None:
    from build_coordinator.watcher import lock as watcher_lock
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.windows_task_scheduler import stable_task_name

    repo = authorize(_watcher_resolve_repo_slug(args))
    task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
    record = watcher_lock.request_stop(session, task_name)
    session.commit()
    _print({"task_name": task_name, "repository_slug": repo.slug, "stop_requested": record is not None})


def _watcher_status(args: argparse.Namespace, session) -> None:
    from build_coordinator.models import BuildWatcherRecord
    from build_coordinator.runner.models import RunnerConfig
    from build_coordinator.watcher import lock as watcher_lock
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.windows_task_scheduler import SchtasksAdapter, stable_task_name

    repo = authorize(_watcher_resolve_repo_slug(args))
    task_name = stable_task_name(str(repo.control_repo_root), repo.slug)
    adapter = SchtasksAdapter()
    record = session.get(BuildWatcherRecord, task_name)
    scheduler_status = adapter.query_status(task_name)
    installed = scheduler_status.installed
    scheduler_running = scheduler_status.running
    process_alive = watcher_lock.query_pid_liveness(record.process_id) if record and record.process_id else False
    poll_seconds = RunnerConfig.default().poll_seconds
    running_health = _watcher_running_health(
        scheduler_running=scheduler_running,
        process_alive=process_alive,
        heartbeat_at=record.heartbeat_at if record else None,
        poll_seconds=poll_seconds,
    )
    _print(
        {
            "installed": installed,
            "running": running_health["running"],
            "scheduler_running": scheduler_running,
            "scheduler_state": scheduler_status.state,
            "scheduler_error": scheduler_status.error,
            "watcher_process_alive": process_alive,
            "watcher_heartbeat_healthy": running_health["heartbeat_healthy"],
            "heartbeat_stale_after_seconds": running_health["heartbeat_stale_after_seconds"],
            "task_name": task_name,
            "control_repo_root": str(repo.control_repo_root),
            "database_url": get_settings().database_url,
            "data_dir": str(get_settings().data_dir),
            "authorized_repository_slug": repo.slug,
            "watcher_pid": record.process_id if record else None,
            "watcher_started_at": record.started_at.isoformat() if record and record.started_at else None,
            "last_cycle_at": record.last_cycle_at.isoformat() if record and record.last_cycle_at else None,
            "last_cycle_summary": record.last_cycle_summary if record else {},
            "last_error_type": record.last_error_type if record else None,
            "restart_count": record.restart_count if record else 0,
            "backoff_until": record.backoff_until.isoformat() if record and record.backoff_until else None,
            "stop_requested": record.stop_requested if record else False,
        }
    )


def _watcher_running_health(
    *,
    scheduler_running: bool | None,
    process_alive: bool | None,
    heartbeat_at,
    poll_seconds: float,
    now: datetime | None = None,
) -> dict:
    from datetime import UTC, datetime, timedelta
    heartbeat_stale_after = timedelta(seconds=max(30.0, poll_seconds * 3))
    if heartbeat_at is not None and heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    heartbeat_healthy = bool(heartbeat_at and now - heartbeat_at <= heartbeat_stale_after)
    return {
        "running": heartbeat_healthy and scheduler_running is not False and process_alive is not False,
        "heartbeat_healthy": heartbeat_healthy,
        "heartbeat_stale_after_seconds": heartbeat_stale_after.total_seconds(),
    }


def _watcher_run(args: argparse.Namespace, session) -> None:
    if not args.foreground:
        raise SystemExit("watcher run: --foreground is required (the watcher never runs detached from this command)")
    from build_coordinator.watcher.loop import run_foreground
    from build_coordinator.watcher.safe_logging import WatcherLogger

    session.commit()
    repo_slug = _watcher_resolve_repo_slug(args)
    logger = WatcherLogger(get_settings().data_dir)
    run_foreground(SessionLocal, repository_slug=repo_slug, logger=logger, once=args.once)
    _print({"watcher": "stopped" if not args.once else "cycle_complete"})


def _watcher_provision_labels(args: argparse.Namespace, session) -> None:
    from build_coordinator.watcher.authorization import authorize
    from build_coordinator.watcher.labels import provision_labels

    repo = authorize(_watcher_resolve_repo_slug(args))
    result = provision_labels(repo, dry_run=args.dry_run)
    _print({"created": list(result.created), "updated": list(result.updated), "unchanged": list(result.unchanged)})



if __name__ == "__main__":
    main()
