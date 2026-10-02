from __future__ import annotations

from pathlib import Path

from .contract_binding import contract_for_candidate
from .contracts import ContractError, changed_files, evaluate_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .git import GitError
from .persistence import Store
from .remediation import finding_identity
from .validation_plan import derive_validation_plan, planned_contract


class Validator:
    def validate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
        )
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            contract = bound.contract
            try:
                inspected_files = tuple(changed_files(project, candidate_sha, bound.baseline_sha))
            except (GitError, OSError):
                inspected_files = ()
            plan = derive_validation_plan(contract, inspected_files)
            evaluation = evaluate_contract(
                project,
                candidate_sha,
                planned_contract(contract, plan),
                baseline_sha=bound.baseline_sha,
                run_gates=True,
            )
            status = EvidenceStatus.PASSED if evaluation.passed else EvidenceStatus.FAILED
            payload = {
                "validator": "contract",
                **bound.evidence_payload(),
                "validation_plan": plan.to_dict(),
                "objective": contract.objective,
                "changed_files": list(evaluation.changed_files),
                "findings": list(evaluation.findings),
                "gates": [
                    {
                        "name": gate.name,
                        "status": gate.status,
                        "command": list(gate.command),
                        "returncode": gate.returncode,
                    }
                    for gate in evaluation.gates
                ],
            }
        except ContractError as exc:
            status = EvidenceStatus.FAILED
            payload = {"validator": "contract", "findings": [{"code": "invalid_contract", "message": str(exc)}]}
        if status is EvidenceStatus.FAILED:
            for item in payload.get("findings", []):
                message = str(item.get("message", "validation failed"))
                store.upsert_finding(
                    finding_identity(candidate_sha, message, item.get("path")),
                    task_id,
                    candidate_sha,
                    str(item.get("severity", "error")),
                    message,
                )
        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, payload)
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
