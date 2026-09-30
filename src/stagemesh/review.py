from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import record_audit
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


class SelfReviewError(ValueError):
    """Raised when a worker attempts to review its own implementation candidate."""


@dataclass(frozen=True)
class ReviewFinding:
    identity: str
    severity: str
    message: str


class Reviewer:
    def __init__(
        self,
        fail_capacity: bool = False,
        findings: list[ReviewFinding] | None = None,
        worker_id: str | None = None,
        provider: str | None = None,
    ):
        self.fail_capacity = fail_capacity
        self.findings = findings or []
        self.worker_id = worker_id or "reviewer-worker"
        self.provider = provider or "builtin-reviewer"

    def review(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        candidate = store.latest_candidate(task_id)
        if candidate is not None:
            # Check if builder worker identity matches reviewer worker identity
            row = store.conn.execute(
                "SELECT worker_id FROM claims WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            builder_worker = row["worker_id"] if row else None
            if builder_worker and builder_worker == self.worker_id:
                raise SelfReviewError(
                    f"Worker '{self.worker_id}' cannot review its own implementation candidate ({candidate_sha})"
                )

        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.REVIEW,
            candidate_sha=candidate_sha,
        )

        evidence_payload = {
            "reviewer_id": self.worker_id,
            "provider": self.provider,
            "candidate_sha": candidate_sha,
        }

        if self.fail_capacity:
            store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, evidence_payload)
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha)
            record_audit(store, "review.capacity_failure", {"task_id": task_id, "provider": self.provider})
            return EvidenceStatus.CAPACITY

        if self.findings:
            for finding in self.findings:
                identity = finding.identity or finding_identity(candidate_sha, finding.message)
                store.upsert_finding(identity, task_id, candidate_sha, finding.severity, finding.message)
            evidence_payload["findings"] = len(self.findings)
            store.add_evidence(
                task_id,
                candidate_sha,
                EvidenceKind.REVIEW,
                EvidenceStatus.FAILED,
                evidence_payload,
            )
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha)
            record_audit(store, "review.findings_recorded", {"task_id": task_id, "count": len(self.findings)})
            return EvidenceStatus.FAILED

        store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, evidence_payload)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, candidate_sha)
        record_audit(store, "evidence.added", {"task_id": task_id, "kind": "REVIEW", "status": "PASSED", "provider": self.provider})
        return EvidenceStatus.PASSED
