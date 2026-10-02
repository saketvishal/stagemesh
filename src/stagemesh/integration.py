from __future__ import annotations

import json
from pathlib import Path

from .contract_binding import contract_for_candidate
from .contracts import ContractError, evaluate_contract
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .persistence import Store
from .remediation import finding_identity


class Integrator:
    def integrate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.INTEGRATION,
            candidate_sha=candidate_sha,
        )
        findings: list[dict[str, object]] = []
        payload: dict[str, object] = {"integrator": "builtin"}
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            missing = [
                kind.value
                for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW)
                if not _has_exact_bound_evidence(store, task_id, candidate_sha, kind, bound)
            ]
            if missing:
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_required_bound_evidence",
                        "message": "candidate is missing required passing bound evidence: " + ", ".join(missing),
                    }
                )
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
        if status is EvidenceStatus.FAILED:
            for item in findings:
                message = str(item.get("message", "integration failed"))
                store.upsert_finding(
                    finding_identity(candidate_sha, message, item.get("path")),
                    task_id,
                    candidate_sha,
                    str(item.get("severity", "error")),
                    message,
                )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status


def _has_exact_bound_evidence(store: Store, task_id: str, candidate_sha: str, kind: EvidenceKind, bound: object) -> bool:
    rows = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=?",
        (task_id, candidate_sha, kind, EvidenceStatus.PASSED),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            payload.get("contract_hash") == bound.digest
            and payload.get("contract_version") == bound.version
            and payload.get("baseline_sha") == bound.baseline_sha
            and payload.get("candidate_sha") == candidate_sha
        ):
            return True
    return False
