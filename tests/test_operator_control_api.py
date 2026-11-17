from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi, make_server
from stagemesh.domain import ExecutionKind, ExecutionStatus, TaskStatus
from stagemesh.operator_control import CONTROL_CONTRACT, OperatorControl
from stagemesh.persistence import Store

TOKEN = "operator-api-token"


@pytest.fixture
def control_db(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.migrate()
    task_id = store.upsert_task("blocked task", source_id="control-api-task")
    store.block_task(task_id)
    store.close()
    return path, task_id


def _api(path: Path, *, enabled: bool = True) -> tuple[ActivityApi, OperatorControl]:
    control = OperatorControl("project-a", "0123456789abcdef", now=lambda: 100.0)
    api = ActivityApi(path, TOKEN, control=control if enabled else None)
    api.bind(12345)
    return api, control


def _headers(control: OperatorControl, body: bytes = b"", **changes: str) -> dict[str, str]:
    headers = {
        "Host": "127.0.0.1:12345",
        "Origin": "http://127.0.0.1:12345",
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "X-Stagemesh-Control": control.csrf_token,
    }
    headers.update(changes)
    return headers


def _call(
    api: ActivityApi,
    method: str,
    target: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict]:
    status, raw, _extra = api.handle(method, target, headers, body)
    return status, json.loads(raw)


def test_control_is_absent_by_default_and_existing_api_stays_read_only(control_db):
    path, _task_id = control_db
    api, control = _api(path, enabled=False)
    before = path.read_bytes()
    body = b'{"action":"queue_pause","idempotency_key":"pause"}'

    status, payload = _call(
        api, "POST", "/api/v1/control/plan", _headers(control, body), body
    )

    assert status == 404 and payload["error"]["code"] == "not_found"
    assert payload["contract"] == CONTROL_CONTRACT
    assert path.read_bytes() == before


def test_capabilities_require_bearer_and_expose_only_bounded_control_contract(control_db):
    path, _task_id = control_db
    api, control = _api(path)
    headers = _headers(control)

    status, payload = _call(api, "GET", "/api/v1/control", headers)
    assert status == 200 and payload["contract"] == CONTROL_CONTRACT
    assert payload["enabled"] is True and payload["project_id"] == "project-a"
    assert payload["actions"] == [
        "queue_pause",
        "queue_resume",
        "release_unknown_execution",
        "task_retry",
    ]
    assert payload["csrf_token"] == control.csrf_token

    headers["Authorization"] = "Bearer wrong"
    assert _call(api, "GET", "/api/v1/control", headers)[0] == 401


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"Origin": ""}, 403),
        ({"Origin": "http://example.test"}, 403),
        ({"X-Stagemesh-Control": "wrong"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Length": "bad"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_plan_fails_closed_on_request_security_boundaries(control_db, change, expected):
    path, _task_id = control_db
    api, control = _api(path)
    body = b'{"action":"queue_pause","idempotency_key":"pause"}'
    headers = _headers(control, body, **change)

    assert _call(api, "POST", "/api/v1/control/plan", headers, body)[0] == expected
    store = Store(path)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='queue.control'"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_duplicate_json_fields_are_rejected_without_action(control_db):
    path, _task_id = control_db
    api, control = _api(path)
    body = b'{"action":"queue_pause","action":"queue_resume","idempotency_key":"duplicate"}'

    status, payload = _call(
        api, "POST", "/api/v1/control/plan", _headers(control, body), body
    )

    assert status == 400 and payload["error"]["code"] == "invalid_request"


def test_plan_confirm_replay_and_receipt_are_current_project_bound(control_db):
    path, task_id = control_db
    api, control = _api(path)
    request = json.dumps(
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "retry-api"}
    ).encode()
    status, planned = _call(
        api, "POST", "/api/v1/control/plan", _headers(control, request), request
    )
    assert status == 200 and planned["action"] == "task_retry"

    confirmation = json.dumps({"intent": planned["intent"]}).encode()
    status, receipt = _call(
        api,
        "POST",
        "/api/v1/control/confirm",
        _headers(control, confirmation),
        confirmation,
    )
    replay_status, replay = _call(
        api,
        "POST",
        "/api/v1/control/confirm",
        _headers(control, confirmation),
        confirmation,
    )
    lookup_status, lookup = _call(
        api,
        "GET",
        "/api/v1/control/receipt?idempotency_key=retry-api",
        _headers(control),
    )

    assert status == replay_status == lookup_status == 200
    assert receipt["status"] == "ACCEPTED" and receipt["replayed"] is False
    assert replay["replayed"] is True and lookup["idempotency_key"] == "retry-api"
    store = Store(path)
    try:
        assert store.get_task(task_id)["status"] == TaskStatus.OPEN
        assert store.conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='operator.control.receipt'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_unknown_execution_release_api_is_exact_and_durable(control_db):
    path, task_id = control_db
    store = Store(path)
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        actor="codex",
    )
    store.close()
    api, control = _api(path)
    request = json.dumps(
        {
            "action": "release_unknown_execution",
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": "checked the operating-system process list; worker is absent",
            "idempotency_key": "release-api",
        }
    ).encode()

    status, planned = _call(
        api, "POST", "/api/v1/control/plan", _headers(control, request), request
    )
    confirmation = json.dumps({"intent": planned["intent"]}).encode()
    confirm_status, receipt = _call(
        api,
        "POST",
        "/api/v1/control/confirm",
        _headers(control, confirmation),
        confirmation,
    )
    lookup_status, lookup = _call(
        api,
        "GET",
        "/api/v1/control/receipt?idempotency_key=release-api",
        _headers(control),
    )

    assert status == confirm_status == lookup_status == 200
    assert planned["target"] == f"execution:{execution_id}"
    assert receipt == lookup and receipt["result"]["outcome"] == "RELEASED_BY_OPERATOR"
    reopened = Store(path)
    try:
        row = reopened.conn.execute(
            "SELECT status FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
        assert row["status"] == ExecutionStatus.FAILED
    finally:
        reopened.close()


def test_real_http_transport_reads_only_bounded_control_bodies(control_db):
    path, task_id = control_db
    control = OperatorControl("project-a", "0123456789abcdef", now=lambda: 100.0)
    httpd, _api_instance = make_server(path, token=TOKEN, control=control)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    body = json.dumps(
        {"action": "task_retry", "task_id": task_id, "idempotency_key": "http-retry"}
    ).encode()
    request = urllib.request.Request(
        base + "/api/v1/control/plan",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": base,
            "Content-Type": "application/json",
            "X-Stagemesh-Control": control.csrf_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["contract"] == CONTROL_CONTRACT and payload["action"] == "task_retry"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_make_server_preserves_legacy_api_class_constructor(control_db):
    path, _task_id = control_db
    control = OperatorControl("project-a", "0123456789abcdef")

    class LegacyApi(ActivityApi):
        def __init__(self, db_path, token, static_dir=None):
            super().__init__(db_path, token, static_dir)

    httpd, api = make_server(path, token=TOKEN, api_class=LegacyApi, control=control)
    try:
        assert isinstance(api, LegacyApi)
        assert api.control is control
        assert api.launch_tickets is not None
    finally:
        httpd.server_close()
