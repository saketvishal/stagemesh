from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import OPERATOR_CONTRACT, ActivityApi
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.persistence import Store

TOKEN = "operator-api-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "operator.sqlite3"
    store = Store(path)
    store.migrate()
    store.close()
    return path


def request(db: Path, target: str = "/api/v1/operator", *, method: str = "GET", headers=None):
    api = ActivityApi(db, TOKEN)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS)
    return status, json.loads(payload), response_headers


def test_operator_snapshot_is_bounded_read_only_and_uses_an_independent_contract(db: Path) -> None:
    before = db.read_bytes()
    status, body, headers = request(db)

    assert status == 200
    assert body["contract"] == OPERATOR_CONTRACT
    assert set(body) == {
        "contract", "observed_at", "summary", "metrics", "sections", "bounded", "detail_limit"
    }
    assert set(body["sections"]) == {
        "attention", "pending_decisions", "active_executions", "workers", "recent_failures",
        "efficiency", "provider_selections",
    }
    assert body["bounded"] is True and body["detail_limit"] == 100
    assert body["sections"]["pending_decisions"][-1]["status"] == "UNKNOWN"
    assert headers["Cache-Control"] == "no-store"
    assert db.read_bytes() == before


def test_operator_snapshot_projects_current_status_without_raw_process_or_payload_data(db: Path) -> None:
    secret = "very-secret-value"
    store = Store(db)
    task = store.upsert_task("operator task", source_id="operator-task")
    running = store.start_execution(
        task_id=task,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        pid=None,
        actor="codex",
    )
    failed = store.start_execution(
        task_id=task,
        claim_id=None,
        kind=ExecutionKind.REVIEW,
        actor="claude",
    )
    store.finish_execution(
        failed,
        ExecutionStatus.FAILED,
        actor="claude",
        result=f"token={secret} https://user:{secret}@example.invalid/private",
    )
    store.upsert_worker(
        worker_id="worker-1",
        provider="codex",
        capabilities=["IMPLEMENT"],
        pid=None,
        process_create_time=None,
        boot_id=None,
        executable=r"C:\\private\\provider.exe",
        heartbeat_at=1,
        lease_expires_at=2,
    )
    store.add_audit_event(
        "provider.selection",
        {
            "task_id": task,
            "stage": "IMPLEMENT",
            "policy": "weighted",
            "order": ["codex"],
            "verdicts": [{"provider": "codex", "eligible": True, "reason": f"token={secret}"}],
            "raw": {"command": r"C:\\private\\provider.exe", "url": "https://example.invalid/private"},
        },
    )
    store.close()

    status, body, _ = request(db)
    encoded = json.dumps(body)
    assert status == 200
    assert secret not in encoded
    assert "provider.exe" not in encoded and "example.invalid" not in encoded
    active, = body["sections"]["active_executions"]
    assert active["task_id"] == task and active["provider"] == "codex"
    assert "pid" not in active
    worker, = body["sections"]["workers"]
    assert worker == {
        "worker_id": "worker-1", "provider": "codex", "capabilities": ["IMPLEMENT"],
        "lease_expires_at": 2.0,
    }
    failure, = body["sections"]["recent_failures"]
    assert failure["scope"] == "current" and failure["provider"] == "claude"
    selection, = body["sections"]["provider_selections"]
    assert selection["order"] == ["codex"] and selection["verdicts"][0]["eligible"] is True
    assert running


def test_operator_snapshot_caps_detail_rows_without_hiding_full_counts(db: Path) -> None:
    store = Store(db)
    for index in range(105):
        store.upsert_worker(
            worker_id=f"worker-{index:03d}",
            provider="codex",
            capabilities=["IMPLEMENT"],
            pid=None,
            process_create_time=None,
            boot_id=None,
            executable=None,
            heartbeat_at=1,
            lease_expires_at=2,
        )
    store.close()

    status, body, _ = request(db)
    assert status == 200
    assert len(body["sections"]["workers"]) == 100
    assert body["metrics"]["worker_count"] == 105
    assert body["detail_limit"] == 100 and body["bounded"] is True


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/operator?limit=1", "GET", HEADERS, 400),
        ("/api/v1/operator", "POST", HEADERS, 405),
        ("/api/v1/operator", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/operator", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_operator_snapshot_fails_closed(db: Path, target: str, method: str, headers, expected: int) -> None:
    status, body, _ = request(db, target, method=method, headers=headers)
    assert status == expected
    assert body["contract"] == OPERATOR_CONTRACT
    assert set(body["error"]) == {"code", "message"}
