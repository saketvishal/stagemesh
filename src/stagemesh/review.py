from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .change_control import (
    ChangeContract,
    ChangeControlError,
    contract_definition_violations,
    contract_violations,
    diff_summary,
    git_diff_check,
)
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


@dataclass(frozen=True)
class ReviewFinding:
    identity: str
    severity: str
    message: str


class Reviewer:
    """Independent deterministic review gate.

    The reviewer intentionally does not trust implementation-agent output. It
    recomputes the candidate diff against the task contract and also requires
    passing validation evidence for the exact candidate SHA.
    """

    def __init__(
        self,
        fail_capacity: bool = False,
        findings: list[ReviewFinding] | None = None,
        require_contract: bool = False,
    ):
        self.fail_capacity = fail_capacity
        self.findings = findings or []
        self.require_contract = require_contract

    def review(
        self,
        store: Store,
        task_id: str,
        candidate_sha: str,
        project: Path,
    ) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.REVIEW,
            candidate_sha=candidate_sha,
        )
        if self.fail_capacity:
            store.add_evidence(
                task_id,
                candidate_sha,
                EvidenceKind.REVIEW,
                EvidenceStatus.CAPACITY,
                {"provider": "fake"},
            )
            store.finish_execution(
                execution_id,
                ExecutionStatus.FAILED,
                candidate_sha,
            )
            return EvidenceStatus.CAPACITY

        findings = list(self.findings)
        contract = None
        try:
            if not store.has_evidence(
                task_id,
                candidate_sha,
                EvidenceKind.VALIDATION,
                EvidenceStatus.PASSED,
            ):
                findings.append(
                    ReviewFinding(
                        "",
                        "ERROR",
                        "review requires passing validation evidence for the exact candidate SHA",
                    )
                )
            contract = ChangeContract.load(project, task_id)
            if contract is None and self.require_contract:
                findings.append(
                    ReviewFinding("", "ERROR", "missing mandatory change contract")
                )
            if contract is not None and self.require_contract:
                findings.extend(
                    ReviewFinding("", "ERROR", message)
                    for message in contract_definition_violations(contract)
                )
            if contract is not None:
                summary = diff_summary(project, candidate_sha)
                for message in contract_violations(contract, summary):
                    findings.append(ReviewFinding("", "ERROR", message))
                diff_check = git_diff_check(project, candidate_sha)
                if diff_check.returncode != 0:
                    findings.append(
                        ReviewFinding("", "ERROR", "git diff check failed during independent review")
                    )
        except ChangeControlError as exc:
            findings.append(ReviewFinding("", "ERROR", str(exc)))

        if findings:
            for finding in findings:
                identity = finding.identity or finding_identity(
                    candidate_sha,
                    finding.message,
                )
                store.upsert_finding(
                    identity,
                    task_id,
                    candidate_sha,
                    finding.severity,
                    finding.message,
                )
            store.add_evidence(
                task_id,
                candidate_sha,
                EvidenceKind.REVIEW,
                EvidenceStatus.FAILED,
                {
                    "reviewer": "builtin-independent",
                    "contract_required": self.require_contract,
                    "contract_present": contract is not None,
                    "findings": len(findings),
                },
            )
            store.finish_execution(
                execution_id,
                ExecutionStatus.FAILED,
                candidate_sha,
            )
            return EvidenceStatus.FAILED

        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.REVIEW,
            EvidenceStatus.PASSED,
            {
                "reviewer": "builtin-independent",
                "contract_required": self.require_contract,
                "contract_present": contract is not None,
            },
        )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED,
            candidate_sha,
        )
        return EvidenceStatus.PASSED
