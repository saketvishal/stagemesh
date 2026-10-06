from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .acceptance import AcceptanceValidationError, local_acceptance_report
from .acceptance_matrix import AcceptanceMatrixValidationError, acceptance_matrix
from .audit import AuditValidationError, export_audit_jsonl
from .capacity import CapacityKind, CapacityRegistry, CapacityValidationError
from .ci import CIValidationError, broken_future_feature_gate, default_gates
from .ci_wait import decide_ci_wait
from .completion_audit import CompletionAuditValidationError, completion_audit
from .config import ConfigValidationError, load_config
from .coordinator import Coordinator, TargetSelection, TargetSelectionError
from .dashboard import dashboard_summary, render_dashboard
from .demo import DemoValidationError, create_demo_project
from .distributed import WorkQueue, WorkQueueError
from .e2e_acceptance import EndToEndAcceptanceValidationError, end_to_end_acceptance
from .external_evidence import (
    ExternalEvidenceValidationError,
    external_evidence_records,
    record_external_evidence,
)
from .final_report import FinalReportValidationError, candidate_sha, render_final_report
from .git import GitWorkspace
from .github_acceptance import run_github_acceptance
from .objectives import ObjectivePlanner, ObjectiveValidationError
from .observability import health
from .operator import operator_report
from .operator_actions import OperatorActionError, adopt_candidate, recover_stale, release_unknown_execution, task_details
from .persistence import Store, StoreValidationError
from .persistence_backends import probe_backend
from .postgres_store import PostgresStore, PostgresUnavailable, postgres_schema_contract
from .process_identity import current_process_identity
from .provider_acceptance import run_provider_acceptance
from .providers import ProviderValidationError, adapters_from_config
from .redaction import redact_command_secrets, redact_url_credentials
from .registry import (
    GlobalRegistry,
    ProjectRegistration,
    RegistryConflictError,
    RegistryValidationError,
)
from .concurrency import IntegrationLock, ProviderLimiter
from .diagnosis import DiagnosisPolicy, diagnose, format_findings, make_adapter_analyst
from .parallel import ParallelRunner, ParallelSummary, SetupRefused, worker_id_for
from .queue_run import QueueRunner
from .recovery import RecoveryRefusal, format_doctor, rebaseline_task, rebind_contract, task_doctor
from .timing import format_task_timing, task_timing
from .run_ready import RunSummary, format_step_update, format_stop, run_ready
from .serialized_integration import SerializedIntegrator
from .provider_pool import IMPLEMENT, REVIEW, PooledExecutor, ProviderLog, ProviderPool, default_pools, describe_verdicts
from .release import ReleaseValidationError, build_release_artifact
from .release_readiness import ReleaseReadinessValidationError, release_readiness
from .retry import RetryRegistry, RetryValidationError
from .review import Reviewer
from .routing import RoutingMode
from .security import SecurityBoundaryError, WorkspaceBoundary
from .task_sources import (
    LocalBacklogSource,
    TaskSourceValidationError,
    sync_source,
    task_sources_from_config,
)
from .validation_plan import derive_validation_plan
from .work_transport import (
    WorkTransportError,
    import_ack,
    write_ack_envelope,
    write_packet_envelope,
)
from .workspaces import legacy_worktree_roots, worktree_root
from .autonomy.cli import register_autonomy_commands
from .autonomy.integration import SupervisedIntegrator
from .autonomy.isolation import check_isolation
from .autonomy.review_adapter import supervise_reviewer
from .autonomy.supervisor import Supervisor
from .autonomy.wiring import SUPERVISED_MIN_REFRESH_ATTEMPTS, load_settings
from .workers import WorkerValidationError, heartbeat_worker, register_worker


def runtime_dir(project: Path) -> Path:
    return project / ".stagemesh"


def db_path(project: Path) -> Path:
    return runtime_dir(project) / "stagemesh.sqlite3"


def command_init(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    runtime_dir(project).mkdir(parents=True, exist_ok=True)
    store = Store(db_path(project))
    store.migrate()
    if args.task:
        store.upsert_task(args.task)
    schema_version = store.schema_version()
    store.close()
    registered = False
    if args.register:
        registry = GlobalRegistry(Path(args.registry).resolve())
        try:
            registry.register(ProjectRegistration(project.name, project, db_path(project)))
            registered = True
        except RegistryConflictError as exc:
            print(f"registry conflict: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(
            json.dumps(
                {
                    "project": str(project),
                    "runtime": str(runtime_dir(project)),
                    "db": str(db_path(project)),
                    "schema_version": schema_version,
                    "registered": registered,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"initialized StageMesh at {runtime_dir(project)}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    backend = probe_backend(config.database_url, db_path(project))
    data = {
        "version": __version__,
        "executable_path": str(Path(sys.argv[0]).resolve()),
        "python_interpreter": sys.executable,
        "imported_package_path": str(Path(__file__).resolve().parent),
        "project": str(project),
        "worktree_root": str(config.runtime.worktree_root) if config.runtime else None,
        "legacy_worktree_roots": [str(path) for path in legacy_worktree_roots(project) if path.exists()],
        "db": str(db_path(project)),
        "schema_version": store.schema_version(),
        "config_source": str(config.source),
        "github_configured": config.github.configured,
        "backend": backend.name,
        "backend_available": backend.available,
        "development_status": "development" if "site-packages" not in __file__ else "installed",
        "platform": platform.platform(),
    }
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        store.close()
        return 0
    print(f"version: {__version__}")
    print(f"executable path: {Path(sys.argv[0]).resolve()}")
    print(f"python interpreter: {sys.executable}")
    print(f"imported package path: {Path(__file__).resolve().parent}")
    print(f"project: {project}")
    if config.runtime:
        print(f"worktree root: {config.runtime.worktree_root}")
    legacy = [path for path in legacy_worktree_roots(project) if path.exists()]
    if legacy:
        print("legacy worktree roots:")
        for path in legacy:
            print(f"  {path}")
    print(f"db: {db_path(project)}")
    print(f"schema version: {store.schema_version()}")
    print(f"config source: {config.source}")
    print(f"github configured: {config.github.configured}")
    print(f"backend: {backend.name}")
    print(f"backend available: {backend.available}")
    print(f"editable/development status: {'development' if 'site-packages' not in __file__ else 'installed'}")
    print(f"platform: {platform.platform()}")
    store.close()
    return 0


class _SetupError(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _sync_all_sources(store: Store, project: Path, config, targeted_task_id: str | None) -> None:
    backlog = project / ".stagemesh" / "backlog.json"
    sync_source(store, _filter_targeted_tasks(LocalBacklogSource(backlog).discover(), targeted_task_id))
    for source in task_sources_from_config(config):
        sync_source(store, _filter_targeted_tasks(source.discover(), targeted_task_id))


def _build_coordinator(
    args,
    project: Path,
    config,
    store: Store,
    target: TargetSelection | None,
    provider_log: ProviderLog | None = None,
    *,
    parallel: ParallelWiring | None = None,
):
    """Wire provider adapters, router, reviewer and integrator; raises _SetupError after printing why."""
    # Wire real provider adapters unless --dry-run is requested.
    executor = None
    reviewer = None
    chosen_provider = "fake"
    chosen_review_provider = "builtin-deterministic-fallback"
    independent_review_configured = False
    require_independent_review = False
    integrator = None
    guard = None
    integration_ref = None
    info_extra: dict[str, object] = {}
    try:
        autonomy = load_settings(runtime_dir(project))
    except (OSError, ValueError) as exc:
        print(f"autonomy config error: {exc}", file=sys.stderr)  # fail closed: never silently run unsupervised
        raise _SetupError(2)
    if autonomy.enabled:  # supervised runs fail closed if any runtime path resolves into another StageMesh checkout
        isolation = check_isolation(project, check_running_code=True)
        if not isolation.isolated:
            for finding in isolation.findings:
                print(f"isolation violation [{finding.code}]: {finding.message}", file=sys.stderr)
            raise _SetupError(2)
    if not getattr(args, "dry_run", False):
        require_independent_review = config.require_independent_review
        try:
            adapters = adapters_from_config(config)
        except ProviderValidationError as exc:
            print(f"provider config error: {exc}", file=sys.stderr)
            raise _SetupError(2)
        adapter_by_name = {adapter.name: adapter for adapter in adapters}
        chosen_name = getattr(args, "provider", None)
        if chosen_name and chosen_name not in adapter_by_name:
            print(f"provider not found: {chosen_name}", file=sys.stderr)
            raise _SetupError(2)
        stage_caps = {"IMPLEMENT": "code", "REVIEW": "review"}
        capable = {
            stage: {a.name for a in adapters if capability in a.capabilities} for stage, capability in stage_caps.items()
        }
        priorities = {n: spec.priority for n, spec in config.provider_specs.items() if spec.priority is not None}
        weights = {n: spec.weight for n, spec in config.provider_specs.items() if spec.weight is not None}
        weights.update(config.provider_weights)  # routing.provider_weights wins over a provider's own weight
        pools = default_pools(
            sorted(adapter_by_name),
            config.stage_routes,
            config.provider_pools,
            config.single_agent_provider,
            config.routing_mode,
            capable=capable,
            priorities=priorities,
        )
        if chosen_name:  # an explicit --provider pins implementation to that one provider (no fallback)
            pools[IMPLEMENT] = (chosen_name,)
        pool = ProviderPool(
            adapters,
            pools,
            require_independent=require_independent_review,
            cooldown_seconds=config.provider_failure_cooldown_seconds,
            log=provider_log,
            policy=config.provider_selection_policy,
            weights=weights,
            priorities=priorities,
            limiter=parallel.limiter if parallel else None,
        )
        staged = config.routing_mode == RoutingMode.STAGED
        ok, diagnostic, impl_verdicts, review_verdicts = pool.preflight(store, target.task_id if target else None)
        if not staged:
            ok = any(v.eligible for v in impl_verdicts)
            diagnostic = f"no implementation provider is available. IMPLEMENT pool: {describe_verdicts(impl_verdicts)}"
        if not ok:
            print(diagnostic, file=sys.stderr)
            raise _SetupError(2)
        executor = PooledExecutor(pool)
        impl_eligible = [adapter_by_name[v.provider] for v in impl_verdicts if v.eligible]
        review_eligible = [adapter_by_name[v.provider] for v in review_verdicts if v.eligible]
        chosen_provider = pool.order(store, IMPLEMENT, impl_eligible)[0][0].name
        if staged:
            reviewer = Reviewer(require_independent=require_independent_review, review_pool=pool)
            ordered_reviewers = [a.name for a in pool.order(store, REVIEW, review_eligible)[0]]
            chosen_review_provider = "dynamic-pool:" + ",".join(ordered_reviewers)
            independent_review_configured = True
        else:
            chosen_review_provider = "single-agent-deterministic-fallback"
            reviewer = Reviewer(provider_name=chosen_review_provider, require_independent=require_independent_review)
        info_extra.update(
            selection_policy=config.provider_selection_policy,
            implementation_pool=list(pools[IMPLEMENT]),
            review_pool=list(pools[REVIEW]),
            implementation_skipped=[v.to_dict() for v in impl_verdicts if not v.eligible],
            review_skipped=[v.to_dict() for v in review_verdicts if not v.eligible],
        )
        integration_ref = config.integration_ref or _current_branch_ref(project)
        if integration_ref is None:
            print("no integration_ref configured and the project has no current branch", file=sys.stderr)
            raise _SetupError(2)
        integrator = (
            SerializedIntegrator(
                integration_ref,
                require_independent_review,
                parallel.lock,
                max_rebases=config.parallel.integration_rebase_attempts,
                on_event=parallel.on_integration_event,
            )
            if parallel
            else SerializedIntegrator(
                integration_ref,
                require_independent_review,
                IntegrationLock(runtime_dir(project) / "integration.lock"),
                max_rebases=config.parallel.integration_rebase_attempts,
            )
        )
        if autonomy.enabled:
            guard = Supervisor(
                store,
                project,
                integration_ref=integration_ref,
                integration_policy=autonomy.integration_policy(),
                recovery_policy=autonomy.recovery_policy(),
                max_reconstructs=autonomy.max_reconstructs,
            )
            if reviewer is not None:
                supervise_reviewer(reviewer, guard)  # only blocking in-scope findings can fail a candidate
            integrator = SupervisedIntegrator(
                guard,
                integration_ref,
                require_independent_review,
                parallel.lock if parallel else IntegrationLock(runtime_dir(project) / "integration.lock"),
                max_rebases=max(config.parallel.integration_rebase_attempts, SUPERVISED_MIN_REFRESH_ATTEMPTS),
                on_event=parallel.on_integration_event if parallel else None,
            )
    elif parallel:  # --dry-run: evidence-only integration, still behind the lock
        integrator = SerializedIntegrator(None, False, parallel.lock)
    coord = Coordinator(
        store,
        project,
        executor=executor,
        reviewer=reviewer,
        integrator=integrator,
        target=target,
        require_independent_review=require_independent_review,
        diagnosis_policy=_diagnosis_policy(config, project, {} if getattr(args, "dry_run", False) else adapter_by_name),
        guard=guard,
        **({"worker_id": worker_id_for(target.task_id)} if parallel and target else {}),
    )
    info = {
        "provider": chosen_provider,
        "review_provider": chosen_review_provider,
        "independent_review_configured": independent_review_configured,
        "require_independent_review": require_independent_review,
        "integration_ref": integration_ref,
        **info_extra,
    }
    return coord, info


@dataclass
class ParallelWiring:
    """What every task of a parallel run shares: provider slots, the integration lock and the lifecycle sink."""

    limiter: ProviderLimiter
    lock: IntegrationLock
    on_integration_event: Callable[[str, str, dict[str, object]], None] | None = None


def _diagnosis_policy(config, project: Path, adapters: dict[str, object]) -> DiagnosisPolicy:
    """Stop-on-repeat policy from config; the separate diagnostic provider pass only exists when a provider is configured."""
    settings = config.diagnosis
    analyst = None
    if settings.provider and settings.dispatch != "never" and settings.provider in adapters:
        analyst = make_adapter_analyst(adapters[settings.provider], project)
    return DiagnosisPolicy(settings.repeat_threshold, settings.stop_on_repeat, settings.dispatch, analyst)


def command_diagnose(args: argparse.Namespace) -> int:
    """Read-only: why is this task failing? Summarizes the failing evidence, compares candidates and classifies the failure."""
    project = Path(args.project).resolve()
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    if store.get_task(args.task) is None:
        print(f"task does not exist: {args.task}", file=sys.stderr)
        store.close()
        return 2
    diagnosis = diagnose(store, args.task, project, args.threshold or config.diagnosis.repeat_threshold)
    if diagnosis is None:
        store.close()
        print(json.dumps({"task_id": args.task, "diagnosis": None}) if args.json else f"task {args.task}: no failed checks or stalled attempts to diagnose")
        return 0
    provider = args.provider or config.diagnosis.provider
    if provider:  # an explicit operator request: run the separate read-only provider pass now
        try:
            adapters = {a.name: a for a in adapters_from_config(config)}
        except ProviderValidationError as exc:
            print(f"provider config error: {exc}", file=sys.stderr)
            store.close()
            return 2
        if provider not in adapters:
            print(f"provider not found: {provider}", file=sys.stderr)
            store.close()
            return 2
        if diagnosis.candidate_sha:
            diagnosis.provider_analysis = make_adapter_analyst(adapters[provider], project)(diagnosis, diagnosis.candidate_sha)
        if diagnosis.provider_analysis is None:
            print(f"diagnostic provider {provider} gave no usable answer; showing the recorded facts only", file=sys.stderr)
    store.close()
    if args.json:
        print(json.dumps(diagnosis.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"task {args.task}")
        print("\n".join(diagnosis.format_lines()))
    return 0


def _repair_command(args: argparse.Namespace, action) -> int:  # type: ignore[no-untyped-def]
    """Shared shell for the repair commands: open the store, run `action(store, project)`, report a refusal as exit 2."""
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    try:
        report = action(store, project)
    except RecoveryRefusal as exc:
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True) if args.json else f"refused ({exc.code}): {exc}", file=None if args.json else sys.stderr)
        return 2
    finally:
        store.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


def command_rebind_contract(args: argparse.Namespace) -> int:
    def run(store, project):  # type: ignore[no-untyped-def]
        report = rebind_contract(store, project, args.task, validate=args.validate, reason=args.reason, force=args.force)
        if not args.json:
            old, new = (report["old_digest"] or "none")[:12], report["new_digest"][:12]
            print(f"task {args.task}: contract rebound {old} -> {new} (version {report['old_version']} -> {report['new_version']})")
            if report["invalidated_evidence"]:
                print(f"  {len(report['invalidated_evidence'])} passed evidence row(s) no longer satisfy the task (kept as history)")
            if report["unblocked"]:
                print("  task unblocked with a fresh remediation budget")
            check = report["validation"]
            if check:
                print("  validation: " + (f"{check['status']}" + (" - advanced" if check["advanced"] else "") if check["ran"] else f"not run ({check['reason']})"))
            print(f"  stage/status: {report['stage']}/{report['status']}")
        return report

    return _repair_command(args, run)


def command_rebaseline_task(args: argparse.Namespace) -> int:
    def run(store, project):  # type: ignore[no-untyped-def]
        report = rebaseline_task(store, project, args.task, args.to, validate=args.validate, force=args.force, reason=args.reason)
        if not args.json:
            print(f"task {args.task}: baseline {report['old_baseline'][:12]} -> {report['new_baseline'][:12]} (against {args.to})")
            print(f"  changed files: {len(report['changed_files_before'])} -> {len(report['changed_files_after'])}")
            for path in report["removed_files"][:20]:
                print(f"    no longer in the task diff: {path}")
            check = report["validation"]
            if check:
                print("  validation: " + (f"{check['status']}" + (" - advanced" if check["advanced"] else "") if check["ran"] else f"not run ({check['reason']})"))
            print(f"  stage/status: {report['stage']}/{report['status']}")
        return report

    return _repair_command(args, run)


def command_task_doctor(args: argparse.Namespace) -> int:
    def run(store, project):  # type: ignore[no-untyped-def]
        report = task_doctor(store, project, args.task, args.to, load_config(project).diagnosis.repeat_threshold)
        if not args.json:
            print(format_doctor(report))
        return report

    return _repair_command(args, run)


def command_task_timing(args: argparse.Namespace) -> int:
    def run(store, project):  # type: ignore[no-untyped-def]
        if store.get_task(args.task) is None:
            raise RecoveryRefusal("unknown_task", f"no task {args.task}")
        timing = task_timing(store, args.task)
        if not args.json:
            print(format_task_timing(timing, verbose=args.verbose))
        return timing

    return _repair_command(args, run)


def command_continue(args: argparse.Namespace) -> int:
    parallel = getattr(args, "parallel", None)
    if parallel is not None:
        if parallel < 1:
            print("--parallel must be at least 1", file=sys.stderr)
            return 2
        if parallel > 1:
            conflicts = [flag for flag, on in (("--task", getattr(args, "task", None)), ("--choose", getattr(args, "choose", False)), ("--once", getattr(args, "once", False))) if on]
            if conflicts:
                print(f"--parallel selects tasks automatically and cannot be combined with {', '.join(conflicts)}", file=sys.stderr)
                return 2
            return command_run_parallel(args)
    if not getattr(args, "once", False):
        return command_run_ready(args)  # default: supervise one task to completion; --once keeps the single tick
    project = Path(args.project).resolve()
    targeted_task_id = getattr(args, "task", None) or getattr(args, "task_id", None)
    targeted_mode = targeted_task_id is not None
    if targeted_task_id is not None and not str(targeted_task_id).strip():
        print("target task id must not be empty", file=sys.stderr)
        return 2
    target = TargetSelection(str(targeted_task_id)) if targeted_task_id is not None else None
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    _sync_all_sources(store, project, config, targeted_task_id)
    try:
        coord, info = _build_coordinator(args, project, config, store, target)
    except _SetupError as exc:
        store.close()
        return exc.code
    chosen_provider = info["provider"]
    chosen_review_provider = info["review_provider"]
    independent_review_configured = info["independent_review_configured"]
    require_independent_review = info["require_independent_review"]
    integration_ref = info["integration_ref"]
    count = 0
    while True:
        try:
            progressed = coord.tick()
        except TargetSelectionError as exc:
            print(f"target selection error: {exc}", file=sys.stderr)
            store.close()
            return 2
        count += progressed
        if args.once or progressed == 0:
            break
    if args.json:
        print(
            json.dumps(
                {
                    "progressed": count,
                    "provider": chosen_provider,
                    "review_provider": chosen_review_provider,
                    "independent_review_configured": independent_review_configured,
                    "require_independent_review": require_independent_review,
                    "integration_ref": integration_ref,
                    "targeted_mode": targeted_mode,
                    "targeted_task_id": str(targeted_task_id) if targeted_task_id is not None else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0
    print(f"progressed: {count}")
    print(f"provider: {chosen_provider}")
    print(f"review_provider: {chosen_review_provider}")
    print(f"independent_review_configured: {independent_review_configured}")
    print(f"require_independent_review: {require_independent_review}")
    print(f"integration_ref: {integration_ref}")
    print(f"targeted_mode: {targeted_mode}")
    print(f"targeted_task_id: {targeted_task_id}")
    store.close()
    return 0


def _current_branch_ref(project: Path) -> str | None:
    result = GitWorkspace(project).run("symbolic-ref", "-q", "HEAD", check=False)
    ref = result.stdout.strip()
    return ref if result.returncode == 0 and ref else None


def _filter_targeted_tasks(tasks, targeted_task_id: str | None):
    if targeted_task_id is None:
        return tasks
    parent_task_id = targeted_task_id.removesuffix("-PLANNER")
    return [
        task
        for task in tasks
        if task.source_id == targeted_task_id or task.source_id == parent_task_id
    ]


def command_status(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    rows = store.tasks()
    if args.json:
        report = health(store)
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "task_count": report.task_count,
                    "blocked_task_count": report.blocked_task_count,
                    "running_count": report.running_count,
                    "done_count": report.done_count,
                    "failed_execution_count": report.failed_execution_count,
                    "unknown_execution_count": report.unknown_execution_count,
                    "backlog_state": report.backlog_state,
                    "latest_implementation_failure": report.latest_implementation_failure,
                    **_health_scope_fields(report),
                    "tasks": [
                        {
                            "id": row["id"],
                            "stage": row["stage"],
                            "status": row["status"],
                            "title": row["title"],
                            "source": row["source"],
                            "source_id": row["source_id"],
                            **task_details(store, row["id"]),
                        }
                        for row in rows
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0
    if not rows:
        print("backlog: EMPTY")
    for row in rows:
        print(f"{row['id']} {row['stage']} {row['status']} {row['title']}")
    store.close()
    return 0


def command_plan(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    payload_path = WorkspaceBoundary(project).require_inside(Path(args.file).resolve())
    raw_payload = payload_path.read_text(encoding="utf-8")
    planner = ObjectivePlanner()
    objective = planner.parse(raw_payload)
    payload = json.loads(raw_payload)
    store = Store(db_path(project))
    store.migrate()
    store.save_objective(objective.id, objective.title, payload)
    planner.write_backlog(objective, payload, runtime_dir(project) / "backlog.json")
    sync_source(store, LocalBacklogSource(runtime_dir(project) / "backlog.json").discover())
    store.close()
    if args.json:
        print(
            json.dumps(
                {
                    "objective_id": objective.id,
                    "title": objective.title,
                    "task_count": len(objective.tasks),
                    "backlog": str((runtime_dir(project) / "backlog.json").resolve()),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"planned objective: {objective.id} ({len(objective.tasks)} tasks)")
    return 0


def command_retry_task(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    task = store.get_task(args.task)
    if task is None:
        print(f"task does not exist: {args.task}", file=sys.stderr)
        store.close()
        return 2
    if not store.unblock_task(args.task):
        print(f"task is not blocked: {args.task} (status {task['status']})", file=sys.stderr)
        store.close()
        return 2
    task = store.get_task(args.task)
    if args.json:
        print(json.dumps({"task_id": args.task, "status": task["status"], "stage": task["stage"]}, sort_keys=True))
    else:
        print(f"task {args.task} unblocked: status {task['status']} stage {task['stage']}")
    store.close()
    return 0


def _health_scope_fields(report) -> dict[str, object]:
    return {
        "ok_scope": "current",
        "current_problems": list(report.current_problems),
        "historical_failed_execution_count": report.historical_failed_execution_count,
        "current_failed_execution_count": report.current_failed_execution_count,
        "stale_execution_count": report.stale_execution_count,
    }


def command_run_ready(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    requested = str(args.task).strip() if args.task is not None else None
    if requested is not None and not requested:
        print("target task id must not be empty", file=sys.stderr)
        return 2
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    _sync_all_sources(store, project, config, requested)
    info: dict[str, object] = {}
    provider_log = ProviderLog(echo=False)

    def make_coordinator(target: TargetSelection) -> Coordinator:
        coord, details = _build_coordinator(args, project, config, store, target, provider_log)
        info.update(details)
        return coord

    def on_step(step: dict[str, object]) -> None:
        if not args.json:
            nonlocal provider_notice_index
            notices = provider_log.lines[provider_notice_index:]
            provider_notice_index = len(provider_log.lines)
            print(format_step_update(step, notices=notices), flush=True)
            print(flush=True)

    def on_start(message: str) -> None:
        if not args.json:
            print(message, flush=True)

    provider_notice_index = 0

    try:
        summary = run_ready(
            store, project, make_coordinator, task_id=requested, max_steps=getattr(args, "max_steps", 50), on_step=on_step, on_start=on_start,
            auto_plan=not getattr(args, "no_auto_plan", False),
            policy=config.task_selection,
            chooser=_interactive_chooser if getattr(args, "choose", False) else None,
            worktree_root_path=config.runtime.worktree_root if config.runtime else None,
        )
    except _SetupError as exc:
        store.close()
        return exc.code
    store.close()
    info["provider_events"] = provider_log.lines
    return _report_run_ready(summary, info, as_json=args.json)


def command_queue_run(args: argparse.Namespace) -> int:
    if args.concurrency < 1:
        print("--concurrency must be at least 1", file=sys.stderr)
        return 2
    args.parallel = args.concurrency
    return command_run_parallel(args, queue=True)


def command_run_parallel(args: argparse.Namespace, *, queue: bool = False) -> int:
    project = Path(args.project).resolve()
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    _sync_all_sources(store, project, config, None)
    gate: dict[str, object] | None = None
    if queue:
        from .queue_run import preflight

        gate = preflight(project, config, require_ref=not getattr(args, "dry_run", False))
        if not gate["ok"]:
            store.close()
            return _report_queue_refusal(gate, as_json=args.json)
    limiter = ProviderLimiter(
        config.parallel.provider_max_concurrency,
        {name: spec.max_concurrency for name, spec in config.provider_specs.items() if spec.max_concurrency is not None},
    )
    wiring = ParallelWiring(limiter, IntegrationLock(runtime_dir(project) / "integration.lock"))
    print_lock = threading.Lock()
    info: dict[str, object] = {}

    def emit(task_id: str, text: str) -> None:
        if args.json:
            return
        prefix = f"[{task_id}] " if task_id != "run" else "[run] "
        with print_lock:  # whole blocks at a time, so one task's lines never interleave with another's
            print("\n".join(prefix + line if line.strip() else prefix.rstrip() for line in text.splitlines()), flush=True)

    runner: ParallelRunner

    def make_coordinator(target: TargetSelection, task_store: Store, task_id: str) -> Coordinator:
        try:
            coord, details = _build_coordinator(args, project, config, task_store, target, runner.provider_log(task_id), parallel=wiring)
        except _SetupError as exc:
            raise SetupRefused(f"provider setup failed for task {task_id} (see stderr; exit {exc.code})") from exc
        info.update(details)
        return coord

    runner_class = QueueRunner if queue else ParallelRunner
    runner = runner_class(
        store,
        project,
        make_coordinator,
        concurrency=args.parallel,
        policy=config.task_selection,
        auto_plan=not getattr(args, "no_auto_plan", False),
        max_steps=getattr(args, "max_steps", 50),
        limiter=limiter,
        emit=emit,
    )
    wiring.on_integration_event = lambda task_id, event, detail: runner.note(task_id, event, **detail)
    emit(
        "run",
        "\n".join(
            [
                f"StageMesh {'queue-run' if queue else 'continue'}: up to {args.parallel} "
                "tasks in parallel, one worktree per task",
                f"project checkout: {project}",
                f"worktree root: {config.runtime.worktree_root if config.runtime else worktree_root(project)}",
            ]
        ),
    )
    summary = runner.run()
    store.close()
    return _report_parallel(summary, info, as_json=args.json, preflight=gate)


def _report_queue_refusal(gate: dict[str, object], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"mode": "queue", "stop_reason": "REFUSED:preflight_failed", "succeeded": False, "preflight": gate, "tasks": []}, indent=2, sort_keys=True))
    else:
        print("queue-run refused: project preflight failed", file=sys.stderr)
        smoke = gate["smoke"]  # type: ignore[index]
        for check in smoke.get("checks", []):  # type: ignore[union-attr]
            if check["status"] == "fail":
                print(f"  [FAIL] {check['name']}: {check['detail']}", file=sys.stderr)
                for item in check.get("items", []):
                    print(f"         - {item}", file=sys.stderr)
        if smoke.get("detail") and not smoke.get("ok"):  # type: ignore[union-attr]
            print(f"  {smoke['detail']}", file=sys.stderr)  # type: ignore[index]
        for check in gate["checks"]:  # type: ignore[union-attr]
            if not check["ok"]:
                print(f"  [FAIL] {check['name']}: {check.get('detail', '')}", file=sys.stderr)
    return 2


def _report_parallel(summary: ParallelSummary, info: dict[str, object], *, as_json: bool, preflight: dict[str, object] | None = None) -> int:
    if as_json:
        extra: dict[str, object] = {}
        if preflight is not None:
            refused = [t.task_id for t in summary.tasks if t.summary and t.summary.stop_reason.startswith("REFUSED:")]
            extra = {"mode": "queue", "preflight": preflight, "refused": refused}
        print(json.dumps({**summary.to_dict(), **extra, "provider_config": info}, indent=2, sort_keys=True, default=str))
    else:
        for task in summary.tasks:
            outcome = task.summary.stop_reason if task.summary else "UNSET"
            print(f"[{task.task_id}] result: {outcome}" + (f" - {task.summary.message}" if task.summary and task.summary.message else ""))
            shown = task.summary.detail.get("diagnosis") if task.summary else None
            if shown:
                print(f"[{task.task_id}]   diagnosis: {shown['category']} at {shown['stage']}: {shown['summary']}")
                print(f"[{task.task_id}]   next step: {shown['recommendation']}")
                for line in format_findings(shown.get("review_findings", [])):
                    print(f"[{task.task_id}] {line}")
        print(f"Parallel run stopped: {summary.stop_reason.replace('_', ' ').lower()}" + (f" ({summary.message})" if summary.message else ""))
    if summary.interrupted:
        return 130
    if summary.succeeded:
        return 0
    return 2 if summary.stop_reason.startswith("REFUSED:") else 1


def _interactive_chooser(candidates) -> str | None:
    """Prompt on the terminal; returns None (a refusal) when stdin is not interactive or the answer is not a listed number."""
    if not sys.stdin or not sys.stdin.isatty():
        print("--choose needs an interactive terminal", file=sys.stderr)
        return None
    for number, candidate in enumerate(candidates, start=1):
        print(f"  {number}. task {candidate.task_id}: {candidate.title[:80]} [{candidate.describe()}]", file=sys.stderr)
    answer = input("Run which task? ").strip()
    return candidates[int(answer) - 1].task_id if answer.isdigit() and 1 <= int(answer) <= len(candidates) else None


def _report_run_ready(summary: RunSummary, info: dict[str, object], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({**summary.to_dict(), "providers": info}, indent=2, sort_keys=True))
    else:
        for item in summary.recovered:
            print(f"recovered stale execution {item['execution_id']} (pid {item['pid']} dead)")
        print(format_stop(summary))
    if summary.succeeded:
        return 0
    return 2 if summary.stop_reason.startswith("REFUSED:") else 1


def command_project_smoke(args: argparse.Namespace) -> int:
    """Generic compatibility smoke: profile, generated contracts, gate safety, forbidden patterns, selection (no implementation)."""
    from .profile_smoke import run_smoke

    project = Path(args.project).resolve()
    config = load_config(project)
    report = run_smoke(
        project,
        config,
        task_ids=args.task,
        dry_run_selection=args.dry_run_selection,
        sync=lambda store, targeted: _sync_all_sources(store, project, config, targeted),
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return 0 if report.ok else 1
    marks = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}
    print(f"StageMesh project smoke: {project}" + (f" (profile {report.profile})" if report.profile else ""))
    for check in report.checks:
        print(f"  [{marks[check.status]}] {check.name}: {check.detail}")
        for item in check.items:
            print(f"         - {item}")
    for probe in report.probes:
        print(f"  probe {probe['title']!r}: expected {probe['expected']}, selected {probe['selected']} ({'ok' if probe['ok'] else 'MISMATCH'})")
    for task in report.tasks:
        if "task_type" in task:
            print(f"  task {task['task_id']}: type {task['task_type']} ({task['selection']['source']}); gates {', '.join(task['gates'])}")
    if report.selection:
        nxt = report.selection["next"]
        print("  next task: " + (f"{nxt['task_id']} - {nxt['reason']}" if "task_id" in nxt else str(nxt.get("message"))))
    print("Smoke " + ("passed" if report.ok else "FAILED"))
    return 0 if report.ok else 1


def command_profile(args: argparse.Namespace) -> int:
    """Validate the project profile; with --task, show the task type, contract scope and gates it would generate (no writes)."""
    from .auto_plan import AutoPlanError, profile_payload, validate_generated
    from .profile import ProfileError, expand_gate, load_profile

    project = Path(args.project).resolve()
    try:
        profile = load_profile(project)
    except ProfileError as exc:
        print(f"profile invalid: {exc}", file=sys.stderr)
        return 2
    if profile is None:
        print("no project profile (.stagemesh/profile.json); auto-planning falls back to root-level gate detection", file=sys.stderr)
        return 1
    report: dict[str, object] = {
        "name": profile.name,
        "task_types": {
            t.id: {"level": t.level, "gates": [expand_gate(profile, g)["name"] for g in t.gates], "allowed_files": list(t.allowed_files)}
            for t in profile.types.values()
        },
        "forbidden_files": list(profile.forbidden_files),
        "default_type": profile.default_type,
        "escalation_type": profile.escalation_type,
    }
    if args.task:
        import tempfile

        scratch = tempfile.TemporaryDirectory(prefix="stagemesh-profile-")
        store = Store(db_path(project))
        store.migrate()
        try:
            if store.get_task(args.task) is None:  # not synced yet: sync into a throwaway store so this stays read-only
                store.close()
                store = Store(Path(scratch.name) / "scratch.sqlite3")
                store.migrate()
                _sync_all_sources(store, project, load_config(project), args.task)
            planned = profile_payload(store, project, args.task)
            payload, info = planned  # type: ignore[misc]
            selection = info["selection"]
            validate_generated(payload)
        except (AutoPlanError, TypeError) as exc:
            print(f"cannot plan task {args.task}: {getattr(exc, 'message', exc)}", file=sys.stderr)
            store.close()
            scratch.cleanup()
            return 2
        store.close()
        scratch.cleanup()
        report["task"] = {
            "task_id": args.task,
            "selection": selection,
            "allowed_files": payload["allowed_files"],
            "forbidden_files": payload["forbidden_files"],
            "gates": [g["name"] for g in payload["required_tests"]],
            "max_changed_files": payload["max_changed_files"],
            "max_diff_lines": payload["max_diff_lines"],
            "validation_classification": payload["validation_classification"],
        }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"profile {profile.name}: {len(profile.types)} task types")
        for type_id, info in report["task_types"].items():  # type: ignore[union-attr]
            print(f"  {type_id} ({info['level']}): {', '.join(info['gates'])}")
        if "task" in report:
            task = report["task"]  # type: ignore[assignment]
            print(f"task {task['task_id']}: type {task['selection']['type']} via {task['selection']['source']} "  # type: ignore[index]
                  f"({'; '.join(task['selection']['evidence'])}) -> gates {', '.join(task['gates'])}")  # type: ignore[index]
    return 0


def command_recover_stale(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    try:
        if args.release_unknown:
            if not args.execution:
                raise OperatorActionError("--release-unknown needs --execution <id>: it releases one named execution, never a sweep")
            actions = [release_unknown_execution(store, args.task, args.execution, args.reason or "")]
        elif args.execution or args.reason:
            raise OperatorActionError("--execution and --reason only apply together with --release-unknown")
        else:
            actions = recover_stale(store, args.task)
    except OperatorActionError as exc:
        print(str(exc), file=sys.stderr)
        store.close()
        return 2
    task = store.get_task(args.task)
    payload = {
        "task_id": args.task,
        "stage": task["stage"],
        "status": task["status"],
        "actions": [action.to_dict() for action in actions],
        "released": sum(1 for action in actions if action.action == "RELEASED"),
    }
    store.close()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for action in actions:
            print(f"{action.execution_id} pid={action.pid} {action.process_state} -> {action.action}")
        print(f"released: {payload['released']} task {args.task} {payload['stage']} {payload['status']}")
    return 0


def command_adopt_candidate(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    try:
        report = adopt_candidate(store, project, args.task, args.sha, args.producer, validate=args.validate)
    except (OperatorActionError, StoreValidationError) as exc:
        print(f"adopt-candidate rejected: {exc}", file=sys.stderr)
        store.close()
        return 2
    store.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"adopted {report['candidate_sha']} for task {args.task}: {report['stage']} {report['status']}")
        if report["validation"]:
            print(f"validation: {report['validation']}")
    return 0


def command_health(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = health(store)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "task_count": report.task_count,
                    "blocked_task_count": report.blocked_task_count,
                    "running_count": report.running_count,
                    "done_count": report.done_count,
                    "failed_execution_count": report.failed_execution_count,
                    "unknown_execution_count": report.unknown_execution_count,
                    "backlog_state": report.backlog_state,
                    "latest_implementation_failure": report.latest_implementation_failure,
                    **_health_scope_fields(report),
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0
    print(f"ok: {report.ok} (current state only)")
    if report.current_problems:
        print(f"current_problems: {', '.join(report.current_problems)}")
    print(f"tasks: {report.task_count}")
    print(f"blocked_tasks: {report.blocked_task_count}")
    print(f"running: {report.running_count}")
    print(f"done: {report.done_count}")
    print(f"failed_executions_historical: {report.historical_failed_execution_count}")
    print(f"failed_executions_current: {report.current_failed_execution_count}")
    print(f"stale_running_executions: {report.stale_execution_count}")
    print(f"unknown_executions: {report.unknown_execution_count}")
    if report.latest_implementation_failure:
        print(f"latest_implementation_failure: {report.latest_implementation_failure.get('reason')}")
    print(f"backlog: {report.backlog_state}")
    store.close()
    return 0


def command_worker(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    capabilities = set(args.capability or ["code"])
    register_worker(store, args.worker_id, args.provider, capabilities, current_process_identity(), args.lease_seconds)
    heartbeat_worker(store, args.worker_id, args.lease_seconds)
    store.close()
    if args.json:
        print(
            json.dumps(
                {
                    "worker_id": args.worker_id,
                    "provider": args.provider,
                    "capabilities": sorted(capabilities),
                    "lease_seconds": args.lease_seconds,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"worker: {args.worker_id}")
    return 0


def command_operator(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = operator_report(store)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": report.summary,
                    "lines": list(report.lines),
                    "sections": [
                        {"name": section.name, "rows": [dict(row) for row in section.rows]}
                        for section in report.sections
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0
    print(f"summary: {report.summary}")
    for line in report.lines:
        print(line)
    store.close()
    return 0


def command_dashboard(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    output.parent.mkdir(parents=True, exist_ok=True)
    html = render_dashboard(store)
    output.write_text(html, encoding="utf-8")
    summary = dashboard_summary(store)
    sections = ["Status Summary"] + [section.name for section in operator_report(store).sections]
    store.close()
    if args.json:
        print(
            json.dumps(
                {
                    "output": str(output),
                    "bytes": output.stat().st_size,
                    "summary": summary,
                    "sections": sections,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"dashboard: {output}")
    return 0


def command_demo(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    demo = create_demo_project(project, Path(args.output).resolve())
    if args.json:
        print(
            json.dumps(
                {
                    "root": str(demo.root),
                    "objective": str(demo.objective),
                    "readme": str(demo.readme),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"demo: {demo.root}")
    print(f"objective: {demo.objective}")
    print(f"readme: {demo.readme}")
    return 0


def command_release(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    artifact = build_release_artifact(project, Path(args.output).resolve(), __version__, args.candidate_sha)
    if args.json:
        manifest_data = json.loads(artifact.manifest.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "archive": str(artifact.archive),
                    "manifest": str(artifact.manifest),
                    "checksums": str(artifact.checksums),
                    "version": manifest_data["version"],
                    "candidate_sha": manifest_data["candidate_sha"],
                    "file_count": manifest_data["file_count"],
                    "archive_size": artifact.archive.stat().st_size,
                    "checksums_size": artifact.checksums.stat().st_size,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"archive: {artifact.archive}")
    print(f"manifest: {artifact.manifest}")
    print(f"checksums: {artifact.checksums}")
    return 0


def command_work(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    queue = WorkQueue(store)
    if args.work_command == "enqueue":
        packet_id = queue.enqueue(args.task_id, args.stage, args.worker_id, args.candidate_sha)
        if args.json:
            print(json.dumps({"packet_id": packet_id}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"packet: {packet_id}")
    elif args.work_command == "poll":
        packets = queue.poll(args.worker_id, args.limit, args.lease_seconds)
        if args.json:
            print(
                json.dumps(
                    {
                        "packets": [
                            {
                                "id": packet.id,
                                "task_id": packet.task_id,
                                "stage": packet.stage,
                                "candidate_sha": packet.candidate_sha,
                                "payload": packet.payload,
                            }
                            for packet in packets
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        for packet in packets:
            print(f"{packet.id} {packet.task_id} {packet.stage} {packet.candidate_sha or ''}")
    elif args.work_command == "list":
        packets = queue.list()
        if args.json:
            print(
                json.dumps(
                    {
                        "packets": [
                            {
                                "id": packet.id,
                                "task_id": packet.task_id,
                                "stage": packet.stage,
                                "worker_id": packet.worker_id,
                                "candidate_sha": packet.candidate_sha,
                                "status": packet.status,
                                "payload": packet.payload,
                                "created_at": packet.created_at,
                                "updated_at": packet.updated_at,
                            }
                            for packet in packets
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        if not packets:
            print("work: EMPTY")
        for packet in packets:
            print(f"{packet.id} {packet.task_id} {packet.stage} {packet.status} {packet.worker_id or ''} {packet.candidate_sha or ''}")
    elif args.work_command == "renew":
        renewed = queue.renew(args.packet_id, args.worker_id)
        if args.json:
            print(json.dumps({"packet_id": args.packet_id, "renewed": renewed}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"renewed: {renewed}")
    elif args.work_command == "ack":
        queue.ack(args.packet_id, args.status)
        if args.json:
            print(json.dumps({"packet_id": args.packet_id, "status": args.status.upper()}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"ack: {args.packet_id}")
    elif args.work_command == "export":
        output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        packet = queue.export(args.packet_id)
        write_packet_envelope(packet, output)
        if args.json:
            print(json.dumps({"packet_id": args.packet_id, "output": str(output)}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"work-packet: {output}")
    elif args.work_command == "ack-file":
        output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        write_ack_envelope(args.packet_id, args.status, output)
        if args.json:
            print(
                json.dumps(
                    {"packet_id": args.packet_id, "status": args.status.upper(), "output": str(output)},
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        print(f"work-ack: {output}")
    elif args.work_command == "import-ack":
        ack_path = WorkspaceBoundary(project).require_inside(Path(args.file).resolve())
        ack = import_ack(queue, ack_path)
        if args.json:
            print(json.dumps({"packet_id": ack.packet_id, "status": ack.status}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"imported-ack: {ack.packet_id}")
    store.close()
    return 0


def command_report(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = render_final_report(project, store)
    output_path: Path | None = None
    if args.output:
        output_path = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    if args.json:
        print(
            json.dumps(
                {
                    "candidate_sha": candidate_sha(project),
                    "output": str(output_path) if output_path else None,
                    "bytes": len(report.encode("utf-8")),
                    "runtime_task_count": len(store.tasks()),
                    "registered_worker_count": len(store.workers()),
                    "external_evidence_records_for_candidate": sum(
                        1
                        for row in store.external_evidence()
                        if row["candidate_sha"] == candidate_sha(project) and row["status"] == "PASS"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif output_path:
        print(f"report: {output_path}")
    else:
        print(report)
    store.close()
    return 0


def command_acceptance_report(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    data = local_acceptance_report(project, include_acceptance=not args.skip_acceptance)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"acceptance-report: {output}")
    return 0


def command_acceptance_matrix(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    store = Store(db_path(project))
    store.migrate()
    data = acceptance_matrix(store, candidate_sha(project))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    store.close()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"acceptance-matrix: {output}")
    return 0


def command_end_to_end_acceptance(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    data = end_to_end_acceptance()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"end-to-end-acceptance: {output}")
    return 0


def command_registry(args: argparse.Namespace) -> int:
    registry = GlobalRegistry(Path(args.registry).resolve())
    projects = registry.load()
    if args.json:
        print(
            json.dumps(
                {
                    "projects": [
                        {"name": project.name, "path": str(project.path), "db_path": str(project.db_path)}
                        for project in projects
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for project in projects:
        print(f"{project.name} {project.path} {project.db_path}")
    return 0


def command_capacity(args: argparse.Namespace) -> int:
    registry = CapacityRegistry()
    registry.record(args.primary, CapacityKind.AVAILABLE if not args.primary_down else CapacityKind.CAPACITY)
    registry.record(args.secondary, CapacityKind.AVAILABLE if not args.secondary_down else CapacityKind.CAPACITY)
    chosen = registry.choose_primary_secondary(args.primary, args.secondary)
    if args.json:
        print(
            json.dumps(
                {
                    "chosen": chosen,
                    "providers": registry.snapshot((args.primary, args.secondary)),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"chosen: {chosen or 'NONE'}")
    return 0


def command_config(args: argparse.Namespace) -> int:
    config = load_config(Path(args.project).resolve(), Path(args.config).resolve() if args.config else None)
    display_database_url = redact_url_credentials(config.database_url) or "sqlite://default"
    display_provider_commands = {
        name: redact_command_secrets(command) for name, command in sorted(config.provider_commands.items())
    }
    if args.json:
        print(
            json.dumps(
                {
                    "source": config.source,
                    "github": {
                        "owner": config.github.owner,
                        "repo": config.github.repo,
                        "configured": config.github.configured,
                    },
                    "database_url": display_database_url,
                    "routing": {
                        "mode": config.routing_mode,
                        "single_agent_provider": config.single_agent_provider,
                        "stage_routes": dict(sorted(config.stage_routes.items())),
                    },
                    "providers": display_provider_commands,
                    "task_sources": [
                        {"name": source.name, "type": source.kind, "path": str(source.path)}
                        for source in config.task_sources
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"source: {config.source}")
    print(f"github.owner: {config.github.owner or ''}")
    print(f"github.repo: {config.github.repo or ''}")
    print(f"github.configured: {config.github.configured}")
    print(f"database_url: {display_database_url}")
    print(f"routing.mode: {config.routing_mode}")
    print(f"routing.single_agent_provider: {config.single_agent_provider or ''}")
    for stage, provider in sorted(config.stage_routes.items()):
        print(f"routing.stage.{stage}: {provider}")
    for name, command in display_provider_commands.items():
        print(f"provider.{name}: {command}")
    for source in config.task_sources:
        print(f"task_source.{source.name}: {source.kind} {source.path}")
    return 0


def command_backend(args: argparse.Namespace) -> int:
    config = load_config(Path(args.project).resolve(), Path(args.config).resolve() if args.config else None)
    probe = probe_backend(config.database_url, db_path(Path(args.project).resolve()))
    postgres_contract = postgres_schema_contract()
    display_database_url = redact_url_credentials(config.database_url) or "sqlite://default"
    ping_result = None
    migration_applied = None
    if args.migrate and probe.name != "postgres":
        migration_applied = False
    if (args.ping or args.migrate) and config.database_url and probe.name == "postgres":
        try:
            store = PostgresStore(config.database_url)
        except PostgresUnavailable as exc:
            print(f"backend error: {exc}", file=sys.stderr)
            return 2
        try:
            if args.migrate:
                store.migrate()
                migration_applied = True
            if args.ping:
                ping_result = store.ping()
        finally:
            store.close()
    if args.json:
        print(
            json.dumps(
                {
                    "name": probe.name,
                    "available": probe.available,
                    "reason": probe.reason,
                    "database_url": display_database_url,
                    "postgres_schema_contract": {
                        "table_count": len(postgres_contract["tables"]),
                        "tables": list(postgres_contract["tables"]),
                    },
                    "ping": ping_result,
                    "migration_applied": migration_applied,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"name: {probe.name}")
    print(f"available: {probe.available}")
    print(f"reason: {probe.reason}")
    print(f"postgres schema contract: {len(postgres_contract['tables'])} tables")
    if ping_result is not None:
        print(f"ping: {ping_result}")
    if migration_applied is not None:
        print(f"migration_applied: {migration_applied}")
    return 0


def command_provider_acceptance(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    result = run_provider_acceptance(store, project)
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "chosen_provider": result.chosen_provider,
                    "execution_status": str(result.execution_status),
                    "capacity_failure_isolated": result.capacity_failure_isolated,
                    "single_agent_provider": result.single_agent_provider,
                    "review_provider": result.review_provider,
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0 if result.status == "PASS" else 1
    print(f"status: {result.status}")
    print(f"chosen_provider: {result.chosen_provider}")
    print(f"execution_status: {result.execution_status}")
    print(f"capacity_failure_isolated: {result.capacity_failure_isolated}")
    print(f"single_agent_provider: {result.single_agent_provider}")
    print(f"review_provider: {result.review_provider}")
    store.close()
    return 0 if result.status == "PASS" else 1


def command_github_acceptance(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    result = run_github_acceptance(store)
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "discovered": result.discovered,
                    "deferred_skipped": result.deferred_skipped,
                    "outbound_status": result.outbound_status,
                    "rate_limit_status": result.rate_limit_status,
                    "detected_repo": {
                        "owner": result.detected_owner,
                        "repo": result.detected_repo,
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0 if result.status == "PASS" else 1
    print(f"status: {result.status}")
    print(f"discovered: {result.discovered}")
    print(f"deferred_skipped: {result.deferred_skipped}")
    print(f"outbound_status: {result.outbound_status}")
    print(f"rate_limit_status: {result.rate_limit_status}")
    print(f"detected_repo: {result.detected_owner}/{result.detected_repo}")
    store.close()
    return 0 if result.status == "PASS" else 1


def command_release_readiness(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    store = Store(db_path(project))
    store.migrate()
    try:
        data = release_readiness(
            project,
            include_acceptance=not args.skip_acceptance,
            run_checks=not args.skip_checks,
            store=store,
            candidate_sha=candidate_sha(project),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
    finally:
        store.close()
    print(f"release-readiness: {output}")
    return 0


def command_validation_plan(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    try:
        task = store.get_task(args.task)
        if task is None:
            print(f"task not found: {args.task}", file=sys.stderr)
            return 2
        candidate = store.latest_candidate(args.task)
        changed: tuple[str, ...] = ()
        if candidate is not None:
            from .contract_binding import contract_for_candidate
            from .contracts import changed_files
            from .git import GitError

            bound = contract_for_candidate(store, args.task, candidate["sha"], project)
            contract = bound.contract
            try:
                changed = tuple(changed_files(project, candidate["sha"], bound.baseline_sha))
            except (GitError, OSError):
                changed = ()
        else:
            from .contracts import load_contract

            contract = load_contract(project, args.task)
        plan = derive_validation_plan(contract, changed)
        data = {
            "task_id": args.task,
            "task_title": task["title"],
            "candidate_sha": candidate["sha"] if candidate is not None else None,
            "changed_files": list(changed),
            "validation_plan": plan.to_dict(),
        }
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
    finally:
        store.close()
    print(f"{args.task}: {plan.classification} {plan.risk_level}")
    for check in plan.planned_checks:
        print(f"- {check}")
    for reason in plan.escalation_reasons:
        print(f"escalation: {reason}")
    return 0


def command_completion_audit(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    store = Store(db_path(project))
    store.migrate()
    data = completion_audit(store, candidate_sha(project))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    store.close()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"completion-audit: {output}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    if args.output:
        output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        export_audit_jsonl(store, output, args.limit, root=project)
        print(f"audit: {output}")
    elif args.json:
        print(
            json.dumps(
                {
                    "events": [
                        {
                            "id": event["id"],
                            "event_type": event["event_type"],
                            "payload": json.loads(event["payload"]),
                            "created_at": event["created_at"],
                        }
                        for event in store.audit_events(args.limit)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for event in store.audit_events(args.limit):
            print(f"{event['id']} {event['event_type']} {event['created_at']}")
    store.close()
    return 0


def command_retries(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    registry = RetryRegistry(store)
    if args.retry_command == "fail":
        decision = registry.record_failure(args.key, args.reason)
        if args.json:
            print(
                json.dumps(
                    {
                        "key": decision.key,
                        "allowed": decision.allowed,
                        "attempts": decision.attempts,
                        "next_attempt_at": decision.next_attempt_at,
                        "reason": decision.reason,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        print(f"{decision.key} attempts={decision.attempts} next_attempt_at={decision.next_attempt_at}")
    elif args.retry_command == "success":
        registry.record_success(args.key)
        if args.json:
            print(json.dumps({"key": args.key, "cleared": True}, indent=2, sort_keys=True))
            store.close()
            return 0
        print(f"{args.key} cleared")
    else:
        rows = store.retry_states()
        if args.json:
            print(
                json.dumps(
                    {
                        "retries": [
                            {
                                "key": row["key"],
                                "attempts": row["attempts"],
                                "next_attempt_at": row["next_attempt_at"],
                                "reason": row["reason"],
                            }
                            for row in rows
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        if not rows:
            print("retries: EMPTY")
        for row in rows:
            print(f"{row['key']} attempts={row['attempts']} next_attempt_at={row['next_attempt_at']} reason={row['reason']}")
    store.close()
    return 0


def command_evidence(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    if args.evidence_command == "add":
        evidence_id = record_external_evidence(
            store,
            args.kind,
            args.status,
            args.url,
            candidate_sha=args.candidate_sha,
            notes=args.notes,
        )
        if args.json:
            current_candidate = candidate_sha(project)
            print(
                json.dumps(
                    {
                        "id": evidence_id,
                        "kind": args.kind,
                        "status": args.status.upper(),
                        "url": args.url,
                        "candidate_sha": args.candidate_sha,
                        "candidate_match": args.candidate_sha == current_candidate,
                        "notes": args.notes,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        print(f"evidence: {evidence_id}")
    else:
        rows = external_evidence_records(store)
        if args.json:
            current_candidate = candidate_sha(project)
            print(
                json.dumps(
                    {
                        "candidate_sha": current_candidate,
                        "records": [
                            {
                                "id": row.id,
                                "kind": row.kind,
                                "status": row.status,
                                "url": row.url,
                                "candidate_sha": row.candidate_sha,
                                "candidate_match": row.candidate_sha == current_candidate,
                                "notes": row.notes,
                            }
                            for row in rows
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            store.close()
            return 0
        if not rows:
            print("evidence: EMPTY")
        for row in rows:
            print(f"{row.id} {row.kind} {row.status} {row.candidate_sha or ''} {row.url}")
    store.close()
    return 0


def command_ci(args: argparse.Namespace) -> int:
    root = Path(args.project).resolve()
    results = default_gates(root, include_acceptance=not args.skip_acceptance)
    if args.future_feature_gate:
        results.append(broken_future_feature_gate(root))
    if args.json:
        print(
            json.dumps(
                {
                    "status": "PASS" if all(result.passed for result in results) else "FAIL",
                    "gates": [
                        {
                            "name": result.name,
                            "passed": result.passed,
                            "output": result.output[-4000:],
                        }
                        for result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if all(result.passed for result in results) else 1
    for result in results:
        print(f"{result.name}: {'PASS' if result.passed else 'FAIL'}")
        if not result.passed and result.output:
            print(result.output[-4000:])
    return 0 if all(result.passed for result in results) else 1


def command_ci_wait(args: argparse.Namespace) -> int:
    decision = decide_ci_wait(args.status, args.elapsed_seconds, args.max_seconds)
    if args.json:
        print(
            json.dumps(
                {
                    "should_wait": decision.should_wait,
                    "release_worker": decision.release_worker,
                    "poll_after_seconds": decision.poll_after_seconds,
                    "reason": decision.reason,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"should_wait: {decision.should_wait}")
    print(f"release_worker: {decision.release_worker}")
    print(f"poll_after_seconds: {decision.poll_after_seconds}")
    print(f"reason: {decision.reason}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stagemesh")
    parser.add_argument("--project", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--task")
    init.add_argument("--register", action="store_true")
    init.add_argument("--registry", default=str(Path.home() / ".stagemesh" / "registry.json"))
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=command_init)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)
    diagnose_cmd = sub.add_parser(
        "diagnose", help="Explain why a task keeps failing: failing evidence, comparison across candidates, category, next step"
    )
    diagnose_cmd.add_argument("--task", required=True)
    diagnose_cmd.add_argument("--threshold", type=int, help="Identical failures that count as a repeat (default: config, 2)")
    diagnose_cmd.add_argument("--provider", help="Also run a separate read-only diagnostic provider pass with this provider")
    diagnose_cmd.add_argument("--json", action="store_true")
    diagnose_cmd.set_defaults(func=command_diagnose)
    rebind = sub.add_parser("rebind-contract", help="Re-read .stagemesh/contracts/<id>.json and replace the task's frozen contract (audited; never edit SQLite)")
    rebind.add_argument("--task", required=True)
    rebind.add_argument("--validate", action="store_true", help="Validate the latest candidate against the new contract and advance if it passes")
    rebind.add_argument("--force", action="store_true", help="Accept that passed evidence bound to the old contract stops counting (it is kept as history)")
    rebind.add_argument("--reason", help="Why the contract changed (recorded in the audit event)")
    rebind.add_argument("--json", action="store_true")
    rebind.set_defaults(func=command_rebind_contract)
    rebase = sub.add_parser("rebaseline-task", help="Move a stale task baseline to the candidate's merge-base with the integration ref (audited)")
    rebase.add_argument("--task", required=True)
    rebase.add_argument("--to", required=True, metavar="INTEGRATION_REF", help="Integration ref the baseline is recomputed against")
    rebase.add_argument("--validate", action="store_true", help="Re-validate the latest candidate afterwards and advance if it passes")
    rebase.add_argument("--force", action="store_true", help="Proceed in ambiguous cases (candidate not based on the baseline, diverged history)")
    rebase.add_argument("--reason", help="Why the baseline moved (recorded in the audit event)")
    rebase.add_argument("--json", action="store_true")
    rebase.set_defaults(func=command_rebaseline_task)
    doctor_task = sub.add_parser("task-doctor", help="Read-only task summary: claims, candidate, baseline, contract, failures, findings, diagnosis, next command")
    doctor_task.add_argument("--task", required=True)
    doctor_task.add_argument("--to", metavar="INTEGRATION_REF", help="Integration ref to compare the baseline with (default: configured ref or current branch)")
    doctor_task.add_argument("--json", action="store_true")
    doctor_task.set_defaults(func=command_task_doctor)
    timing_cmd = sub.add_parser("task-timing", help="Read-only per-execution timing (actor, started, finished, duration, result) and a task timing summary")
    timing_cmd.add_argument("--task", required=True)
    timing_cmd.add_argument("--verbose", action="store_true", help="Show task, candidate SHA, started and finished times")
    timing_cmd.add_argument("--json", action="store_true")
    timing_cmd.set_defaults(func=command_task_timing)
    queue_cmd = sub.add_parser(
        "queue-run", help="Run several ready tasks at once when their contracts do not conflict (strict admission, serial integration)"
    )
    queue_cmd.add_argument("--concurrency", type=int, required=True, metavar="N", help="Maximum tasks running at once")
    queue_cmd.add_argument("--max-steps", type=int, default=50, help="Step budget for each task")
    queue_cmd.add_argument("--no-auto-plan", action="store_true", help="Refuse tasks without a contract instead of auto-planning one")
    queue_cmd.add_argument("--json", action="store_true")
    queue_cmd.add_argument("--provider", help="Provider name to use for implementation (e.g. claude, codex)")
    queue_cmd.add_argument("--dry-run", action="store_true", help="Use FakeExecutor instead of a real provider")
    queue_cmd.set_defaults(func=command_queue_run)
    cont = sub.add_parser("continue")
    cont.add_argument(
        "--once",
        action="store_true",
        help="Run a single coordinator pass (legacy behavior) instead of supervising one task to completion",
    )
    cont.add_argument("--choose", "--interactive", dest="choose", action="store_true", help="Pick among eligible tasks interactively")
    cont.add_argument("--no-auto-plan", action="store_true", help="Refuse instead of generating a missing change contract")
    cont.add_argument("--max-steps", type=int, default=50, help="Step budget for the supervised default mode")
    cont.add_argument(
        "--parallel",
        type=int,
        metavar="N",
        help="Run up to N independent eligible tasks at once, each in its own worktree, with serialized integration",
    )
    cont.add_argument("--json", action="store_true")
    cont.add_argument("--provider", help="Provider name to use for implementation (e.g. claude, codex)")
    cont.add_argument("--dry-run", action="store_true", help="Use FakeExecutor instead of a real provider")
    cont.add_argument("--task", dest="task", help="Run exactly one selected task id")
    cont.add_argument("--task-id", dest="task", help=argparse.SUPPRESS)
    cont.set_defaults(func=command_continue)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)
    plan = sub.add_parser("plan")
    plan.add_argument("file")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=command_plan)
    retry_task = sub.add_parser("retry-task", help="Return a BLOCKED task to OPEN with a fresh remediation budget")
    retry_task.add_argument("--task", required=True)
    retry_task.add_argument("--json", action="store_true")
    retry_task.set_defaults(func=command_retry_task)
    run_ready_cmd = sub.add_parser(
        "run-ready", help="Run one ready task to completion under supervision (sync, select, tick, recover dead claims)"
    )
    run_ready_cmd.add_argument("--task", help="Run this task id instead of the single eligible OPEN task")
    run_ready_cmd.add_argument("--max-steps", type=int, default=50)
    run_ready_cmd.add_argument("--provider", help="Provider name to use for implementation")
    run_ready_cmd.add_argument("--dry-run", action="store_true", help="Use FakeExecutor instead of a real provider")
    run_ready_cmd.add_argument("--choose", "--interactive", dest="choose", action="store_true", help="Pick among eligible tasks interactively")
    run_ready_cmd.add_argument("--no-auto-plan", action="store_true", help="Refuse instead of generating a missing change contract")
    run_ready_cmd.add_argument("--json", action="store_true")
    run_ready_cmd.set_defaults(func=command_run_ready)
    smoke_cmd = sub.add_parser(
        "project-smoke", help="Check this project's profile is safe and usable by this StageMesh version (no implementation runs)"
    )
    smoke_cmd.add_argument("--task", action="append", help="Show the type, contract and gates this task would get (repeatable)")
    smoke_cmd.add_argument(
        "--dry-run-selection", action="store_true", help="Also dry-run task selection and auto-planning against discovered tasks"
    )
    smoke_cmd.add_argument("--json", action="store_true")
    smoke_cmd.set_defaults(func=command_project_smoke)
    profile_cmd = sub.add_parser("profile", help="Validate the project profile; --task shows the contract it would generate")
    profile_cmd.add_argument("--task", help="Task id to explain (read-only)")
    profile_cmd.add_argument("--json", action="store_true")
    profile_cmd.set_defaults(func=command_profile)
    recover = sub.add_parser("recover-stale", help="Release stale claims/executions whose process is provably dead (--release-unknown for an inspected unknown one)")
    recover.add_argument("--task", required=True)
    recover.add_argument(
        "--release-unknown",
        action="store_true",
        help="Operator override: terminalize ONE running execution whose process identity is unknown (needs --execution and --reason)",
    )
    recover.add_argument("--execution", help="Execution id to release with --release-unknown")
    recover.add_argument("--reason", help="What you inspected that shows the execution is dead (recorded in the audit log)")
    recover.add_argument("--json", action="store_true")
    recover.set_defaults(func=command_recover_stale)
    adopt = sub.add_parser("adopt-candidate", help="Register an existing commit as the task's latest candidate")
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--sha", required=True)
    adopt.add_argument("--producer", default="manual-reconciliation")
    adopt.add_argument("--validate", action="store_true", help="Run validation and advance to REVIEW on pass")
    adopt.add_argument("--json", action="store_true")
    adopt.set_defaults(func=command_adopt_candidate)
    health_cmd = sub.add_parser("health")
    health_cmd.add_argument("--json", action="store_true")
    health_cmd.set_defaults(func=command_health)
    capacity = sub.add_parser("capacity")
    capacity.add_argument("--primary", default="codex")
    capacity.add_argument("--secondary", default="claude")
    capacity.add_argument("--primary-down", action="store_true")
    capacity.add_argument("--secondary-down", action="store_true")
    capacity.add_argument("--json", action="store_true")
    capacity.set_defaults(func=command_capacity)
    config = sub.add_parser("config")
    config.add_argument("--config")
    config.add_argument("--json", action="store_true")
    config.set_defaults(func=command_config)
    backend = sub.add_parser("backend")
    backend.add_argument("--config")
    backend.add_argument("--ping", action="store_true")
    backend.add_argument("--migrate", action="store_true")
    backend.add_argument("--json", action="store_true")
    backend.set_defaults(func=command_backend)
    provider_acceptance = sub.add_parser("provider-acceptance")
    provider_acceptance.add_argument("--json", action="store_true")
    provider_acceptance.set_defaults(func=command_provider_acceptance)
    github_acceptance = sub.add_parser("github-acceptance")
    github_acceptance.add_argument("--json", action="store_true")
    github_acceptance.set_defaults(func=command_github_acceptance)
    readiness = sub.add_parser("release-readiness")
    readiness.add_argument("--output", default=".stagemesh/release-readiness.json")
    readiness.add_argument("--skip-acceptance", action="store_true")
    readiness.add_argument("--skip-checks", action="store_true")
    readiness.add_argument("--json", action="store_true")
    readiness.set_defaults(func=command_release_readiness)
    validation_plan = sub.add_parser("validation-plan")
    validation_plan.add_argument("--task", required=True)
    validation_plan.add_argument("--json", action="store_true")
    validation_plan.set_defaults(func=command_validation_plan)
    worker = sub.add_parser("worker")
    worker.add_argument("worker_id")
    worker.add_argument("--provider", default="local")
    worker.add_argument("--capability", action="append")
    worker.add_argument("--lease-seconds", type=float, default=300)
    worker.add_argument("--json", action="store_true")
    worker.set_defaults(func=command_worker)
    operator = sub.add_parser("operator")
    operator.add_argument("--json", action="store_true")
    operator.set_defaults(func=command_operator)
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--output", default="stagemesh-dashboard.html")
    dashboard.add_argument("--json", action="store_true")
    dashboard.set_defaults(func=command_dashboard)
    demo = sub.add_parser("demo")
    demo.add_argument("--output", default=".stagemesh/demo-project")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=command_demo)
    release = sub.add_parser("release")
    release.add_argument("--candidate-sha", required=True)
    release.add_argument("--output", default="dist")
    release.add_argument("--json", action="store_true")
    release.set_defaults(func=command_release)
    work = sub.add_parser("work")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    enqueue = work_sub.add_parser("enqueue")
    enqueue.add_argument("task_id")
    enqueue.add_argument("--stage", default="IMPLEMENT")
    enqueue.add_argument("--worker-id")
    enqueue.add_argument("--candidate-sha")
    enqueue.add_argument("--json", action="store_true")
    enqueue.set_defaults(func=command_work)
    poll = work_sub.add_parser("poll")
    poll.add_argument("worker_id")
    poll.add_argument("--limit", type=int, default=1)
    poll.add_argument("--lease-seconds", type=float, default=300)
    poll.add_argument("--json", action="store_true")
    poll.set_defaults(func=command_work)
    work_list = work_sub.add_parser("list")
    work_list.add_argument("--json", action="store_true")
    work_list.set_defaults(func=command_work)
    renew = work_sub.add_parser("renew")
    renew.add_argument("packet_id")
    renew.add_argument("worker_id")
    renew.add_argument("--json", action="store_true")
    renew.set_defaults(func=command_work)
    ack = work_sub.add_parser("ack")
    ack.add_argument("packet_id")
    ack.add_argument("--status", default="SUCCEEDED")
    ack.add_argument("--json", action="store_true")
    ack.set_defaults(func=command_work)
    work_export = work_sub.add_parser("export")
    work_export.add_argument("packet_id")
    work_export.add_argument("--output", required=True)
    work_export.add_argument("--json", action="store_true")
    work_export.set_defaults(func=command_work)
    ack_file = work_sub.add_parser("ack-file")
    ack_file.add_argument("packet_id")
    ack_file.add_argument("--status", default="SUCCEEDED")
    ack_file.add_argument("--output", required=True)
    ack_file.add_argument("--json", action="store_true")
    ack_file.set_defaults(func=command_work)
    import_ack_cmd = work_sub.add_parser("import-ack")
    import_ack_cmd.add_argument("file")
    import_ack_cmd.add_argument("--json", action="store_true")
    import_ack_cmd.set_defaults(func=command_work)
    registry = sub.add_parser("registry")
    registry.add_argument("--registry", default=str(Path.home() / ".stagemesh" / "registry.json"))
    registry.add_argument("--json", action="store_true")
    registry.set_defaults(func=command_registry)
    report = sub.add_parser("report")
    report.add_argument("--output")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=command_report)
    acceptance_report = sub.add_parser("acceptance-report")
    acceptance_report.add_argument("--output", default=".stagemesh/acceptance-report.json")
    acceptance_report.add_argument("--skip-acceptance", action="store_true")
    acceptance_report.add_argument("--json", action="store_true")
    acceptance_report.set_defaults(func=command_acceptance_report)
    acceptance_matrix = sub.add_parser("acceptance-matrix")
    acceptance_matrix.add_argument("--output", default=".stagemesh/acceptance-matrix.json")
    acceptance_matrix.add_argument("--json", action="store_true")
    acceptance_matrix.set_defaults(func=command_acceptance_matrix)
    e2e = sub.add_parser("end-to-end-acceptance")
    e2e.add_argument("--output", default=".stagemesh/end-to-end-acceptance.json")
    e2e.add_argument("--json", action="store_true")
    e2e.set_defaults(func=command_end_to_end_acceptance)
    audit = sub.add_parser("completion-audit")
    audit.add_argument("--output", default=".stagemesh/completion-audit.json")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=command_completion_audit)
    audit_log = sub.add_parser("audit")
    audit_log.add_argument("--output")
    audit_log.add_argument("--limit", type=int, default=500)
    audit_log.add_argument("--json", action="store_true")
    audit_log.set_defaults(func=command_audit)
    retries = sub.add_parser("retries")
    retry_sub = retries.add_subparsers(dest="retry_command")
    retry_list = retry_sub.add_parser("list")
    retry_list.add_argument("--json", action="store_true")
    retry_list.set_defaults(func=command_retries)
    retry_fail = retry_sub.add_parser("fail")
    retry_fail.add_argument("key")
    retry_fail.add_argument("--reason", default="failure")
    retry_fail.add_argument("--json", action="store_true")
    retry_fail.set_defaults(func=command_retries)
    retry_success = retry_sub.add_parser("success")
    retry_success.add_argument("key")
    retry_success.add_argument("--json", action="store_true")
    retry_success.set_defaults(func=command_retries)
    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_sub.add_parser("add")
    evidence_add.add_argument("kind")
    evidence_add.add_argument("status")
    evidence_add.add_argument("url")
    evidence_add.add_argument("--candidate-sha")
    evidence_add.add_argument("--notes", default="")
    evidence_add.add_argument("--json", action="store_true")
    evidence_add.set_defaults(func=command_evidence)
    evidence_list = evidence_sub.add_parser("list")
    evidence_list.add_argument("--json", action="store_true")
    evidence_list.set_defaults(func=command_evidence)
    ci = sub.add_parser("ci")
    ci.add_argument("--future-feature-gate", action="store_true")
    ci.add_argument("--skip-acceptance", action="store_true")
    ci.add_argument("--json", action="store_true")
    ci.set_defaults(func=command_ci)
    ci_wait = sub.add_parser("ci-wait")
    ci_wait.add_argument("status")
    ci_wait.add_argument("--elapsed-seconds", type=float, default=0)
    ci_wait.add_argument("--max-seconds", type=float, default=1800)
    ci_wait.add_argument("--json", action="store_true")
    ci_wait.set_defaults(func=command_ci_wait)
    register_autonomy_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigValidationError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except TaskSourceValidationError as exc:
        print(f"task source error: {exc}", file=sys.stderr)
        return 2
    except ObjectiveValidationError as exc:
        print(f"objective error: {exc}", file=sys.stderr)
        return 2
    except StoreValidationError as exc:
        print(f"store error: {exc}", file=sys.stderr)
        return 2
    except RegistryValidationError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return 2
    except SecurityBoundaryError as exc:
        print(f"security boundary error: {exc}", file=sys.stderr)
        return 2
    except ReleaseValidationError as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2
    except FinalReportValidationError as exc:
        print(f"final report error: {exc}", file=sys.stderr)
        return 2
    except AcceptanceValidationError as exc:
        print(f"acceptance error: {exc}", file=sys.stderr)
        return 2
    except AcceptanceMatrixValidationError as exc:
        print(f"acceptance matrix error: {exc}", file=sys.stderr)
        return 2
    except EndToEndAcceptanceValidationError as exc:
        print(f"end-to-end acceptance error: {exc}", file=sys.stderr)
        return 2
    except CompletionAuditValidationError as exc:
        print(f"completion audit error: {exc}", file=sys.stderr)
        return 2
    except CIValidationError as exc:
        print(f"ci error: {exc}", file=sys.stderr)
        return 2
    except ReleaseReadinessValidationError as exc:
        print(f"release readiness error: {exc}", file=sys.stderr)
        return 2
    except WorkQueueError as exc:
        print(f"work queue error: {exc}", file=sys.stderr)
        return 2
    except WorkTransportError as exc:
        print(f"work transport error: {exc}", file=sys.stderr)
        return 2
    except ExternalEvidenceValidationError as exc:
        print(f"external evidence error: {exc}", file=sys.stderr)
        return 2
    except RetryValidationError as exc:
        print(f"retry error: {exc}", file=sys.stderr)
        return 2
    except CapacityValidationError as exc:
        print(f"capacity error: {exc}", file=sys.stderr)
        return 2
    except AuditValidationError as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2
    except WorkerValidationError as exc:
        print(f"worker error: {exc}", file=sys.stderr)
        return 2
    except DemoValidationError as exc:
        print(f"demo error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
