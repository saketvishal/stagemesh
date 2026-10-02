from __future__ import annotations

from pathlib import Path

from .contract_binding import contract_for_candidate
from .contracts import ContractError, evaluate_contract
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
        payload: dict[str, object] = {"integrator": "builtin"}
        if missing:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_required_evidence",
                    "message": f"candidate is missing required passing evidence: {', '.join(missing)}",
                }
            )
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
            payload.update(bound.evidence_payload())
            findings.extend(evaluation.findings)
        except ContractError as exc:
            findings.append({"severity": "error", "code": "invalid_contract", "message": str(exc)})

        status = EvidenceStatus.PASSED if not findings else EvidenceStatus.FAILED
        store.add_evidence(
            task_id,
            candidate_sha,
            EvidenceKind.INTEGRATION,
            status,
            {**payload, "findings": findings},
        )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
