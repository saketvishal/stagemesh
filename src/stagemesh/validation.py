from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .contracts import (
    ContractError,
    changed_files,
    declarative_invariants,
    evaluate_contract,
    scope_classification,
)
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus
from .focused_validation import discover_focused_gates, with_focused_gates
from .git import GitError
from .persistence import Store
from .process_identity import owner_execution_fields
from .remediation import finding_identity
from .validation_reuse import GATE_REUSE_POLICY_VERSION, GateReuse, candidate_tree, load_prior_passes, select_files
from .validation_plan import (
    contract_with_browser_e2e_gates,
    derive_validation_plan,
    with_required_checks,
)


def _integration_ref(project: Path) -> str | None:
    try:
        from .baseline import resolve_integration_ref
        from .config import load_config

        return resolve_integration_ref(project, load_config(project).integration_ref)
    except Exception:  # noqa: BLE001 - selection hygiene must never fail validation; without a ref nothing is excluded
        return None


class Validator:
    def validate(self, store: Store, task_id: str, candidate_sha: str, project: Path) -> EvidenceStatus:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=None,
            kind=ExecutionKind.VALIDATION,
            candidate_sha=candidate_sha,
            actor="stagemesh-validator",
            **owner_execution_fields(),
        )
        bound = None
        try:
            bound = contract_for_candidate(store, task_id, candidate_sha, project)
            contract = bound.contract
            try:
                inspected_files = tuple(changed_files(project, candidate_sha, bound.baseline_sha))
            except (GitError, OSError):
                inspected_files = ()
            # The bound baseline measures the change and the scope check sees all of it. Automatic selection (browser end-to-end,
            # nearby tests, risk class) uses only the candidate's own files when the baseline is demonstrably behind the integration ref.
            selection = select_files(
                project, candidate_sha, bound.baseline_sha, inspected_files, _integration_ref(project), contract.gates,
            )
            selected_files = selection.selection_files
            declared_names = {gate.name for gate in contract.gates}
            contract = contract_with_browser_e2e_gates(project, contract, selected_files)
            focused_gates = discover_focused_gates(project, selected_files, contract, candidate_sha=candidate_sha)
            contract = with_focused_gates(contract, focused_gates)
            exploratory = tuple(gate.name for gate in contract.gates if gate.name not in declared_names)
            plan = with_required_checks(contract, derive_validation_plan(contract, selected_files))
            gate_reuse = GateReuse(candidate_tree(project, candidate_sha), bound.digest, load_prior_passes(store, task_id))
            evaluation = evaluate_contract(
                project,
                candidate_sha,
                replace(contract, validation_classification=plan.classification, validation_risk=plan.risk_level,
                        validation_escalation_reasons=plan.escalation_reasons),
                baseline_sha=bound.baseline_sha,
                run_gates=True,
                gate_reuse=gate_reuse,
            )
            findings = list(evaluation.findings)
            executed = tuple(gate.name for gate in evaluation.gates)
            missing: tuple[str, ...] = ()
            if contract.explicit:
                # Plan labels are not evidence: every planned check must have actually executed.
                missing = tuple(dict.fromkeys(name for name in plan.planned_checks if name not in executed))
                findings.extend(
                    {
                        "severity": "error",
                        "code": "planned_check_missing_command",
                        "message": f"planned check '{name}' has no executable gate and did not run",
                    }
                    for name in missing
                )
                if contract.acceptance_criteria and not executed:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "acceptance_criteria_without_executable_gate",
                            "message": "acceptance criteria require at least one executed validation gate",
                        }
                    )
            status = (
                EvidenceStatus.PASSED
                if evaluation.passed and len(findings) == len(evaluation.findings)
                else EvidenceStatus.FAILED
            )
            payload = {
                "validator": "contract",
                **bound.evidence_payload(),
                "validation_plan": plan.to_dict(),
                "focused_validation": {"discovered": [gate.name for gate in focused_gates], "required_gates_replaced": False},
                "objective": contract.objective,
                "review_invariants": list(declarative_invariants(contract)),
                "changed_files": list(evaluation.changed_files),
                "scope": scope_classification(contract, evaluation.changed_files),
                "findings": findings,
                "validation_checks": {
                    "planned": list(plan.planned_checks),
                    "executed": list(executed),
                    "missing": list(missing),
                    "required": [name for name in executed if name not in exploratory],
                    "exploratory": [name for name in executed if name in exploratory],
                },
                "selection": selection.to_dict(),
                "validation_reuse": {
                    "policy_version": GATE_REUSE_POLICY_VERSION,
                    "ran": [gate.name for gate in evaluation.gates if gate.reused_from is None],
                    "reused": [
                        {"gate": gate.name, "from": gate.reused_from}
                        for gate in evaluation.gates
                        if gate.reused_from is not None
                    ],
                    "note": "a prior PASSED result is reused only for an identical tree, contract, command, environment and policy; "
                    "hosted checks and independent review never reuse local results",
                },
                "gates": [
                    {
                        "name": gate.name,
                        "status": gate.status,
                        "command": list(gate.command),
                        "returncode": gate.returncode,
                        "failure_kind": gate.failure_kind,
                        **gate.provenance(),
                    }
                    for gate in evaluation.gates
                ],
            }
        except ContractError as exc:
            status = EvidenceStatus.FAILED
            payload = {"validator": "contract", **(bound.evidence_payload() if bound else {}),
                       "findings": [{"code": "invalid_contract", "message": str(exc)}]}
        store.add_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, status, payload)
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
        elif status is EvidenceStatus.PASSED:
            # Only a fresh passed validation can supersede old parser errors.
            # Other validation/review findings remain open for actual remediation.
            for previous in store.conn.execute(
                "SELECT id, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='VALIDATION' AND status='FAILED'",
                (task_id, candidate_sha),
            ).fetchall():
                for item in json.loads(previous["payload"]).get("findings", []):
                    if item.get("code") != "invalid_contract":
                        continue
                    identity = finding_identity(candidate_sha, str(item.get("message", "")), item.get("path"))
                    if any(str(f["id"]) == identity for f in store.open_findings_for_candidate(task_id, candidate_sha)):
                        store.close_finding(identity)
                        record_audit(store, "contract.invalid_finding_resolved", {
                            "task_id": task_id, "candidate_sha": candidate_sha, "finding_id": identity,
                            "previous_evidence_id": previous["id"], **bound.evidence_payload(),
                            "reason": "fresh validation passed the explicitly repaired contract",
                        })
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED if status is EvidenceStatus.PASSED else ExecutionStatus.FAILED,
            candidate_sha,
        )
        return status
