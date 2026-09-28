from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


@dataclass(frozen=True)
class ReviewFinding:
    identity: str
    severity: str
    message: str


class Reviewer:
    def __init__(self, fail_capacity: bool = False):
        self.fail_capacity = fail_capacity

    def review(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.REVIEW,
            candidate_sha=candidate_sha,
        )
        if self.fail_capacity:
            store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, {"provider": "fake"})
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha)
            return EvidenceStatus.CAPACITY
        store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, {"reviewer": "builtin"})
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, candidate_sha)
        return EvidenceStatus.PASSED
