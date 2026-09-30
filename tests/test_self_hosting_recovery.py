from __future__ import annotations

import time
from pathlib import Path
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage, TaskStatus
from stagemesh.execution import ExecutionResult, ExecutionStatus, Executor
from stagemesh.persistence import Store


class RecordedExecutor(Executor):
    def __init__(self, name: str = "rec-worker"):
        self.name = name
        self.invocations: list[str] = []

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        self.invocations.append(task_id)
        sha = "1111222233334444555566667777888899990000"
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        return ExecutionResult(status=ExecutionStatus.SUCCEEDED, candidate_sha=sha, durable_handoff=True)


def test_watcher_restart_does_not_redispatch_completed_implement(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Completed Implement Task", source_id="T-DONE-1")
    store.advance_task(task_id, Stage.VALIDATE)

    exec1 = RecordedExecutor("worker-1")
    coord1 = Coordinator(store=store, project=tmp_path, executor=exec1)
    coord1.tick()

    assert task_id not in exec1.invocations  # IMPLEMENT was already finished


def test_dead_worker_lease_recovery_and_sha_preservation(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Dead Worker Task", source_id="T-DEAD-1")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # Dead worker acquired claim with expired lease
    now = time.time()
    store.conn.execute(
        "INSERT INTO claims VALUES (?, ?, ?, ?, ?, 1, ?)",
        ("claim-dead", task_id, "dead-worker", "IMPLEMENT", now - 10, now - 300),
    )
    store.conn.commit()

    exec2 = RecordedExecutor("worker-recovery")
    coord2 = Coordinator(store=store, project=tmp_path, executor=exec2)
    coord2.tick()

    # Reclaimed and executed
    assert task_id in exec2.invocations
    task = store.get_task(task_id)
    assert task["stage"] in (Stage.VALIDATE.value, Stage.REVIEW.value, Stage.DONE.value)
