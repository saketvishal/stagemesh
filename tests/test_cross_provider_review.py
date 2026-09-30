from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import FakeExecutor
from stagemesh.persistence import Store
from stagemesh.review import Reviewer


def test_cross_provider_independent_review_preserves_sha_and_records_identity(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Test Cross-Provider Review", source_id="T-4")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # Provider A implements
    builder = FakeExecutor()
    builder.name = "provider-A"
    claim_id = store.acquire_claim(task_id, "builder-worker-1")
    sha = "4444444444444444444444444444444444444444"
    store.add_candidate(task_id, sha, "provider-A", durable_handoff=True)
    store.advance_task(task_id, Stage.VALIDATE)
    store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.REVIEW)

    # Provider B reviews
    reviewer = Reviewer(worker_id="reviewer-worker-2", provider="provider-B")
    status = reviewer.review(store, task_id, sha, tmp_path)

    assert status == EvidenceStatus.PASSED
    evidence = store.has_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    assert evidence is True

    audits = store.audit_events()
    review_events = [e for e in audits if e.get("event") == "evidence.added" and e.get("data", {}).get("kind") == "REVIEW"]
    assert len(review_events) > 0
