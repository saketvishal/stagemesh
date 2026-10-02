from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError, evaluate_contract, load_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


@dataclass(frozen=True)
class ReviewFinding:
    identity: str
    severity: str
    message: str


class Reviewer:
    def __init__(self, fail_capacity: bool = False, findings: list[ReviewFinding] | None = None):
        self.fail_capacity = fail_capacity
        self.findings = findings or []

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
        findings = list(self.findings)
        review_payload: dict[str, object] = {"reviewer": "deterministic-contract"}
        if not findings:
            try:
                contract = load_contract(project, task_id)
                evaluation = evaluate_contract(project, candidate_sha, contract, run_gates=False)
                review_payload.update(
                    {
                        "objective": contract.objective,
                        "changed_files": list(evaluation.changed_files),
                        "findings": list(evaluation.findings),
                    }
                )
                findings.extend(
                    ReviewFinding(
                        identity=finding_identity(candidate_sha, item["message"], item.get("path")),
                        severity=str(item.get("severity", "error")),
                        message=str(item["message"]),
                    )
                    for item in evaluation.findings
                )
            except ContractError as exc:
                findings.append(
                    ReviewFinding(
                        finding_identity(candidate_sha, str(exc)),
                        "error",
                        f"invalid change contract: {exc}",
                    )
                )
                review_payload["findings"] = [{"code": "invalid_contract", "message": str(exc)}]
        if findings:
            for finding in findings:
                identity = finding.identity or finding_identity(candidate_sha, finding.message)
                store.upsert_finding(identity, task_id, candidate_sha, finding.severity, finding.message)
            store.add_evidence(
                task_id,
                candidate_sha,
                EvidenceKind.REVIEW,
                EvidenceStatus.FAILED,
                {**review_payload, "finding_count": len(findings)},
            )
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha)
            return EvidenceStatus.FAILED
        store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, review_payload)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, candidate_sha)
        return EvidenceStatus.PASSED
