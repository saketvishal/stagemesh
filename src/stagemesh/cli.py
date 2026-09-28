from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from . import __version__
from .capacity import CapacityKind, CapacityRegistry
from .coordinator import Coordinator
from .observability import health
from .objectives import ObjectivePlanner
from .persistence import Store
from .task_sources import LocalBacklogSource, sync_source


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
    print(f"initialized StageMesh at {runtime_dir(project)}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    store = Store(db_path(project))
    store.migrate()
    print(f"version: {__version__}")
    print(f"executable path: {Path(sys.argv[0]).resolve()}")
    print(f"python interpreter: {sys.executable}")
    print(f"imported package path: {Path(__file__).resolve().parent}")
    print(f"project: {project}")
    print(f"db: {db_path(project)}")
    print(f"config source: defaults")
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


def command_capacity(args: argparse.Namespace) -> int:
    registry = CapacityRegistry()
    registry.record(args.primary, CapacityKind.AVAILABLE if not args.primary_down else CapacityKind.CAPACITY)
    registry.record(args.secondary, CapacityKind.AVAILABLE if not args.secondary_down else CapacityKind.CAPACITY)
    chosen = registry.choose_primary_secondary(args.primary, args.secondary)
    print(f"chosen: {chosen or 'NONE'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stagemesh")
    parser.add_argument("--project", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--task")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
