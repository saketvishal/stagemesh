from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from . import __version__
from .acceptance import write_acceptance_report
from .acceptance_matrix import write_acceptance_matrix
from .audit import export_audit_jsonl
from .capacity import CapacityKind, CapacityRegistry
from .ci import broken_future_feature_gate, default_gates
from .ci_wait import decide_ci_wait
from .completion_audit import write_completion_audit
from .config import ConfigValidationError, load_config
from .dashboard import render_dashboard
from .coordinator import Coordinator
from .distributed import WorkQueue
from .final_report import render_final_report
from .external_evidence import external_evidence_records, record_external_evidence
from .github_acceptance import run_github_acceptance
from .observability import health
from .operator import operator_report
from .objectives import ObjectivePlanner
from .persistence import Store
from .persistence_backends import probe_backend
from .postgres_store import PostgresStore, postgres_schema_contract
from .provider_acceptance import run_provider_acceptance
from .process_identity import current_process_identity
from .registry import GlobalRegistry, ProjectRegistration, RegistryConflictError
from .release import ReleaseValidationError, build_release_artifact
from .release_readiness import write_release_readiness
from .retry import RetryRegistry
from .security import WorkspaceBoundary
from .task_sources import LocalBacklogSource, TaskSourceValidationError, sync_source
from .workers import heartbeat_worker, register_worker


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
    store.close()
    if args.register:
        registry = GlobalRegistry(Path(args.registry).resolve())
        try:
            registry.register(ProjectRegistration(project.name, project, db_path(project)))
        except RegistryConflictError as exc:
            print(f"registry conflict: {exc}", file=sys.stderr)
            return 2
    print(f"initialized StageMesh at {runtime_dir(project)}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    config = load_config(project)
    store = Store(db_path(project))
    store.migrate()
    print(f"version: {__version__}")
    print(f"executable path: {Path(sys.argv[0]).resolve()}")
    print(f"python interpreter: {sys.executable}")
    print(f"imported package path: {Path(__file__).resolve().parent}")
    print(f"project: {project}")
    print(f"db: {db_path(project)}")
    print(f"schema version: {store.schema_version()}")
    print(f"config source: {config.source}")
    print(f"github configured: {config.github.configured}")
    backend = probe_backend(config.database_url, db_path(project))
    print(f"backend: {backend.name}")
    print(f"backend available: {backend.available}")
    print(f"editable/development status: {'development' if 'site-packages' not in __file__ else 'installed'}")
    print(f"platform: {platform.platform()}")
    store.close()
    return 0


def command_continue(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    backlog = project / ".stagemesh" / "backlog.json"
    sync_source(store, LocalBacklogSource(backlog).discover())
    coord = Coordinator(store, project)
    count = 0
    while True:
        progressed = coord.tick()
        count += progressed
        if args.once or progressed == 0:
            break
    print(f"progressed: {count}")
    store.close()
    return 0


def command_status(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    rows = store.tasks()
    if not rows:
        print("backlog: EMPTY")
    for row in rows:
        print(f"{row['id']} {row['stage']} {row['status']} {row['title']}")
    store.close()
    return 0


def command_plan(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    payload_path = Path(args.file).resolve()
    payload = __import__("json").loads(payload_path.read_text(encoding="utf-8"))
    planner = ObjectivePlanner()
    objective = planner.parse(payload)
    store = Store(db_path(project))
    store.migrate()
    store.save_objective(objective.id, objective.title, payload)
    planner.write_backlog(objective, payload, runtime_dir(project) / "backlog.json")
    sync_source(store, LocalBacklogSource(runtime_dir(project) / "backlog.json").discover())
    store.close()
    print(f"planned objective: {objective.id} ({len(objective.tasks)} tasks)")
    return 0


def command_health(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = health(store)
    print(f"ok: {report.ok}")
    print(f"tasks: {report.task_count}")
    print(f"running: {report.running_count}")
    print(f"done: {report.done_count}")
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
    print(f"worker: {args.worker_id}")
    return 0


def command_operator(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = operator_report(store)
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
    output.write_text(render_dashboard(store), encoding="utf-8")
    store.close()
    print(f"dashboard: {output}")
    return 0


def command_release(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    artifact = build_release_artifact(project, Path(args.output).resolve(), __version__, args.candidate_sha)
    print(f"archive: {artifact.archive}")
    print(f"manifest: {artifact.manifest}")
    return 0


def command_work(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    queue = WorkQueue(store)
    if args.work_command == "enqueue":
        packet_id = queue.enqueue(args.task_id, args.stage, args.worker_id, args.candidate_sha)
        print(f"packet: {packet_id}")
    elif args.work_command == "poll":
        for packet in queue.poll(args.worker_id, args.limit, args.lease_seconds):
            print(f"{packet.id} {packet.task_id} {packet.stage} {packet.candidate_sha or ''}")
    elif args.work_command == "renew":
        renewed = queue.renew(args.packet_id, args.worker_id)
        print(f"renewed: {renewed}")
    elif args.work_command == "ack":
        queue.ack(args.packet_id, args.status)
        print(f"ack: {args.packet_id}")
    store.close()
    return 0


def command_report(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    report = render_final_report(project, store)
    if args.output:
        output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"report: {output}")
    else:
        print(report)
    store.close()
    return 0


def command_acceptance_report(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    write_acceptance_report(project, output, include_acceptance=not args.skip_acceptance)
    print(f"acceptance-report: {output}")
    return 0


def command_acceptance_matrix(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    store = Store(db_path(project))
    store.migrate()
    write_acceptance_matrix(output, store)
    store.close()
    print(f"acceptance-matrix: {output}")
    return 0


def command_registry(args: argparse.Namespace) -> int:
    registry = GlobalRegistry(Path(args.registry).resolve())
    for project in registry.load():
        print(f"{project.name} {project.path} {project.db_path}")
    return 0


def command_capacity(args: argparse.Namespace) -> int:
    registry = CapacityRegistry()
    registry.record(args.primary, CapacityKind.AVAILABLE if not args.primary_down else CapacityKind.CAPACITY)
    registry.record(args.secondary, CapacityKind.AVAILABLE if not args.secondary_down else CapacityKind.CAPACITY)
    chosen = registry.choose_primary_secondary(args.primary, args.secondary)
    print(f"chosen: {chosen or 'NONE'}")
    return 0


def command_config(args: argparse.Namespace) -> int:
    config = load_config(Path(args.project).resolve(), Path(args.config).resolve() if args.config else None)
    print(f"source: {config.source}")
    print(f"github.owner: {config.github.owner or ''}")
    print(f"github.repo: {config.github.repo or ''}")
    print(f"github.configured: {config.github.configured}")
    print(f"database_url: {config.database_url or 'sqlite://default'}")
    print(f"routing.mode: {config.routing_mode}")
    print(f"routing.single_agent_provider: {config.single_agent_provider or ''}")
    for stage, provider in sorted(config.stage_routes.items()):
        print(f"routing.stage.{stage}: {provider}")
    for name, command in sorted(config.provider_commands.items()):
        print(f"provider.{name}: {command}")
    return 0


def command_backend(args: argparse.Namespace) -> int:
    config = load_config(Path(args.project).resolve(), Path(args.config).resolve() if args.config else None)
    probe = probe_backend(config.database_url, db_path(Path(args.project).resolve()))
    postgres_contract = postgres_schema_contract()
    print(f"name: {probe.name}")
    print(f"available: {probe.available}")
    print(f"reason: {probe.reason}")
    print(f"postgres schema contract: {len(postgres_contract['tables'])} tables")
    if args.ping and config.database_url and probe.name == "postgres":
        store = PostgresStore(config.database_url)
        try:
            print(f"ping: {store.ping()}")
        finally:
            store.close()
    return 0


def command_provider_acceptance(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    result = run_provider_acceptance(store, project)
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
        write_release_readiness(
            project,
            output,
            include_acceptance=not args.skip_acceptance,
            run_checks=not args.skip_checks,
            store=store,
        )
    finally:
        store.close()
    print(f"release-readiness: {output}")
    return 0


def command_completion_audit(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
    store = Store(db_path(project))
    store.migrate()
    write_completion_audit(output, store)
    store.close()
    print(f"completion-audit: {output}")
    return 0


def command_audit(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    if args.output:
        output = WorkspaceBoundary(project).require_inside(Path(args.output).resolve())
        export_audit_jsonl(store, output, args.limit)
        print(f"audit: {output}")
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
        print(f"{decision.key} attempts={decision.attempts} next_attempt_at={decision.next_attempt_at}")
    elif args.retry_command == "success":
        registry.record_success(args.key)
        print(f"{args.key} cleared")
    else:
        rows = store.retry_states()
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
        print(f"evidence: {evidence_id}")
    else:
        rows = external_evidence_records(store)
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
    for result in results:
        print(f"{result.name}: {'PASS' if result.passed else 'FAIL'}")
    return 0 if all(result.passed for result in results) else 1


def command_ci_wait(args: argparse.Namespace) -> int:
    decision = decide_ci_wait(args.status, args.elapsed_seconds, args.max_seconds)
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
    init.set_defaults(func=command_init)
    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=command_doctor)
    cont = sub.add_parser("continue")
    cont.add_argument("--once", action="store_true")
    cont.set_defaults(func=command_continue)
    status = sub.add_parser("status")
    status.set_defaults(func=command_status)
    plan = sub.add_parser("plan")
    plan.add_argument("file")
    plan.set_defaults(func=command_plan)
    health_cmd = sub.add_parser("health")
    health_cmd.set_defaults(func=command_health)
    capacity = sub.add_parser("capacity")
    capacity.add_argument("--primary", default="codex")
    capacity.add_argument("--secondary", default="claude")
    capacity.add_argument("--primary-down", action="store_true")
    capacity.add_argument("--secondary-down", action="store_true")
    capacity.set_defaults(func=command_capacity)
    config = sub.add_parser("config")
    config.add_argument("--config")
    config.set_defaults(func=command_config)
    backend = sub.add_parser("backend")
    backend.add_argument("--config")
    backend.add_argument("--ping", action="store_true")
    backend.set_defaults(func=command_backend)
    provider_acceptance = sub.add_parser("provider-acceptance")
    provider_acceptance.set_defaults(func=command_provider_acceptance)
    github_acceptance = sub.add_parser("github-acceptance")
    github_acceptance.set_defaults(func=command_github_acceptance)
    readiness = sub.add_parser("release-readiness")
    readiness.add_argument("--output", default=".stagemesh/release-readiness.json")
    readiness.add_argument("--skip-acceptance", action="store_true")
    readiness.add_argument("--skip-checks", action="store_true")
    readiness.set_defaults(func=command_release_readiness)
    worker = sub.add_parser("worker")
    worker.add_argument("worker_id")
    worker.add_argument("--provider", default="local")
    worker.add_argument("--capability", action="append")
    worker.add_argument("--lease-seconds", type=float, default=300)
    worker.set_defaults(func=command_worker)
    operator = sub.add_parser("operator")
    operator.set_defaults(func=command_operator)
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--output", default="stagemesh-dashboard.html")
    dashboard.set_defaults(func=command_dashboard)
    release = sub.add_parser("release")
    release.add_argument("--candidate-sha", required=True)
    release.add_argument("--output", default="dist")
    release.set_defaults(func=command_release)
    work = sub.add_parser("work")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    enqueue = work_sub.add_parser("enqueue")
    enqueue.add_argument("task_id")
    enqueue.add_argument("--stage", default="IMPLEMENT")
    enqueue.add_argument("--worker-id")
    enqueue.add_argument("--candidate-sha")
    enqueue.set_defaults(func=command_work)
    poll = work_sub.add_parser("poll")
    poll.add_argument("worker_id")
    poll.add_argument("--limit", type=int, default=1)
    poll.add_argument("--lease-seconds", type=float, default=300)
    poll.set_defaults(func=command_work)
    renew = work_sub.add_parser("renew")
    renew.add_argument("packet_id")
    renew.add_argument("worker_id")
    renew.set_defaults(func=command_work)
    ack = work_sub.add_parser("ack")
    ack.add_argument("packet_id")
    ack.add_argument("--status", default="SUCCEEDED")
    ack.set_defaults(func=command_work)
    registry = sub.add_parser("registry")
    registry.add_argument("--registry", default=str(Path.home() / ".stagemesh" / "registry.json"))
    registry.set_defaults(func=command_registry)
    report = sub.add_parser("report")
    report.add_argument("--output")
    report.set_defaults(func=command_report)
    acceptance_report = sub.add_parser("acceptance-report")
    acceptance_report.add_argument("--output", default=".stagemesh/acceptance-report.json")
    acceptance_report.add_argument("--skip-acceptance", action="store_true")
    acceptance_report.set_defaults(func=command_acceptance_report)
    acceptance_matrix = sub.add_parser("acceptance-matrix")
    acceptance_matrix.add_argument("--output", default=".stagemesh/acceptance-matrix.json")
    acceptance_matrix.set_defaults(func=command_acceptance_matrix)
    audit = sub.add_parser("completion-audit")
    audit.add_argument("--output", default=".stagemesh/completion-audit.json")
    audit.set_defaults(func=command_completion_audit)
    audit_log = sub.add_parser("audit")
    audit_log.add_argument("--output")
    audit_log.add_argument("--limit", type=int, default=500)
    audit_log.set_defaults(func=command_audit)
    retries = sub.add_parser("retries")
    retry_sub = retries.add_subparsers(dest="retry_command")
    retry_list = retry_sub.add_parser("list")
    retry_list.set_defaults(func=command_retries)
    retry_fail = retry_sub.add_parser("fail")
    retry_fail.add_argument("key")
    retry_fail.add_argument("--reason", default="failure")
    retry_fail.set_defaults(func=command_retries)
    retry_success = retry_sub.add_parser("success")
    retry_success.add_argument("key")
    retry_success.set_defaults(func=command_retries)
    evidence = sub.add_parser("evidence")
    evidence_sub = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_sub.add_parser("add")
    evidence_add.add_argument("kind")
    evidence_add.add_argument("status")
    evidence_add.add_argument("url")
    evidence_add.add_argument("--candidate-sha")
    evidence_add.add_argument("--notes", default="")
    evidence_add.set_defaults(func=command_evidence)
    evidence_list = evidence_sub.add_parser("list")
    evidence_list.set_defaults(func=command_evidence)
    ci = sub.add_parser("ci")
    ci.add_argument("--future-feature-gate", action="store_true")
    ci.add_argument("--skip-acceptance", action="store_true")
    ci.set_defaults(func=command_ci)
    ci_wait = sub.add_parser("ci-wait")
    ci_wait.add_argument("status")
    ci_wait.add_argument("--elapsed-seconds", type=float, default=0)
    ci_wait.add_argument("--max-seconds", type=float, default=1800)
    ci_wait.set_defaults(func=command_ci_wait)
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
    except ReleaseValidationError as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
