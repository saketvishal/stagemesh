"""CLI provider cooldown: the durable, visible, clearable view of providers StageMesh has stopped using for a while.

A provider enters cooldown when its CLI reports it is out of quota, rate limited, unauthenticated or unavailable, or when it makes
no progress repeatedly. Cooldown is a property of the *provider*, not of a task: selection skips the provider and uses the others,
the task is never classified as a code defect, and no attempt is charged to the task. The state lives in the audit stream the pool
already writes (`provider.failure` with a `retry_at`); this module aggregates it for operators and adds the clear control.
Only CLI providers are involved; nothing here calls a provider API.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .persistence import Store

CLEARED_EVENT = "provider.cooldown_cleared"
REPEATED_NO_PROGRESS = "repeated_no_progress"
NO_PROGRESS_THRESHOLD = 3  # consecutive no-progress invocations by one provider before it is rested

_HINTS = {
    "quota_rate_limit": "out of quota or rate limited: wait for the reset{reset}, or clear once it has reset",
    "authentication_failure": "not logged in or credentials rejected: log in to the provider CLI, then clear the cooldown",
    "provider_unavailable": "the provider CLI is missing or unavailable: install it or fix PATH, then clear the cooldown",
    "transient_provider_failure": "temporary provider outage: it retries automatically at the deadline",
    REPEATED_NO_PROGRESS: "the provider keeps returning without changes: other providers are used meanwhile; it retries at the deadline",
    "provider_failure": "provider failure: it retries automatically at the deadline",
}


def cleared_at(store: Store, provider: str) -> float:
    """When an operator last cleared this provider's cooldown (failures recorded before that no longer count)."""
    latest = 0.0
    for row in store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at DESC LIMIT 200", (CLEARED_EVENT,)
    ):
        payload = _loads(row["payload"])
        if payload.get("provider") in (provider, "*"):
            latest = max(latest, float(row["created_at"]))
            break
    return latest


def active_cooldowns(store: Store, cooldown_seconds: float, now: float | None = None) -> list[dict[str, Any]]:
    """Providers currently resting, one entry each: class, stages, first/last seen, last error, deadline and what to do."""
    from .provider_pool import PROVIDER_FAILURE_EVENT, _failure_cooldown_seconds, provider_wide_outcomes

    now = time.time() if now is None else now
    wide = provider_wide_outcomes()
    entries: dict[str, dict[str, Any]] = {}
    for row in store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (PROVIDER_FAILURE_EVENT,)
    ):
        payload = _loads(row["payload"])
        provider = str(payload.get("provider") or "")
        reason = str(payload.get("reason") or "provider_failure")
        seen = float(row["created_at"])
        if not provider or reason not in wide or seen <= cleared_at(store, provider):
            continue
        cooldown = _failure_cooldown_seconds(reason, cooldown_seconds)
        retry_at = payload.get("retry_at")
        deadline = float(retry_at) if isinstance(retry_at, (int, float)) else seen + cooldown
        if cooldown <= 0 or deadline <= now:
            continue
        entry = entries.setdefault(provider, {"provider": provider, "first_seen": seen, "stages": set(), "retry_at": deadline})
        entry["failure_class"] = reason
        entry["last_seen"] = seen
        entry["stages"].add(str(payload.get("stage") or ""))
        entry["retry_at"] = max(entry["retry_at"], deadline)
        entry["last_error"] = str(payload.get("provider_output") or reason)[:200]
        entry["reset_hint"] = payload.get("retry_after")
    result = []
    for entry in sorted(entries.values(), key=lambda item: item["provider"]):
        entry["stages"] = sorted(s for s in entry["stages"] if s)
        entry["seconds_remaining"] = max(0, int(entry["retry_at"] - now))
        reset = f" ({entry['reset_hint']})" if entry.get("reset_hint") else ""
        entry["action"] = _HINTS.get(entry["failure_class"], _HINTS["provider_failure"]).format(reset=reset)
        entry["clear_command"] = f"stagemesh cooldown clear {entry['provider']}"
        result.append(entry)
    return result


def format_cooldowns(cooldowns: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in cooldowns:
        minutes, seconds = divmod(item["seconds_remaining"], 60)
        hours, minutes = divmod(minutes, 60)
        remaining = f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"
        lines.append(
            f"provider cooldown {item['provider']}: {item['failure_class']} ({', '.join(item['stages']) or 'any stage'}), "
            f"{remaining} left; {item['action']}; clear: {item['clear_command']}"
        )
    return lines


def clear_cooldown(store: Store, provider: str | None, cooldown_seconds: float) -> list[str]:
    """Clear one provider's cooldown (or every active one when `provider` is None). Returns the providers cleared."""
    active = [item["provider"] for item in active_cooldowns(store, cooldown_seconds)]
    targets = [provider] if provider else active
    for name in targets:
        store.add_audit_event(CLEARED_EVENT, {"provider": name, "was_active": name in active})
    return targets


def no_progress_streak(store: Store, provider: str) -> int:
    """Consecutive no-progress invocations by this provider since it last produced a candidate, was rested or was cleared."""
    from .provider_pool import PROVIDER_FAILURE_EVENT, PROVIDER_NO_PROGRESS_EVENT

    boundary = cleared_at(store, provider)
    row = store.conn.execute("SELECT MAX(created_at) AS at FROM candidates WHERE produced_by=?", (provider,)).fetchone()
    if row is not None and row["at"] is not None:
        boundary = max(boundary, float(row["at"]))
    for failure in store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at DESC LIMIT 200", (PROVIDER_FAILURE_EVENT,)
    ):
        payload = _loads(failure["payload"])
        if payload.get("provider") == provider and payload.get("reason") == REPEATED_NO_PROGRESS:
            boundary = max(boundary, float(failure["created_at"]))
            break
    return sum(
        1
        for event in store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? AND created_at>?", (PROVIDER_NO_PROGRESS_EVENT, boundary)
        )
        if _loads(event["payload"]).get("provider") == provider
    )


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
