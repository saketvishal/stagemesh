"""Evidence-based routing policy proposals.

This module is deliberately outside the coordinator hot path. It can summarize
historical execution evidence and produce auditable recommendations, but it has
no function that mutates the enforced routing policy. Promotion remains an
explicit operator action in configuration/version-control space.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.models import BuildRunnerExecution, BuildTask
from build_coordinator.runner.routing import RoutingPolicy, WorkerEvidence

SUCCESS_STATUSES = frozenset({"SUCCEEDED"})
FAILURE_STATUSES = frozenset({"FAILED", "HUMAN_ACTION_REQUIRED", "TERMINATED", "LOST"})
TERMINAL_STATUSES = SUCCESS_STATUSES | FAILURE_STATUSES
APPROVING_REVIEW_VERDICTS = frozenset({"GREEN", "GREEN_WITH_NOTES"})
REMEDIATION_REVIEW_VERDICTS = frozenset({"REMEDIATION_REQUIRED"})
TRUSTWORTHY_COST_SOURCES = frozenset({"metered", "billing_export", "operator_verified"})
MIN_STRONG_SAMPLE_SIZE = 10

FORBIDDEN_POLICY_FIELDS = frozenset(
    {
        "review_policy",
        "review_required",
        "independent_review_required",
        "permissions",
        "required_permissions",
        "capabilities",
        "required_capabilities",
        "security_review",
        "scm_write",
    }
)


@dataclass(frozen=True)
class NormalizedEvidence:
    """A provider/runtime/task observation safe to use in proposal reports."""

    execution_id: str
    task_id: str
    worker_id: str
    provider: str | None
    runtime: str
    role: str
    status: str
    latency_ms: float | None = None
    review_outcome: str | None = None
    remediation_required: bool = False
    capability_fit: float | None = None
    cost: float | None = None
    cost_trustworthy: bool = False
    task_risk_class: str = "MEDIUM"
    review_policy: str = "SELF"
    platform: str | None = None
    compatibility: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "provider": self.provider,
            "runtime": self.runtime,
            "role": self.role,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "review_outcome": self.review_outcome,
            "remediation_required": self.remediation_required,
            "capability_fit": self.capability_fit,
            "cost": self.cost,
            "cost_trustworthy": self.cost_trustworthy,
            "task_risk_class": self.task_risk_class,
            "review_policy": self.review_policy,
            "platform": self.platform,
            "compatibility": self.compatibility,
        }


@dataclass(frozen=True)
class EvidenceSummary:
    worker_id: str
    provider: str | None
    runtime: str
    role: str
    sample_size: int
    success_rate: float | None
    failure_rate: float | None
    mean_latency_ms: float | None
    remediation_rate: float | None
    review_approval_rate: float | None
    capability_fit: float | None
    cost: float | None
    cost_trustworthy: bool
    risk_classes: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    compatibility: tuple[str, ...] = ()
    evidence_strength: str = "none"

    def to_worker_evidence(self) -> WorkerEvidence:
        return WorkerEvidence(
            reliability=self.success_rate,
            latency_ms=self.mean_latency_ms,
            capability_fit=self.capability_fit,
            failure_rate=self.failure_rate,
            cost=self.cost if self.cost_trustworthy else None,
            sample_size=self.sample_size,
            source="policy_learning_summary",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "provider": self.provider,
            "runtime": self.runtime,
            "role": self.role,
            "sample_size": self.sample_size,
            "success_rate": self.success_rate,
            "failure_rate": self.failure_rate,
            "mean_latency_ms": self.mean_latency_ms,
            "remediation_rate": self.remediation_rate,
            "review_approval_rate": self.review_approval_rate,
            "capability_fit": self.capability_fit,
            "cost": self.cost,
            "cost_trustworthy": self.cost_trustworthy,
            "risk_classes": list(self.risk_classes),
            "platforms": list(self.platforms),
            "compatibility": list(self.compatibility),
            "evidence_strength": self.evidence_strength,
        }


@dataclass(frozen=True)
class PolicyEvaluation:
    method: str
    sample_size: int
    baseline_success_rate: float | None
    proposed_success_rate: float | None
    baseline_mean_latency_ms: float | None
    proposed_mean_latency_ms: float | None
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "sample_size": self.sample_size,
            "baseline_success_rate": self.baseline_success_rate,
            "proposed_success_rate": self.proposed_success_rate,
            "baseline_mean_latency_ms": self.baseline_mean_latency_ms,
            "proposed_mean_latency_ms": self.proposed_mean_latency_ms,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True)
class PolicyProposal:
    proposal_id: str
    base_policy_id: str
    base_policy_version: str
    proposed_policy_id: str
    proposed_policy_version: str
    status: str
    changes: tuple[dict[str, Any], ...]
    explanation: tuple[str, ...]
    evidence: tuple[EvidenceSummary, ...]
    evaluation: PolicyEvaluation
    guardrails: tuple[str, ...] = (
        "proposal_only_not_enforced",
        "explicit_operator_approval_required",
        "no_black_box_provider_selection",
        "review_security_permission_requirements_not_weakened",
        "sparse_data_labeled_as_limited",
    )
    rollback: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "base_policy_id": self.base_policy_id,
            "base_policy_version": self.base_policy_version,
            "proposed_policy_id": self.proposed_policy_id,
            "proposed_policy_version": self.proposed_policy_version,
            "status": self.status,
            "changes": [dict(change) for change in self.changes],
            "explanation": list(self.explanation),
            "evidence": [item.to_dict() for item in self.evidence],
            "evaluation": self.evaluation.to_dict(),
            "guardrails": list(self.guardrails),
            "rollback": dict(self.rollback),
        }


@dataclass(frozen=True)
class PolicyApprovalRecord:
    proposal_id: str
    approved_by: str
    approved_at: str
    approved_policy_id: str
    approved_policy_version: str
    rollback: dict[str, Any]
    audit_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approved_policy_id": self.approved_policy_id,
            "approved_policy_version": self.approved_policy_version,
            "rollback": dict(self.rollback),
            "audit_note": self.audit_note,
        }


class PolicyLearningGuardrailError(ValueError):
    """Raised when a proposed change would violate policy-learning guardrails."""


def collect_normalized_evidence(session: Session) -> tuple[NormalizedEvidence, ...]:
    rows = session.execute(select(BuildRunnerExecution, BuildTask).join(BuildTask)).all()
    return tuple(_normalize_execution(execution, task) for execution, task in rows if execution.status in TERMINAL_STATUSES)


def summarize_evidence(observations: Iterable[NormalizedEvidence]) -> tuple[EvidenceSummary, ...]:
    grouped: dict[tuple[str, str | None, str, str], list[NormalizedEvidence]] = {}
    for observation in observations:
        key = (observation.worker_id, observation.provider, observation.runtime, observation.role)
        grouped.setdefault(key, []).append(observation)
    summaries = []
    for (worker_id, provider, runtime, role), rows in grouped.items():
        sample_size = len(rows)
        successes = sum(1 for row in rows if row.status in SUCCESS_STATUSES)
        failures = sum(1 for row in rows if row.status in FAILURE_STATUSES)
        latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
        remediation_rows = [row for row in rows if row.review_outcome is not None or row.remediation_required]
        review_rows = [row for row in rows if row.review_outcome is not None]
        fit_values = [row.capability_fit for row in rows if row.capability_fit is not None]
        trustworthy_costs = [row.cost for row in rows if row.cost is not None and row.cost_trustworthy]
        summaries.append(
            EvidenceSummary(
                worker_id=worker_id,
                provider=provider,
                runtime=runtime,
                role=role,
                sample_size=sample_size,
                success_rate=successes / sample_size if sample_size else None,
                failure_rate=failures / sample_size if sample_size else None,
                mean_latency_ms=sum(latencies) / len(latencies) if latencies else None,
                remediation_rate=(
                    sum(1 for row in remediation_rows if row.remediation_required) / len(remediation_rows)
                    if remediation_rows
                    else None
                ),
                review_approval_rate=(
                    sum(1 for row in review_rows if row.review_outcome in APPROVING_REVIEW_VERDICTS) / len(review_rows)
                    if review_rows
                    else None
                ),
                capability_fit=sum(fit_values) / len(fit_values) if fit_values else None,
                cost=sum(trustworthy_costs) / len(trustworthy_costs) if trustworthy_costs else None,
                cost_trustworthy=bool(trustworthy_costs),
                risk_classes=tuple(sorted({row.task_risk_class for row in rows})),
                platforms=tuple(sorted({row.platform for row in rows if row.platform})),
                compatibility=tuple(sorted({row.compatibility for row in rows if row.compatibility})),
                evidence_strength="strong" if sample_size >= MIN_STRONG_SAMPLE_SIZE else ("limited" if sample_size else "none"),
            )
        )
    return tuple(sorted(summaries, key=lambda item: (item.role, item.worker_id)))


def propose_policy_change(
    summaries: Iterable[EvidenceSummary],
    *,
    current_policy: RoutingPolicy,
    held_out_evidence: Iterable[NormalizedEvidence] = (),
    min_sample_size: int = MIN_STRONG_SAMPLE_SIZE,
) -> PolicyProposal:
    summaries = tuple(summaries)
    changes: list[dict[str, Any]] = []
    explanation: list[str] = []
    for role in sorted({summary.role for summary in summaries}):
        eligible = [summary for summary in summaries if summary.role == role and summary.sample_size >= min_sample_size]
        if len(eligible) < 2:
            explanation.append(f"{role}: insufficient comparable evidence for a strong preference recommendation")
            continue
        ranked = sorted(eligible, key=_summary_rank_key)
        for preference, summary in enumerate(ranked, start=1):
            changes.append(
                {
                    "field": "worker.preference",
                    "worker_id": summary.worker_id,
                    "role": summary.role,
                    "suggested_preference": preference,
                    "reason": _summary_reason(summary),
                    "evidence_strength": summary.evidence_strength,
                }
            )
        best = ranked[0]
        explanation.append(
            f"{role}: prefer {best.worker_id} based on success/failure rate, latency, remediation/review outcomes, capability fit, and trustworthy cost where present"
        )
    _validate_proposed_changes(changes)
    proposed_workers_by_role = {
        str(change["role"]): str(change["worker_id"])
        for change in changes
        if change.get("field") == "worker.preference" and change.get("suggested_preference") == 1
    }
    evaluation = evaluate_proposal(held_out_evidence, proposed_workers_by_role=proposed_workers_by_role)
    proposal_version = _next_version(current_policy.version)
    proposal_payload = {
        "base_policy_id": current_policy.policy_id,
        "base_policy_version": current_policy.version,
        "version": proposal_version,
        "changes": changes,
        "evidence": [summary.to_dict() for summary in summaries],
        "evaluation": evaluation.to_dict(),
    }
    proposal_id = _stable_id(proposal_payload)
    return PolicyProposal(
        proposal_id=proposal_id,
        base_policy_id=current_policy.policy_id,
        base_policy_version=current_policy.version,
        proposed_policy_id=f"{current_policy.policy_id}-proposal",
        proposed_policy_version=proposal_version,
        status="PROPOSED_OPERATOR_APPROVAL_REQUIRED",
        changes=tuple(changes),
        explanation=tuple(explanation),
        evidence=summaries,
        evaluation=evaluation,
        rollback={
            "restore_policy_id": current_policy.policy_id,
            "restore_policy_version": current_policy.version,
            "proposal_id": proposal_id,
        },
    )


def evaluate_proposal(
    held_out_evidence: Iterable[NormalizedEvidence],
    *,
    proposed_workers_by_role: dict[str, str],
) -> PolicyEvaluation:
    rows = tuple(held_out_evidence)
    if not rows:
        return PolicyEvaluation(
            method="held_out_replay",
            sample_size=0,
            baseline_success_rate=None,
            proposed_success_rate=None,
            baseline_mean_latency_ms=None,
            proposed_mean_latency_ms=None,
            limitations=("no held-out evidence supplied; controlled experiment still required",),
        )
    baseline = rows
    proposed = tuple(
        row
        for row in rows
        if proposed_workers_by_role.get(row.role) == row.worker_id
    )
    limitations = []
    missing_roles = sorted({row.role for row in rows if row.role in proposed_workers_by_role} - {row.role for row in proposed})
    unevaluated_roles = sorted(set(proposed_workers_by_role) - {row.role for row in rows})
    if not proposed:
        limitations.append("held-out evidence contains no observations for the proposed preferred workers; controlled experiment still required")
    if missing_roles:
        limitations.append(f"no held-out observations matched proposed preferred workers for roles: {', '.join(missing_roles)}")
    if unevaluated_roles:
        limitations.append(f"held-out evidence contains no observations for proposed roles: {', '.join(unevaluated_roles)}")
    if len(proposed) < MIN_STRONG_SAMPLE_SIZE:
        limitations.append("proposed policy replay has sparse evidence; do not treat as strong proof")
    return PolicyEvaluation(
        method="held_out_replay",
        sample_size=len(rows),
        baseline_success_rate=_success_rate(baseline),
        proposed_success_rate=_success_rate(proposed),
        baseline_mean_latency_ms=_mean_latency(baseline),
        proposed_mean_latency_ms=_mean_latency(proposed),
        limitations=tuple(limitations),
    )


def approved_policy_from_proposal(_proposal: PolicyProposal) -> None:
    raise PolicyLearningGuardrailError(
        "policy-learning proposals are reports only; apply changes through an explicit operator-approved config/version-control change"
    )


def record_operator_approval(
    proposal: PolicyProposal,
    *,
    approved_by: str,
    approved_at: datetime | None = None,
    audit_note: str = "",
) -> PolicyApprovalRecord:
    if not approved_by.strip():
        raise PolicyLearningGuardrailError("operator approval requires a non-empty approved_by identity")
    approved_at = approved_at or datetime.now(UTC)
    return PolicyApprovalRecord(
        proposal_id=proposal.proposal_id,
        approved_by=approved_by,
        approved_at=approved_at.isoformat(),
        approved_policy_id=proposal.proposed_policy_id,
        approved_policy_version=proposal.proposed_policy_version,
        rollback=proposal.rollback,
        audit_note=audit_note,
    )


def _normalize_execution(execution: BuildRunnerExecution, task: BuildTask) -> NormalizedEvidence:
    data = execution.result_data or {}
    review = data.get("review") if isinstance(data.get("review"), dict) else data
    verdict = str(review.get("verdict", "")).upper() or None
    cost_data = data.get("cost") if isinstance(data.get("cost"), dict) else {}
    cost_source = str(cost_data.get("source") or "").lower()
    metadata = data.get("routing") if isinstance(data.get("routing"), dict) else {}
    capability_fit = _routing_capability_fit(metadata, execution.worker_id)
    return NormalizedEvidence(
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        worker_id=execution.worker_id,
        provider=execution.provider,
        runtime=execution.adapter,
        role=execution.role,
        status=execution.status,
        latency_ms=_latency_ms(execution),
        review_outcome=verdict,
        remediation_required=verdict in REMEDIATION_REVIEW_VERDICTS or bool(review.get("required_remediation")),
        capability_fit=capability_fit,
        cost=_float_or_none(cost_data.get("amount", cost_data.get("score"))),
        cost_trustworthy=cost_source in TRUSTWORTHY_COST_SOURCES,
        task_risk_class=task.risk_level,
        review_policy=task.review_policy,
        platform=str(data.get("platform")) if data.get("platform") else None,
        compatibility=str(data.get("compatibility")) if data.get("compatibility") else None,
    )


def _routing_capability_fit(routing_data: dict[str, Any], worker_id: str) -> float | None:
    direct = _float_or_none(routing_data.get("capability_fit"))
    if direct is not None:
        return direct
    selected_worker = str(routing_data.get("selected_worker") or worker_id)
    candidates = routing_data.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict) or str(candidate.get("worker_id") or "") != selected_worker:
            continue
        evidence = candidate.get("evidence")
        if isinstance(evidence, dict):
            return _float_or_none(evidence.get("capability_fit"))
        return _float_or_none(candidate.get("capability_fit"))
    return None


def _summary_rank_key(summary: EvidenceSummary) -> tuple[float, float, float, float, float, str]:
    return (
        -(summary.success_rate if summary.success_rate is not None else 0.0),
        summary.failure_rate if summary.failure_rate is not None else 1.0,
        summary.remediation_rate if summary.remediation_rate is not None else 1.0,
        summary.mean_latency_ms if summary.mean_latency_ms is not None else float("inf"),
        summary.cost if summary.cost_trustworthy and summary.cost is not None else float("inf"),
        summary.worker_id,
    )


def _summary_reason(summary: EvidenceSummary) -> dict[str, Any]:
    return {
        "sample_size": summary.sample_size,
        "success_rate": summary.success_rate,
        "failure_rate": summary.failure_rate,
        "mean_latency_ms": summary.mean_latency_ms,
        "remediation_rate": summary.remediation_rate,
        "review_approval_rate": summary.review_approval_rate,
        "capability_fit": summary.capability_fit,
        "cost": summary.cost if summary.cost_trustworthy else None,
        "cost_trustworthy": summary.cost_trustworthy,
    }


def _validate_proposed_changes(changes: Iterable[dict[str, Any]]) -> None:
    for change in changes:
        field = str(change.get("field", "")).lower()
        if field in FORBIDDEN_POLICY_FIELDS or any(part in FORBIDDEN_POLICY_FIELDS for part in field.split(".")):
            raise PolicyLearningGuardrailError(f"policy-learning may not change guarded field: {field}")


def _success_rate(rows: tuple[NormalizedEvidence, ...]) -> float | None:
    return sum(1 for row in rows if row.status in SUCCESS_STATUSES) / len(rows) if rows else None


def _mean_latency(rows: tuple[NormalizedEvidence, ...]) -> float | None:
    latencies = [row.latency_ms for row in rows if row.latency_ms is not None]
    return sum(latencies) / len(latencies) if latencies else None


def _latency_ms(execution: BuildRunnerExecution) -> float | None:
    if execution.launched_at is None or execution.completed_at is None:
        return None
    return (_naive(execution.completed_at) - _naive(execution.launched_at)).total_seconds() * 1000.0


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _next_version(version: str) -> str:
    try:
        return str(int(version) + 1)
    except ValueError:
        return f"{version}.proposal"


def _stable_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]
