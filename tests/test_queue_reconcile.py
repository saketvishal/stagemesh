from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from test_survivor_wave import make_store

import stagemesh.cli as cli_module
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.queue_reconcile import ReconcileOptions, reconcile_queue


def _decision_block(store, task_id: str) -> None:
    sha = (task_id + "d" * 40)[:40]
    store.add_candidate(task_id, sha, "codex", True)
    store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    store.upsert_finding(f"finding-{task_id}", task_id, sha, "error", "needs product decision")
    store.block_task(task_id)


def _by_id(report):
    return {item.task_id: item for item in report.items}


def test_queue_reconcile_dry_run_classifies_noise_without_changing_tasks(tmp_path: Path) -> None:
    store, _project = make_store(tmp_path, ["run", "retry", "decide", "closed", "operator"])
    store.block_task("retry")
    _decision_block(store, "decide")
    closed = store.get_task("closed")
    store.cache_source(closed["source"], closed["source_id"], {"retirement_reason": "source closed"}, "CLOSED")
    store.block_task("closed")
    operator = store.get_task("operator")
    store.retire_source_task(operator["source"], operator["source_id"], "duplicate or already satisfied")

    report = reconcile_queue(store, ReconcileOptions(park_design_decisions=True, unblock_retryable=True), sync_sources=False)
    items = _by_id(report)

    assert items["run"].classification == "runnable"
    assert items["retry"].classification == "retryable_blocked" and items["retry"].action == "unblock_retryable"
    assert items["decide"].classification == "needs_decision" and items["decide"].action == "park_design_decision"
    assert items["closed"].classification == "retired_closed"
    assert items["operator"].classification == "retired_operator"
    assert store.get_task("retry")["status"] == "BLOCKED"
    assert store.operator_retirement_reason(operator["source"], operator["source_id"]) == "duplicate or already satisfied"


def test_queue_reconcile_write_applies_only_explicit_actions_and_audits_summary(tmp_path: Path) -> None:
    store, _project = make_store(tmp_path, ["retry", "decide"])
    store.block_task("retry")
    _decision_block(store, "decide")
    decide = store.get_task("decide")

    report = reconcile_queue(
        store,
        ReconcileOptions(write=True, park_design_decisions=True, unblock_retryable=True),
        sync_sources=False,
    )
    items = _by_id(report)

    assert items["retry"].applied and store.get_task("retry")["status"] == "OPEN"
    assert items["decide"].applied
    assert store.operator_retirement_reason(decide["source"], decide["source_id"]) == "Parked by queue reconcile: needs design decision"
    events = store.audit_events()
    assert any(row["event_type"] == "queue.reconciled" for row in events)


def test_queue_reconcile_write_without_action_flags_is_a_noop(tmp_path: Path) -> None:
    store, _project = make_store(tmp_path, ["retry", "decide"])
    store.block_task("retry")
    _decision_block(store, "decide")

    report = reconcile_queue(store, ReconcileOptions(write=True), sync_sources=False)

    assert report.actions == []
    assert store.get_task("retry")["status"] == "BLOCKED"
    assert store.operator_retirement_reason(store.get_task("decide")["source"], store.get_task("decide")["source_id"]) is None


def test_queue_reconcile_cli_json_is_dry_run_by_default(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["retry"])
    store.block_task("retry")
    store.close()
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "queue", "reconcile", "--no-sync", "--unblock-retryable", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert data["write"] is False
    assert data["actions"][0]["task_id"] == "retry"
    assert data["actions"][0]["applied"] is False
