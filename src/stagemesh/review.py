from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class ReviewAdapter(Protocol):
    name: str

    def review(self, prompt: str) -> str:
        ...


class Reviewer:
    def __init__(
        self,
        fail_capacity: bool = False,
        findings: list[ReviewFinding] | None = None,
        provider_name: str = "builtin-deterministic-fallback",
        adapter: ReviewAdapter | None = None,
    ):
        self.fail_capacity = fail_capacity
        self.findings = findings or []
        self.provider_name = provider_name
        self.adapter = adapter

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
        adapter_name = getattr(self.adapter, "name", None)
        reviewer_provider = adapter_name or self.provider_name
        independent = bool(self.adapter is not None and reviewer_provider and reviewer_provider != implementer)
        findings = list(self.findings)
        review_payload: dict[str, object] = {
            "review_provider": reviewer_provider,
            "implementer_provider": implementer,
            "independent_reviewer": independent,
            "deterministic_contract_gate": True,
            "review_execution_provider": adapter_name,
            "review_execution_invoked": self.adapter is not None,
        }
        if implementer and reviewer_provider == implementer:
            findings.append(
                ReviewFinding(
                    finding_identity(candidate_sha, "review provider must differ from implementer"),
                    "error",
                    "review provider must differ from implementer",
                )
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
            if self.adapter is not None and not findings:
                prompt = (
                    f"Review candidate {candidate_sha} for task {task_id} under contract {bound.digest}.\n"
                    f"Objective: {contract.objective}\n"
                    "Return JSON only: {\"decision\":\"PASS\"} or "
                    "{\"decision\":\"FAIL\",\"findings\":[{\"severity\":\"error\",\"message\":\"...\"}]}."
                )
                candidate_review = getattr(self.adapter, "review_candidate", None)
                if callable(candidate_review):
                    response = candidate_review(prompt, project, candidate_sha)
                else:
                    response = self.adapter.review(prompt)
                review_payload["review_response"] = response
                try:
                    parsed = json.loads(response)
                except json.JSONDecodeError:
                    parsed = None
                if not isinstance(parsed, dict) or parsed.get("decision") not in {"PASS", "FAIL"}:
                    findings.append(
                        ReviewFinding(
                            finding_identity(candidate_sha, "malformed independent review output"),
                            "error",
                            "independent review output must be JSON with decision PASS or FAIL",
                        )
                    )
                elif parsed["decision"] == "FAIL":
                    raw_findings = parsed.get("findings")
                    if isinstance(raw_findings, list) and raw_findings:
                        for item in raw_findings:
                            if isinstance(item, dict):
                                message = str(item.get("message") or "independent review failed")
                                severity = str(item.get("severity") or "error")
                            else:
                                message = str(item)
                                severity = "error"
                            findings.append(ReviewFinding(finding_identity(candidate_sha, message), severity, message))
                    else:
                        findings.append(
                            ReviewFinding(
                                finding_identity(candidate_sha, "independent review failed"),
                                "error",
                                "independent review failed",
                            )
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
