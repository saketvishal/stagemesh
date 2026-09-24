"""First-run and maintenance commands: init, agent, doctor, upgrade."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

from build_coordinator import __version__
from build_coordinator.agents import machine
from build_coordinator.agents.profiles import PROFILES, READY
from build_coordinator.project.definition import ProjectError, register_project, registry_path, resolve_project
from build_coordinator.project.doctor import format_report, run_doctor
from build_coordinator.project.onboarding import init_project

EXTRA_COMMANDS = {"init", "agent", "doctor", "upgrade"}


def add_extra_commands(sub: argparse._SubParsersAction) -> None:
    init = sub.add_parser("init", help="make a git repository a StageMesh project and register it")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--name")
    init.add_argument("--no-register", action="store_true")

    agent = sub.add_parser("agent", help="detect, verify and manage coding-agent runtimes")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    setup = agent_sub.add_parser("setup", help="detect installed agents and verify each with a real headless run")
    setup.add_argument("runtime", nargs="*", help="limit to these runtimes")
    setup.add_argument("--quick", action="store_true", help="check install + login only (no live headless run; runtimes are NOT marked ready)")
    agent_sub.add_parser("list", help="show what setup last found")
    enable = agent_sub.add_parser("enable")
    enable.add_argument("runtime")
    disable = agent_sub.add_parser("disable")
    disable.add_argument("runtime")

    doctor = sub.add_parser("doctor", help="diagnose the installation, projects, state and agent runtimes")
    doctor.add_argument("project", nargs="*", help="limit project checks to one project (default: all registered)")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    upgrade = sub.add_parser("upgrade", help="upgrade StageMesh with pip (project state is migrated only on request)")
    upgrade.add_argument("--dry-run", action="store_true")


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def handle_extra(args: argparse.Namespace) -> None:
    command = args.command
    if command == "init":
        project, created = init_project(args.path, name=args.name)
        registered = None
        if not args.no_register:
            register_project(project.root)
            registered = str(registry_path())
        ready = machine.ready_runtime_ids()
        _print(
            {
                "project": project.project_id,
                "root": str(project.root),
                "created": created,
                "registered_in": registered,
                "next": (
                    ["stagemesh agent setup   # verify a coding agent"] if not ready else []
                )
                + ["edit .stagemesh/tasks/", "stagemesh doctor", "stagemesh continue"],
            }
        )
        return
    if command == "agent":
        _handle_agent(args)
        return
    if command == "doctor":
        projects = [resolve_project(name) for name in args.project] if args.project else None
        report = run_doctor(projects)
        print(json.dumps(report, indent=2) if args.as_json else format_report(report))
        if not report["healthy"]:
            raise SystemExit(1)
        return
    if command == "upgrade":
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "stagemesh"]
        if args.dry_run:
            _print({"current": __version__, "would_run": cmd})
            return
        print(f"current version {__version__}; running: {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)
        print("Upgraded. Durable project state is never migrated automatically; run `stagemesh doctor`.")


def _handle_agent(args: argparse.Namespace) -> None:
    sub = args.agent_command
    if sub == "setup":
        only = args.runtime or None
        for name in only or []:
            if name not in PROFILES:
                raise ProjectError(f"unknown runtime {name!r}; known: {', '.join(PROFILES)}")
        print("Verifying coding agents (each ready runtime runs a tiny real headless task)...", file=sys.stderr)
        statuses = machine.setup_agents(live=not args.quick, only=only)
        rows = [s.as_dict() for s in statuses]
        for s in statuses:
            print(f"  {s.runtime_id:<12} {s.state:<18} {s.detail}", file=sys.stderr)
        ready = [s.runtime_id for s in statuses if s.state == READY]
        _print(
            {
                "runtimes": rows,
                "ready": machine.ready_runtime_ids() if not args.quick else ready,
                "note": "quick check does not mark runtimes ready" if args.quick else "",
                "next": "stagemesh doctor" if ready else "log in to an agent CLI (e.g. `codex login` or `claude auth login`) and re-run `stagemesh agent setup`",
            }
        )
        return
    if sub == "list":
        _print({"runtimes": machine.known_statuses(), "known": list(PROFILES)})
        return
    machine.set_enabled(args.runtime, sub == "enable")
    _print({"runtime": args.runtime, "enabled": sub == "enable"})
