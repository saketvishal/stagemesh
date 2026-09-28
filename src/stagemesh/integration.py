from __future__ import annotations

from pathlib import Path

from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store


class Integrator:
    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.INTEGRATION,
            candidate_sha=candidate_sha,
        )
        store.add_evidence(task_id, candidate_sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED, {"integrator": "builtin"})
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, candidate_sha)
        return EvidenceStatus.PASSED
