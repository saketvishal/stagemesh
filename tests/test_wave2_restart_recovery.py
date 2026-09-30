from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import ExecutionResult, ExecutionStatus, Executor
from stagemesh.persistence import Store
from stagemesh.review import Reviewer
from stagemesh.routing import Provider, Router, RoutingMode


class CountingExecutor(Executor):
    name = "counting-executor"

    def __init__(self):
        self.call_count = 0

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        self.call_count += 1
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind="IMPLEMENTATION")
        sha = "6666666666666666666666666666666666666666"
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


def test_completed_implement_not_rerun_after_restart(tmp_path: Path):
    store = Store(tmp_path / "test_restart.db")
    store.migrate()
    task_id = store.upsert_task("Test Restart Implement", source_id="T-RESTART-1")
    store.advance_task(task_id, Stage.IMPLEMENT)

    exec_1 = CountingExecutor()
    coord_1 = Coordinator(store=store, project=tmp_path, executor=exec_1)
    coord_1.tick()

    assert exec_1.call_count == 1
    task_after_1 = store.get_task(task_id)
    assert task_after_1["stage"] == Stage.VALIDATE

    # Restart coordinator with new executor instance
    exec_2 = CountingExecutor()
    coord_2 = Coordinator(store=store, project=tmp_path, executor=exec_2)
    coord_2.tick()

    # Implementation is NOT rerun
    assert exec_2.call_count == 0


def test_review_routing_persists_across_restart(tmp_path: Path):
    store = Store(tmp_path / "test_review_restart.db")
    store.migrate()
    task_id = store.upsert_task("Test Review Routing Restart", source_id="T-RESTART-2")
    store.advance_task(task_id, Stage.IMPLEMENT)

    sha = "7777777777777777777777777777777777777777"
    store.add_candidate(task_id, sha, "provider-A", durable_handoff=True)
    store.advance_task(task_id, Stage.VALIDATE)
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.REVIEW)

    # Configure router for REVIEW stage
    p_rev = Provider("review-provider-B", frozenset({"review"}))
    router = Router([p_rev], mode=RoutingMode.STAGED, stage_routes={str(Stage.REVIEW): "review-provider-B"})
    chosen = router.choose_for_stage(Stage.REVIEW, "review")
    assert chosen is not None
    assert chosen.name == "review-provider-B"

    # Simulate coordinator restart
    reviewer = Reviewer(worker_id="reviewer-1", provider=chosen.name)
    coord_restart = Coordinator(store=store, project=tmp_path, reviewer=reviewer)
    coord_restart.tick()

    task_final = store.get_task(task_id)
    assert task_final["stage"] == Stage.INTEGRATE


def test_review_capacity_failure_preserves_implementation_and_validation_on_restart(tmp_path: Path):
    store = Store(tmp_path / "test_review_cap_restart.db")
    store.migrate()
    task_id = store.upsert_task("Test Review Capacity Restart", source_id="T-RESTART-3")
    store.advance_task(task_id, Stage.IMPLEMENT)

    sha = "8888888888888888888888888888888888888888"
    store.add_candidate(task_id, sha, "provider-A", durable_handoff=True)
    store.advance_task(task_id, Stage.VALIDATE)
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.REVIEW)

    reviewer_fail = Reviewer(fail_capacity=True, worker_id="reviewer-1", provider="provider-B")
    coord_1 = Coordinator(store=store, project=tmp_path, reviewer=reviewer_fail)
    coord_1.tick()

    # Task remains in REVIEW stage, candidate and validation evidence preserved
    task = store.get_task(task_id)
    assert task["stage"] == Stage.REVIEW
    assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)

    # Restart coordinator with working reviewer
    reviewer_pass = Reviewer(fail_capacity=False, worker_id="reviewer-2", provider="provider-C")
    coord_2 = Coordinator(store=store, project=tmp_path, reviewer=reviewer_pass)
    coord_2.tick()

    task_after = store.get_task(task_id)
    assert task_after["stage"] == Stage.INTEGRATE
