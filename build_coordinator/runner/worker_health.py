"""Pure derivation of worker health from recorded provider-failure events.

This module never touches the database or the clock itself: callers pass in
the `runner.provider_failure` event payloads and the current time, which keeps
the health computation deterministic and unit-testable in isolation from
routing (build_coordinator.runner.routing) and orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from build_coordinator.runner.routing import PROVIDER_FAILURES


class WorkerLike(Protocol):
    worker_id: str
    provider: str
    enabled: bool


@dataclass(frozen=True)
class WorkerHealth:
    worker_id: str
    status: str
    failure_class: str | None = None
    seconds_remaining: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"worker_id": self.worker_id, "status": self.status}
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        if self.seconds_remaining is not None:
            data["seconds_remaining"] = round(self.seconds_remaining, 3)
        return data


def derive_worker_health(
    workers: Iterable[WorkerLike],
    provider_failure_events: Iterable[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, WorkerHealth]:
    """Map each worker to AVAILABLE or UNAVAILABLE (with failure class and time
    remaining) based on whether its provider is currently on a failure
    cooldown. `provider_failure_events` are `runner.provider_failure`
    event_data payloads (each with `provider`, `failure`, `until`); the
    latest-expiring cooldown per provider wins when several are recorded.
    """
    cooldowns: dict[str, tuple[datetime, str]] = {}
    for event in provider_failure_events:
        if not isinstance(event, dict):
            continue
        provider = event.get("provider")
        failure = str(event.get("failure") or "UNKNOWN").upper()
        if failure not in PROVIDER_FAILURES:
            continue
        until_raw = event.get("until")
        if not provider or not until_raw:
            continue
        try:
            until = until_raw if isinstance(until_raw, datetime) else datetime.fromisoformat(str(until_raw))
        except (TypeError, ValueError):
            continue
        existing = cooldowns.get(provider)
        if existing is None or until > existing[0]:
            cooldowns[provider] = (until, failure)

    health: dict[str, WorkerHealth] = {}
    for worker in workers:
        until_failure = cooldowns.get(worker.provider)
        if until_failure is not None and until_failure[0] > now:
            until, failure = until_failure
            health[worker.worker_id] = WorkerHealth(
                worker_id=worker.worker_id,
                status="UNAVAILABLE",
                failure_class=failure,
                seconds_remaining=(until - now).total_seconds(),
            )
        else:
            health[worker.worker_id] = WorkerHealth(worker_id=worker.worker_id, status="AVAILABLE")
    return health
