from __future__ import annotations

import json

import pytest

from stagemesh.diagnosis import TOOL_FAILURE_EVENT, plan_tool_recovery, tool_failure_evidence, tool_failure_signature
from stagemesh.execution import SubprocessExecutor
from stagemesh.remediation import tool_recovery_plan


def _ev(stderr: str, cwd: str = "/w") -> dict:
    ev = tool_failure_evidence(["tool", "run"], cwd, 1, "", stderr)
    assert ev is not None
    return ev


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add_audit_event(self, event_type: str, payload: dict) -> str:
        self.rows.append({"event_type": event_type, "payload": json.dumps(payload)})
        return "id"

    def audit_events(self, limit: int = 500) -> list[dict]:
        return list(reversed(self.rows))[:limit]


def test_transient_failure_gets_one_retry_then_stops_with_operator_action() -> None:
    assert plan_tool_recovery(_ev("ETIMEDOUT"))["action"] == "retry_once"
    stopped = plan_tool_recovery(_ev("ETIMEDOUT"), ["retry"])
    assert stopped["action"] == "stop" and stopped["attempted"] == ["retry"] and stopped["operator_action"]


@pytest.mark.parametrize(
    "stderr", ["EACCES: permission denied", "command not found", "No module named 'x'", "AssertionError: assert 1 == 2"]
)
def test_non_transient_failures_never_retry_or_install(stderr: str) -> None:
    plan = plan_tool_recovery(_ev(stderr))
    assert plan["action"] == "stop" and "setup_command" not in plan and plan["operator_action"]


def test_only_explicitly_configured_setup_runs_once() -> None:
    ev = _ev("No module named 'x'")
    configured = {"missing_dependency": ["make", "setup"]}
    plan = plan_tool_recovery(ev, setup_actions=configured)
    assert plan["action"] == "setup" and plan["setup_command"] == ["make", "setup"]
    assert plan_tool_recovery(ev, ["setup"], configured)["action"] == "stop"


def test_permission_failures_ignore_configured_setup() -> None:
    plan = plan_tool_recovery(_ev("EACCES: permission denied"), setup_actions={"permission": ["chmod", "-R", "777", "."]})
    assert plan["action"] == "stop" and "setup_command" not in plan


def test_signature_ignores_excerpt_but_not_cwd() -> None:
    assert tool_failure_signature(_ev("ETIMEDOUT a")) == tool_failure_signature(_ev("ETIMEDOUT b"))
    assert tool_failure_signature(_ev("ETIMEDOUT", "/a")) != tool_failure_signature(_ev("ETIMEDOUT", "/b"))


def test_executor_stops_on_repeated_signature_and_isolates_tasks() -> None:
    store = _Store()
    ex = SubprocessExecutor(["tool"])
    for execution in ("e1", "e2"):
        ex._record_tool_evidence(store, "t1", execution, "/w", 1, "", "request failed: ETIMEDOUT", "tool_execution_failure", False)
    first, second = (json.loads(r["payload"])["evidence"]["recovery"] for r in store.rows)
    assert first["action"] == "retry_once"
    assert second["action"] == "stop" and second["attempted"] == ["retry"]
    ex._record_tool_evidence(store, "t2", "e3", "/w", 1, "", "request failed: ETIMEDOUT", "tool_execution_failure", False)
    assert json.loads(store.rows[-1]["payload"])["evidence"]["recovery"]["action"] == "retry_once"
    assert all(r["event_type"] == TOOL_FAILURE_EVENT for r in store.rows)


def test_plan_reads_only_this_tasks_history() -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "other", "evidence": {**ev, "recovery": {"action": "retry_once"}}})
    assert tool_recovery_plan(store, "t1", ev)["action"] == "retry_once"
