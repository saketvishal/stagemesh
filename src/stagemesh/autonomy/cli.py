"""`stagemesh autonomy ...`: read-mostly commands for isolation, the decision trace and the ten-task Founder Hands-Off streak."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..persistence import Store
from .corpus import CAPABILITY_LEVELS, scenario_summary
from .decisions import (
    EscalationReason,
    TaskOutcome,
    autonomy_streak,
    decision_trace,
    record_task_outcome,
)
from .isolation import check_isolation

EXIT_NOT_ISOLATED = 3


def _store(project: Path) -> Store:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return store


def command_isolation(args: argparse.Namespace) -> int:
    report = check_isolation(Path(args.project).resolve(), forbidden_checkouts=args.forbid or (), check_running_code=True)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        for name, path in report.paths.items():
            print(f"{name}: {path}")
        for finding in report.findings:
            print(f"VIOLATION [{finding.code}] {finding.message}")
        print("isolated: yes" if report.isolated else "isolated: NO (failing closed)")
    return 0 if report.isolated else EXIT_NOT_ISOLATED


def command_trace(args: argparse.Namespace) -> int:
    store = _store(Path(args.project).resolve())
    decisions = decision_trace(store, args.task, limit=args.limit)
    store.close()
    if args.json:
        print(json.dumps(decisions, indent=2, sort_keys=True))
    else:
        for decision in decisions:
            print(f"[{decision.get('task_id') or '-'}] {decision['trace']}")
    return 0


def command_streak(args: argparse.Namespace) -> int:
    store = _store(Path(args.project).resolve())
    result = autonomy_streak(store)
    store.close()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"autonomy streak: {result['streak']}/{result['required']} ({'GATE MET' if result['gate_met'] else 'gate not met'})")
        print(f"tasks recorded: {result['tasks_recorded']}; escalations observed: {len(result['escalations_observed'])}")
    return 0


def command_readiness(args: argparse.Namespace) -> int:
    store = _store(Path(args.project).resolve())
    streak = autonomy_streak(store)
    store.close()
    report = {
        **scenario_summary(),
        "capabilities": {name: {"level": level, "note": note} for name, (level, note) in CAPABILITY_LEVELS.items()},
        "streak": streak,
        "founder_hands_off_gate_met": streak["gate_met"],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name, (level, note) in CAPABILITY_LEVELS.items():
            print(f"[{level}/3] {name}: {note}")
        print(f"capability readiness: {report['capability_readiness_percent']}%  incident scenarios: {len(report['scenarios'])} ({report['tests']} tests)")
        print(f"Founder Hands-Off gate: {streak['streak']}/{streak['required']} consecutive hands-off tasks ({'MET' if streak['gate_met'] else 'NOT MET'})")
    return 0


def command_record_outcome(args: argparse.Namespace) -> int:
    for reason in args.escalation or ():
        EscalationReason(reason)  # reject anything that is not a typed escalation reason
    store = _store(Path(args.project).resolve())
    record_task_outcome(
        store,
        TaskOutcome(args.task, args.completed, args.interventions, tuple(args.escalation or ()), args.notes or ""),
    )
    result = autonomy_streak(store)
    store.close()
    print(json.dumps(result, sort_keys=True) if args.json else f"recorded; streak {result['streak']}/{result['required']}")
    return 0


def register_autonomy_commands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    autonomy = sub.add_parser("autonomy", help="Founder Hands-Off supervisor: isolation check, decision trace, autonomy streak")
    commands = autonomy.add_subparsers(dest="autonomy_command", required=True)

    isolation = commands.add_parser("isolation", help="Fail closed if any runtime path resolves into another StageMesh checkout")
    isolation.add_argument("--forbid", action="append", help="a StageMesh checkout this one must never touch (repeatable)")
    isolation.add_argument("--json", action="store_true")
    isolation.set_defaults(func=command_isolation)

    trace = commands.add_parser("trace", help="Durable autonomous-decision trace")
    trace.add_argument("--task")
    trace.add_argument("--limit", type=int, default=200)
    trace.add_argument("--json", action="store_true")
    trace.set_defaults(func=command_trace)

    streak = commands.add_parser("streak", help="Consecutive hands-off task completions toward the ten-task gate")
    streak.add_argument("--json", action="store_true")
    streak.set_defaults(func=command_streak)

    readiness = commands.add_parser("readiness", help="Capability readiness, incident corpus coverage and the ten-task gate")
    readiness.add_argument("--json", action="store_true")
    readiness.set_defaults(func=command_readiness)

    outcome = commands.add_parser("record-outcome", help="Record one real task's outcome for the streak")
    outcome.add_argument("--task", required=True)
    outcome.add_argument("--completed", action="store_true")
    outcome.add_argument("--interventions", type=int, default=0, help="operational instructions the founder had to give")
    outcome.add_argument("--escalation", action="append", help="typed EscalationReason raised (repeatable)")
    outcome.add_argument("--notes")
    outcome.add_argument("--json", action="store_true")
    outcome.set_defaults(func=command_record_outcome)
