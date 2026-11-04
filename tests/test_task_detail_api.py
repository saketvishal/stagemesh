from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import TASK_DETAIL_CONTRACT, ActivityApi
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.persistence import Store

TOKEN = "task-detail-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "detail.sqlite3"
    store = Store(path)
    store.migrate()
    dependency = store.upsert_task("dependency", source_id="dependency")
    task = store.upsert_task("detail token=private", source="github", source_id="issue-1", project="team/alpha")
    store.add_dependency(task, dependency)
    store.add_candidate(task, "a" * 40, "codex", True)
    store.add_evidence(
        task,
        "a" * 40,
        EvidenceKind.VALIDATION,
        EvidenceStatus.PASSED,
        {"command": "pytest token=private", "url": "https://secret.invalid"},
    )
    store.add_audit_event(
        "task.operator_retry_requested",
        {"task_id": task, "stage": "PLAN", "candidate_sha": "a" * 40},
    )
    store.block_task(task)
    record_block(store, task, BlockKind.HUMAN_DECISION, "choose behavior token=private")
    store.close()
    return path, task


def request(db: Path, target: str, *, method: str = "GET", headers=None):
    api = ActivityApi(db, TOKEN)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS)
    return status, json.loads(payload), response_headers


def test_task_detail_is_read_only_bounded_and_keeps_current_facts_separate(db) -> None:
    path, task = db
    before = path.read_bytes()
    status, body, headers = request(path, f"/api/v1/task?task_id={task}")

    assert status == 200 and body["contract"] == TASK_DETAIL_CONTRACT
    assert set(body) == {
        "contract", "observed_at", "task", "source", "candidate", "dependencies",
        "pending_decisions", "timeline", "evidence", "retries", "history",
    }
    assert body["task"]["id"] == task and body["task"]["status"] == "BLOCKED"
    assert body["source"] == {"kind": "github", "id": "issue-1", "status": None}
    assert body["candidate"]["sha"] == "a" * 40 and body["candidate"]["durable_handoff"] is True
    assert len(body["dependencies"]) == 1 and body["dependencies"][0]["status"] == "OPEN"
    assert body["pending_decisions"][0]["kind"] == "human_decision"
    assert body["pending_decisions"][-1]["status"] == "UNKNOWN"
    assert body["history"]["limit"] == 200 and body["history"]["recorded_creation"] is True
    assert headers["Cache-Control"] == "no-store" and path.read_bytes() == before


def test_task_detail_projects_typed_evidence_and_retry_without_payloads(db) -> None:
    path, task = db
    status, body, _ = request(path, f"/api/v1/task?task_id={task}")
    encoded = json.dumps(body)

    assert status == 200
    evidence, = body["evidence"]
    assert evidence["evidence_kind"] == "VALIDATION" and evidence["evidence_status"] == "PASSED"
    retry, = body["retries"]
    assert retry["retry_kind"] == "operator" and retry["retry_state"] == "QUEUED"
    assert "secret.invalid" not in encoded and "pytest" not in encoded and "private" not in encoded
    assert all("payload" not in entry and "command" not in entry for entry in body["timeline"])


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/task", "GET", HEADERS, 400),
        ("/api/v1/task?task_id=", "GET", HEADERS, 400),
        ("/api/v1/task?task_id=x&task_id=y", "GET", HEADERS, 400),
        ("/api/v1/task?task=x", "GET", HEADERS, 400),
        ("/api/v1/task?task_id=missing", "GET", HEADERS, 404),
        ("/api/v1/task?task_id=x", "POST", HEADERS, 405),
        ("/api/v1/task?task_id=x", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/task?task_id=x", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_task_detail_fails_closed(db, target: str, method: str, headers, expected: int) -> None:
    path, _task = db
    status, body, _ = request(path, target, method=method, headers=headers)
    assert status == expected and body["contract"] == TASK_DETAIL_CONTRACT
    assert set(body["error"]) == {"code", "message"}
