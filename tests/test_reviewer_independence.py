from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store
from stagemesh.review import Reviewer, SelfReviewError


def test_reviewer_cannot_be_implementation_worker(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Test Self Review Prevention", source_id="T-1")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # Implement by worker-1 on provider-A
    claim_id = store.acquire_claim(task_id, "worker-1")
    sha = "1111111111111111111111111111111111111111"
    store.add_candidate(task_id, sha, "provider-A", durable_handoff=True)
    store.advance_task(task_id, Stage.VALIDATE)

    # Add validation evidence
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.REVIEW)

    reviewer = Reviewer(worker_id="worker-1", provider="provider-A")
    with pytest.raises(SelfReviewError, match="cannot review its own implementation candidate"):
        reviewer.review(store, task_id, sha, tmp_path)


def test_reviewer_capacity_failure_does_not_restart_implementation(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Test Review Capacity Failure", source_id="T-2")
    store.advance_task(task_id, Stage.IMPLEMENT)

    claim_id = store.acquire_claim(task_id, "worker-1")
    sha = "2222222222222222222222222222222222222222"
    store.add_candidate(task_id, sha, "provider-A", durable_handoff=True)
    store.advance_task(task_id, Stage.VALIDATE)
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.REVIEW)

    reviewer = Reviewer(fail_capacity=True, worker_id="worker-2", provider="provider-B")
    reviewer.review(store, task_id, sha, tmp_path)

    task = store.get_task(task_id)
    assert task["stage"] == Stage.REVIEW
    candidate = store.latest_candidate(task_id)
    assert candidate["sha"] == sha
    # Implementation is NOT rerun
    candidates = list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))
    assert len(candidates) == 1
