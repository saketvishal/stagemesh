from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import OBJECTIVES_CONTRACT, OPERATOR_CONTRACT, ActivityApi
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.efficiency import Usage, record_provider_call
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


def test_operator_snapshot_projects_only_validated_provider_reported_cost(db: Path) -> None:
    store = Store(db)
    task = store.upsert_task("operator task", source_id="operator-task")
    record_provider_call(
        store,
        task_id=task,
        purpose="implementation",
        provider="fixture",
        outcome="succeeded",
        duration_seconds=1,
        usage=Usage(requests=1, cost_amount="1.250", cost_currency="USD"),
    )
    store.close()

    status, body, _ = request(db)

    assert status == 200
    efficiency, = body["sections"]["efficiency"]
    assert efficiency["billing_cost"] == "1.250 USD"
    assert efficiency["billing_cost_comparable"] is True
    assert efficiency["usage"]["requests"] == 1
    assert efficiency["usage"]["costs"] == [{"currency": "USD", "amount": "1.250"}]


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
    assert active["process_state"] == "UNKNOWN"
    assert active["release_unknown_allowed"] is True
    assert active["started_at"] > 0 and active["updated_at"] >= active["started_at"]
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


def test_objectives_snapshot_correlates_live_children_without_projecting_raw_payload(db: Path) -> None:
    secret = "PRIVATE-OBJECTIVE-BODY-CANARY"
    store = Store(db)
    store.save_objective(
        "objective-1",
        "Ship bounded objective view",
        {
            "id": "objective-1",
            "title": "Ship bounded objective view",
            "body": secret,
            "prompt": secret,
            "tasks": [
                {"id": "child-done", "title": "Done child"},
                {"id": "child-blocked", "title": "Blocked child", "dependencies": ["child-done"]},
                {"id": "child-pending", "title": "Not materialized"},
            ],
        },
    )
    store.upsert_task("Done child from store", source_id="child-done")
    store.upsert_task("Blocked child from store", source_id="child-blocked")
    store.conn.execute("UPDATE tasks SET stage='DONE',status='DONE' WHERE id='child-done'")
    store.conn.execute("UPDATE tasks SET stage='PLAN',status='BLOCKED' WHERE id='child-blocked'")
    store.conn.commit()
    store.close()
    before = db.read_bytes()

    status, body, headers = request(db, "/api/v1/objectives")

    assert status == 200 and body["contract"] == OBJECTIVES_CONTRACT
    assert set(body) == {
        "contract", "observed_at", "objectives", "total", "bounded", "detail_limit",
        "child_limit", "truncated",
    }
    assert body["bounded"] is True and body["detail_limit"] == body["child_limit"] == 100
    objective, = body["objectives"]
    assert set(objective) == {
        "id", "title", "created_at", "updated_at", "plan_state", "attention_required",
        "counts", "children", "children_truncated",
    }
    assert objective["plan_state"] == "BLOCKED" and objective["attention_required"] is True
    assert objective["counts"] == {
        "total": 3, "recorded": 2, "not_recorded": 1, "OPEN": 0, "CLAIMED": 0,
        "BLOCKED": 1, "DONE": 1, "UNKNOWN": 0,
    }
    assert objective["children"][0]["title"] == "Done child from store"
    assert objective["children"][1]["dependencies"] == ["child-done"]
    assert objective["children"][2]["recorded"] is False
    encoded = json.dumps(body)
    assert secret not in encoded and "body" not in encoded and "prompt" not in encoded
    assert headers["Cache-Control"] == "no-store" and db.read_bytes() == before


def test_objectives_snapshot_distinguishes_unplanned_and_unavailable_records(db: Path) -> None:
    secret = "PRIVATE-SOURCE-ISSUE-CANARY"
    store = Store(db)
    store.save_objective(
        "135",
        "Source objective",
        {"source": "github", "source_id": "135", "body": secret, "dependencies": []},
    )
    store.conn.execute(
        "INSERT INTO objectives(id,title,payload,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("damaged", "Damaged objective", '{"tasks":"not-a-list","private":"' + secret + '"}', 1, 2),
    )
    store.conn.commit()
    store.close()

    status, body, _ = request(db, "/api/v1/objectives")

    assert status == 200
    by_id = {row["id"]: row for row in body["objectives"]}
    assert by_id["135"]["plan_state"] == "UNPLANNED"
    assert by_id["damaged"]["plan_state"] == "UNAVAILABLE"
    assert by_id["135"]["children"] == by_id["damaged"]["children"] == []
    assert secret not in json.dumps(body)


def test_objectives_snapshot_classifies_ready_partial_in_progress_and_complete(db: Path) -> None:
    store = Store(db)
    plans = {
        "ready": [{"id": "ready-child", "title": "Ready"}],
        "partial": [
            {"id": "partial-recorded", "title": "Recorded"},
            {"id": "partial-missing", "title": "Missing"},
        ],
        "progress": [{"id": "progress-child", "title": "Progress"}],
        "complete": [{"id": "complete-child", "title": "Complete"}],
        "inconsistent": [{"id": "inconsistent-child", "title": "Inconsistent"}],
    }
    for objective_id, tasks in plans.items():
        store.save_objective(
            objective_id,
            objective_id.title(),
            {"id": objective_id, "title": objective_id.title(), "tasks": tasks},
        )
    for task_id in (
        "ready-child", "partial-recorded", "progress-child", "complete-child",
        "inconsistent-child",
    ):
        store.upsert_task(task_id, source_id=task_id)
    store.conn.execute("UPDATE tasks SET stage='IMPLEMENT',status='OPEN' WHERE id='progress-child'")
    store.conn.execute("UPDATE tasks SET stage='DONE',status='DONE' WHERE id='complete-child'")
    store.conn.execute("UPDATE tasks SET stage='PLAN',status='DONE' WHERE id='inconsistent-child'")
    store.conn.commit()
    store.close()

    status, body, _ = request(db, "/api/v1/objectives")

    assert status == 200
    states = {row["id"]: row["plan_state"] for row in body["objectives"]}
    assert states == {
        "ready": "READY", "partial": "PARTIAL", "progress": "IN_PROGRESS",
        "complete": "COMPLETE", "inconsistent": "IN_PROGRESS",
    }


def test_objectives_snapshot_counts_all_children_while_bounding_display_rows(db: Path) -> None:
    store = Store(db)
    tasks = [{"id": f"child-{index:03d}", "title": f"Child {index}"} for index in range(105)]
    store.save_objective(
        "large-plan",
        "Large bounded plan",
        {"id": "large-plan", "title": "Large bounded plan", "tasks": tasks},
    )
    store.close()

    status, body, _ = request(db, "/api/v1/objectives")

    assert status == 200
    objective, = body["objectives"]
    assert objective["plan_state"] == "PARTIAL"
    assert objective["counts"]["total"] == objective["counts"]["not_recorded"] == 105
    assert len(objective["children"]) == 100 and objective["children_truncated"] is True


def test_objectives_snapshot_caps_records_and_reports_truncation(db: Path) -> None:
    store = Store(db)
    store.conn.executemany(
        "INSERT INTO objectives(id,title,payload,created_at,updated_at) VALUES (?,?,?,?,?)",
        [
            (f"objective-{index:03d}", f"Objective {index}", '{"source":"github"}', index, index)
            for index in range(105)
        ],
    )
    store.conn.commit()
    store.close()

    status, body, _ = request(db, "/api/v1/objectives")

    assert status == 200 and body["total"] == 105
    assert len(body["objectives"]) == 100 and body["truncated"] is True
    assert body["objectives"][0]["id"] == "objective-104"


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/objectives?limit=1", "GET", HEADERS, 400),
        ("/api/v1/objectives", "POST", HEADERS, 405),
        ("/api/v1/objectives", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/objectives", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_objectives_snapshot_fails_closed(db: Path, target: str, method: str, headers, expected: int) -> None:
    status, body, _ = request(db, target, method=method, headers=headers)
    assert status == expected
    assert body["contract"] == OBJECTIVES_CONTRACT
    assert set(body["error"]) == {"code", "message"}
