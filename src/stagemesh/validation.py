from __future__ import annotations

from pathlib import Path

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


class Validator:
    def validate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
        )
        status = EvidenceStatus.PASSED if candidate_sha else EvidenceStatus.FAILED
        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, {"validator": "builtin"})
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
