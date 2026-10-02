from __future__ import annotations

from pathlib import Path

from .contracts import ContractError, evaluate_contract, load_contract
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
        missing = [
            kind.value
            for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW)
            if not store.has_evidence(task_id, candidate_sha, kind, EvidenceStatus.PASSED)
        ]
        findings: list[dict[str, object]] = []
        if missing:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_required_evidence",
                    "message": f"candidate is missing required passing evidence: {', '.join(missing)}",
                }
            )
        try:
            contract = load_contract(project, task_id)
            evaluation = evaluate_contract(project, candidate_sha, contract, run_gates=False)
            findings.extend(evaluation.findings)
        except ContractError as exc:
            findings.append({"severity": "error", "code": "invalid_contract", "message": str(exc)})

        status = EvidenceStatus.PASSED if not findings else EvidenceStatus.FAILED
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.INTEGRATION,
            status,
            {"integrator": "builtin", "findings": findings},
        )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
