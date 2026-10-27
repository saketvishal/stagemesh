from __future__ import annotations

import json

import pytest

from stagemesh.audit import record_audit
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh.dashboard import render_dashboard
from stagemesh.delivery_state import clear_external_block, record_external_block
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage
from stagemesh.efficiency import Usage, record_provider_call
from stagemesh.operator import operator_report
from stagemesh.persistence import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "operator.sqlite3")
    database.migrate()
    database.upsert_task("work", source_id="T1")
    yield database
    database.close()


def sections(report):
    return {section.name: list(section.rows) for section in report.sections}


def finish(store, status, reason=None):
    execution = store.start_execution(task_id="T1", claim_id=None, kind=ExecutionKind.VALIDATION)
    store.finish_execution(execution, status, actor="validator", candidate_sha="a" * 40, result=reason)
    return execution


def test_attention_acts_on_current_failures_and_keeps_superseded_history_informational(store):
    failed = finish(store, ExecutionStatus.FAILED, "a check failed")
    current = operator_report(store)
    assert {"kind": "failed_executions", "count": 1, "status": "ACTION"} in sections(current)["Attention"]
    assert sections(current)["Recent Failures"][0]["scope"] == "current"

    finish(store, ExecutionStatus.SUCCEEDED)
    report = operator_report(store)
    assert report.summary == "ok"
    assert sections(report)["Attention"] == [{"kind": "failed_execution_history", "count": 1, "status": "INFO"}]
    assert sections(report)["Recent Failures"][0]["id"] == failed
    assert sections(report)["Recent Failures"][0]["scope"] == "history"
    assert "failed_executions=1" in report.lines  # Lifetime field remains compatible.
    assert "current_failed_executions=0" in report.lines


def test_done_task_failure_is_history_instead_of_action(store):
    finish(store, ExecutionStatus.FAILED)
    store.advance_task("T1", Stage.DONE)
    report = operator_report(store)
    assert report.summary == "ok"
    assert sections(report)["Recent Failures"][0]["scope"] == "history"
    assert all(row["status"] != "ACTION" for row in sections(report)["Attention"])


def test_recent_failures_are_bounded_identified_and_redacted(store):
    for i in range(13):
        finish(store, ExecutionStatus.FAILED, f"failure {i}: password=private-value")
    report = operator_report(store)
    failures = sections(report)["Recent Failures"]
    assert len(failures) == 10
    assert failures[0]["reason"] == "failure 12: password=***REDACTED***"
    assert failures[0]["provider"] == "validator"
    assert failures[0]["candidate_sha"] == "a" * 40
    assert sum(row["scope"] == "current" for row in failures) == 1
    assert "private-value" not in "\n".join(report.lines)


def test_efficiency_uses_bounded_samples_and_preserves_unreported_usage(store):
    for i in range(105):
        record_provider_call(
            store, task_id="T1", purpose="review", provider="fixture", outcome="succeeded", duration_seconds=1,
            usage=Usage(input=i, output=2) if i < 104 else Usage(),
        )
        store.add_evidence("T1", "a" * 40, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {"validation_reuse": {"ran": ["check"], "reused": []}})
    report = operator_report(store)
    efficiency = sections(report)["Recent Efficiency And Usage"][0]
    assert efficiency["provider_attempts"] == 100
    assert efficiency["attempts_by_purpose"] == {"review": 100}
    assert efficiency["provider_seconds"] == 100
    assert efficiency["checks_executed"] == 100
    assert efficiency["usage"]["calls_reporting"] == 99
    assert efficiency["usage"]["calls_unknown"] == 1
    assert efficiency["usage"]["input"] == sum(range(5, 104))
    assert efficiency["billing_cost"] == "UNKNOWN"
    assert "samples, not lifetime totals" in efficiency["scope"]


def test_no_usage_receipt_never_becomes_zero_tokens_or_zero_cost(store):
    record_provider_call(store, task_id="T1", purpose="implementation", provider="fixture", outcome="succeeded", duration_seconds=None)
    efficiency = sections(operator_report(store))["Recent Efficiency And Usage"][0]
    assert efficiency["usage"]["input"] is None
    assert efficiency["usage"]["output"] is None
    assert efficiency["usage"]["calls_unknown"] == 1
    assert efficiency["provider_seconds"] is None
    assert efficiency["provider_duration_unknown"] == 1
    assert efficiency["billing_cost"] == "UNKNOWN"


def test_bounded_legacy_provider_outcomes_remain_unknown_instead_of_failed(store):
    for _ in range(102):
        record_audit(store, "provider.call", {"task_id": "T1"})
    for outcome in ({"legacy": "unrecognised"}, "failed", "timeout", "succeeded"):
        record_audit(store, "provider.call", {"task_id": "T1", "outcome": outcome})

    efficiency = sections(operator_report(store))["Recent Efficiency And Usage"][0]

    assert efficiency["provider_attempts"] == 100
    assert efficiency["failed_provider_attempts"] == 2
    assert efficiency["provider_outcome_unknown"] == 97
    assert efficiency["usage"]["calls_unknown"] == 100
    assert efficiency["usage"]["input"] is None
    assert efficiency["usage"]["output"] is None
    assert efficiency["billing_cost"] == "UNKNOWN"


def test_only_current_typed_human_blocks_become_recorded_decisions(store):
    store.block_task("T1")
    assert [row["status"] for row in sections(operator_report(store))["Recorded Pending Decisions"]] == ["UNKNOWN"]
    record_block(store, "T1", BlockKind.HUMAN_DECISION, "choose the supported product behavior")
    report = operator_report(store)
    pending = sections(report)["Recorded Pending Decisions"]
    assert pending[0]["kind"] == "human_decision"
    assert pending[0]["status"] == "RECORDED"
    assert pending[0]["task_id"] == "T1"
    assert pending[-1]["status"] == "UNKNOWN"  # Never infer complete approval coverage from this sample.
    store.unblock_task("T1")
    assert [row["status"] for row in sections(operator_report(store))["Recorded Pending Decisions"]] == ["UNKNOWN"]


def test_delivery_permission_block_is_candidate_bound_and_explicit_clear_removes_it(store):
    store.add_candidate("T1", "a" * 40, "fixture", True)
    store.advance_task("T1", Stage.DONE)
    record_external_block(store, "T1", "a" * 40, "protected branch", "maintainer approval")
    report = operator_report(store)
    assert report.summary == "degraded"
    assert sections(report)["Recorded Pending Decisions"][0]["kind"] == "delivery_permission"
    assert "maintainer approval" in sections(report)["Recorded Pending Decisions"][0]["detail"]
    clear_external_block(store, "T1", "maintainer", "permission resolved")
    assert [row["status"] for row in sections(operator_report(store))["Recorded Pending Decisions"]] == ["UNKNOWN"]


def test_obsolete_candidate_block_or_merge_cannot_describe_current_delivery(store):
    store.add_candidate("T1", "a" * 40, "fixture", True)
    record_external_block(store, "T1", "b" * 40, "old candidate", "old approval")
    assert [row["status"] for row in sections(operator_report(store))["Recorded Pending Decisions"]] == ["UNKNOWN"]
    record_external_block(store, "T1", "a" * 40, "current candidate", "current approval")
    record_audit(store, "window.fast_forward_merged", {"task_id": "T1", "candidate_sha": "b" * 40})
    assert sections(operator_report(store))["Recorded Pending Decisions"][0]["candidate_sha"] == "a" * 40


def test_operator_and_dashboard_are_read_only_and_render_new_sections(store):
    finish(store, ExecutionStatus.FAILED, "<script>failure</script>")
    before = store.conn.total_changes
    store.conn.execute("PRAGMA query_only=ON")
    report = operator_report(store)
    html = render_dashboard(store)
    assert store.conn.total_changes == before
    assert "Recent Failures" in html and "Recent Efficiency And Usage" in html and "Recorded Pending Decisions" in html
    assert "&lt;script&gt;failure&lt;/script&gt;" in html
    assert "<script>failure</script>" not in html
    assert {"Attention", "Tasks", "Workers", "External Evidence", "Provider Selections"} <= sections(report).keys()
    json.dumps({"summary": report.summary, "lines": report.lines, "sections": sections(report)})


@pytest.mark.parametrize("kind", [ExecutionKind.VALIDATION, ExecutionKind.IMPLEMENTATION, ExecutionKind.REVIEW, ExecutionKind.INTEGRATION])
@pytest.mark.parametrize("parent_state", ["LIVE", "DEAD", "UNKNOWN"])
def test_validation_tree_ownership_is_distinct_from_parent_liveness(store, monkeypatch, kind, parent_state):
    from stagemesh.observability import health

    execution = store.start_execution(task_id="T1", claim_id=None, kind=kind, pid=12345)
    monkeypatch.setattr("stagemesh.operator.classify_process", lambda *_: parent_state)
    monkeypatch.setattr("stagemesh.observability.classify_process", lambda *_: parent_state)
    before = [tuple(row) for row in store.conn.execute("SELECT * FROM executions")]
    audits = store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    pending = kind == ExecutionKind.VALIDATION
    effective = "UNKNOWN" if pending and parent_state != "LIVE" else parent_state
    report = operator_report(store)
    row, = sections(report)["Active Executions"]
    assert row["process_state"] == effective
    assert row["parent_process_state"] == parent_state
    assert row["stale"] is (effective == "DEAD")
    assert row["cleanup_state"] == ("UNCONFIRMED_VALIDATION_TREE" if pending else None)
    retained = int(pending and parent_state != "LIVE")
    current = health(store)
    assert current.unresolved_validation_count == retained
    assert current.stale_execution_count == int(effective == "DEAD")
    assert current.unknown_execution_count == 0  # durable UNKNOWN status is a separate metric
    assert current.dead_validation_parent_count == int(pending and parent_state == "DEAD")
    from stagemesh.run_ready import current_problems
    assert current_problems(store) == (("stale_running_executions",) if parent_state == "DEAD" else ())
    assert ("unresolved_validation_trees" in current.current_problems) is bool(retained)
    attention = [row for row in sections(report)["Attention"] if row["kind"] == "unresolved_validation_trees"]
    assert attention == ([{"kind": "unresolved_validation_trees", "count": 1, "status": "REVIEW"}] if retained else [])
    assert f"unresolved_validation_trees={retained}" in report.lines
    active_line, = [line for line in report.lines if line.startswith("active_execution ")]
    assert f"process={effective}" in active_line and f"parent={parent_state}" in active_line
    html = render_dashboard(store)
    assert "parent_process_state" in html
    assert ("UNCONFIRMED_VALIDATION_TREE" in html) is pending
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM executions")] == before
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audits
    assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution,)).fetchone()[0] == "RUNNING"


def test_validation_hold_metric_does_not_rewrite_unknown_status_or_survive_completion(store, monkeypatch):
    from stagemesh.observability import health

    validation = store.start_execution(task_id="T1", claim_id=None, kind=ExecutionKind.VALIDATION)
    unknown = store.start_execution(task_id="T1", claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(unknown, ExecutionStatus.UNKNOWN)
    monkeypatch.setattr("stagemesh.operator.classify_process", lambda *_: "DEAD")
    monkeypatch.setattr("stagemesh.observability.classify_process", lambda *_: "DEAD")
    report = operator_report(store)
    assert {"kind": "unknown_executions", "count": 1, "status": "REVIEW"} in sections(report)["Attention"]
    assert {"kind": "unresolved_validation_trees", "count": 1, "status": "REVIEW"} in sections(report)["Attention"]
    assert health(store).unknown_execution_count == 1
    assert not health(store).ok
    store.finish_execution(validation, ExecutionStatus.SUCCEEDED)
    assert health(store).unresolved_validation_count == 0
    assert health(store).unknown_execution_count == 1
    assert sections(operator_report(store))["Active Executions"] == []
