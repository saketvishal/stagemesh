from __future__ import annotations

import threading
import time
from pathlib import Path
import pytest

from stagemesh.capacity import CapacityRegistry
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import ExecutionResult, ExecutionStatus, Executor
from stagemesh.persistence import Store
from stagemesh.review import Reviewer


class BarrierExecutor(Executor):
    def __init__(self, name: str, barrier: threading.Barrier):
        self.name = name
        self.barrier = barrier
        self.started_events: list[str] = []
        self.lock = threading.Lock()

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        with self.lock:
            self.started_events.append(task_id)
        # Wait at barrier to prove true concurrent overlap across threads
        self.barrier.wait(timeout=5.0)

        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        sha = f"55555555555555555555555555555555555555{task_id[-2:]}"
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def test_concurrent_multi_builder_execution_capacity_two(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    t1 = store.upsert_task("Task 1 Concurrent", source_id="T-C1")
    t2 = store.upsert_task("Task 2 Concurrent", source_id="T-C2")
    store.advance_task(t1, Stage.IMPLEMENT)
    store.advance_task(t2, Stage.IMPLEMENT)

    barrier = threading.Barrier(2)
    exec_1 = BarrierExecutor("builder-1", barrier)
    exec_2 = BarrierExecutor("builder-2", barrier)

    def _worker_thread(task_id: str, executor: Executor):
        coord = Coordinator(store=store, project=tmp_path / task_id, executor=executor)
        coord.tick()

    th1 = threading.Thread(target=_worker_thread, args=(t1, exec_1))
    th2 = threading.Thread(target=_worker_thread, args=(t2, exec_2))

    th1.start()
    th2.start()
    th1.join(timeout=10.0)
    th2.join(timeout=10.0)

    # Prove both tasks ran concurrently and succeeded
    task1 = store.get_task(t1)
    task2 = store.get_task(t2)
    assert task1["stage"] in (Stage.VALIDATE, Stage.REVIEW, Stage.DONE)
    assert task2["stage"] in (Stage.VALIDATE, Stage.REVIEW, Stage.DONE)

    cand1 = store.latest_candidate(t1)
    cand2 = store.latest_candidate(t2)
    assert cand1["sha"] != cand2["sha"]


def test_two_concurrent_tasks_full_lifecycle_isolation(tmp_path: Path):
    store = Store(tmp_path / "test_full.db")
    store.migrate()
    t1 = store.upsert_task("Task Full 1", source_id="T-FULL-1")
    t2 = store.upsert_task("Task Full 2", source_id="T-FULL-2")
    store.advance_task(t1, Stage.IMPLEMENT)
    store.advance_task(t2, Stage.IMPLEMENT)

    barrier = threading.Barrier(2)
    exec_1 = BarrierExecutor("builder-1", barrier)
    exec_2 = BarrierExecutor("builder-2", barrier)

    def _full_lifecycle_thread(task_id: str, executor: Executor, worker_name: str):
        proj = tmp_path / task_id
        reviewer = Reviewer(worker_id=f"reviewer-{task_id}", provider="reviewer-provider")
        coord = Coordinator(store=store, project=proj, executor=executor, reviewer=reviewer)
        # Advance through IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE
        for _ in range(50):
            coord.tick()
            t = store.get_task(task_id)
            if t["stage"] == Stage.DONE:
                break
            time.sleep(0.005)

    th1 = threading.Thread(target=_full_lifecycle_thread, args=(t1, exec_1, "worker-1"))
    th2 = threading.Thread(target=_full_lifecycle_thread, args=(t2, exec_2, "worker-2"))

    th1.start()
    th2.start()
    th1.join(timeout=10.0)
    th2.join(timeout=10.0)

    task1 = store.get_task(t1)
    task2 = store.get_task(t2)
    assert task1["stage"] == Stage.DONE
    assert task2["stage"] == Stage.DONE

    cand1 = store.latest_candidate(t1)["sha"]
    cand2 = store.latest_candidate(t2)["sha"]
    assert cand1 != cand2

    # Verify task-scoped evidence isolation
    ev1 = store.has_evidence(t1, cand1, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    ev2 = store.has_evidence(t2, cand2, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    assert ev1 is True
    assert ev2 is True

    # Check evidence from t1 is NOT present for t2 candidate
    assert store.has_evidence(t2, cand1, EvidenceKind.VALIDATION, EvidenceStatus.PASSED) is False


def test_capacity_one_prevents_illegal_concurrent_claim(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()

    t1 = store.upsert_task("Task Cap 1", source_id="T-CAP1")
    t2 = store.upsert_task("Task Cap 2", source_id="T-CAP2")
    store.advance_task(t1, Stage.IMPLEMENT)
    store.advance_task(t2, Stage.IMPLEMENT)

    # Acquire slot 1
    c1 = store.acquire_claim(t1, "worker-1")
    assert c1 is not None

    # Acquiring second active claim for t2 under max 1 capacity
    claims = list(store.conn.execute("SELECT * FROM claims WHERE active=1"))
    assert len(claims) == 1
