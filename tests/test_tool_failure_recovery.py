from __future__ import annotations

import json

import pytest

from stagemesh.diagnosis import (
    TOOL_FAILURE_EVENT,
    plan_tool_recovery,
    tool_failure_evidence,
    tool_failure_signature,
)
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

    def task_audit_events(self, event_type: str, task_id: str) -> list[dict]:
        return [row for row in self.rows if row["event_type"] == event_type and json.loads(row["payload"]).get("task_id") == task_id]


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
    assert second["action"] == "stop" and second["attempted"] == []
    assert second["previous_recommendations"] == ["retry_once"]
    ex._record_tool_evidence(store, "t2", "e3", "/w", 1, "", "request failed: ETIMEDOUT", "tool_execution_failure", False)
    assert json.loads(store.rows[-1]["payload"])["evidence"]["recovery"]["action"] == "retry_once"
    assert all(r["event_type"] == TOOL_FAILURE_EVENT for r in store.rows)


def test_plan_reads_only_this_tasks_history() -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "other", "evidence": {**ev, "recovery": {"action": "retry_once"}}})
    assert tool_recovery_plan(store, "t1", ev)["action"] == "retry_once"


def test_unexecuted_retry_does_not_spend_an_attempt_or_unlock_setup() -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    configured = {"transient": ["configured-setup", "--local"]}
    first = tool_recovery_plan(store, "t1", ev, configured)
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})

    stopped = tool_recovery_plan(store, "t1", ev, configured)

    assert stopped["action"] == "stop"
    assert stopped["attempted"] == []
    assert stopped["previous_recommendations"] == ["retry_once"]
    assert stopped["execution_status"] == "NOT_CONFIRMED"
    assert "execution" in stopped["operator_action"]
    assert "setup_command" not in stopped


def test_unexecuted_setup_is_not_reported_as_an_attempt() -> None:
    store = _Store()
    ev = _ev("No module named 'x'")
    configured = {"missing_dependency": ["configured-setup"]}
    first = tool_recovery_plan(store, "t1", ev, configured)
    assert first["action"] == "setup"
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})

    stopped = tool_recovery_plan(store, "t1", ev, configured)

    assert stopped["action"] == "stop" and stopped["attempted"] == []
    assert stopped["previous_recommendations"] == ["setup"]


@pytest.mark.parametrize("prior_recovery", [None, {}, {"action": "stop"}, {"action": "future_action"}])
def test_previous_failure_without_confirmed_execution_still_stops(prior_recovery) -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": prior_recovery}})

    stopped = tool_recovery_plan(store, "t1", ev)

    assert stopped["action"] == "stop" and stopped["attempted"] == []
    assert stopped["execution_status"] == "NOT_CONFIRMED"


def test_recommendation_history_survives_store_reopen_and_stays_stopped(tmp_path) -> None:
    from stagemesh.persistence import Store

    path = tmp_path / "recovery.sqlite3"
    ev = _ev("ETIMEDOUT")
    store = Store(path)
    store.migrate()
    first = tool_recovery_plan(store, "t1", ev)
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})
    store.close()
    store = Store(path)
    try:
        second = tool_recovery_plan(store, "t1", ev)
        store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": second}})
        third = tool_recovery_plan(store, "t1", ev)
        assert second["action"] == third["action"] == "stop"
        assert second["attempted"] == third["attempted"] == []
        assert third["previous_recommendations"] == ["retry_once", "stop"]
        assert tool_recovery_plan(store, "other", ev)["action"] == "retry_once"
        assert tool_recovery_plan(store, "t1", _ev("ETIMEDOUT", "/other"))["action"] == "retry_once"
    finally:
        store.close()


@pytest.mark.parametrize("noise", ["unrelated_events", "same_task_other_signatures"])
def test_other_activity_cannot_evict_recovery_stop_history(tmp_path, noise) -> None:
    from stagemesh.persistence import Store

    path = tmp_path / "history.sqlite3"
    store = Store(path)
    store.migrate()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {
        "task_id": "t1", "evidence": {**ev, "recovery": tool_recovery_plan(store, "t1", ev)},
    })
    for index in range(201):
        if noise == "unrelated_events":
            store.add_audit_event("unrelated.event", {"task_id": "other", "index": index})
        else:
            store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": _ev("ETIMEDOUT", f"/noise/{index}")})
    store.close()
    store = Store(path)
    try:
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["retry_once"]
    finally:
        store.close()


def test_recovery_summary_is_bounded_without_losing_history_count(tmp_path) -> None:
    from stagemesh.persistence import Store

    store = Store(tmp_path / "summary.sqlite3")
    store.migrate()
    ev = _ev("ETIMEDOUT")
    try:
        for action in ["retry_once", *(["stop"] * 24)]:
            store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": {"action": action}}})
        # Force timestamp ties so insertion order must still be deterministic.
        store.conn.execute("UPDATE audit_events SET created_at=42")
        store.conn.commit()
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["stop"] * 20
        assert stopped["previous_recommendation_count"] == 25
    finally:
        store.close()


def test_corrupt_or_non_object_audit_payload_does_not_hide_known_failure(tmp_path) -> None:
    from stagemesh.persistence import Store

    store = Store(tmp_path / "corrupt.sqlite3")
    store.migrate()
    ev = _ev("ETIMEDOUT")
    try:
        store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": {"action": "retry_once"}}})
        for raw in ["{", "[]", "null", '{"task_id":"other"}']:
            event_id = store.add_audit_event(TOOL_FAILURE_EVENT, {})
            # Inject stored corruption rather than weakening the normal audit writer.
            store.conn.execute("UPDATE audit_events SET payload=? WHERE id=?", (raw, event_id))
        store.conn.commit()
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["retry_once"]
        assert stopped["previous_recommendation_count"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("stderr,kind", [("ETIMEDOUT", "transient"), ("No module named 'x'", "missing_dependency")])
@pytest.mark.parametrize("attempted", [["retry"], ["setup"], ["unknown"], ("retry", "setup")])
def test_any_prior_action_stops_signature_even_with_configured_setup(
    stderr: str, kind: str, attempted: list[str] | tuple[str, ...]
) -> None:
    evidence = _ev(stderr)
    plan = plan_tool_recovery(evidence, attempted, {kind: ["configured-setup", "--local"]})
    assert plan["action"] == "stop"
    assert plan["attempted"] == list(attempted)
    assert plan["signature"] == tool_failure_signature(evidence)
    assert plan["operator_action"] == evidence["next_action"]
    assert "setup_command" not in plan
