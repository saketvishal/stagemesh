from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .contracts import (
    ContractError,
    candidate_workspace,
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
    DOCS_ONLY,
    contract_with_browser_e2e_gates,
    derive_validation_plan,
    with_required_checks,
)
from .validation_services import (
    ValidationServiceError, discover_validation_services, run_configured_evaluations,
)
from .e2e_evidence import (
    E2EEvidenceManifest, E2EEvidenceValidationError, validate_e2e_evidence_manifest,
)


def record_e2e_evidence_in_validation(
    payload: dict[str, Any],
    manifest: E2EEvidenceManifest,
    bound: Any,
    *,
    project: Path,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Bind and validate an E2E evidence manifest into the validation evidence payload."""
    validate_e2e_evidence_manifest(
        manifest,
        bound,
        expected_candidate_sha=payload.get("candidate_sha") or bound.candidate_sha,
        expected_service_fingerprint=discover_validation_services(project).fingerprint,
        artifact_root=artifact_root,
    )
    payload["e2e_evidence"] = manifest.to_dict()
    return payload



def _integration_ref(project: Path) -> str | None:
    try:
        from .baseline import resolve_integration_ref
        from .config import load_config

        return resolve_integration_ref(project, load_config(project).integration_ref)
    except Exception:  # noqa: BLE001 - selection hygiene must never fail validation; without a ref nothing is excluded
        return None


class Validator:
    def validate(
        self, store: Store, task_id: str, candidate_sha: str, project: Path,
        *, e2e_evidence: E2EEvidenceManifest | None = None,
        artifact_root: Path | None = None,
        prerequisite_manifest: dict[str, Any] | None = None,
    ) -> EvidenceStatus:
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
            services = discover_validation_services(project)
            service_fingerprint = services.fingerprint
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

            manifest_load_error = False
            if prerequisite_manifest is None:
                spec_manifest_paths = [
                    project / ".stagemesh" / "contracts" / f"{task_id}.spec_manifest.json",
                    project / ".stagemesh" / "spec-manifest.json",
                ]
                for p in spec_manifest_paths:
                    if p.is_file():
                        try:
                            prerequisite_manifest = json.loads(p.read_text(encoding="utf-8"))
                            break
                        except (OSError, ValueError) as exc:
                            findings.append({
                                "severity": "error",
                                "code": "spec_manifest_load_failure",
                                "message": f"cannot read prerequisite manifest from {p}: {exc}",
                            })
                            manifest_load_error = True
                            break

            is_spec_governed = bool(
                contract.spec_refs
                and any("openspec/" in r or "adapter.json" in r or r.startswith("specs/") for r in contract.spec_refs)
            ) or any("openspec/" in f or f == "adapter.json" for f in inspected_files) or any(
                "openspec/" in f or f == "adapter.json" for f in contract.allowed_files
            )

            if prerequisite_manifest is None and is_spec_governed and not manifest_load_error:
                findings.append({
                    "severity": "error",
                    "code": "spec_manifest_missing",
                    "message": "specification modification requires pinned adapter validation and prerequisite manifest",
                })

            spec_verdict = None
            if prerequisite_manifest is not None:
                from .spec_integrity import resolve_pinned_adapter, validate_prerequisite_manifest

                if isinstance(prerequisite_manifest, dict):
                    adapter_info = prerequisite_manifest.get("adapter", {})
                else:
                    adapter_info = {}
                if isinstance(adapter_info, dict) and isinstance(prerequisite_manifest, dict):
                    name = adapter_info.get("name")
                    version = adapter_info.get("version")
                    if isinstance(name, str) and isinstance(version, str):
                        try:
                            adapter = resolve_pinned_adapter(
                                name,
                                version,
                                contract_path=adapter_info.get("contract_path")
                                or prerequisite_manifest.get("contract_path")
                                or "adapter.json",
                            )
                            with candidate_workspace(project, candidate_sha) as candidate_project:
                                adapter.discover(candidate_project)
                        except Exception as exc:
                            findings.append({
                                "severity": "error",
                                "code": "spec_adapter_validation_failed",
                                "message": f"pinned adapter validation failed: {exc}",
                            })

                spec_verdict = validate_prerequisite_manifest(
                    prerequisite_manifest,
                    allowed_files=contract.allowed_files,
                    expected_candidate_revision=candidate_sha,
                    expected_base_revision=bound.baseline_sha,
                    expected_changed_files=inspected_files,
                )
                if not spec_verdict.ok:
                    for err in spec_verdict.errors:
                        findings.append({"severity": "error", "code": "spec_governance_failure", "message": err})
                if spec_verdict.review_findings:
                    for rf in spec_verdict.review_findings:
                        findings.append({"severity": "warning", "code": "spec_semantic_overlap", "message": rf})

            has_error_findings = any(f.get("severity") == "error" for f in findings)
            status = (
                EvidenceStatus.PASSED
                if evaluation.passed and not has_error_findings
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
                },
                "check_roles": {
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
            if spec_verdict is not None:
                payload["spec_governance"] = spec_verdict.to_dict()

            evaluation_services = services
            if plan.classification == DOCS_ONLY:
                evaluation_services = replace(services, services=tuple(
                    service for service in services.services if service.name not in services.conventional_services
                ))
            if any(s.kind in {"browser", "api"} for s in evaluation_services.services):
                output_root = (artifact_root or project / ".stagemesh" / "artifacts" / "e2e") / execution_id
                try:
                    manifests, e2e_gates, e2e_findings = run_configured_evaluations(
                        project, bound, evaluation_services, output_root,
                    )
                    payload["e2e_evaluations"] = manifests
                    payload["e2e_artifact_root"] = str(output_root)
                    payload["gates"].extend(e2e_gates)
                    findings.extend(e2e_findings)
                    if e2e_findings:
                        status = EvidenceStatus.FAILED
                except (ValidationServiceError, GitError, OSError, E2EEvidenceValidationError) as exc:
                    from .remediation import bounded_failure_text

                    status = EvidenceStatus.FAILED
                    findings.append({"severity": "error", "code": "gate_failed",
                                     "message": "configured E2E evaluation unavailable",
                                     "failure_kind": "environment", "stderr": bounded_failure_text(exc)})
            if e2e_evidence is not None:
                try:
                    validate_e2e_evidence_manifest(
                        e2e_evidence, bound, expected_candidate_sha=candidate_sha,
                        expected_service_fingerprint=service_fingerprint, artifact_root=artifact_root,
                    )
                    matching = [gate for gate in evaluation.gates if gate.command == e2e_evidence.command]
                    expected_status = "PASSED" if e2e_evidence.result == "PASS" else "FAILED"
                    if not matching or any(
                        gate.status != expected_status or gate.returncode is None
                        or (gate.returncode == 0) != (e2e_evidence.result == "PASS")
                        for gate in matching
                    ):
                        raise E2EEvidenceValidationError("E2E command/result does not match executed gates")
                    record_e2e_evidence_in_validation(
                        payload, e2e_evidence, bound, project=project, artifact_root=artifact_root,
                    )
                    if e2e_evidence.result == "FAIL":
                        status = EvidenceStatus.FAILED
                except E2EEvidenceValidationError as exc:
                    status = EvidenceStatus.FAILED
                    findings.append({"severity": "error", "code": "invalid_e2e_evidence", "message": str(exc)})
        except ValidationServiceError as exc:
            from .remediation import bounded_failure_text

            status = EvidenceStatus.FAILED
            payload = {"validator": "contract", **(bound.evidence_payload() if bound else {}),
                       "findings": [{"code": "gate_failed", "message": "invalid E2E prerequisites",
                                     "failure_kind": "environment", "stderr": bounded_failure_text(exc)}]}
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
            for item in payload.get("findings", []):
                if item.get("severity") == "warning":
                    message = str(item.get("message", "warning"))
                    store.upsert_finding(
                        finding_identity(candidate_sha, message, item.get("path")),
                        task_id,
                        candidate_sha,
                        "warning",
                        message,
                    )
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
