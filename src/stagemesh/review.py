from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .contract_binding import contract_for_candidate
from .contracts import ContractError, evaluate_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


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
        provider_name: str = "builtin-deterministic-fallback",
    ):
        self.fail_capacity = fail_capacity
        self.findings = findings or []
        self.provider_name = provider_name

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
        candidate = store.latest_candidate(task_id)
        implementer = str(candidate["produced_by"]) if candidate is not None and candidate["sha"] == candidate_sha else None
        deterministic_fallback = self.provider_name == "builtin-deterministic-fallback"
        independent = bool(self.provider_name and self.provider_name != implementer and not deterministic_fallback)
        findings = list(self.findings)
        review_payload: dict[str, object] = {
            "review_provider": self.provider_name,
            "implementer_provider": implementer,
            "independent_reviewer": independent,
            "deterministic_contract_gate": deterministic_fallback,
        }
        if implementer and self.provider_name == implementer:
            findings.append(
                ReviewFinding(
                    finding_identity(candidate_sha, "review provider must differ from implementer"),
                    "error",
                    "review provider must differ from implementer",
                )
            )
        if not findings:
            try:
                bound = contract_for_candidate(store, task_id, candidate_sha, project)
                contract = bound.contract
                evaluation = evaluate_contract(
                    project,
                    candidate_sha,
                    contract,
                    baseline_sha=bound.baseline_sha,
                    run_gates=False,
                )
                review_payload.update(
                    {
                        **bound.evidence_payload(),
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
