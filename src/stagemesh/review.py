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


INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"


def _same_provider(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def independent_review_verified(payload: dict[str, object]) -> bool:
    """True only when a distinct provider's review was actually executed (not a label or a fallback)."""
    implementer = payload.get("implementer_provider")
    reviewer = payload.get("review_provider")
    return bool(
        payload.get("independent_reviewer") is True
        and payload.get("review_execution_invoked") is True
        and isinstance(implementer, str)
        and isinstance(reviewer, str)
        and implementer
        and reviewer
        and not _same_provider(implementer, reviewer)
    )


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
        require_independent: bool = False,
        review_pool: object | None = None,
    ):
        self.review_pool = review_pool
        self.require_independent = require_independent
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
            actor=self.provider_name,
        )
        if self.fail_capacity:
            store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, {"provider": "fake"})
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha, result="capacity")
            return EvidenceStatus.CAPACITY
        produced = store.conn.execute(
            "SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, candidate_sha)
        ).fetchone()
        implementer = str(produced["produced_by"]) if produced is not None else None
        adapter = self.adapter
        considered: list[dict[str, object]] = []
        if self.review_pool is not None:
            # Dynamic selection: first eligible provider that is not the implementer, with automatic fallback.
            adapter, verdicts = self.review_pool.review_adapter(store, task_id, candidate_sha, implementer)
            considered = [v.to_dict() for v in verdicts]
        adapter_name = getattr(adapter, "name", None)
        reviewer_provider = adapter_name or self.provider_name
        same_provider = bool(implementer and _same_provider(reviewer_provider, implementer))
        independent = bool(adapter is not None and reviewer_provider and implementer and not same_provider)
        findings = list(self.findings)
        infrastructure_failure: str | None = None
        review_payload: dict[str, object] = {
            "review_provider": reviewer_provider,
            "implementer_provider": implementer,
            "independent_reviewer": independent,
            "deterministic_contract_gate": True,
            "review_execution_provider": adapter_name,
            "review_execution_invoked": False,
            "independent_review_required": self.require_independent,
        }
        if considered:
            review_payload["review_providers_considered"] = considered
        if same_provider and not self.require_independent:
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
            if adapter is not None and not findings and not (same_provider and self.require_independent):
                review_payload["review_execution_invoked"] = True
                prompt = (
                    f"Review candidate {candidate_sha} for task {task_id} under contract {bound.digest}.\n"
                    f"Objective: {contract.objective}\n"
                    "Return JSON only: {\"decision\":\"PASS\"} or "
                    "{\"decision\":\"FAIL\",\"findings\":[{\"severity\":\"error\",\"message\":\"...\"}]}."
                )
                candidate_review = getattr(adapter, "review_candidate", None)
                if callable(candidate_review):
                    response = candidate_review(prompt, project, candidate_sha)
                else:
                    response = adapter.review(prompt)
                final_provider = getattr(adapter, "name", reviewer_provider)
                if final_provider != reviewer_provider:  # a fallback reviewer produced the answer
                    reviewer_provider = final_provider
                    review_payload["review_provider"] = final_provider
                    review_payload["review_execution_provider"] = final_provider
                review_payload["review_response"] = response
                try:
                    parsed = json.loads(response)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("decision") == INFRASTRUCTURE_FAILURE:
                    infrastructure_failure = str(parsed.get("reason") or "review_provider_failure")
                elif not isinstance(parsed, dict) or parsed.get("decision") not in {"PASS", "FAIL"}:
                    # A reviewer that cannot produce the review JSON failed as a provider; that is not a finding about the code.
                    infrastructure_failure = "malformed_review_output: independent review output must be JSON with decision PASS or FAIL"
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
        if not findings and infrastructure_failure is None and self.require_independent and not independent:
            infrastructure_failure = (
                "review_provider_same_as_implementer"
                if same_provider
                else "implementer_unknown"
                if not implementer
                else "independent_review_unavailable"
            )
        if infrastructure_failure is not None and not findings:
            # Not a code defect: no findings, so no implementation remediation; the task stays in REVIEW.
            review_payload["review_infrastructure_failure"] = infrastructure_failure
            providers_text = "; ".join(f"{c['provider']}: {c['reason']}" for c in considered)
            store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY, review_payload)
            store.add_audit_event(
                "review.infrastructure_failure",
                {
                    "task_id": task_id,
                    "candidate_sha": candidate_sha,
                    "reason": infrastructure_failure,
                    **({"providers": providers_text} if providers_text else {}),
                },
            )
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha, actor=reviewer_provider, result="capacity")
            return EvidenceStatus.CAPACITY
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
            store.finish_execution(execution_id, ExecutionStatus.FAILED, candidate_sha, actor=reviewer_provider, result="findings")
            return EvidenceStatus.FAILED
        store.add_evidence(task_id, candidate_sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, review_payload)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, candidate_sha, actor=reviewer_provider)
        return EvidenceStatus.PASSED
