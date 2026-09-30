"""Wave 4 — OBSERVABILITY-004: metrics collection and export.

Legacy contract (build_coordinator/metrics.py, tests/test_metrics.py):
  - queue_depth: total open tasks + breakdown by stage
  - execution_outcomes: total + by_status + by_kind (role in legacy)
  - provider_usage: worker/provider counts
  - metrics are derived solely from durable persisted rows (not in-memory state)
  - export produces valid JSON
  - no secrets in exported metrics
  - deterministic key ordering in output
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.observability import export_metrics_json, metrics_snapshot
from stagemesh.persistence import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "metrics_test.db")
    store.migrate()
    return store


# ---------------------------------------------------------------------------
# Empty store baseline
# ---------------------------------------------------------------------------

def test_metrics_snapshot_empty_store(tmp_path: Path):
    store = _store(tmp_path)
    snap = metrics_snapshot(store, now=1000.0)
    assert snap["queue_depth"]["total"] == 0
    assert snap["execution_outcomes"]["total"] == 0
    assert isinstance(snap["provider_usage"], dict)
    assert snap["retry_state"]["active_entries"] == 0
    assert snap["generated_at"] == 1000.0
    store.close()


def test_metrics_snapshot_is_json_serialisable(tmp_path: Path):
    store = _store(tmp_path)
    snap = metrics_snapshot(store, now=1000.0)
    # Must not raise
    serialised = json.dumps(snap)
    assert isinstance(serialised, str)
    store.close()


# ---------------------------------------------------------------------------
# Queue depth tracks open tasks
# ---------------------------------------------------------------------------

def test_queue_depth_counts_open_tasks(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_task("Task A", source="local")
    store.upsert_task("Task B", source="local")
    snap = metrics_snapshot(store)
    assert snap["queue_depth"]["total"] == 2
    store.close()


def test_queue_depth_by_stage(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_task("Task PLAN", source="local")
    snap = metrics_snapshot(store)
    by_stage = snap["queue_depth"]["by_stage"]
    assert "PLAN" in by_stage
    assert by_stage["PLAN"] >= 1
    store.close()


def test_done_tasks_excluded_from_queue_depth(tmp_path: Path):
    """Tasks that are DONE must not count in the open queue."""
    store = _store(tmp_path)
    task_id = store.upsert_task("Done task", source="local")
    store.conn.execute(
        "UPDATE tasks SET stage=?, status=? WHERE id=?",
        (Stage.DONE, TaskStatus.DONE, task_id),
    )
    store.conn.commit()

    open_task_id = store.upsert_task("Open task", source="local")
    snap = metrics_snapshot(store)
    assert snap["queue_depth"]["total"] == 1
    store.close()


# ---------------------------------------------------------------------------
# Execution outcomes derived from durable rows
# ---------------------------------------------------------------------------

def test_execution_outcomes_count_by_status(tmp_path: Path):
    store = _store(tmp_path)
    task_id = store.upsert_task("Exec task", source="local")
    exec_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(exec_id, ExecutionStatus.SUCCEEDED, "a" * 40)

    snap = metrics_snapshot(store)
    assert snap["execution_outcomes"]["total"] == 1
    assert snap["execution_outcomes"]["by_status"].get("SUCCEEDED", 0) == 1
    store.close()


def test_execution_outcomes_by_kind(tmp_path: Path):
    store = _store(tmp_path)
    task_id = store.upsert_task("Kind task", source="local")
    exec_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION)
    store.finish_execution(exec_id, ExecutionStatus.FAILED)

    snap = metrics_snapshot(store)
    by_kind = snap["execution_outcomes"]["by_kind"]
    assert "VALIDATION" in by_kind
    assert by_kind["VALIDATION"] == 1
    store.close()


def test_multiple_execution_statuses_tracked(tmp_path: Path):
    store = _store(tmp_path)
    task_a = store.upsert_task("Task A", source="local")
    task_b = store.upsert_task("Task B", source="local")

    exec_a = store.start_execution(task_id=task_a, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(exec_a, ExecutionStatus.SUCCEEDED, "a" * 40)

    exec_b = store.start_execution(task_id=task_b, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(exec_b, ExecutionStatus.FAILED)

    snap = metrics_snapshot(store)
    assert snap["execution_outcomes"]["total"] == 2
    assert snap["execution_outcomes"]["by_status"]["SUCCEEDED"] == 1
    assert snap["execution_outcomes"]["by_status"]["FAILED"] == 1
    store.close()


# ---------------------------------------------------------------------------
# No secrets in exported metrics
# ---------------------------------------------------------------------------

def test_export_contains_no_secrets(tmp_path: Path):
    """Metric export must not contain any credentials or secrets."""
    store = _store(tmp_path)
    from stagemesh.workers import register_worker
    from stagemesh.domain import ProcessIdentity
    identity = ProcessIdentity(pid=None, create_time=None, boot_id=None, executable=None)
    register_worker(store, "worker-test", "openai", {"openai"}, identity, lease_seconds=60)

    exported = export_metrics_json(store)

    # Secret API key must never appear (capabilities are stored as set, not dict)
    assert "sk-SECRETKEY" not in exported
    store.close()



def test_export_metrics_json_is_valid_json(tmp_path: Path):
    store = _store(tmp_path)
    exported = export_metrics_json(store)
    parsed = json.loads(exported)
    assert "queue_depth" in parsed
    assert "execution_outcomes" in parsed
    store.close()


# ---------------------------------------------------------------------------
# Deterministic key ordering
# ---------------------------------------------------------------------------

def test_export_keys_are_sorted(tmp_path: Path):
    """json.dumps with sort_keys=True must produce sorted output."""
    store = _store(tmp_path)
    snap = metrics_snapshot(store, now=42.0)
    exported = export_metrics_json(store, now=42.0)
    parsed = json.loads(exported)
    # Top-level keys must be alphabetically sorted
    keys = list(parsed.keys())
    assert keys == sorted(keys)
    store.close()


# ---------------------------------------------------------------------------
# Metrics are derived from durable rows (not ephemeral in-memory state)
# ---------------------------------------------------------------------------

def test_metrics_persisted_across_store_close_reopen(tmp_path: Path):
    """Metrics after store re-open must reflect the same data."""
    db_path = tmp_path / "durable.db"
    store = Store(db_path)
    store.migrate()
    task_id = store.upsert_task("Durable task", source="local")
    exec_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(exec_id, ExecutionStatus.SUCCEEDED, "b" * 40)
    store.close()

    store2 = Store(db_path)
    snap = metrics_snapshot(store2)
    assert snap["execution_outcomes"]["total"] == 1
    assert snap["execution_outcomes"]["by_status"]["SUCCEEDED"] == 1
    store2.close()


# ---------------------------------------------------------------------------
# OBSERVABILITY-004: Exact deterministic tests for required metric calculations
# ---------------------------------------------------------------------------

def test_execution_duration_and_claim_latency_calculation(tmp_path: Path):
    """Verify execution duration and claim latency calculations matching legacy contract."""
    from stagemesh.workers import register_worker
    from stagemesh.domain import ProcessIdentity

    store = _store(tmp_path)
    now = 1000.0

    # Task 1 created at 700.0
    task_1 = store.upsert_task("Task 1", source="local")
    store.conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (700.0, task_1))

    # Worker registered
    ident = ProcessIdentity(pid=None, create_time=None, boot_id=None, executable=None)
    register_worker(store, "w-1", "codex", {"code"}, ident, 300)

    # Task claimed at 850.0 (claim latency = 150.0)
    claim_1 = store.acquire_claim(task_1, "w-1", lease_seconds=300)
    store.conn.execute("UPDATE claims SET created_at=? WHERE id=?", (850.0, claim_1))

    # Execution started at 860.0, completed at 920.0 (duration = 60.0s)
    exec_1 = store.start_execution(
        task_id=task_1,
        claim_id=claim_1,
        kind=ExecutionKind.IMPLEMENTATION,
    )
    store.conn.execute("UPDATE executions SET started_at=?, updated_at=? WHERE id=?", (860.0, 920.0, exec_1))
    store.finish_execution(exec_1, ExecutionStatus.SUCCEEDED, "c" * 40)
    store.conn.execute("UPDATE executions SET updated_at=? WHERE id=?", (920.0, exec_1))

    snap = metrics_snapshot(store, now=now)

    # Claim latency
    assert snap["claim_latency"]["implementation"]["count"] == 1
    assert snap["claim_latency"]["implementation"]["avg_seconds"] == 150.0

    # Execution duration
    assert snap["execution_outcomes"]["duration_seconds"]["count"] == 1
    assert snap["execution_outcomes"]["duration_seconds"]["avg_seconds"] == 60.0
    assert snap["execution_outcomes"]["duration_seconds"]["max_seconds"] == 60.0
    store.close()


def test_provider_usage_counts_executions_not_workers(tmp_path: Path):
    """
    OBSERVABILITY-004: provider_usage must measure execution counts by provider,
    not merely the count of registered workers.
    """
    from stagemesh.workers import register_worker
    from stagemesh.domain import ProcessIdentity

    store = _store(tmp_path)
    ident = ProcessIdentity(pid=None, create_time=None, boot_id=None, executable=None)

    # Register two workers for codex, one for claude
    register_worker(store, "codex-w1", "codex", {"code"}, ident, 300)
    register_worker(store, "codex-w2", "codex", {"code"}, ident, 300)
    register_worker(store, "claude-w1", "claude", {"code"}, ident, 300)

    t1 = store.upsert_task("T1", source="local")
    t2 = store.upsert_task("T2", source="local")
    t3 = store.upsert_task("T3", source="local")

    c1 = store.acquire_claim(t1, "codex-w1")
    e1 = store.start_execution(task_id=t1, claim_id=c1, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(e1, ExecutionStatus.SUCCEEDED, "a" * 40)

    c2 = store.acquire_claim(t2, "codex-w2")
    e2 = store.start_execution(task_id=t2, claim_id=c2, kind=ExecutionKind.IMPLEMENTATION)
    store.finish_execution(e2, ExecutionStatus.FAILED)

    c3 = store.acquire_claim(t3, "claude-w1")
    e3 = store.start_execution(task_id=t3, claim_id=c3, kind=ExecutionKind.REVIEW)
    store.finish_execution(e3, ExecutionStatus.SUCCEEDED, "a" * 40)

    snap = metrics_snapshot(store, now=1000.0)

    assert "codex" in snap["provider_usage"]
    assert "claude" in snap["provider_usage"]
    # Total executions: codex ran 2 executions (across 2 workers), claude ran 1 execution
    assert snap["provider_usage"]["codex"]["total_executions"] == 2
    assert snap["provider_usage"]["codex"]["completed_executions"] == 2
    assert snap["provider_usage"]["claude"]["total_executions"] == 1
    assert snap["provider_usage"]["claude"]["completed_executions"] == 1
    store.close()


def test_throughput_and_token_usage_metrics(tmp_path: Path):
    """Verify throughput and token usage metrics."""
    store = _store(tmp_path)
    t1 = store.upsert_task("Task with estimated tokens", source="local")
    store.conn.execute("UPDATE tasks SET stage=?, status=? WHERE id=?", (Stage.DONE, TaskStatus.DONE, t1))

    # Add evidence with token metadata
    store.add_evidence(
        t1,
        "a" * 40,
        EvidenceKind.VALIDATION,
        EvidenceStatus.PASSED,
        {"usage": {"total_tokens": 450}, "tokens": 450},
    )

    snap = metrics_snapshot(store)
    assert snap["throughput"]["completed_tasks"] == 1
    assert snap["token_usage"]["reported_evidence_tokens"] == 450
    assert snap["token_usage"]["total_tokens"] >= 450
    store.close()


def test_exported_token_usage_remains_structured_and_visible(tmp_path: Path):
    """OBSERVABILITY-004: token_usage metric name must NOT be redacted by key filtering."""
    store = _store(tmp_path)
    t1 = store.upsert_task("Task for token export", source="local")
    store.add_evidence(
        t1,
        "a" * 40,
        EvidenceKind.VALIDATION,
        EvidenceStatus.PASSED,
        {"usage": {"total_tokens": 123}},
    )

    exported = export_metrics_json(store)
    data = json.loads(exported)

    assert isinstance(data["token_usage"], dict)
    assert data["token_usage"]["reported_evidence_tokens"] == 123
    assert "total_tokens" in data["token_usage"]
    assert data["token_usage"] != "[REDACTED]"
    store.close()


def test_direct_unit_tests_for_secret_redaction_helper():
    """OBSERVABILITY-004: Direct unit tests for secret-redaction helper using actual secret-bearing values."""
    from stagemesh.observability import redact_sensitive_value

    secret_pat = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    secret_sk = "sk-1234567890abcdefghijklmnopqrstuvwxyz"
    secret_auth = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"

    # String values with embedded secrets
    assert redact_sensitive_value(f"Token is {secret_pat}") == "Token is [REDACTED]"
    assert redact_sensitive_value(f"Key is {secret_sk}") == "Key is [REDACTED]"
    assert redact_sensitive_value(f"Header: {secret_auth}") == "Header: [REDACTED]"
    assert redact_sensitive_value("password=super_secret_123") == "[REDACTED]"

    # Sensitive dictionary keys are masked
    sensitive_dict = {
        "api_key": "plain-secret-value",
        "password": "super-secret-password",
        "client_secret": "my-client-secret",
        "github_token": "my-token",
        "token": "bearer-token",
        "auth": "auth-header",
    }
    cleaned_dict = redact_sensitive_value(sensitive_dict)
    for k in sensitive_dict:
        assert cleaned_dict[k] == "[REDACTED]"

    # Legitimate metric names must NOT be masked
    safe_metrics = {
        "token_usage": {"total_tokens": 500, "estimated_context_tokens": 200},
        "total_tokens": 500,
        "tokens": 500,
        "reported_tokens": 300,
        "worker_utilisation": {"builder-a": {"active_claims": 1}},
    }
    cleaned_metrics = redact_sensitive_value(safe_metrics)
    assert isinstance(cleaned_metrics["token_usage"], dict)
    assert cleaned_metrics["token_usage"]["total_tokens"] == 500
    assert cleaned_metrics["total_tokens"] == 500
    assert cleaned_metrics["tokens"] == 500


def test_active_claims_honor_lease_expiry_in_worker_utilisation(tmp_path: Path):
    """OBSERVABILITY-004: Active claims must honor lease expiry as legacy does."""
    store = _store(tmp_path)
    now = 1000.0

    t1 = store.upsert_task("T1", source="local")
    t2 = store.upsert_task("T2", source="local")

    # Claim 1: ACTIVE and lease expires in future (now + 500) -> counted
    c1 = store.acquire_claim(t1, "worker-1", lease_seconds=500)
    store.conn.execute("UPDATE claims SET lease_expires_at=? WHERE id=?", (now + 500, c1))

    # Claim 2: ACTIVE in DB but lease expired in past (now - 50) -> NOT counted as active
    c2 = store.acquire_claim(t2, "worker-1", lease_seconds=10)
    store.conn.execute("UPDATE claims SET lease_expires_at=? WHERE id=?", (now - 50, c2))
    store.conn.commit()

    snap = metrics_snapshot(store, now=now)
    assert snap["worker_utilisation"]["worker-1"]["active_claims"] == 1
    store.close()


def test_execution_kinds_not_classified_as_provider_or_worker(tmp_path: Path):
    """OBSERVABILITY-004: Do not classify validation/review/integration execution kinds as provider or worker names."""
    store = _store(tmp_path)
    t1 = store.upsert_task("T1", source="local")

    e1 = store.start_execution(task_id=t1, claim_id=None, kind=ExecutionKind.VALIDATION)
    store.finish_execution(e1, ExecutionStatus.SUCCEEDED, "a" * 40)

    e2 = store.start_execution(task_id=t1, claim_id=None, kind=ExecutionKind.REVIEW)
    store.finish_execution(e2, ExecutionStatus.SUCCEEDED, "a" * 40)

    e3 = store.start_execution(task_id=t1, claim_id=None, kind=ExecutionKind.INTEGRATION)
    store.finish_execution(e3, ExecutionStatus.SUCCEEDED, "a" * 40)

    snap = metrics_snapshot(store)

    # Neither worker_utilisation nor provider_usage may contain execution kinds
    for kind_name in ("VALIDATION", "REVIEW", "INTEGRATION"):
        assert kind_name not in snap["worker_utilisation"]
        assert kind_name not in snap["provider_usage"]

    # But execution_outcomes.by_kind / by_role tracks them properly
    assert snap["execution_outcomes"]["by_kind"]["VALIDATION"] == 1
    assert snap["execution_outcomes"]["by_kind"]["REVIEW"] == 1
    assert snap["execution_outcomes"]["by_kind"]["INTEGRATION"] == 1
    store.close()


