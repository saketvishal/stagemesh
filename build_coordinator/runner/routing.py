"""Provider-neutral worker capability routing.

This module is deterministic infrastructure. It never asks a model to choose a
worker and it only records configuration references, not credential material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.claims import last_implementation_worker
from build_coordinator.models import BuildRunnerExecution, BuildTaskClaim

CAP_CHEAP = "CHEAP"
CAP_FAST = "FAST"
CAP_CODING = "CODING"
CAP_ADVANCED_REASONING = "ADVANCED_REASONING"
CAP_CODE_REVIEW = "CODE_REVIEW"
CAP_SECURITY_REVIEW = "SECURITY_REVIEW"
CAP_SCM_OPERATOR = "SCM_OPERATOR"
CAP_ARCHITECTURE = "ARCHITECTURE"

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
}

DEFAULT_ROLE_STAGES: dict[str, tuple[str, ...]] = {
    "PLANNER": ("planning",),
    "BUILDER": ("implementation", "remediation"),
    "REMEDIATION": ("remediation",),
    "REVIEWER": ("review",),
    "INTEGRATION": ("integration",),
}

DEFAULT_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "INTEGRATION": ("SCM_WRITE",),
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

    @classmethod
    def from_mapping(cls, provider_id: str, data: dict[str, Any] | None) -> "ProviderConfig":
        row = data or {}
        return cls(
            provider_id=provider_id,
            enabled=bool(row.get("enabled", True)),
            availability=str(row.get("availability", "AVAILABLE")).upper(),
            consumption_mode=str(row.get("consumption_mode", "ACTIVE")).upper(),
            auth=dict(row.get("auth") or {}),
            metadata=dict(row.get("metadata") or {}),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "enabled": self.enabled,
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
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
    }


def role_to_stage(role: str) -> str:
    return {
        "PLANNER": "planning",
        "BUILDER": "implementation",
        "REMEDIATION": "remediation",
        "REVIEWER": "review",
        "INTEGRATION": "integration",
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
    deprioritized_workers: set[str] | None = None,
) -> RoutingDecision:
    excluded_workers = excluded_workers or set()
    deprioritized_workers = deprioritized_workers or set()
    workers = list(workers)
    all_workers = workers
    active_by_worker = _active_implementation_counts(session)
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
        )
        ok = not reasons
        candidates.append(CandidateExplanation(worker.worker_id, ok, tuple(reasons or ("eligible",))))
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
            )
            for candidate in candidates
        ]
    provider_load = {
        provider: sum(active_by_worker.get(w.worker_id, 0) for w in all_workers if w.provider == provider)
        for provider in {w.provider for w in all_workers}
    }
    selected = sorted(
        eligible,
        key=lambda worker: (
            1 if worker.worker_id in deprioritized_workers else 0,
            0 if (prefer_different_provider and worker.provider != builder_provider) else 1,
            0 if worker.worker_id in stage_requirement.preferred_workers else 1,
            0 if (providers.get(worker.provider) and providers[worker.provider].consumption_mode == "ACTIVE") else 1,
            worker.preference,
            provider_load.get(worker.provider, 0),
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


def reviewer_exclusions(
    session: Session | None,
    task_id: str | None,
    *,
    reviewed_feature_sha: str | None = None,
) -> set[str]:
    if session is None or not task_id:
        return set()
    excluded = set(approving_reviewers(session, task_id, reviewed_feature_sha=reviewed_feature_sha))
    implementer = last_implementation_worker(session, task_id)
    if implementer:
        excluded.add(implementer)
    return excluded


def approving_reviewers(
    session: Session,
    task_id: str,
    *,
    reviewed_feature_sha: str | None = None,
) -> set[str]:
    """Distinct workers that approved this task at the requested feature SHA."""
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
    approvers: set[str] = set()
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
            approvers.add(row.worker_id)
    return approvers


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
) -> list[str]:
    reasons: list[str] = []
    if not worker.enabled:
        reasons.append("worker_disabled")
    if worker.adapter == "unconfigured":
        reasons.append("worker_unconfigured")
    if worker.worker_id in excluded_workers:
        reasons.append("worker_excluded_for_independence")
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


def _active_implementation_counts(session: Session | None) -> dict[str, int]:
    if session is None:
        return {}
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
    live = session.scalars(
        select(BuildRunnerExecution.worker_id)
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
        .where(BuildRunnerExecution.role.in_(("REVIEWER", "INTEGRATION", "PLANNER")))
    ).all()
    for worker_id in live:
        counts[worker_id] = counts.get(worker_id, 0) + 1
    return counts


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
