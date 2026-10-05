"""Operator-chosen agent configuration (`.stagemesh/agents.json`) merged into the effective provider settings.

Precedence, highest first, for every value:

    1. environment      STAGEMESH_PROVIDER_SELECTION_POLICY (policy), STAGEMESH_<ID>_CMD (command; never stored here)
    2. runtime config   .stagemesh/agents.json, written by `stagemesh agents configure`
    3. project config   .stagemesh/config.json: `providers`, `routing.pools`, `routing.stage_routes`, `routing.provider_weights`,
                        `routing.provider_selection_policy`, `routing.provider_profile` (unchanged, still fully supported)
    4. built-in default the agent plugin's declared defaults, then StageMesh's own

The file holds routing and capacity settings only. Its keys are an allow-list, so a token or any other secret cannot be stored in it,
and it is never part of a task contract. It is read when a command starts: a running queue keeps the configuration it started
with (see README), and only later provider selections of a *new* run see an edit.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import AGENT_STAGES, IMPLEMENT, REVIEW, AgentRegistry, default_registry
from .config import SELECTION_POLICIES, ConfigValidationError, ProviderSpec, StageMeshConfig

STATE_FILE = ".stagemesh/agents.json"
AGENT_KEYS = {"enabled", "max_concurrency", "priority", "weight", "expires_at"}
BUILTIN_DEFAULT, PROJECT, RUNTIME, ENVIRONMENT = "built-in default", "project config", "runtime config", "environment"


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool | None = None
    max_concurrency: int | None = None
    priority: int | None = None
    weight: int | None = None
    expires_at: float | None = None  # epoch seconds: when this agent's capacity/reset window ends


@dataclass(frozen=True)
class AgentState:
    policy: str | None = None
    pools: dict[str, tuple[str, ...]] = field(default_factory=dict)  # stage -> ordered agents explicitly assigned to it
    agents: dict[str, AgentSettings] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        agents: dict[str, Any] = {}
        for name, s in sorted(self.agents.items()):
            entry = {k: v for k, v in (("enabled", s.enabled), ("max_concurrency", s.max_concurrency), ("priority", s.priority), ("weight", s.weight)) if v is not None}
            if s.expires_at is not None:
                entry["expires_at"] = format_timestamp(s.expires_at)
            if entry:
                agents[name] = entry
        out: dict[str, Any] = {"schema_version": 1}
        if self.policy:
            out["policy"] = self.policy
        if self.pools:
            out["pools"] = {stage: list(names) for stage, names in sorted(self.pools.items())}
        out["agents"] = agents
        return out


def state_path(project: Path) -> Path:
    return Path(project) / STATE_FILE


def parse_timestamp(value: object, field_name: str = "expires_at") -> float:
    """Epoch seconds from a number, a numeric string, or an ISO-8601 timestamp (naive means UTC)."""
    if isinstance(value, bool):
        raise ConfigValidationError(f"{field_name} must be a timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigValidationError(f"{field_name} must be epoch seconds or an ISO-8601 timestamp (got {text!r})") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ConfigValidationError(f"{field_name} must be a timestamp")


def format_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigValidationError(f"{name} must be an integer >= {minimum}")
    return value


def parse_state(raw: Any) -> AgentState:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ConfigValidationError(f"{STATE_FILE} must be an object with schema_version 1")
    unknown = set(raw) - {"schema_version", "policy", "pools", "agents"}
    if unknown:
        raise ConfigValidationError(f"{STATE_FILE} has unsupported keys: {', '.join(sorted(unknown))}")
    policy = raw.get("policy")
    if policy is not None and policy not in SELECTION_POLICIES:
        raise ConfigValidationError(f"{STATE_FILE}: policy must be one of: {', '.join(SELECTION_POLICIES)}")
    pools: dict[str, tuple[str, ...]] = {}
    for stage, names in (raw.get("pools") or {}).items():
        if stage not in AGENT_STAGES:
            raise ConfigValidationError(f"{STATE_FILE}: unsupported stage {stage!r} (use {', '.join(AGENT_STAGES)})")
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
            raise ConfigValidationError(f"{STATE_FILE}: pools.{stage} must be a list of distinct agent ids")
        pools[stage] = tuple(names)
    agents: dict[str, AgentSettings] = {}
    for name, entry in (raw.get("agents") or {}).items():
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"{STATE_FILE}: agents.{name} must be an object")
        extra = set(entry) - AGENT_KEYS
        if extra:  # an allow-list: tokens, secrets and commands cannot be smuggled into this file
            raise ConfigValidationError(f"{STATE_FILE}: agents.{name} has unsupported keys: {', '.join(sorted(extra))}")
        enabled = entry.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ConfigValidationError(f"{STATE_FILE}: agents.{name}.enabled must be a boolean")
        agents[name] = AgentSettings(
            enabled,
            _int(entry["max_concurrency"], f"agents.{name}.max_concurrency", 1) if "max_concurrency" in entry else None,
            _int(entry["priority"], f"agents.{name}.priority", 0) if "priority" in entry else None,
            _int(entry["weight"], f"agents.{name}.weight", 1) if "weight" in entry else None,
            parse_timestamp(entry["expires_at"], f"agents.{name}.expires_at") if entry.get("expires_at") is not None else None,
        )
    return AgentState(policy, pools, agents)


def load_state(project: Path) -> AgentState | None:
    path = state_path(project)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigValidationError(f"{STATE_FILE} must be valid JSON: {exc}") from exc
    return parse_state(raw)


def save_state(project: Path, state: AgentState) -> Path:
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


def _project_config_has(project: Path, *keys: str) -> bool:
    try:
        data = json.loads((Path(project) / ".stagemesh" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return False
        data = data[key]
    return data is not None


def apply_agent_state(
    project: Path, config: StageMeshConfig, registry: AgentRegistry | None = None, state: AgentState | None = None
) -> StageMeshConfig:
    """The configuration every command uses: project config, then the runtime agent state on top, then the effective report.

    `state` overrides the file (used to validate a change before it is saved).
    """
    registry = registry or default_registry()
    state = state if state is not None else (load_state(project) or AgentState())
    known = set(registry.ids()) | set(config.provider_commands)
    for name in [*state.agents, *(n for names in state.pools.values() for n in names)]:
        if name not in known:
            raise ConfigValidationError(f"{STATE_FILE} references unknown agent: {name} (known: {', '.join(sorted(known))})")

    specs = dict(config.provider_specs)
    weights = dict(config.provider_weights)
    pools = dict(config.provider_pools)
    skips: dict[str, dict[str, str]] = {IMPLEMENT: {}, REVIEW: {}}
    expiry: dict[str, float] = {}
    unstructured: set[str] = set()
    disabled: set[str] = set()
    report: dict[str, dict[str, Any]] = {}

    env_policy = os.environ.get("STAGEMESH_PROVIDER_SELECTION_POLICY")
    if env_policy:
        policy, policy_source = config.provider_selection_policy, ENVIRONMENT
    elif state.policy:
        policy, policy_source = state.policy, RUNTIME
    else:
        policy = config.provider_selection_policy
        configured = _project_config_has(project, "routing", "provider_selection_policy") or config.provider_profile is not None
        policy_source = PROJECT if configured else BUILTIN_DEFAULT

    for name in sorted(known):
        plugin = registry.get(name)
        runtime = state.agents.get(name, AgentSettings())
        legacy = config.provider_specs.get(name)
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}

        def pick(key: str, runtime_value: Any, legacy_value: Any, plugin_value: Any, default: Any = None) -> None:
            if runtime_value is not None:
                values[key], sources[key] = runtime_value, RUNTIME
            elif legacy_value is not None:
                values[key], sources[key] = legacy_value, PROJECT
            else:
                values[key], sources[key] = (plugin_value if plugin_value is not None else default), BUILTIN_DEFAULT

        pick("enabled", runtime.enabled, None, None, True)
        pick("max_concurrency", runtime.max_concurrency, legacy.max_concurrency if legacy else None, plugin.default_max_concurrency if plugin else None)
        pick("priority", runtime.priority, legacy.priority if legacy else None, None)
        legacy_weight = config.provider_weights.get(name, legacy.weight if legacy else None)
        pick("weight", runtime.weight, legacy_weight, None, 1)
        pick("expires_at", runtime.expires_at, None, None)

        base_stages = set(legacy.capabilities) if legacy else (set(plugin.stages) if plugin else set(AGENT_STAGES))
        stages = set(base_stages)
        stage_source = PROJECT if legacy else BUILTIN_DEFAULT
        if state.pools:
            stages = {stage for stage in AGENT_STAGES if name in state.pools.get(stage, ()) or (stage not in state.pools and stage in base_stages)}
            stage_source = RUNTIME
        if plugin is not None and not plugin.structured_review and name not in state.pools.get(REVIEW, ()) and REVIEW in stages:
            stages.discard(REVIEW)  # not a reliable structured-review producer: only an explicit assignment makes it a reviewer
            unstructured.add(name)
            skips[REVIEW][name] = (
                f"not_structured_review: {plugin.display_name} is not marked as reliably producing review JSON; "
                f"assign it explicitly with `stagemesh agents configure --review ...` to allow it"
            )
        if plugin is not None and not plugin.structured_review and name in state.pools.get(REVIEW, ()):
            unstructured.add(name)  # explicitly allowed, but never preferred over a structured reviewer

        if not values["enabled"]:
            disabled.add(name)
            for stage in AGENT_STAGES:
                skips[stage][name] = f"disabled: turned off in {RUNTIME if runtime.enabled is not None else PROJECT}"
            stages = set()
        else:
            for stage in AGENT_STAGES:
                if stage not in stages and name not in skips[stage] and stage in state.pools:
                    skips[stage][name] = f"not_assigned: not in the configured {stage} pool"
                elif stage not in stages and name not in skips[stage] and plugin is not None and stage not in plugin.stages:
                    skips[stage][name] = f"missing_capability: {plugin.display_name} does not serve {stage}"
        if values["expires_at"] is not None:
            expiry[name] = float(values["expires_at"])
        # fold the effective values back into the provider settings the queue already understands
        touched = (
            legacy is not None
            or runtime != AgentSettings()
            or any(name in names for names in state.pools.values())
            or name in unstructured
            or stages != base_stages
        )
        if touched:  # an untouched built-in keeps no spec at all, exactly as before this layer existed
            specs[name] = ProviderSpec(
                capabilities=frozenset(stages) or frozenset(base_stages),
                priority=values["priority"],
                weight=values["weight"] if (runtime.weight is not None or legacy_weight is not None) else None,
                max_concurrency=values["max_concurrency"],
                review_command=legacy.review_command if legacy else None,
            )
        if runtime.weight is not None:
            weights[name] = runtime.weight
        report[name] = {
            "id": name,
            "display_name": plugin.display_name if plugin else name,
            "plugin": "built-in" if plugin and registry.is_builtin(name) else ("registered" if plugin else "custom provider (project config)"),
            "enabled": bool(values["enabled"]),
            "stages": sorted(stages),
            "priority": values["priority"],
            "weight": values["weight"],
            "max_concurrency": values["max_concurrency"],
            "expires_at": values["expires_at"],
            "structured_review": plugin.structured_review if plugin else True,
            "sources": {**sources, "stages": stage_source},
        }

    for stage, names in state.pools.items():
        pools[stage] = tuple(n for n in names if n not in disabled)
    for stage, names in list(pools.items()):
        pools[stage] = tuple(n for n in names if n not in disabled)

    return replace(
        config,
        provider_specs=specs,
        provider_weights=weights,
        provider_pools=pools,
        provider_selection_policy=policy,
        agent_report=report,
        agent_skips=skips,
        agent_expiry=expiry,
        agent_unstructured=frozenset(unstructured),
        disabled_agents=frozenset(disabled),
        agent_policy_source=policy_source,
        agent_state_file=str(state_path(project)) if load_state_exists(project) else None,
    )


def pool_kwargs(config: StageMeshConfig, registry: AgentRegistry | None = None) -> dict[str, Any]:
    """The agent-aware options every ProviderPool is built with (expiry windows, skip reasons, reviewer reliability, parsers)."""
    registry = registry or default_registry()
    return {
        "expires_at": config.agent_expiry,
        "skips": config.agent_skips,
        "unstructured": config.agent_unstructured,
        "response_parsers": {p.id: p.response_parser for p in registry.plugins() if p.response_parser is not None},
    }


def load_state_exists(project: Path) -> bool:
    return state_path(project).is_file()


def describe_expiry(epoch: float | None, now: float | None = None) -> str:
    if epoch is None:
        return "none"
    now = time.time() if now is None else now
    delta = int(epoch - now)
    if delta <= 0:
        return f"{format_timestamp(epoch)} (window ended)"
    hours, rem = divmod(delta, 3600)
    return f"{format_timestamp(epoch)} (in {hours}h{rem // 60:02d}m)"
