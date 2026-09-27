from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from build_coordinator.cli import _build_parser, _metrics
from build_coordinator.db import DatabaseLifecycle
from build_coordinator.metrics import coordinator_metrics
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskClaim


def _session():
    lifecycle = DatabaseLifecycle("sqlite:///:memory:")
    lifecycle.initialize_schema()
    return lifecycle.session()


def _task(task_id: str, state: str, created_at: datetime) -> BuildTask:
    return BuildTask(
        task_id=task_id,
        title=f"Task {task_id}",
        description="",
        acceptance_criteria=[],
        dependencies=[],
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )


def test_coordinator_metrics_are_derived_from_durable_rows():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with _session() as session:
        session.add_all(
            [
                _task("READY-1", "READY", now - timedelta(minutes=10)),
                _task("READY-2", "RESUMABLE", now - timedelta(minutes=9)),
                _task("DONE-1", "DONE", now - timedelta(minutes=8)),
            ]
        )
        session.add_all(
            [
                BuildTaskClaim(
                    task_id="READY-1",
                    claim_type="IMPLEMENTATION",
                    worker_id="builder-a",
                    claimed_at=now - timedelta(minutes=5),
                    lease_expires_at=now + timedelta(minutes=25),
                    last_heartbeat_at=now - timedelta(minutes=1),
                    status="ACTIVE",
                ),
                BuildTaskClaim(
                    task_id="DONE-1",
                    claim_type="REVIEW",
                    worker_id="reviewer-a",
                    claimed_at=now - timedelta(minutes=4),
                    lease_expires_at=now - timedelta(minutes=1),
                    last_heartbeat_at=now - timedelta(minutes=4),
                    status="COMPLETED",
                ),
            ]
        )
        session.add_all(
            [
                BuildRunnerExecution(
                    execution_id="exec-1",
                    task_id="READY-1",
                    role="BUILDER",
                    worker_id="builder-a",
                    adapter="fake",
                    claim_id=None,
                    status="RUNNING",
                    launched_at=now - timedelta(seconds=30),
                    last_observed_at=now - timedelta(seconds=5),
                ),
                BuildRunnerExecution(
                    execution_id="exec-2",
                    task_id="DONE-1",
                    role="REVIEWER",
                    worker_id="reviewer-a",
                    adapter="fake",
                    claim_id=None,
                    status="SUCCEEDED",
                    launched_at=now - timedelta(minutes=3),
                    last_observed_at=now - timedelta(minutes=2),
                    completed_at=now - timedelta(minutes=2),
                ),
            ]
        )
        session.commit()

        payload = coordinator_metrics(session, now=now)

    assert payload["queue_depth"]["total"] == 2
    assert payload["queue_depth"]["by_state"]["READY"] == 1
    assert payload["queue_depth"]["by_state"]["RESUMABLE"] == 1
    assert payload["claim_latency"]["implementation"]["count"] == 1
    assert payload["claim_latency"]["implementation"]["avg_seconds"] == 300
    assert payload["execution_outcomes"]["by_status"] == {"RUNNING": 1, "SUCCEEDED": 1}
    assert payload["execution_outcomes"]["by_role"] == {
        "BUILDER": {"RUNNING": 1},
        "REVIEWER": {"SUCCEEDED": 1},
    }
    assert payload["worker_utilisation"]["builder-a"]["active_claims"] == 1
    assert payload["worker_utilisation"]["builder-a"]["active_executions"] == 1
    assert payload["worker_utilisation"]["builder-a"]["active_units"] == 1
    assert payload["worker_utilisation"]["builder-a"]["total_busy_seconds"] == 30
    assert payload["worker_utilisation"]["reviewer-a"]["completed_executions"] == 1
    assert payload["worker_utilisation"]["reviewer-a"]["total_busy_seconds"] == 60


def test_metrics_command_parses_and_emits_json(capsys):
    parser = _build_parser()
    args = parser.parse_args(["metrics"])
    assert args.command == "metrics"

    with _session() as session:
        session.add(_task("READY-1", "READY", datetime(2026, 1, 1, tzinfo=UTC)))
        session.commit()

        _metrics(args, session)

    payload = json.loads(capsys.readouterr().out)
    assert payload["queue_depth"]["total"] == 1
    assert payload["execution_outcomes"]["total"] == 0
