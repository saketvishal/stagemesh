"""Bounded provider posture for the loopback operator UI.

The projection separates configuration, explicit authorization, executable
discovery, durable live-acceptance evidence, cooldown state, and recorded
invocation.  It never launches a provider or returns commands, paths, output,
credentials, billing guesses, or free-form failure text.
"""

from __future__ import annotations

import json
import math
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .activity import safe_activity_code
from .config import BUILTIN_PROVIDERS, StageMeshConfig, load_config
from .domain import ExecutionKind, ExecutionStatus
from .persistence import Store
from .provider_acceptance import LIVE_PROVIDER_ACCEPTANCE_EVENT
from .provider_cooldown import (
    DEFAULT_FAILURE_COOLDOWN_SECONDS,
    DEFAULT_QUOTA_COOLDOWN_SECONDS,
    active_cooldowns,
)
from .providers import RuntimeCommandAdapter, adapters_from_config

PROVIDERS_CONTRACT = "stagemesh.provider-status/1"
PROVIDER_LIMIT = 100
OBSERVED_PROVIDER_SCAN_LIMIT = 500
ACCEPTANCE_SCAN_LIMIT = 5000
_CAPABILITY_STAGES = {"code": "IMPLEMENT", "review": "REVIEW"}


def _command_discovery(adapter: RuntimeCommandAdapter) -> str:
    states = []
    for command in adapter.authorization_commands():
        executable = command[0]
        if "/" in executable or "\\" in executable:
            states.append("NOT_PROBED_PATH")
        else:
            states.append("DISCOVERED" if shutil.which(executable) else "NOT_DISCOVERED")
    if not states:
        return "NOT_APPLICABLE"
    if "NOT_DISCOVERED" in states:
        return "NOT_DISCOVERED"
    if "NOT_PROBED_PATH" in states:
        return "NOT_PROBED_PATH"
    return "DISCOVERED"


def _configured_names(config: StageMeshConfig) -> set[str]:
    return set(BUILTIN_PROVIDERS) | set(config.provider_commands) | set(config.provider_specs)


def _source(config: StageMeshConfig, name: str) -> str:
    if name in config.provider_commands:
        return "PROJECT_OR_ENV"
    if name in BUILTIN_PROVIDERS:
        return "BUILTIN"
    return "RECORDED_ONLY"


def _execution_rows(store: Store) -> tuple[dict[str, dict[str, Any]], bool, set[str]]:
    rows = store.conn.execute(
        "SELECT actor,COUNT(*) AS total,"
        "SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) AS running,"
        "SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded,"
        "SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,"
        "SUM(CASE WHEN status='UNKNOWN' THEN 1 ELSE 0 END) AS unknown,"
        "MAX(started_at) AS last_started_at "
        "FROM executions WHERE actor IS NOT NULL GROUP BY actor "
        "ORDER BY last_started_at DESC,actor LIMIT ?",
        (OBSERVED_PROVIDER_SCAN_LIMIT + 1,),
    ).fetchall()
    observed: dict[str, dict[str, Any]] = {}
    withheld: set[str] = set()
    for row in rows[:OBSERVED_PROVIDER_SCAN_LIMIT]:
        name = safe_activity_code(row["actor"])
        if name is None:
            if isinstance(row["actor"], str):
                withheld.add(row["actor"].casefold())
            continue
        latest = store.conn.execute(
            "SELECT kind,status,started_at FROM executions WHERE actor=? "
            "ORDER BY started_at DESC,rowid DESC LIMIT 1",
            (row["actor"],),
        ).fetchone()
        key = name.casefold()
        current = observed.get(key)
        candidate = {
            "name": name,
            "total": int(row["total"] or 0),
            "running": int(row["running"] or 0),
            "succeeded": int(row["succeeded"] or 0),
            "failed": int(row["failed"] or 0),
            "unknown": int(row["unknown"] or 0),
            "last_started_at": float(row["last_started_at"]),
            "last_kind": (
                str(latest["kind"])
                if latest is not None and latest["kind"] in {kind.value for kind in ExecutionKind}
                else "UNKNOWN"
            ),
            "last_status": (
                str(latest["status"])
                if latest is not None
                and latest["status"] in {status.value for status in ExecutionStatus}
                else "UNKNOWN"
            ),
        }
        if current is None:
            observed[key] = candidate
            continue
        for field in ("total", "running", "succeeded", "failed", "unknown"):
            current[field] += candidate[field]
        if candidate["last_started_at"] > current["last_started_at"]:
            current.update(
                name=candidate["name"],
                last_started_at=candidate["last_started_at"],
                last_kind=candidate["last_kind"],
                last_status=candidate["last_status"],
            )
    return observed, len(rows) > OBSERVED_PROVIDER_SCAN_LIMIT, withheld


def _acceptance_payloads(store: Store) -> tuple[dict[str, dict[str, Any]], bool]:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT ?",
        (LIVE_PROVIDER_ACCEPTANCE_EVENT, ACCEPTANCE_SCAN_LIMIT + 1),
    ).fetchall()
    payloads: dict[str, dict[str, Any]] = {}
    for row in rows[:ACCEPTANCE_SCAN_LIMIT]:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        provider = safe_activity_code(payload.get("provider"))
        if provider is not None:
            payloads.setdefault(provider.casefold(), payload)
    return payloads, len(rows) > ACCEPTANCE_SCAN_LIMIT


def _acceptance_state(
    payload: dict[str, Any] | None,
    capability: str,
    ttl_seconds: float,
    now: float,
    scan_truncated: bool,
) -> dict[str, Any]:
    if payload is None:
        return {
            "state": "UNAVAILABLE" if scan_truncated else "MISSING",
            "checked_at": None,
            "age_seconds": None,
            "ttl_seconds": ttl_seconds,
        }
    checked_at = payload.get("checked_at")
    valid_checked_at = (
        isinstance(checked_at, (int, float))
        and not isinstance(checked_at, bool)
        and math.isfinite(checked_at)
    )
    age = max(0.0, now - float(checked_at)) if valid_checked_at else None
    prefix = "implementation" if capability == "code" else "review"
    ready = payload.get(f"{prefix}_checked") is True and payload.get(f"{prefix}_ready") is True
    if ready:
        state = "PASSING" if age is not None and age <= ttl_seconds else "STALE"
    else:
        delay = {
            "quota_rate_limit": DEFAULT_QUOTA_COOLDOWN_SECONDS,
            "provider_timeout": DEFAULT_FAILURE_COOLDOWN_SECONDS,
            "transient_provider_failure": DEFAULT_FAILURE_COOLDOWN_SECONDS,
        }.get(payload.get("failure_reason"))
        retry_due = delay is not None and age is not None and age >= delay
        state = "RETRY_DUE" if retry_due else "FAILED"
    return {
        "state": state,
        "checked_at": float(checked_at) if valid_checked_at else None,
        "age_seconds": age,
        "ttl_seconds": ttl_seconds,
    }


def _live(
    payloads: dict[str, dict[str, Any]],
    name: str,
    capabilities: list[str],
    ttl_seconds: float,
    now: float,
    scan_truncated: bool,
) -> dict[str, dict[str, Any] | str]:
    result: dict[str, dict[str, Any] | str] = {}
    for stage, capability in (("IMPLEMENT", "code"), ("REVIEW", "review")):
        if stage not in capabilities:
            result[stage] = "NOT_APPLICABLE"
            continue
        result[stage] = _acceptance_state(
            payloads.get(name.casefold()), capability, ttl_seconds, now, scan_truncated
        )
    return result


def _unavailable(observed_at: str) -> dict[str, Any]:
    return {
        "observed_at": observed_at,
        "status": "UNAVAILABLE",
        "providers": [],
        "provider_total": 0,
        "provider_total_kind": "EXACT",
        "provider_limit": PROVIDER_LIMIT,
        "withheld_provider_count": 0,
        "truncated": False,
        "attention": [
            {
                "code": "PROVIDER_CONFIGURATION_UNAVAILABLE",
                "message": "Provider configuration could not be safely loaded.",
            }
        ],
        "provider_calls": 0,
        "network_calls": 0,
        "database_writes": 0,
        "billing_usage": "UNKNOWN",
        "acceptance_scan_limit": ACCEPTANCE_SCAN_LIMIT,
        "acceptance_history_truncated": False,
    }


class ProviderStatusProjection:
    """Fresh local configuration plus durable, read-only provider observations."""

    def __init__(self, project: Path):
        self.project = Path(project).resolve()

    def serialize(self, store: Store) -> dict[str, Any]:
        observed_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            config = load_config(self.project)
            adapters = adapters_from_config(config)
        except Exception:  # noqa: BLE001 - never return config, command, path or parser details
            return _unavailable(observed_at)

        all_configured = _configured_names(config)
        configured = {
            name.casefold(): name
            for name in all_configured
            if safe_activity_code(name) is not None
        }
        withheld = {
            name.casefold() for name in all_configured if safe_activity_code(name) is None
        }
        adapter_by_name = {adapter.name.casefold(): adapter for adapter in adapters}
        observed, observed_truncated, observed_withheld = _execution_rows(store)
        withheld.update(observed_withheld)
        acceptance_payloads, acceptance_truncated = _acceptance_payloads(store)
        now = time.time()
        cooldown_by_name = {
            str(item["provider"]).casefold(): item
            for item in active_cooldowns(store, config.provider_failure_cooldown_seconds)
            if safe_activity_code(item.get("provider")) is not None
        }
        names = sorted(
            set(configured) | set(observed),
            key=lambda key: (
                0 if key in configured else 1,
                configured.get(key, observed.get(key, {}).get("name", key)).casefold(),
            ),
        )
        provider_total = len(names) + len(withheld)
        result = []
        authorized = {name.casefold() for name in config.authorized_providers or ()}
        for key in names[:PROVIDER_LIMIT]:
            adapter = adapter_by_name.get(key)
            execution = observed.get(key)
            name = configured.get(key) or (execution or {}).get("name") or key
            capabilities = (
                sorted(_CAPABILITY_STAGES[item] for item in adapter.capabilities if item in _CAPABILITY_STAGES)
                if adapter is not None
                else []
            )
            cooldown = cooldown_by_name.get(key)
            result.append(
                {
                    "name": name,
                    "configured": key in configured,
                    "command_source": _source(config, name) if key in configured else "RECORDED_ONLY",
                    "capabilities": capabilities,
                    "authorization_configured": config.authorized_providers is not None,
                    "authorized_for_unattended": key in authorized,
                    "executable_discovery": (
                        _command_discovery(adapter) if adapter is not None else "NOT_APPLICABLE"
                    ),
                    "live_acceptance": _live(
                        acceptance_payloads,
                        name,
                        capabilities,
                        config.live_acceptance_ttl_seconds,
                        now,
                        acceptance_truncated,
                    ),
                    "cooldown": (
                        {"active": False}
                        if cooldown is None
                        else {
                            "active": True,
                            "failure_class": cooldown["failure_class"],
                            "stages": cooldown["stages"],
                            "seconds_remaining": cooldown["seconds_remaining"],
                        }
                    ),
                    "invocation": (
                        {
                            "recorded": False,
                            "total": 0,
                            "running": 0,
                            "succeeded": 0,
                            "failed": 0,
                            "unknown": 0,
                            "last_started_at": None,
                            "last_kind": None,
                            "last_status": None,
                        }
                        if execution is None
                        else {"recorded": True, **{field: execution[field] for field in (
                            "total", "running", "succeeded", "failed", "unknown",
                            "last_started_at", "last_kind", "last_status",
                        )}}
                    ),
                }
            )
        return {
            "observed_at": observed_at,
            "status": "AVAILABLE",
            "providers": result,
            "provider_total": provider_total,
            "provider_total_kind": "LOWER_BOUND" if observed_truncated else "EXACT",
            "provider_limit": PROVIDER_LIMIT,
            "withheld_provider_count": len(withheld),
            "truncated": provider_total > PROVIDER_LIMIT or observed_truncated,
            "attention": [],
            "provider_calls": 0,
            "network_calls": 0,
            "database_writes": 0,
            "billing_usage": "UNKNOWN",
            "acceptance_scan_limit": ACCEPTANCE_SCAN_LIMIT,
            "acceptance_history_truncated": acceptance_truncated,
        }
