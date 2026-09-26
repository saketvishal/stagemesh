from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from build_coordinator.cli import _build_parser, _operator
from build_coordinator.db import DatabaseLifecycle
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveGate,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskEvent,
)
from build_coordinator.operator_dashboard import operator_dashboard


def _session():
    lifecycle = DatabaseLifecycle("sqlite:///:memory:")
    lifecycle.initialize_schema()
    return lifecycle.session()


def _task(task_id: str, state: str, *, objective_id: str | None = None, waiting_input: dict | None = None) -> BuildTask:
    return BuildTask(
        task_id=task_id,
        title=f"Task {task_id}",
        description="",
        acceptance_criteria=[],
        dependencies=[],
        objective_id=objective_id,
        state=state,
        waiting_input=waiting_input or {},
    )


def test_operator_dashboard_separates_attention_from_recoverable_and_passive_waits():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with _session() as session:
        session.add(BuildObjective(objective_id="OBJ-1", goal="ship dashboard", state="HUMAN_GATE"))
        session.add(
            BuildObjectiveGate(
                gate_id="gate-1",
                objective_id="OBJ-1",
                source_task_id="POLICY-1",
                gate_type="REMOTE_PUSH_APPROVAL_REQUIRED",
                reason="approve integration push",
                status="OPEN",
            )
        )
        session.add_all(
            [
                _task(
                    "POLICY-1",
                    "WAITING_FOR_INPUT",
                    objective_id="OBJ-1",
                    waiting_input={"type": "REMOTE_PUSH_APPROVAL_REQUIRED", "question": "approve push?"},
                ),
                _task("RECOVER-1", "RESUMABLE"),
                _task(
                    "RECOVER-BLOCKED-1",
                    "BLOCKED",
                    waiting_input={
                        "failure_evidence": {
                            "reason": "dirty worktree",
                            "recovery_classification": "RECOVERABLE_WORKTREE",
                        }
                    },
                ),
                _task(
                    "RECOVER-BLOCKED-2",
                    "BLOCKED",
                    waiting_input={
                        "failure_evidence": {
                            "reason": "git ref drift",
                            "recovery_classification": "RECOVERABLE_GIT_STATE",
                        }
                    },
                ),
                _task("CI-1", "AWAITING_EXTERNAL_CI"),
                _task("READY-1", "READY"),
            ]
        )
        session.add(
            BuildTaskEvent(
                event_id="event-policy",
                task_id="POLICY-1",
                event_type="task.input_requested",
                event_data={"reason": "policy gate"},
                created_at=now,
            )
        )
        session.add(
            BuildRunnerExecution(
                execution_id="exec-policy",
                task_id="POLICY-1",
                role="INTEGRATION",
                worker_id="integration-1",
                provider="local",
                adapter="fake",
                status="HUMAN_ACTION_REQUIRED",
                human_escalation_type="REMOTE_PUSH_APPROVAL_REQUIRED",
                result_path="/tmp/results/exec-policy.json",
                launched_at=now - timedelta(minutes=1),
                last_observed_at=now,
            )
        )
        session.commit()

        payload = operator_dashboard(session, now=now)

    attention = payload["attention_queue"]
    assert {item["task_id"] for item in attention if item.get("task_id")} == {"POLICY-1"}
    assert {item["category"] for item in attention} == {"unresolved_policy_decision", "human_action_required"}
    assert attention[0]["evidence"]
    autonomous_recovery_ids = {
        item["task_id"] for item in payload["non_actionable"]["autonomous_recovery"]
    }
    assert autonomous_recovery_ids == {"RECOVER-1", "RECOVER-BLOCKED-1", "RECOVER-BLOCKED-2"}
    assert payload["non_actionable"]["passive_waits"][0]["task_id"] == "CI-1"
    assert payload["tasks"]["by_state"]["AWAITING_EXTERNAL_CI"] == 1
    assert "providers" in payload["fleet"]
    assert "workers" in payload["fleet"]


def test_operator_dashboard_classifies_configuration_and_exhausted_recovery_attention():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with _session() as session:
        session.add_all(
            [
                _task(
                    "CONFIG-1",
                    "WAITING_FOR_INPUT",
                    waiting_input={"type": "AGENT_AUTHENTICATION_REQUIRED", "question": "log in"},
                ),
                _task("FAILED-1", "FAILED"),
                _task(
                    "OPERATOR-BLOCKED-1",
                    "BLOCKED",
                    waiting_input={
                        "failure_evidence": {
                            "reason": "manual repair required",
                            "recovery_classification": "OPERATOR_ACTION_REQUIRED",
                        }
                    },
                ),
                _task(
                    "EXHAUSTED-BLOCKED-1",
                    "BLOCKED",
                    waiting_input={
                        "failure_evidence": {
                            "reason": "task needs redesign",
                            "recovery_classification": "TASK_REDESIGN_REQUIRED",
                        }
                    },
                ),
            ]
        )
        session.add(
            BuildRunnerExecution(
                execution_id="exec-retry",
                task_id="FAILED-1",
                role="BUILDER",
                worker_id="builder-a",
                provider="local",
                adapter="fake",
                status="HUMAN_ACTION_REQUIRED",
                human_escalation_type="EXECUTION_RETRY_LIMIT_REACHED",
                launched_at=now - timedelta(minutes=2),
                last_observed_at=now,
            )
        )
        session.commit()

        payload = operator_dashboard(session, now=now)

    categories = {(item["task_id"], item["category"]) for item in payload["attention_queue"]}
    assert ("CONFIG-1", "credentials_or_configuration") in categories
    assert ("FAILED-1", "exhausted_automated_recovery") in categories
    assert ("OPERATOR-BLOCKED-1", "human_action_required") in categories
    assert ("EXHAUSTED-BLOCKED-1", "exhausted_automated_recovery") in categories


def test_operator_dashboard_command_emits_json(capsys):
    parser = _build_parser()
    args = parser.parse_args(["operator", "dashboard"])
    assert args.command == "operator"

    with _session() as session:
        session.add(_task("READY-1", "READY"))
        session.commit()

        _operator(args, session)

    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"]["count"] == 1
    assert payload["attention_queue"] == []
