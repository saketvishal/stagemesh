from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage
from stagemesh.execution import ExecutionResult, ExecutionStatus, Executor
from stagemesh.persistence import Store


class FailingPrimaryExecutor(Executor):
    name = "primary-provider"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            capacity_failure=True,
            failure_reason="quota_rate_limit",
        )


class WorkingFallbackExecutor(Executor):
    name = "fallback-provider"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        sha = "3333333333333333333333333333333333333333"
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def test_automatic_cross_provider_failover_recovers_task(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Test Provider Failover", source_id="T-3")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # 1. Primary executor fails with capacity error (rate limit)
    failing_exec = FailingPrimaryExecutor()
    coord = Coordinator(store=store, project=tmp_path, executor=failing_exec)
    coord.tick()

    # Task is still in IMPLEMENT stage, claim released immediately
    task = store.get_task(task_id)
    assert task["stage"] == Stage.IMPLEMENT
    claims = list(store.conn.execute("SELECT * FROM claims WHERE active=1"))
    assert len(claims) == 0

    # 2. Re-dispatch with fallback executor succeeds
    fallback_exec = WorkingFallbackExecutor()
    coord_fallback = Coordinator(store=store, project=tmp_path, executor=fallback_exec)
    coord_fallback.tick()

    task_after = store.get_task(task_id)
    assert task_after["stage"] == Stage.VALIDATE
    candidate = store.latest_candidate(task_id)
    assert candidate["sha"] == "3333333333333333333333333333333333333333"
    assert candidate["produced_by"] == "fallback-provider"
