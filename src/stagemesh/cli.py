from __future__ import annotations

import argparse
import json
import platform
import sys
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
from .execution import SubprocessExecutor
from .external_evidence import (
    ExternalEvidenceValidationError,
    external_evidence_records,
    record_external_evidence,
)
from .final_report import FinalReportValidationError, candidate_sha, render_final_report
from .github_acceptance import run_github_acceptance
from .objectives import ObjectivePlanner, ObjectiveValidationError
from .observability import health
from .operator import operator_report
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
from .release import ReleaseValidationError, build_release_artifact
from .release_readiness import ReleaseReadinessValidationError, release_readiness
from .retry import RetryRegistry, RetryValidationError
from .review import Reviewer
from .routing import Provider, Router, RoutingMode
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


def command_continue(args: argparse.Namespace) -> int:
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
    backlog = project / ".stagemesh" / "backlog.json"
    sync_source(store, _filter_targeted_tasks(LocalBacklogSource(backlog).discover(), targeted_task_id))
    for source in task_sources_from_config(config):
        sync_source(store, _filter_targeted_tasks(source.discover(), targeted_task_id))
    # Wire real provider adapters unless --dry-run is requested.
    executor = None
    reviewer = None
    chosen_provider = "fake"
    chosen_review_provider = "builtin-deterministic-fallback"
    independent_review_configured = False
    if not getattr(args, "dry_run", False):
        try:
            adapters = adapters_from_config(config)
        except ProviderValidationError as exc:
            print(f"provider config error: {exc}", file=sys.stderr)
            store.close()
            return 2
        adapter_by_name = {adapter.name: adapter for adapter in adapters}
        router = Router(
            [
                Provider(
                    adapter.name,
                    adapter.capabilities,
                    adapter.check_capacity() == CapacityKind.AVAILABLE,
                )
                for adapter in adapters
            ],
            mode=config.routing_mode,
            stage_routes=config.stage_routes,
            single_agent_provider=config.single_agent_provider,
        )
        chosen_name = getattr(args, "provider", None)
        if chosen_name:
            adapter = adapter_by_name.get(chosen_name)
            if adapter is None:
                print(f"provider not found: {chosen_name}", file=sys.stderr)
                store.close()
                return 2
        else:
            routed = router.choose_for_stage("IMPLEMENT", "code")
            adapter = adapter_by_name.get(routed.name) if routed else (adapters[0] if adapters else None)
        if adapter is not None:
            chosen_provider = adapter.name
            executor = SubprocessExecutor(list(adapter.command), name=adapter.name)
        if config.routing_mode == RoutingMode.STAGED:
            routed_review = router.choose_for_stage("REVIEW", "review")
            review_adapter = adapter_by_name.get(routed_review.name) if routed_review else None
            if review_adapter is not None and review_adapter.name != chosen_provider:
                reviewer = Reviewer(adapter=review_adapter)
                chosen_review_provider = review_adapter.name
                independent_review_configured = True
            elif review_adapter is not None:
                chosen_review_provider = "builtin-deterministic-fallback"
        else:
            reviewer = Reviewer(provider_name="single-agent-deterministic-fallback")
            chosen_review_provider = "single-agent-deterministic-fallback"
    coord = Coordinator(store, project, executor=executor, reviewer=reviewer, target=target)
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
    print(f"targeted_mode: {targeted_mode}")
    print(f"targeted_task_id: {targeted_task_id}")
    store.close()
    return 0


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
                    "tasks": [
                        {
                            "id": row["id"],
                            "stage": row["stage"],
                            "status": row["status"],
                            "title": row["title"],
                            "source": row["source"],
                            "source_id": row["source_id"],
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
                },
                indent=2,
                sort_keys=True,
            )
        )
        store.close()
        return 0
    print(f"ok: {report.ok}")
    print(f"tasks: {report.task_count}")
    print(f"blocked_tasks: {report.blocked_task_count}")
    print(f"running: {report.running_count}")
    print(f"done: {report.done_count}")
    print(f"failed_executions: {report.failed_execution_count}")
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
    cont = sub.add_parser("continue")
    cont.add_argument("--once", action="store_true")
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
