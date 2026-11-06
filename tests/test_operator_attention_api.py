from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.delivery_state import clear_external_block, record_external_block
from stagemesh.operator_attention import ATTENTION_CONTRACT, EVENT_SCAN_LIMIT
from stagemesh.persistence import Store

TOKEN = "attention-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Store]:
    db = tmp_path / "state.sqlite3"
    store = Store(db)
    store.migrate()
    return db, store


def request(
    db: Path,
    target: str = "/api/v1/attention",
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    api = ActivityApi(db, TOKEN)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS, body)
    return status, json.loads(payload), response_headers


def test_attention_queue_contains_only_current_typed_human_actions_and_is_read_only(
    project: tuple[Path, Store],
) -> None:
    db, store = project
    human = store.upsert_task("Choose behavior token=private-value", source_id="human")
    operational = store.upsert_task("Retry ordinary failure", source_id="operational")
    delivery = store.upsert_task("Land reviewed change", source_id="delivery")
    record_block(store, human, BlockKind.HUMAN_DECISION, "Choose retention token=private-value")
    record_block(store, operational, BlockKind.RECOVERABLE_OPERATIONAL, "provider timeout")
    store.add_candidate(delivery, "a" * 40, "fixture", True)
    record_external_block(store, delivery, "a" * 40, "protected branch", "maintainer approval")
    store.close()
    before = db.read_bytes()

    status, body, headers = request(db)
    encoded = json.dumps(body)

    assert status == 200 and body["contract"] == ATTENTION_CONTRACT
    assert body["queue_total"] == 2 and body["queue_total_kind"] == "EXACT"
    assert body["counts"] == {"HUMAN_DECISION": 1, "DELIVERY_PERMISSION": 1}
    assert body["human_action_only"] is True and body["operational_blocks_excluded"] is True
    assert body["approval_coverage"] == "UNKNOWN"
    assert body["database_writes"] == body["external_calls"] == 0
    assert operational not in {item["task_id"] for item in body["items"]}
    assert "private-value" not in encoded
    assert all(item["recommended_surface"] == "TASK_DETAIL" for item in body["items"])
    assert headers["Cache-Control"] == "no-store"
    assert db.read_bytes() == before


def test_attention_queue_removes_resolved_and_obsolete_records(
    project: tuple[Path, Store],
) -> None:
    db, store = project
    human = store.upsert_task("Resolved decision", source_id="human")
    delivery = store.upsert_task("Resolved permission", source_id="delivery")
    obsolete = store.upsert_task("Obsolete candidate", source_id="obsolete")
    record_block(store, human, BlockKind.HUMAN_DECISION, "choose behavior")
    assert store.unblock_task(human)
    store.add_candidate(delivery, "b" * 40, "fixture", True)
    record_external_block(store, delivery, "b" * 40, "protected branch", "maintainer approval")
    clear_external_block(store, delivery, "maintainer", "resolved")
    store.add_candidate(obsolete, "c" * 40, "fixture", True)
    record_external_block(store, obsolete, "d" * 40, "old candidate", "old approval")
    store.close()

    status, body, _ = request(db)

    assert status == 200
    assert body["items"] == []
    assert body["queue_total"] == 0 and body["queue_total_kind"] == "EXACT"


def test_attention_queue_labels_truncated_history_as_a_lower_bound(
    project: tuple[Path, Store],
) -> None:
    db, store = project
    task = store.upsert_task("Older decision", source_id="older")
    record_block(store, task, BlockKind.HUMAN_DECISION, "choose behavior")
    for index in range(EVENT_SCAN_LIMIT + 1):
        store.add_audit_event("task.unblocked", {"unrelated": index})
    store.close()

    status, body, _ = request(db)

    assert status == 200
    assert body["history_truncated"] is True and body["truncated"] is True
    assert body["queue_total_kind"] == "LOWER_BOUND"
    assert body["history_scan_limit"] == EVENT_SCAN_LIMIT
    assert body["approval_coverage"] == "UNKNOWN"


def test_newer_obsolete_candidate_event_does_not_hide_current_delivery_permission(
    project: tuple[Path, Store],
) -> None:
    db, store = project
    task = store.upsert_task("Candidate-scoped permission", source_id="candidate-scope")
    store.add_candidate(task, "a" * 40, "fixture", True)
    record_external_block(store, task, "a" * 40, "protected branch", "current approval")
    record_external_block(store, task, "b" * 40, "obsolete branch", "obsolete approval")
    store.close()

    status, body, _ = request(db)

    assert status == 200 and body["queue_total"] == 1
    assert body["items"][0]["candidate_sha"] == "a" * 40
    assert "current approval" in body["items"][0]["detail"]
    assert "obsolete approval" not in json.dumps(body)


def test_attention_queue_caps_rows_without_losing_the_exact_known_total(
    project: tuple[Path, Store],
) -> None:
    db, store = project
    for index in range(105):
        task = store.upsert_task(f"Decision {index}", source_id=f"decision-{index}")
        record_block(store, task, BlockKind.HUMAN_DECISION, f"choose behavior {index}")
    store.close()

    status, body, _ = request(db)

    assert status == 200
    assert len(body["items"]) == body["queue_limit"] == 100
    assert body["queue_total"] == 105 and body["queue_total_kind"] == "EXACT"
    assert body["truncated"] is True and body["counts_scope"] == "SHOWN"
    assert body["counts"]["HUMAN_DECISION"] == 100


@pytest.mark.parametrize(
    ("method", "target", "body", "expected"),
    [
        ("POST", "/api/v1/attention", b"", 405),
        ("GET", "/api/v1/attention?limit=1", b"", 400),
        ("GET", "/api/v1/attention", b"{}", 400),
    ],
)
def test_attention_queue_rejects_mutation_query_and_body(
    project: tuple[Path, Store], method: str, target: str, body: bytes, expected: int
) -> None:
    db, store = project
    store.close()

    status, payload, _ = request(db, target, method=method, body=body)

    assert status == expected
    assert payload["contract"] == ATTENTION_CONTRACT


def test_attention_queue_requires_authentication(project: tuple[Path, Store]) -> None:
    db, store = project
    store.close()

    status, payload, _ = request(db, headers={"Host": "127.0.0.1:12345"})

    assert status == 401 and payload["contract"] == ATTENTION_CONTRACT


def test_attention_queue_is_isolated_to_the_selected_project_store(tmp_path: Path) -> None:
    first_db = tmp_path / "first.sqlite3"
    second_db = tmp_path / "second.sqlite3"
    first = Store(first_db)
    second = Store(second_db)
    first.migrate()
    second.migrate()
    task = first.upsert_task("First project decision", source_id="first")
    record_block(first, task, BlockKind.HUMAN_DECISION, "choose first-project behavior")
    first.close()
    second.close()

    first_status, first_body, _ = request(first_db)
    second_status, second_body, _ = request(second_db)

    assert first_status == second_status == 200
    assert [item["task_id"] for item in first_body["items"]] == [task]
    assert second_body["items"] == []
