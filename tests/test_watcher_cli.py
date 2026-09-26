"""CLI parser tests for the watcher command surface (SDD-001 section 9.1,
acceptance criterion 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from build_coordinator.cli import _build_parser, _run, _watcher_metrics, _watcher_running_health


@pytest.mark.parametrize(
    "argv",
    [
        ["watcher", "install"],
        ["watcher", "install", "--repo", "stagemesh-orchestrator"],
        ["watcher", "uninstall"],
        ["watcher", "start"],
        ["watcher", "stop"],
        ["watcher", "status"],
        ["watcher", "run", "--foreground"],
        ["watcher", "run", "--foreground", "--once"],
        ["watcher", "run", "--foreground", "--repo", "stagemesh-orchestrator"],
        ["watcher", "provision-labels"],
        ["watcher", "provision-labels", "--dry-run"],
    ],
)
def test_watcher_command_surface_parses(argv):
    parser = _build_parser()
    args = parser.parse_args(argv)
    assert args.command == "watcher"


def test_watcher_requires_a_subcommand():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["watcher"])


def test_watcher_run_defaults_once_to_false():
    parser = _build_parser()
    args = parser.parse_args(["watcher", "run", "--foreground"])
    assert args.once is False


def test_watcher_command_dispatches_to_watcher_handler(monkeypatch):
    called = {}

    def fake_watcher(args, session):
        called["args"] = args
        called["session"] = session

    monkeypatch.setattr("build_coordinator.cli._watcher", fake_watcher)
    args = SimpleNamespace(command="watcher", watcher_command="status")
    session = object()

    _run(args, session)

    assert called == {"args": args, "session": session}


def test_watcher_provision_labels_defaults_dry_run_to_false():
    parser = _build_parser()
    args = parser.parse_args(["watcher", "provision-labels"])
    assert args.dry_run is False


def test_watcher_running_health_requires_recent_heartbeat():
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)

    healthy = _watcher_running_health(
        scheduler_running=True,
        process_alive=True,
        heartbeat_at=now - timedelta(seconds=10),
        poll_seconds=5,
        now=now,
    )
    assert healthy["running"] is True

    stale = _watcher_running_health(
        scheduler_running=True,
        process_alive=True,
        heartbeat_at=now - timedelta(seconds=60),
        poll_seconds=5,
        now=now,
    )
    assert stale["running"] is False
    assert stale["heartbeat_healthy"] is False


def test_watcher_running_health_allows_unknown_os_visibility_with_recent_heartbeat():
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)

    health = _watcher_running_health(
        scheduler_running=None,
        process_alive=None,
        heartbeat_at=now - timedelta(seconds=10),
        poll_seconds=5,
        now=now,
    )

    assert health["running"] is True
    assert health["heartbeat_healthy"] is True


def test_watcher_running_health_rejects_confirmed_dead_process():
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)

    health = _watcher_running_health(
        scheduler_running=None,
        process_alive=False,
        heartbeat_at=now - timedelta(seconds=10),
        poll_seconds=5,
        now=now,
    )

    assert health["running"] is False


def test_watcher_status_metrics_include_reload_and_failure_counts():
    last_cycle_summary = {"config_reload": {"state": "reloaded", "worker_count": 2}}
    record = SimpleNamespace(
        restart_count=3,
        consecutive_failure_count=2,
        backoff_until=None,
        last_cycle_summary=last_cycle_summary,
    )

    metrics = _watcher_metrics(record)

    assert metrics["restart_count"] == 3
    assert metrics["consecutive_failure_count"] == 2
    assert metrics["config_reload"] == {"state": "reloaded", "worker_count": 2}
