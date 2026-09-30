from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage
from stagemesh.execution import ExecutionResult, ExecutionStatus, Executor
from stagemesh.persistence import Store


class CategorizedFailingExecutor(Executor):
    def __init__(self, name: str, failure_reason: str):
        self.name = name
        self.failure_reason = failure_reason

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            capacity_failure=True,
            failure_reason=self.failure_reason,
        )


class WorkingFallbackExecutor(Executor):
    name = "fallback-provider"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        sha = "3333333333333333333333333333333333333333"
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


@pytest.mark.parametrize("failure_reason", [
    "quota_rate_limit",
    "quota_exhausted",
    "provider_unavailable",
    "authentication_failure",
    "transient_provider_failure",
])
def test_automatic_cross_provider_failover_all_categories(tmp_path: Path, failure_reason: str):
    store = Store(tmp_path / f"test_{failure_reason}.db")
    store.migrate()
    task_id = store.upsert_task(f"Test Failover {failure_reason}", source_id=f"T-FAILOVER-{failure_reason}")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # 1. Primary executor fails with specific capacity failure category
    failing_exec = CategorizedFailingExecutor("primary-provider", failure_reason)
    coord = Coordinator(store=store, project=tmp_path, executor=failing_exec)
    coord.tick()

    # Task is still in IMPLEMENT stage, claim released immediately, audit event recorded
    task = store.get_task(task_id)
    assert task["stage"] == Stage.IMPLEMENT
    claims = list(store.conn.execute("SELECT * FROM claims WHERE active=1"))
    assert len(claims) == 0

    audits = store.audit_events()
    cap_events = [e for e in audits if e.get("event") == "task.capacity_failure"]
    assert len(cap_events) > 0
    assert cap_events[0].get("data", {}).get("reason") == failure_reason

    # 2. Re-dispatch with fallback executor succeeds
    fallback_exec = WorkingFallbackExecutor()
    coord_fallback = Coordinator(store=store, project=tmp_path, executor=fallback_exec)
    coord_fallback.tick()

    task_after = store.get_task(task_id)
    assert task_after["stage"] == Stage.VALIDATE
    candidate = store.latest_candidate(task_id)
    assert candidate["sha"] == "3333333333333333333333333333333333333333"
    assert candidate["produced_by"] == "fallback-provider"
