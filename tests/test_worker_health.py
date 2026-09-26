from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from build_coordinator.runner.worker_health import derive_worker_health


@dataclass(frozen=True)
class _Worker:
    worker_id: str
    provider: str
    enabled: bool = True


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_worker_with_no_failure_events_is_available() -> None:
    workers = [_Worker("w1", "anthropic")]

    health = derive_worker_health(workers, [], now=NOW)

    assert health["w1"].status == "AVAILABLE"
    assert health["w1"].failure_class is None


def test_worker_on_active_cooldown_is_unavailable_with_failure_class() -> None:
    workers = [_Worker("w1", "anthropic")]
    events = [
        {
            "provider": "anthropic",
            "failure": "RATE_LIMITED",
            "until": (NOW + timedelta(minutes=5)).isoformat(),
        }
    ]

    health = derive_worker_health(workers, events, now=NOW)

    assert health["w1"].status == "UNAVAILABLE"
    assert health["w1"].failure_class == "RATE_LIMITED"
    assert health["w1"].seconds_remaining == 300.0


def test_expired_cooldown_reports_available() -> None:
    workers = [_Worker("w1", "anthropic")]
    events = [
        {
            "provider": "anthropic",
            "failure": "RATE_LIMITED",
            "until": (NOW - timedelta(minutes=5)).isoformat(),
        }
    ]

    health = derive_worker_health(workers, events, now=NOW)

    assert health["w1"].status == "AVAILABLE"
    assert health["w1"].failure_class is None


def test_multiple_providers_are_derived_independently() -> None:
    workers = [
        _Worker("anthropic-worker", "anthropic"),
        _Worker("openai-worker", "openai"),
        _Worker("local-worker", "local"),
    ]
    events = [
        {
            "provider": "anthropic",
            "failure": "QUOTA_EXHAUSTED",
            "until": (NOW + timedelta(minutes=10)).isoformat(),
        },
        {
            "provider": "openai",
            "failure": "AUTH_FAILURE",
            "until": (NOW - timedelta(minutes=1)).isoformat(),
        },
    ]

    health = derive_worker_health(workers, events, now=NOW)

    assert health["anthropic-worker"].status == "UNAVAILABLE"
    assert health["anthropic-worker"].failure_class == "QUOTA_EXHAUSTED"
    assert health["openai-worker"].status == "AVAILABLE"
    assert health["local-worker"].status == "AVAILABLE"


def test_latest_expiring_cooldown_wins_for_same_provider() -> None:
    workers = [_Worker("w1", "anthropic")]
    events = [
        {
            "provider": "anthropic",
            "failure": "RATE_LIMITED",
            "until": (NOW + timedelta(minutes=2)).isoformat(),
        },
        {
            "provider": "anthropic",
            "failure": "NETWORK_FAILURE",
            "until": (NOW + timedelta(minutes=20)).isoformat(),
        },
    ]

    health = derive_worker_health(workers, events, now=NOW)

    assert health["w1"].status == "UNAVAILABLE"
    assert health["w1"].failure_class == "NETWORK_FAILURE"


def test_non_provider_failure_event_does_not_poison_provider_health() -> None:
    workers = [_Worker("w1", "openai")]
    events = [
        {
            "provider": "openai",
            "failure": "NO_CHANGES_PRODUCED",
            "until": (NOW + timedelta(minutes=30)).isoformat(),
        }
    ]

    health = derive_worker_health(workers, events, now=NOW)

    assert health["w1"].status == "AVAILABLE"
    assert health["w1"].failure_class is None
