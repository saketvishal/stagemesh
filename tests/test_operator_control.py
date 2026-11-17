from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import stagemesh.operator_control as operator_control_module
from stagemesh.audit import record_audit
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.operator_control import (
    CONTROL_RECEIPT_EVENT,
    OperatorControl,
    OperatorControlError,
)
from stagemesh.persistence import Store
from stagemesh.process_identity import owner_execution_fields
from stagemesh.queue_control import QUEUE_CONTROL_EVENT, latest_queue_control


@pytest.fixture
def control_store(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("blocked task", source_id="control-task")
    store.block_task(task_id)
    yield store, task_id
    store.close()


def _control(now=lambda: 100.0, *, secret="0123456789abcdef"):
    return OperatorControl("project-a", secret, now=now, ttl_seconds=30)


def test_retry_requires_plan_current_state_and_records_one_idempotent_receipt(control_store):
    store, task_id = control_store
    control = _control()
    intent = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "retry-1"},
    )

    receipt = control.confirm(store, intent.token)
    replay = control.confirm(store, intent.token)

    assert receipt["status"] == "ACCEPTED" and receipt["replayed"] is False
    assert replay["replayed"] is True
    assert store.get_task(task_id)["status"] == TaskStatus.OPEN
    assert store.get_task(task_id)["stage"] == Stage.PLAN
    retry_rows = store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='task.operator_retry_requested'"
    ).fetchone()[0]
    receipt_rows = store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (CONTROL_RECEIPT_EVENT,)
    ).fetchone()[0]
    assert retry_rows == receipt_rows == 1


def test_stale_task_state_refuses_confirmation_without_mutation(control_store):
    store, task_id = control_store
    control = _control()
    intent = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "retry-stale"},
    )
    store.add_candidate(task_id, "a" * 40, "provider", True)

    with pytest.raises(OperatorControlError, match="state changed") as error:
        control.confirm(store, intent.token)

    assert error.value.code == "stale_intent" and error.value.status == 409
    assert store.get_task(task_id)["status"] == TaskStatus.BLOCKED
    assert control.receipt(store, "retry-stale") is None


def test_signed_intent_is_project_secret_and_expiry_bound(control_store):
    store, task_id = control_store
    clock = [100.0]
    control = _control(now=lambda: clock[0])
    intent = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "retry-expiry"},
    )

    with pytest.raises(OperatorControlError, match="signature"):
        _control(secret="fedcba9876543210").confirm(store, intent.token)
    with pytest.raises(OperatorControlError, match="identity"):
        OperatorControl("project-b", "0123456789abcdef", now=lambda: clock[0]).confirm(
            store, intent.token
        )
    clock[0] = 131.0
    with pytest.raises(OperatorControlError, match="expired") as expired:
        control.confirm(store, intent.token)
    assert expired.value.code == "expired_intent"


def test_idempotency_key_cannot_be_rebound_to_another_intent(control_store):
    store, task_id = control_store
    control = _control()
    first = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "same-key"},
    )
    control.confirm(store, first.token)
    store.block_task(task_id)
    second = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "same-key"},
    )

    with pytest.raises(OperatorControlError, match="different intent") as conflict:
        control.confirm(store, second.token)
    assert conflict.value.code == "idempotency_conflict"


def test_concurrent_confirmation_executes_exactly_once(control_store):
    store, task_id = control_store
    control = _control()
    intent = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "concurrent"},
    )

    def confirm() -> dict:
        worker_store = Store(store.db_path)
        try:
            return control.confirm(worker_store, intent.token)
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _index: confirm(), range(2)))

    assert sorted(receipt["replayed"] for receipt in receipts) == [False, True]
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (CONTROL_RECEIPT_EVENT,)
    ).fetchone()[0] == 1


def test_active_claim_refuses_retry_without_releasing_ownership(control_store):
    store, task_id = control_store
    claim_id = str(uuid.uuid4())
    now = time.time()
    store.conn.execute(
        "INSERT INTO claims (id,task_id,worker_id,stage,lease_expires_at,active,created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (claim_id, task_id, "worker", "PLAN", now + 300, now),
    )
    store.conn.commit()

    with pytest.raises(OperatorControlError) as error:
        _control().plan(
            store,
            {"action": "task_retry", "task_id": task_id, "idempotency_key": "owned"},
        )

    assert error.value.code == "task_owned"
    assert store.has_active_claim(task_id) is True


def test_receipt_survives_control_restart_and_excludes_task_secrets(control_store):
    store, task_id = control_store
    store.conn.execute(
        "UPDATE tasks SET title=? WHERE id=?", ("secret-value C:\\private\\repo", task_id)
    )
    store.conn.commit()
    control = _control()
    intent = control.plan(
        store,
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "durable"},
    )
    receipt = control.confirm(store, intent.token)

    restarted = _control(secret="different-secret-value")
    durable = restarted.receipt(store, "durable")

    assert durable == receipt
    serialized = json.dumps(durable)
    assert "secret-value" not in serialized and "private" not in serialized


def test_queue_pause_and_resume_are_typed_non_terminating_events(control_store):
    store, _task_id = control_store
    control = _control()
    pause = control.plan(
        store, {"action": "queue_pause", "idempotency_key": "pause-1"}
    )
    assert control.confirm(store, pause.token)["result"] == {"queue_state": "paused"}
    assert latest_queue_control(store)["state"] == "paused"
    resume = control.plan(
        store, {"action": "queue_resume", "idempotency_key": "resume-1"}
    )
    assert control.confirm(store, resume.token)["result"] == {"queue_state": "resumed"}

    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY rowid", (QUEUE_CONTROL_EVENT,)
    ).fetchall()
    events = [json.loads(row["payload"]) for row in rows]
    assert [event["state"] for event in events] == ["paused", "resumed"]
    assert all(event["source"] == "operator-control" for event in events)
    assert all(event["terminate_running"] is False for event in events)


def test_unknown_execution_release_is_exact_idempotent_and_redacted(control_store):
    store, task_id = control_store
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        actor="codex",
    )
    reason = "checked Task Manager and confirmed the recorded worker is gone"
    control = _control()

    intent = control.plan(
        store,
        {
            "action": "release_unknown_execution",
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": reason,
            "idempotency_key": "release-unknown-1",
        },
    )
    receipt = control.confirm(store, intent.token)
    replay = control.confirm(store, intent.token)

    execution = store.conn.execute(
        "SELECT status,result FROM executions WHERE id=?", (execution_id,)
    ).fetchone()
    assert execution["status"] == ExecutionStatus.FAILED
    assert receipt["result"] == {
        "task_id": task_id,
        "execution_id": execution_id,
        "execution_kind": ExecutionKind.IMPLEMENTATION,
        "process_state": "UNKNOWN",
        "outcome": "RELEASED_BY_OPERATOR",
    }
    assert receipt["reason"] == reason and replay["replayed"] is True
    serialized = json.dumps(receipt)
    assert "pid" not in serialized and "executable" not in serialized
    assert store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (CONTROL_RECEIPT_EVENT,)
    ).fetchone()[0] == 1


def test_unknown_implementation_release_restores_claimed_task_to_runnable(control_store):
    store, _blocked_task = control_store
    task_id = store.upsert_task("claimed task", source_id="claimed-control-task")
    claim_id = store.acquire_claim(task_id, "worker")
    assert claim_id is not None
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=claim_id,
        kind=ExecutionKind.IMPLEMENTATION,
        actor="codex",
    )
    intent = _control().plan(
        store,
        {
            "action": "release_unknown_execution",
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": "confirmed the interrupted worker process is no longer present",
            "idempotency_key": "release-claim",
        },
    )

    _control().confirm(store, intent.token)

    assert store.has_active_claim(task_id) is False
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.OPEN


def test_unknown_execution_release_refuses_live_cross_task_and_changed_targets(control_store):
    store, task_id = control_store
    other_task = store.upsert_task("other task", source_id="other-control-task")
    unknown = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
    )
    live = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        **owner_execution_fields(),
    )
    control = _control()
    base = {
        "action": "release_unknown_execution",
        "task_id": task_id,
        "reason": "verified outside StageMesh that no worker remains",
    }

    with pytest.raises(OperatorControlError) as live_error:
        control.plan(store, {**base, "execution_id": live, "idempotency_key": "live"})
    assert live_error.value.code == "execution_live"
    with pytest.raises(OperatorControlError) as mismatch:
        control.plan(
            store,
            {
                **base,
                "task_id": other_task,
                "execution_id": unknown,
                "idempotency_key": "mismatch",
            },
        )
    assert mismatch.value.code == "execution_task_mismatch"

    intent = control.plan(store, {**base, "execution_id": unknown, "idempotency_key": "changed"})
    store.finish_execution(unknown, ExecutionStatus.FAILED, result="changed after plan")
    with pytest.raises(OperatorControlError) as changed:
        control.confirm(store, intent.token)
    assert changed.value.code == "execution_not_running"
    assert store.conn.execute(
        "SELECT status FROM executions WHERE id=?", (live,)
    ).fetchone()["status"] == ExecutionStatus.RUNNING


def test_unknown_execution_release_does_not_echo_raced_process_details(
    control_store, monkeypatch
):
    store, task_id = control_store
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
    )
    control = _control()
    intent = control.plan(
        store,
        {
            "action": "release_unknown_execution",
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": "confirmed the old worker is gone",
            "idempotency_key": "release-race",
        },
    )

    def raced(*_args, **_kwargs):
        raise operator_control_module.OperatorActionError(
            r"LIVE pid 1234 C:\private\provider.exe token=secret"
        )

    monkeypatch.setattr(operator_control_module, "release_unknown_execution", raced)
    with pytest.raises(OperatorControlError) as refused:
        control.confirm(store, intent.token)

    assert refused.value.code == "action_refused"
    assert "1234" not in str(refused.value)
    assert "private" not in str(refused.value)
    assert "secret" not in str(refused.value)


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"action": "shell", "idempotency_key": "x"}, "action_not_allowed"),
        ({"action": "queue_pause", "task_id": "x", "idempotency_key": "x"}, "unexpected_target"),
        ({"action": "queue_pause", "idempotency_key": "bad key"}, "invalid_idempotency_key"),
        ({"action": "queue_pause", "idempotency_key": "x", "command": "rm"}, "invalid_request"),
        (
            {
                "action": "release_unknown_execution",
                "task_id": "x",
                "execution_id": "x",
                "reason": "",
                "idempotency_key": "x",
            },
            "invalid_reason",
        ),
    ],
)
def test_control_request_allowlist_fails_closed(control_store, payload, code):
    store, _task_id = control_store
    with pytest.raises(OperatorControlError) as error:
        _control().plan(store, payload)
    assert error.value.code == code


def test_objective_root_and_recorded_source_refusal_cannot_be_bypassed(control_store):
    store, _task_id = control_store
    root_id = store.upsert_task("objective root", source="github", source_id="700")
    store.block_task(root_id)
    store.cache_source(
        "github",
        "700",
        {"state": "OPEN", "eligible": True, "labels": ["stagemesh:objective"]},
        "ok",
    )

    with pytest.raises(OperatorControlError) as error:
        _control().plan(
            store,
            {"action": "task_retry", "task_id": root_id, "idempotency_key": "root"},
        )
    assert error.value.code == "objective_root"

    source_id = store.upsert_task("closed source", source="github", source_id="701")
    store.block_task(source_id)
    store.cache_source(
        "github", "701", {"state": "CLOSED", "eligible": False, "labels": []}, "ok"
    )
    with pytest.raises(OperatorControlError) as source_error:
        _control().plan(
            store,
            {"action": "task_retry", "task_id": source_id, "idempotency_key": "closed"},
        )
    assert source_error.value.code == "source_ineligible"


def test_receipt_lookup_skips_malformed_rows(control_store):
    store, _task_id = control_store
    record_audit(store, CONTROL_RECEIPT_EVENT, {"idempotency_key": "valid", "status": "ACCEPTED"})
    for index in range(205):
        record_audit(
            store,
            CONTROL_RECEIPT_EVENT,
            {"idempotency_key": f"other-{index}", "status": "ACCEPTED"},
        )
    store.conn.execute(
        "INSERT INTO audit_events VALUES ('bad', ?, 'not-json', 999)", (CONTROL_RECEIPT_EVENT,)
    )
    store.conn.commit()

    assert _control().receipt(store, "valid")["status"] == "ACCEPTED"
