"""Provider-neutral worker capability routing.

This module is deterministic infrastructure. It never asks a model to choose a
worker and it only records configuration references, not credential material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.claims import last_implementation_provider, last_implementation_worker
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskClaim, BuildWorkerLease
from build_coordinator.policy import review_policy_spec

CAP_CHEAP = "CHEAP"
CAP_FAST = "FAST"
CAP_CODING = "CODING"
CAP_ADVANCED_REASONING = "ADVANCED_REASONING"
CAP_CODE_REVIEW = "CODE_REVIEW"
CAP_SECURITY_REVIEW = "SECURITY_REVIEW"
CAP_SCM_OPERATOR = "SCM_OPERATOR"
CAP_ARCHITECTURE = "ARCHITECTURE"
CAP_MAINTENANCE = "MAINTENANCE"

PROVIDER_FAILURES = frozenset(
    {
        "UNAVAILABLE",
        "RATE_LIMITED",
        "QUOTA_EXHAUSTED",
        "AUTH_FAILURE",
        "NETWORK_FAILURE",
        "EXECUTION_FAILURE",
    }
)
PROVIDER_CONSUMPTION_MODES = frozenset({"ACTIVE", "FALLBACK", "DISABLED"})
# Transient provider failures worth an automatic, bounded retry with backoff.
# The remaining PROVIDER_FAILURES values (AUTH_FAILURE, QUOTA_EXHAUSTED,
# EXECUTION_FAILURE) are not blindly retried here: they typically need a
# credential fix, a quota reset, or investigation rather than a short wait.
RETRYABLE_PROVIDER_FAILURES = frozenset({"RATE_LIMITED", "UNAVAILABLE", "NETWORK_FAILURE"})
DEFAULT_FALLBACK_ON = ("UNAVAILABLE", "RATE_LIMITED", "QUOTA_EXHAUSTED", "NETWORK_FAILURE", "EXECUTION_FAILURE")
DEFAULT_NO_FALLBACK_ON = ("AUTH_FAILURE",)

DEFAULT_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "PLANNER": (CAP_ADVANCED_REASONING, CAP_ARCHITECTURE),
    "BUILDER": (CAP_CODING,),
    "REMEDIATION": (CAP_CODING,),
    "REVIEWER": (CAP_CODE_REVIEW,),
    "INTEGRATION": (CAP_SCM_OPERATOR,),
    "STEWARD": (CAP_MAINTENANCE,),
}

DEFAULT_ROLE_STAGES: dict[str, tuple[str, ...]] = {
    "PLANNER": ("planning",),
    "BUILDER": ("implementation", "remediation"),
    "REMEDIATION": ("remediation",),
    "REVIEWER": ("review",),
    "INTEGRATION": ("integration",),
    "STEWARD": ("maintenance",),
}

DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "INTEGRATION": ("SCM_WRITE",),
    "STEWARD": ("COORDINATOR_MAINTENANCE",),
}


class WorkerLike(Protocol):
    worker_id: str
    role: str
    provider: str
    runtime: str
    model: str | None
    adapter: str
    enabled: bool
    max_concurrency: int
    preference: int
    permissions: tuple[str, ...]

    def stage_names(self) -> tuple[str, ...]: ...

    def capability_names(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    enabled: bool = True
    availability: str = "AVAILABLE"
    consumption_mode: str = "ACTIVE"  # ACTIVE, FALLBACK, DISABLED
    auth: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = str(self.consumption_mode or "ACTIVE").upper()
        if mode not in PROVIDER_CONSUMPTION_MODES:
            mode = "ACTIVE"
        object.__setattr__(self, "consumption_mode", mode)
        object.__setattr__(self, "availability", str(self.availability or "AVAILABLE").upper())

    @classmethod
    def from_mapping(cls, provider_id: str, data: dict[str, Any] | None) -> "ProviderConfig":
        row = data or {}
        mode = str(row.get("mode", row.get("consumption_mode", "ACTIVE"))).upper()
        return cls(
            provider_id=provider_id,
            enabled=bool(row.get("enabled", True)),
            availability=str(row.get("availability", "AVAILABLE")).upper(),
            consumption_mode=mode,
            auth=dict(row.get("auth") or {}),
            metadata=dict(row.get("metadata") or {}),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "enabled": self.enabled,
            "mode": self.consumption_mode,
            "availability": self.availability,
            "consumption_mode": self.consumption_mode,
            "auth": _public_refs(self.auth),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class RuntimeConfig:
    runtime_id: str
    enabled: bool = True
    harness: str = "subprocess"
    availability: str = "AVAILABLE"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, runtime_id: str, data: dict[str, Any] | None) -> "RuntimeConfig":
        row = data or {}
        return cls(
            runtime_id=runtime_id,
            enabled=bool(row.get("enabled", True)),
            harness=str(row.get("harness", row.get("adapter", "subprocess"))),
            availability=str(row.get("availability", "AVAILABLE")).upper(),
            metadata=dict(row.get("metadata") or {}),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "enabled": self.enabled,
            "harness": self.harness,
            "availability": self.availability,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class WorkerModelConfig:
    model_id: str
    provider: str | None = None
    capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, model_id: str, data: dict[str, Any] | None) -> "WorkerModelConfig":
        row = data or {}
        return cls(
            model_id=model_id,
            provider=row.get("provider"),
            capabilities=_tuple(row.get("capabilities")),
            metadata=dict(row.get("metadata") or {}),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "capabilities": list(self.capabilities),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class StageRequirement:
    stage: str
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    preferred_workers: tuple[str, ...] = ()
    pinned_worker: str | None = None
    pinned_provider: str | None = None
    pinned_model: str | None = None

    @classmethod
    def from_mapping(cls, stage: str, data: dict[str, Any] | None) -> "StageRequirement":
        row = data or {}
        return cls(
            stage=stage,
            capabilities=_tuple(row.get("capabilities")),
            permissions=_tuple(row.get("permissions")),
            preferred_workers=_tuple(row.get("preferred_workers")),
            pinned_worker=row.get("pinned_worker"),
            pinned_provider=row.get("pinned_provider"),
            pinned_model=row.get("pinned_model"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "preferred_workers": list(self.preferred_workers),
            "pinned_worker": self.pinned_worker,
            "pinned_provider": self.pinned_provider,
            "pinned_model": self.pinned_model,
        }


@dataclass(frozen=True)
class WorkerEvidence:
    reliability: float | None = None
    latency_ms: float | None = None
    capability_fit: float | None = None
    failure_rate: float | None = None
    cost: float | None = None
    sample_size: int = 0
    source: str = "none"

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "WorkerEvidence":
        row = data or {}
        return cls(
            reliability=_float_or_none(row.get("reliability")),
            latency_ms=_float_or_none(row.get("latency_ms", row.get("latency"))),
            capability_fit=_float_or_none(row.get("capability_fit")),
            failure_rate=_float_or_none(row.get("failure_rate")),
            cost=_float_or_none(row.get("cost")),
            sample_size=int(row.get("sample_size") or 0),
            source=str(row.get("source") or "configured"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "reliability": self.reliability,
            "latency_ms": self.latency_ms,
            "capability_fit": self.capability_fit,
            "failure_rate": self.failure_rate,
            "cost": self.cost,
            "sample_size": self.sample_size,
            "source": self.source,
        }


@dataclass(frozen=True)
class RoutingPolicy:
    policy_id: str = "deterministic-v1"
    version: str = "1"
    fallback_on: tuple[str, ...] = DEFAULT_FALLBACK_ON
    no_fallback_on: tuple[str, ...] = DEFAULT_NO_FALLBACK_ON

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "RoutingPolicy":
        row = data or {}
        return cls(
            policy_id=str(row.get("policy_id", "deterministic-v1")),
            version=str(row.get("version", "1")),
            fallback_on=_tuple(row.get("fallback_on")) or DEFAULT_FALLBACK_ON,
            no_fallback_on=_tuple(row.get("no_fallback_on")) or DEFAULT_NO_FALLBACK_ON,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "fallback_on": list(self.fallback_on),
            "no_fallback_on": list(self.no_fallback_on),
        }


@dataclass(frozen=True)
class CandidateExplanation:
    worker_id: str
    eligible: bool
    reasons: tuple[str, ...]
    provider: str | None = None
    provider_mode: str = "ACTIVE"
    provider_availability: str = "AVAILABLE"
    runtime: str | None = None
    runtime_availability: str = "AVAILABLE"
    active_workers: int = 0
    active_provider_workers: int = 0
    evidence: WorkerEvidence = field(default_factory=WorkerEvidence)
    routing_score: float | None = None
    score_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "provider": self.provider,
            "provider_mode": self.provider_mode,
            "provider_availability": self.provider_availability,
            "runtime": self.runtime,
            "runtime_availability": self.runtime_availability,
            "active_workers": self.active_workers,
            "active_provider_workers": self.active_provider_workers,
            "evidence": self.evidence.to_public_dict(),
            "routing_score": self.routing_score,
            "score_reasons": list(self.score_reasons),
        }


@dataclass(frozen=True)
class RoutingDecision:
    stage: str
    required_capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...]
    selected_worker_id: str | None
    availability: str
    candidates: tuple[CandidateExplanation, ...]
    routing_policy: RoutingPolicy

    def selected_candidate(self) -> CandidateExplanation | None:
        if self.selected_worker_id is None:
            return None
        return next((item for item in self.candidates if item.worker_id == self.selected_worker_id), None)

    def to_audit_dict(self, worker: WorkerLike | None = None) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "required_capabilities": list(self.required_capabilities),
            "required_permissions": list(self.required_permissions),
            "selected_worker": self.selected_worker_id,
            "selected_provider": worker.provider if worker else None,
            "selected_runtime": worker.runtime if worker else None,
            "selected_model": worker.model if worker else None,
            "routing_policy": self.routing_policy.to_public_dict(),
            "availability": self.availability,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def default_stage_requirements() -> dict[str, StageRequirement]:
    return {
        "planning": StageRequirement("planning", capabilities=(CAP_ADVANCED_REASONING,)),
        "implementation": StageRequirement("implementation", capabilities=(CAP_CODING,)),
        "remediation": StageRequirement("remediation", capabilities=(CAP_CODING,)),
        "review": StageRequirement("review", capabilities=(CAP_CODE_REVIEW,)),
        "integration": StageRequirement("integration", capabilities=(CAP_SCM_OPERATOR,), permissions=("SCM_WRITE",)),
        "maintenance": StageRequirement(
            "maintenance",
            capabilities=(CAP_MAINTENANCE,),
            permissions=("COORDINATOR_MAINTENANCE",),
        ),
    }


def role_to_stage(role: str) -> str:
    return {
        "PLANNER": "planning",
        "BUILDER": "implementation",
        "REMEDIATION": "remediation",
        "REVIEWER": "review",
        "INTEGRATION": "integration",
        "STEWARD": "maintenance",
    }.get(role, role.lower())


def route_worker(
    workers: Iterable[WorkerLike],
    *,
    stage: str,
    stage_requirement: StageRequirement,
    providers: dict[str, ProviderConfig],
    runtimes: dict[str, RuntimeConfig],
    routing_policy: RoutingPolicy,
    session: Session | None = None,
    task_id: str | None = None,
    excluded_workers: set[str] | None = None,
    excluded_providers: set[str] | None = None,
    deprioritized_workers: set[str] | None = None,
    deprioritized_providers: set[str] | None = None,
    evidence_by_worker: dict[str, WorkerEvidence] | None = None,
) -> RoutingDecision:
    excluded_workers = excluded_workers or set()
    excluded_providers = excluded_providers or set()
    deprioritized_workers = deprioritized_workers or set()
    deprioritized_providers = deprioritized_providers or set()
    evidence_by_worker = evidence_by_worker or {}
    workers = list(workers)
    all_workers = workers
    active_by_worker = _active_implementation_counts(session)
    active_by_provider = _active_provider_counts(all_workers, active_by_worker)
    builder_provider = _builder_provider_for_review(session, task_id, workers) if stage == "review" else None
    candidates: list[CandidateExplanation] = []
    eligible: list[WorkerLike] = []
    for worker in workers:
        reasons = _candidate_reasons(
            worker,
            stage=stage,
            requirement=stage_requirement,
            providers=providers,
            runtimes=runtimes,
            active_by_worker=active_by_worker,
            excluded_workers=excluded_workers,
            excluded_providers=excluded_providers,
        )
        ok = not reasons
        evidence = evidence_by_worker.get(worker.worker_id, WorkerEvidence())
        score, score_reasons = _routing_score(worker, stage_requirement, evidence)
        candidates.append(
            CandidateExplanation(
                worker.worker_id,
                ok,
                tuple(reasons or ("eligible",)),
                provider=worker.provider,
                provider_mode=_provider_mode(worker.provider, providers),
                provider_availability=_provider_availability(worker.provider, providers),
                runtime=worker.runtime,
                runtime_availability=_runtime_availability(worker.runtime, runtimes),
                active_workers=active_by_worker.get(worker.worker_id, 0),
                active_provider_workers=active_by_provider.get(worker.provider, 0),
                evidence=evidence,
                routing_score=score,
                score_reasons=score_reasons,
            )
        )
        if ok:
            eligible.append(worker)
    no_fallback_hits = tuple(
        candidate
        for candidate in candidates
        if any(_reason_matches_no_fallback(reason, routing_policy.no_fallback_on) for reason in candidate.reasons)
    )
    if no_fallback_hits:
        return RoutingDecision(
            stage=stage,
            required_capabilities=stage_requirement.capabilities,
            required_permissions=stage_requirement.permissions,
            selected_worker_id=None,
            availability="blocked_no_fallback",
            candidates=tuple(candidates),
            routing_policy=routing_policy,
        )
    if not eligible:
        has_provider_issue = any(
            any(r.startswith("provider_") for r in candidate.reasons)
            for candidate in candidates
        )
        has_slots_issue = any("max_concurrency_reached" in candidate.reasons for candidate in candidates)
        if has_slots_issue:
            availability = "slots_occupied"
        elif has_provider_issue:
            availability = "providers_unavailable"
        else:
            availability = "no_eligible"
        return RoutingDecision(
            stage=stage,
            required_capabilities=stage_requirement.capabilities,
            required_permissions=stage_requirement.permissions,
            selected_worker_id=None,
            availability=availability,
            candidates=tuple(candidates),
            routing_policy=routing_policy,
        )
    prefer_different_provider = (
        builder_provider is not None
        and any(worker.provider != builder_provider for worker in eligible)
    )
    if prefer_different_provider:
        candidates = [
            CandidateExplanation(
                candidate.worker_id,
                candidate.eligible,
                (
                    candidate.reasons + ("preferred_different_provider_than_builder",)
                    if candidate.eligible
                    and _worker_provider(candidate.worker_id, workers) != builder_provider
                    else candidate.reasons
                ),
                provider=candidate.provider,
                provider_mode=candidate.provider_mode,
                provider_availability=candidate.provider_availability,
                runtime=candidate.runtime,
                runtime_availability=candidate.runtime_availability,
                active_workers=candidate.active_workers,
                active_provider_workers=candidate.active_provider_workers,
                evidence=candidate.evidence,
                routing_score=candidate.routing_score,
                score_reasons=candidate.score_reasons,
            )
            for candidate in candidates
        ]
    scores = {candidate.worker_id: candidate.routing_score for candidate in candidates}
    selected = sorted(
        eligible,
        key=lambda worker: (
            1 if worker.worker_id in deprioritized_workers else 0,
            1 if worker.provider in deprioritized_providers else 0,
            0 if (prefer_different_provider and worker.provider != builder_provider) else 1,
            0 if worker.worker_id in stage_requirement.preferred_workers else 1,
            0 if _provider_mode(worker.provider, providers) == "ACTIVE" else 1,
            worker.preference,
            -(scores.get(worker.worker_id) or 0.0),
            active_by_provider.get(worker.provider, 0),
            active_by_worker.get(worker.worker_id, 0),
            worker.worker_id,
        ),
    )[0]
    return RoutingDecision(
        stage=stage,
        required_capabilities=stage_requirement.capabilities,
        required_permissions=stage_requirement.permissions,
        selected_worker_id=selected.worker_id,
        availability="selected",
        candidates=tuple(candidates),
        routing_policy=routing_policy,
    )


def _builder_provider_for_review(
    session: Session | None,
    task_id: str | None,
    workers: Iterable[WorkerLike],
) -> str | None:
    if session is None or not task_id:
        return None
    implementer = last_implementation_worker(session, task_id)
    if not implementer:
        return None
    provider = session.scalar(
        select(BuildTaskClaim.provider)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .order_by(BuildTaskClaim.claimed_at.desc())
        .limit(1)
    )
    if provider:
        return provider
    return _worker_provider(implementer, workers)


def _worker_provider(worker_id: str, workers: Iterable[WorkerLike]) -> str | None:
    for worker in workers:
        if worker.worker_id == worker_id:
            return worker.provider
    return None


@dataclass(frozen=True)
class ReviewerExclusions:
    workers: frozenset[str] = frozenset()
    providers: frozenset[str] = frozenset()


def reviewer_exclusions(
    session: Session | None,
    task_id: str | None,
    *,
    reviewed_feature_sha: str | None = None,
) -> ReviewerExclusions:
    if session is None or not task_id:
        return ReviewerExclusions()
    build_task = session.get(BuildTask, task_id)
    if build_task is None:
        return ReviewerExclusions()
    spec = review_policy_spec(build_task.review_policy)
    excluded = set(approving_reviewers(session, task_id, reviewed_feature_sha=reviewed_feature_sha))
    excluded_providers = set()
    if spec.independent_worker and (implementer := last_implementation_worker(session, task_id)):
        excluded.add(implementer)
    if spec.independent_provider:
        excluded_providers.update(
            approving_providers(session, task_id, reviewed_feature_sha=reviewed_feature_sha)
        )
        if provider := last_implementation_provider(session, task_id):
            excluded_providers.add(provider)
    return ReviewerExclusions(frozenset(excluded), frozenset(excluded_providers))


def approving_reviewers(
    session: Session,
    task_id: str,
    *,
    reviewed_feature_sha: str | None = None,
) -> set[str]:
    """Distinct workers that approved this task at the requested feature SHA."""
    return {row.worker_id for row in approving_review_executions(session, task_id, reviewed_feature_sha=reviewed_feature_sha)}


def approving_providers(
    session: Session,
    task_id: str,
    *,
    reviewed_feature_sha: str | None = None,
) -> set[str]:
    """Distinct providers that approved this task at the requested feature SHA."""
    return {
        row.provider
        for row in approving_review_executions(session, task_id, reviewed_feature_sha=reviewed_feature_sha)
        if row.provider
    }


def approving_review_executions(
    session: Session,
    task_id: str,
    *,
    reviewed_feature_sha: str | None = None,
) -> list[BuildRunnerExecution]:
    """Review executions that are eligible approvals for the exact feature SHA."""
    since = session.scalar(
        select(BuildTaskClaim.claimed_at)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .order_by(BuildTaskClaim.claimed_at.desc())
        .limit(1)
    )
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == task_id)
        .where(BuildRunnerExecution.role == "REVIEWER")
        .where(BuildRunnerExecution.status == "SUCCEEDED")
    ).all()
    approvals: list[BuildRunnerExecution] = []
    for row in rows:
        if since is not None and row.launched_at is not None and _naive(row.launched_at) < _naive(since):
            continue
        if reviewed_feature_sha and row.reviewed_feature_sha != reviewed_feature_sha:
            continue
        data = row.result_data or {}
        review = data.get("review") if isinstance(data.get("review"), dict) else data
        if (
            str(review.get("verdict", "")).upper() in {"GREEN", "GREEN_WITH_NOTES"}
            and review.get("ready_for_integration") is True
            and not review.get("required_remediation")
        ):
            approvals.append(row)
    return approvals


def _naive(value):
    return value.replace(tzinfo=None) if getattr(value, "tzinfo", None) else value


def _candidate_reasons(
    worker: WorkerLike,
    *,
    stage: str,
    requirement: StageRequirement,
    providers: dict[str, ProviderConfig],
    runtimes: dict[str, RuntimeConfig],
    active_by_worker: dict[str, int],
    excluded_workers: set[str],
    excluded_providers: set[str],
) -> list[str]:
    reasons: list[str] = []
    if not worker.enabled:
        reasons.append("worker_disabled")
    if worker.adapter == "unconfigured":
        reasons.append("worker_unconfigured")
    if worker.worker_id in excluded_workers:
        reasons.append("worker_excluded_for_independence")
    if worker.provider in excluded_providers:
        reasons.append("provider_excluded_for_independence")
    if requirement.pinned_worker and worker.worker_id != requirement.pinned_worker:
        reasons.append(f"pinned_worker:{requirement.pinned_worker}")
    if stage not in worker.stage_names():
        reasons.append("stage_not_allowed")
    worker_caps = set(worker.capability_names())
    missing_caps = sorted(set(requirement.capabilities) - worker_caps)
    if missing_caps:
        reasons.append(f"missing_capabilities:{','.join(missing_caps)}")
    missing_perms = sorted(set(requirement.permissions) - set(worker.permissions))
    if missing_perms:
        reasons.append(f"missing_permissions:{','.join(missing_perms)}")
    if requirement.pinned_provider and worker.provider != requirement.pinned_provider:
        reasons.append(f"pinned_provider:{requirement.pinned_provider}")
    if requirement.pinned_model and worker.model != requirement.pinned_model:
        reasons.append(f"pinned_model:{requirement.pinned_model}")
    provider = providers.get(worker.provider)
    if provider is not None:
        if not provider.enabled or provider.consumption_mode == "DISABLED":
            reasons.append("provider_disabled")
        elif provider.availability != "AVAILABLE":
            reasons.append(f"provider_{provider.availability}")
    runtime = runtimes.get(worker.runtime)
    if runtime is not None:
        if not runtime.enabled:
            reasons.append("runtime_disabled")
        elif runtime.availability != "AVAILABLE":
            reasons.append(f"runtime_{runtime.availability}")
    if active_by_worker.get(worker.worker_id, 0) >= worker.max_concurrency:
        reasons.append("max_concurrency_reached")
    return reasons


def _active_provider_counts(workers: Iterable[WorkerLike], active_by_worker: dict[str, int]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for worker in workers:
        counts[worker.provider] = counts.get(worker.provider, 0) + active_by_worker.get(worker.worker_id, 0)
    return counts


def _provider_mode(provider_id: str, providers: dict[str, ProviderConfig]) -> str:
    provider = providers.get(provider_id)
    return provider.consumption_mode if provider is not None else "ACTIVE"


def _provider_availability(provider_id: str, providers: dict[str, ProviderConfig]) -> str:
    provider = providers.get(provider_id)
    return provider.availability if provider is not None else "AVAILABLE"


def _runtime_availability(runtime_id: str, runtimes: dict[str, RuntimeConfig]) -> str:
    runtime = runtimes.get(runtime_id)
    return runtime.availability if runtime is not None else "AVAILABLE"


def _active_implementation_counts(session: Session | None) -> dict[str, int]:
    if session is None:
        return {}
    now = datetime.now(UTC)
    rows = session.scalars(
        select(BuildTaskClaim.worker_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .where(BuildTaskClaim.status == "ACTIVE")
    ).all()
    counts: dict[str, int] = {}
    for worker_id in rows:
        counts[worker_id] = counts.get(worker_id, 0) + 1
    # Reviewer, integration and planner workers hold no IMPLEMENTATION claim;
    # their live executions occupy the worker's slots the same way.
    live = session.execute(
        select(BuildRunnerExecution.worker_id, BuildRunnerExecution.execution_id)
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
        .where(BuildRunnerExecution.role.in_(("REVIEWER", "INTEGRATION", "PLANNER")))
    ).all()
    live_execution_ids = {execution_id for _worker_id, execution_id in live}
    for worker_id, _execution_id in live:
        counts[worker_id] = counts.get(worker_id, 0) + 1
    leases = session.execute(
        select(BuildWorkerLease.worker_id, BuildWorkerLease.execution_id)
        .where(BuildWorkerLease.status == "ACTIVE")
        .where(BuildWorkerLease.lease_expires_at > now)
    ).all()
    lease_counts: dict[str, int] = {}
    for worker_id, execution_id in leases:
        if execution_id in live_execution_ids:
            continue
        lease_counts[worker_id] = lease_counts.get(worker_id, 0) + 1
    for worker_id, lease_count in lease_counts.items():
        counts[worker_id] = max(counts.get(worker_id, 0), lease_count)
    return counts


def execution_evidence_by_worker(session: Session | None, workers: Iterable[WorkerLike]) -> dict[str, WorkerEvidence]:
    if session is None:
        return {}
    worker_ids = {worker.worker_id for worker in workers}
    if not worker_ids:
        return {}
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.worker_id.in_(worker_ids))
        .where(BuildRunnerExecution.status.in_(("SUCCEEDED", "FAILED", "HUMAN_ACTION_REQUIRED", "TERMINATED", "LOST")))
    ).all()
    grouped: dict[str, list[BuildRunnerExecution]] = {}
    for row in rows:
        grouped.setdefault(row.worker_id, []).append(row)
    evidence: dict[str, WorkerEvidence] = {}
    for worker_id, worker_rows in grouped.items():
        sample_size = len(worker_rows)
        successes = sum(1 for row in worker_rows if row.status == "SUCCEEDED")
        failures = sample_size - successes
        latencies = [
            (_naive(row.completed_at) - _naive(row.launched_at)).total_seconds() * 1000.0
            for row in worker_rows
            if row.completed_at is not None and row.launched_at is not None
        ]
        evidence[worker_id] = WorkerEvidence(
            reliability=successes / sample_size if sample_size else None,
            latency_ms=(sum(latencies) / len(latencies)) if latencies else None,
            failure_rate=failures / sample_size if sample_size else None,
            sample_size=sample_size,
            source="execution_history",
        )
    return evidence


def merge_worker_evidence(configured: WorkerEvidence, observed: WorkerEvidence, worker: WorkerLike, requirement: StageRequirement) -> WorkerEvidence:
    capability_fit = configured.capability_fit
    if capability_fit is None:
        required = set(requirement.capabilities)
        if required:
            caps = set(worker.capability_names())
            capability_fit = len(required & caps) / len(required)
    cost = configured.cost
    raw_cost = getattr(worker, "cost", None)
    if cost is None and isinstance(raw_cost, dict):
        cost = _float_or_none(raw_cost.get("score", raw_cost.get("relative", raw_cost.get("per_1k_tokens"))))
    return WorkerEvidence(
        reliability=observed.reliability if observed.reliability is not None else configured.reliability,
        latency_ms=observed.latency_ms if observed.latency_ms is not None else configured.latency_ms,
        capability_fit=capability_fit,
        failure_rate=observed.failure_rate if observed.failure_rate is not None else configured.failure_rate,
        cost=cost,
        sample_size=observed.sample_size or configured.sample_size,
        source="execution_history+configured" if observed.sample_size and configured.source != "none" else (observed.source if observed.sample_size else configured.source),
    )


def _routing_score(worker: WorkerLike, requirement: StageRequirement, evidence: WorkerEvidence) -> tuple[float | None, tuple[str, ...]]:
    if evidence == WorkerEvidence():
        return None, ()
    score = 0.0
    reasons: list[str] = []
    if evidence.reliability is not None:
        score += _clamp01(evidence.reliability) * 45.0
        reasons.append(f"reliability:{evidence.reliability:.3f}")
    if evidence.failure_rate is not None:
        score += (1.0 - _clamp01(evidence.failure_rate)) * 20.0
        reasons.append(f"failure_rate:{evidence.failure_rate:.3f}")
    if evidence.capability_fit is not None:
        score += _clamp01(evidence.capability_fit) * 20.0
        reasons.append(f"capability_fit:{evidence.capability_fit:.3f}")
    if evidence.latency_ms is not None:
        latency_score = 1.0 / (1.0 + max(evidence.latency_ms, 0.0) / 60000.0)
        score += latency_score * 10.0
        reasons.append(f"latency_ms:{evidence.latency_ms:.1f}")
    if evidence.cost is not None:
        cost_score = 1.0 / (1.0 + max(evidence.cost, 0.0))
        score += cost_score * 5.0
        reasons.append(f"cost:{evidence.cost:.3f}")
    return round(score, 6), tuple(reasons)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reason_matches_no_fallback(reason: str, no_fallback_on: tuple[str, ...]) -> bool:
    for failure in no_fallback_on:
        if reason in {f"provider_{failure}", f"runtime_{failure}"}:
            return True
    return False


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _public_refs(data: dict[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            public[key] = _public_refs(value)
        elif str(key).lower() in {"value", "token", "secret", "password", "api_key"}:
            public[key] = "<redacted-ref>"
        else:
            public[key] = value
    return public
