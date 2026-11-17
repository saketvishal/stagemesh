"""Durable operator pause state for configured provider adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable

from .persistence import Store

PAUSED_EVENT = "provider.operator_paused"
RESUMED_EVENT = "provider.operator_resumed"


def canonical_provider(name: str, configured: Iterable[str]) -> str:
    matches = {item.casefold(): item for item in configured}
    canonical = matches.get(name.casefold())
    if canonical is None:
        raise ValueError(f"unknown provider: {name}")
    return canonical


def is_provider_paused(store: Store, provider: str) -> bool:
    key = provider.casefold()
    for row in store.conn.execute(
        "SELECT event_type,payload FROM audit_events "
        "WHERE event_type IN (?,?) ORDER BY created_at DESC,rowid DESC",
        (PAUSED_EVENT, RESUMED_EVENT),
    ):
        payload = _payload(row["payload"])
        if str(payload.get("provider", "")).casefold() == key:
            return str(row["event_type"]) == PAUSED_EVENT
    return False


def set_provider_paused(
    store: Store, provider: str, configured: Iterable[str], *, paused: bool
) -> str:
    canonical = canonical_provider(provider, configured)
    store.add_audit_event(
        PAUSED_EVENT if paused else RESUMED_EVENT,
        {"provider": canonical, "paused": paused},
    )
    return canonical


def operator_pause_rows(store: Store, configured: Iterable[str]) -> list[dict[str, object]]:
    return [
        {
            "provider": name,
            "paused": is_provider_paused(store, name),
            "resume_command": f"stagemesh provider resume {name}",
        }
        for name in sorted(configured, key=str.casefold)
    ]


def format_operator_pauses(rows: Iterable[dict[str, object]]) -> list[str]:
    return [
        f"provider {row['provider']}: paused by operator; resume: {row['resume_command']}"
        for row in rows
        if row["paused"]
    ]


def _payload(raw: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
