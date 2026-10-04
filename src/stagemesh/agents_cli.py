"""`stagemesh agents ...` (and its alias `stagemesh configure agents`): list, configure and inspect agent plugins."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .agent_config import (
    AgentSettings,
    AgentState,
    apply_agent_state,
    describe_expiry,
    format_timestamp,
    load_state,
    parse_state,
    parse_timestamp,
    save_state,
    state_path,
)
from .agents import AGENT_STAGES, IMPLEMENT, REVIEW, default_registry
from .config import SELECTION_POLICIES, ConfigValidationError, load_base_config, load_config


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _pairs(value: str | None, flag: str, cast) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    out: dict[str, Any] = {}
    for item in _csv(value):
        name, sep, raw = item.partition("=")
        if not sep or not name.strip() or not raw.strip():
            raise ConfigValidationError(f"{flag} expects name=value pairs separated by commas (got {item!r})")
        try:
            out[name.strip()] = cast(raw.strip())
        except (ValueError, ConfigValidationError) as exc:
            raise ConfigValidationError(f"{flag} {name.strip()}: {exc}") from exc
    return out


def _stage_list(value: str | None) -> list[str]:
    return [s.upper() for s in _csv(value)]


def build_state(base: AgentState, args: argparse.Namespace) -> AgentState:
    """The state after applying the command-line changes to `base`. Validation happens when it is parsed back."""
    agents = dict(base.agents)

    def update(name: str, **changes: Any) -> None:
        agents[name] = replace(agents.get(name, AgentSettings()), **changes)

    for name in _csv(args.enable):
        update(name, enabled=True)
    for name in _csv(args.disable):
        update(name, enabled=False)
    for name, value in _pairs(args.max_concurrency, "--max-concurrency", int).items():
        update(name, max_concurrency=value)
    for name, value in _pairs(args.priority, "--priority", int).items():
        update(name, priority=value)
    for name, value in _pairs(args.weight, "--weight", int).items():
        update(name, weight=value)
    for name, value in _pairs(args.expires_at, "--expires-at", lambda raw: None if raw.lower() in ("none", "clear") else parse_timestamp(raw)).items():
        update(name, expires_at=value)
    pools = dict(base.pools)
    if args.implementation is not None:
        pools[IMPLEMENT] = tuple(_csv(args.implementation))
    if args.review is not None:
        pools[REVIEW] = tuple(_csv(args.review))
    if args.clear_pools:
        pools = {}
    return AgentState(args.policy or base.policy, pools, agents)


def _validated(project: Path, state: AgentState):  # type: ignore[no-untyped-def]
    """Round-trips the state through the file schema and applies it over the project config; raises ConfigValidationError."""
    state = parse_state(state.to_json())
    config = apply_agent_state(project, load_base_config(project), state=state)
    for stage in AGENT_STAGES:
        for name in state.pools.get(stage, ()):
            if name in config.disabled_agents:
                raise ConfigValidationError(f"agent {name} is disabled and cannot be assigned to {stage}; enable it too")
    return state, config


def _health(name: str, config) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    plugin = default_registry().get(name)
    command = os.environ.get(plugin.env_command_var) if plugin else None
    command = command or config.provider_commands.get(name) or (plugin.command if plugin else "")
    if plugin is None:
        from .agents import default_health

        return default_health(command)
    return plugin.health(command)


def status_report(project: Path) -> dict[str, Any]:
    from .cli import _provider_capacity_report

    config = load_config(project)
    capacity = _provider_capacity_report(project)
    by_name = {p["name"]: p for p in capacity.get("providers", [])} if capacity.get("available") else {}
    now = time.time()
    agents = []
    for name, info in sorted(config.agent_report.items()):
        live = by_name.get(name, {})
        ok, detail = (False, "disabled") if not info["enabled"] else _health(name, config)
        agents.append(
            {
                **info,
                "expires_at_text": describe_expiry(info["expires_at"], now) if info["expires_at"] else "none",
                "window_active": bool(info["expires_at"] and info["expires_at"] > now),
                "healthy": ok,
                "health": detail,
                "cooldown": live.get("cooldown"),
                "recent_use": live.get("recent_use", []),
                "skipped": {stage: config.agent_skips.get(stage, {}).get(name) for stage in AGENT_STAGES if config.agent_skips.get(stage, {}).get(name)},
            }
        )
    return {
        "policy": config.provider_selection_policy,
        "policy_source": config.agent_policy_source,
        "state_file": config.agent_state_file,
        "stage_pools": capacity.get("stage_pools", {}),
        "require_independent_review": config.require_independent_review,
        "failure_cooldown_seconds": config.provider_failure_cooldown_seconds,
        "agents": agents,
    }


def format_status(report: dict[str, Any]) -> str:
    lines = [
        f"selection policy: {report['policy']} (source: {report['policy_source']})",
        f"runtime agent config: {report['state_file'] or 'none (project config and built-in defaults)'}",
        f"independent review required: {report['require_independent_review']}",
    ]
    for stage, names in report["stage_pools"].items():
        lines.append(f"{stage} pool: {', '.join(names) or '(empty)'}")
    for a in report["agents"]:
        src = a["sources"]
        state = "enabled" if a["enabled"] else "DISABLED"
        lines.append(f"agent {a['id']} ({a['display_name']}; {a['plugin']}): {state}; health: {a['health']}")
        lines.append(f"  roles: {', '.join(a['stages']) or 'none'} [{src['stages']}]")
        lines.append(
            f"  priority: {a['priority'] if a['priority'] is not None else 'default'} [{src.get('priority')}]; "
            f"weight: {a['weight']} [{src.get('weight')}]; max concurrency: {a['max_concurrency'] if a['max_concurrency'] is not None else 'project default'} [{src.get('max_concurrency')}]"
        )
        lines.append(f"  capacity window: {a['expires_at_text']}; cooldown: {a['cooldown'] or 'none'}")
        recent = ", ".join(f"{u['stage']} {u['seconds_ago']}s ago" for u in a["recent_use"]) or "never"
        lines.append(f"  recent use: {recent}")
        for stage, why in a["skipped"].items():
            lines.append(f"  not in {stage} pool: {why}")
    return "\n".join(lines)


def format_plugins() -> str:
    lines = []
    for plugin in default_registry().plugins():
        d = plugin.describe()
        lines.append(
            f"{d['id']}: {d['display_name']} - stages {'+'.join(d['stages'])}; structured review {'yes' if d['structured_review'] else 'NO'}; "
            f"default command `{d['default_command']}` (override: {plugin.env_command_var})"
        )
    return "\n".join(lines)


def _interactive(project: Path, base: AgentState) -> AgentState:
    agents = dict(base.agents)
    pools = dict(base.pools)
    print("Configure agents (Enter keeps the current value).")
    for plugin in default_registry().plugins():
        current = agents.get(plugin.id, AgentSettings())
        answer = input(f"  enable {plugin.id} ({plugin.display_name})? [{'Y' if current.enabled is not False else 'n'}] ").strip().lower()
        enabled = current.enabled if not answer else answer.startswith("y")
        conc = input(f"  {plugin.id} max concurrency [{current.max_concurrency or 'default'}] ").strip()
        agents[plugin.id] = replace(current, enabled=enabled, max_concurrency=int(conc) if conc else current.max_concurrency)
    for stage in AGENT_STAGES:
        answer = input(f"  {stage} agents in order, comma separated [{','.join(pools.get(stage, ())) or 'default'}] ").strip()
        if answer:
            pools[stage] = tuple(_csv(answer))
    policy = input(f"  selection policy ({'|'.join(SELECTION_POLICIES)}) [{base.policy or 'default'}] ").strip()
    return AgentState(policy or base.policy, pools, agents)


def command_agents_list(args: argparse.Namespace) -> int:
    plugins = [p.describe() for p in default_registry().plugins()]
    if args.json:
        print(json.dumps({"plugins": plugins}, indent=2, sort_keys=True))
    else:
        print(format_plugins())
    return 0


def command_agents_status(args: argparse.Namespace) -> int:
    try:
        report = status_report(Path(args.project).resolve())
    except ConfigValidationError as exc:
        print(f"agent configuration error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else format_status(report))
    return 0


def command_agents_configure(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    changes = any(
        getattr(args, name) not in (None, False)
        for name in ("enable", "disable", "implementation", "review", "policy", "max_concurrency", "priority", "weight", "expires_at", "clear_pools")
    )
    try:
        base = load_state(project) or AgentState()
        if args.show or (not changes and (args.json or not sys.stdin or not sys.stdin.isatty())):
            return command_agents_status(args)
        state = build_state(base, args) if changes else _interactive(project, base)
        state, config = _validated(project, state)
    except (ConfigValidationError, ValueError) as exc:
        print(f"agent configuration refused: {exc}", file=sys.stderr)
        return 2
    path = save_state(project, state)  # runtime state only; contracts, secrets and provider commands are never written
    warnings = [
        f"no enabled agent serves {stage}" for stage in AGENT_STAGES if stage in config.provider_pools and not config.provider_pools[stage]
    ]
    if args.json:
        print(json.dumps({"saved": str(path), "state": state.to_json(), "warnings": warnings}, indent=2, sort_keys=True))
    else:
        print(f"agent configuration saved to {path}")
        print("applies to future provider selections only; a run already in progress keeps the configuration it started with")
        for warning in warnings:
            print(f"warning: {warning}")
        print(format_status(status_report(project)))
    return 0


def _add_configure_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--enable", metavar="IDS", help="Comma-separated agents to enable")
    p.add_argument("--disable", metavar="IDS", help="Comma-separated agents to disable")
    p.add_argument("--implementation", metavar="IDS", help="Ordered agents that serve IMPLEMENT (replaces the pool)")
    p.add_argument("--review", metavar="IDS", help="Ordered agents that serve REVIEW (replaces the pool)")
    p.add_argument("--clear-pools", action="store_true", help="Forget explicit IMPLEMENT/REVIEW assignments")
    p.add_argument("--policy", choices=SELECTION_POLICIES, help="Selection policy among eligible agents")
    p.add_argument("--max-concurrency", metavar="A=N,...", help="Per-agent simultaneous runs, e.g. claude=2,grok=1")
    p.add_argument("--priority", metavar="A=N,...", help="Per-agent priority (lower first)")
    p.add_argument("--weight", metavar="A=N,...", help="Per-agent weight for the weighted policy")
    p.add_argument("--expires-at", metavar="A=TIME,...", help="When an agent's capacity/reset window ends (ISO-8601 or epoch; 'none' clears)")
    p.add_argument("--show", action="store_true", help="Only show the effective configuration")
    p.add_argument("--json", action="store_true")


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    agents = sub.add_parser("agents", help="Agent plugins: list, configure (enable, roles, policy, limits) and inspect")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    listing = agents_sub.add_parser("list", help="List the known agent plugins")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=command_agents_list)
    status = agents_sub.add_parser("status", help="Effective agent configuration, with the source of each value, health and recent use")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_agents_status)
    configure = agents_sub.add_parser("configure", help="Enable/disable agents, assign roles, set policy, weights, limits and expiry")
    _add_configure_flags(configure)
    configure.set_defaults(func=command_agents_configure)
    alias = sub.add_parser("configure", help="Configure StageMesh (alias: `configure agents` = `agents configure`)")
    alias_sub = alias.add_subparsers(dest="configure_target", required=True)
    alias_agents = alias_sub.add_parser("agents", help="Same as `stagemesh agents configure`")
    _add_configure_flags(alias_agents)
    alias_agents.set_defaults(func=command_agents_configure)
