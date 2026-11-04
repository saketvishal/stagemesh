from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import EXECUTION_DETAIL_CONTRACT, ActivityApi
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.persistence import Store

TOKEN = "execution-detail-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "execution.sqlite3"
    store = Store(path)
    store.migrate()
    task = store.upsert_task("execution token=private", source_id="execution-task", project="team/alpha")
    candidate = "a" * 40
    store.add_candidate(task, candidate, "codex", True)
    claim = store.acquire_claim(task, "worker-history")
    assert claim is not None
    failed = store.start_execution(
        task_id=task,
        claim_id=claim,
        kind=ExecutionKind.VALIDATION,
        candidate_sha=candidate,
        actor="codex",
    )
    store.finish_execution(failed, ExecutionStatus.FAILED, result="provider_timeout")
    store.release_claim(claim)
    store.add_evidence(
        task,
        candidate,
        EvidenceKind.VALIDATION,
        EvidenceStatus.FAILED,
        {"command": "pytest token=private", "url": "https://secret.invalid"},
    )
    active_claim = store.acquire_claim(task, "worker-current")
    assert active_claim is not None
    running = store.start_execution(
        task_id=task,
        claim_id=active_claim,
        kind=ExecutionKind.IMPLEMENTATION,
        actor="token-secret-provider",
    )
    store.close()
    return path, {"task": task, "failed": failed, "running": running}


def request(db: Path, target: str, *, method: str = "GET", headers=None):
    api = ActivityApi(db, TOKEN)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS)
    return status, json.loads(payload), response_headers


def test_execution_detail_is_read_only_and_keeps_lifecycle_ownership_and_evidence_distinct(db) -> None:
    path, ids = db
    before = path.read_bytes()
    status, body, headers = request(path, f"/api/v1/execution?execution_id={ids['failed']}")

    assert status == 200 and body["contract"] == EXECUTION_DETAIL_CONTRACT
    assert set(body) == {
        "contract", "observed_at", "execution", "task", "candidate", "timing", "outcome",
        "claim", "runtime", "evidence", "evidence_meta",
    }
    assert body["execution"]["kind"] == "VALIDATION" and body["execution"]["status"] == "FAILED"
    assert body["execution"]["current_for_task"] is False
    assert body["candidate"] == {"sha": "a" * 40, "current": True}
    assert body["timing"]["terminal_at"] is not None
    assert body["timing"]["recorded_duration_seconds"] >= 0
    assert body["timing"]["duration_kind"] == "recorded_terminal"
    assert body["outcome"] == {
        "code": "provider_timeout",
        "classification": "provider_availability",
        "raw_result_available": True,
        "scope": "history",
    }
    assert body["claim"]["active"] is False and body["claim"]["worker_id"] == "worker-history"
    assert body["runtime"]["current"] is False and body["runtime"]["process_state"] == "NOT_CURRENT"
    assert body["evidence"] == [
        {
            "id": body["evidence"][0]["id"],
            "kind": "VALIDATION",
            "status": "FAILED",
            "recorded_at": body["evidence"][0]["recorded_at"],
            "current_candidate": True,
        }
    ]
    assert body["evidence_meta"] == {
        "limit": 100,
        "truncated": False,
        "correlation": "exact_task_and_candidate",
    }
    assert headers["Cache-Control"] == "no-store" and path.read_bytes() == before


def test_running_execution_does_not_invent_terminal_time_duration_or_provider_identity(db) -> None:
    path, ids = db
    status, body, _ = request(path, f"/api/v1/execution?execution_id={ids['running']}")
    encoded = json.dumps(body)

    assert status == 200 and body["execution"]["status"] == "RUNNING"
    assert body["execution"]["current_for_task"] is True
    assert body["execution"]["provider"] is None
    assert body["timing"]["terminal_at"] is None
    assert body["timing"]["recorded_duration_seconds"] is None
    assert body["timing"]["duration_kind"] == "unavailable"
    assert body["claim"]["active"] is True and body["claim"]["worker_id"] == "worker-current"
    assert body["runtime"]["current"] is True
    assert body["runtime"]["identity_coverage"] == "UNAVAILABLE"
    assert body["evidence"] == []
    assert body["evidence_meta"]["correlation"] == "unavailable_without_candidate"
    assert "token-secret" not in encoded and "secret.invalid" not in encoded and "pytest" not in encoded


def test_unknown_result_text_is_not_serialized(db) -> None:
    path, ids = db
    store = Store(path)
    store.conn.execute(
        "UPDATE executions SET result=? WHERE id=?",
        ("<script>token=private https://secret.invalid</script>", ids["failed"]),
    )
    store.conn.commit()
    store.close()

    status, body, _ = request(path, f"/api/v1/execution?execution_id={ids['failed']}")
    encoded = json.dumps(body)
    assert status == 200
    assert body["outcome"]["code"] is None and body["outcome"]["classification"] == "unavailable"
    assert body["outcome"]["raw_result_available"] is True
    assert "script" not in encoded and "private" not in encoded and "secret.invalid" not in encoded


def test_malformed_legacy_timing_becomes_unavailable(db) -> None:
    path, ids = db
    store = Store(path)
    store.conn.execute(
        "UPDATE executions SET started_at=?,updated_at=? WHERE id=?",
        ("legacy-invalid", float("inf"), ids["failed"]),
    )
    store.conn.commit()
    store.close()

    status, body, _ = request(path, f"/api/v1/execution?execution_id={ids['failed']}")
    assert status == 200
    assert body["timing"] == {
        "started_at": None,
        "updated_at": None,
        "terminal_at": None,
        "recorded_duration_seconds": None,
        "duration_kind": "unavailable",
    }


def test_exact_candidate_evidence_is_capped_and_reports_truncation(db) -> None:
    path, ids = db
    store = Store(path)
    for index in range(101):
        store.add_evidence(
            ids["task"],
            "a" * 40,
            EvidenceKind.REVIEW,
            EvidenceStatus.PASSED,
            {"private": f"token={index}"},
        )
    store.close()

    status, body, _ = request(path, f"/api/v1/execution?execution_id={ids['failed']}")
    assert status == 200 and len(body["evidence"]) == 100
    assert body["evidence_meta"]["truncated"] is True and body["evidence_meta"]["limit"] == 100


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/execution", "GET", HEADERS, 400),
        ("/api/v1/execution?execution_id=", "GET", HEADERS, 400),
        ("/api/v1/execution?execution_id=x&execution_id=y", "GET", HEADERS, 400),
        ("/api/v1/execution?execution=x", "GET", HEADERS, 400),
        ("/api/v1/execution?execution_id=missing", "GET", HEADERS, 404),
        ("/api/v1/execution?execution_id=x", "POST", HEADERS, 405),
        ("/api/v1/execution?execution_id=x", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/execution?execution_id=x", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_execution_detail_fails_closed(db, target: str, method: str, headers, expected: int) -> None:
    path, _ids = db
    status, body, _ = request(path, target, method=method, headers=headers)
    assert status == expected and body["contract"] == EXECUTION_DETAIL_CONTRACT
    assert set(body["error"]) == {"code", "message"}
