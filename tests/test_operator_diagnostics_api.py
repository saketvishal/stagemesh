from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from stagemesh.operator_diagnostics import DIAGNOSTICS_CONTRACT, EVIDENCE_SCAN_LIMIT
from stagemesh.persistence import Store

TOKEN = "diagnostics-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "diagnostics.sqlite3"
    store = Store(path)
    store.migrate()
    task = store.upsert_task("Diagnostics token=private-value", source_id="diagnostics", project="team/alpha")
    candidate = "a" * 40
    store.add_candidate(task, candidate, "fixture", True)
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
        {"command": "pytest token=private-value", "provider_output": "private-value"},
    )
    unknown = store.start_execution(
        task_id=task,
        claim_id=None,
        kind=ExecutionKind.REVIEW,
        candidate_sha=candidate,
        actor="claude",
    )
    store.finish_execution(unknown, ExecutionStatus.FAILED, result="token=private-value raw provider output")
    running = store.start_execution(
        task_id=task,
        claim_id=None,
        kind=ExecutionKind.IMPLEMENTATION,
        actor="token-secret-provider",
    )
    store.close()
    return path, {"task": task, "failed": failed, "unknown": unknown, "running": running}


def request(
    path: Path,
    target: str = "/api/v1/diagnostics",
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    api = ActivityApi(path, TOKEN)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS, body)
    return status, json.loads(payload), response_headers


def test_diagnostics_are_read_only_bounded_and_withhold_raw_logs_and_secrets(db) -> None:
    path, ids = db
    before = path.read_bytes()

    status, body, headers = request(path)
    encoded = json.dumps(body)

    assert status == 200 and body["contract"] == DIAGNOSTICS_CONTRACT
    assert body["execution_total"] == 3 and body["truncated"] is False
    assert body["raw_logs"] == "WITHHELD"
    assert body["database_writes"] == body["external_calls"] == 0
    assert "private-value" not in encoded and "provider output" not in encoded and "pytest" not in encoded
    assert headers["Cache-Control"] == "no-store" and path.read_bytes() == before
    by_id = {row["id"]: row for row in body["executions"]}
    failed = by_id[ids["failed"]]
    assert failed["outcome"] == {
        "code": "provider_timeout",
        "classification": "provider_availability",
        "raw_result_available": True,
        "raw_result_projected": False,
        "scope": "HISTORY",
    }
    assert failed["timing"]["duration_kind"] == "RECORDED_TERMINAL"
    assert failed["claim"]["active"] is False and failed["claim"]["worker_id"] == "worker-history"
    assert failed["runtime"]["process_state"] == "NOT_CURRENT"
    assert failed["evidence"]["counts"] == {"VALIDATION": {"FAILED": 1}}
    assert failed["evidence"]["latest"]["status"] == "FAILED"


def test_unknown_result_and_secret_shaped_provider_are_not_projected(db) -> None:
    path, ids = db

    status, body, _ = request(path)
    by_id = {row["id"]: row for row in body["executions"]}

    assert status == 200
    assert by_id[ids["unknown"]]["outcome"]["code"] is None
    assert by_id[ids["unknown"]]["outcome"]["classification"] == "UNAVAILABLE"
    assert by_id[ids["unknown"]]["outcome"]["raw_result_available"] is True
    running = by_id[ids["running"]]
    assert running["provider"] is None and running["status"] == "RUNNING"
    assert running["timing"]["terminal_at"] is None
    assert running["timing"]["recorded_duration_seconds"] is None
    assert running["timing"]["duration_kind"] == "UNAVAILABLE"
    assert running["runtime"]["current"] is True


def test_exact_candidate_evidence_is_bounded_and_payload_free(db) -> None:
    path, ids = db
    store = Store(path)
    for index in range(EVIDENCE_SCAN_LIMIT + 1):
        store.add_evidence(
            ids["task"],
            "a" * 40,
            EvidenceKind.REVIEW,
            EvidenceStatus.PASSED,
            {"private": f"token=private-{index}"},
        )
    store.close()

    status, body, _ = request(path)
    failed = next(row for row in body["executions"] if row["id"] == ids["failed"])

    assert status == 200
    assert failed["evidence"]["correlation"] == "EXACT_TASK_AND_CANDIDATE"
    assert failed["evidence"]["truncated"] is True
    assert sum(sum(values.values()) for values in failed["evidence"]["counts"].values()) == EVIDENCE_SCAN_LIMIT
    assert "private-" not in json.dumps(body)


def test_diagnostics_cap_execution_rows_and_report_exact_total(tmp_path: Path) -> None:
    path = tmp_path / "many.sqlite3"
    store = Store(path)
    store.migrate()
    task = store.upsert_task("Many executions", source_id="many")
    for _index in range(105):
        execution = store.start_execution(task_id=task, claim_id=None, kind=ExecutionKind.VALIDATION)
        store.finish_execution(execution, ExecutionStatus.SUCCEEDED)
    store.close()

    status, body, _ = request(path)

    assert status == 200
    assert len(body["executions"]) == body["execution_limit"] == 100
    assert body["execution_total"] == 105 and body["truncated"] is True


@pytest.mark.parametrize(
    ("method", "target", "body", "expected"),
    [
        ("POST", "/api/v1/diagnostics", b"", 405),
        ("GET", "/api/v1/diagnostics?limit=1", b"", 400),
        ("GET", "/api/v1/diagnostics", b"{}", 400),
    ],
)
def test_diagnostics_reject_mutation_query_and_body(
    db, method: str, target: str, body: bytes, expected: int
) -> None:
    path, _ids = db
    status, payload, _ = request(path, target, method=method, body=body)
    assert status == expected and payload["contract"] == DIAGNOSTICS_CONTRACT


def test_diagnostics_require_authentication(db) -> None:
    path, _ids = db
    status, body, _ = request(path, headers={"Host": "127.0.0.1:12345"})
    assert status == 401 and body["contract"] == DIAGNOSTICS_CONTRACT


def test_diagnostics_are_isolated_to_the_selected_project_store(tmp_path: Path) -> None:
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    first = Store(first_path)
    second = Store(second_path)
    first.migrate()
    second.migrate()
    task = first.upsert_task("First project", source_id="first")
    execution = first.start_execution(task_id=task, claim_id=None, kind=ExecutionKind.REVIEW)
    first.finish_execution(execution, ExecutionStatus.SUCCEEDED)
    first.close()
    second.close()

    first_status, first_body, _ = request(first_path)
    second_status, second_body, _ = request(second_path)

    assert first_status == second_status == 200
    assert [row["id"] for row in first_body["executions"]] == [execution]
    assert second_body["executions"] == []
