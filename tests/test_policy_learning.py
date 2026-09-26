from __future__ import annotations

from datetime import timedelta

import pytest

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildRunnerExecution
from build_coordinator.policy_learning import (
    PolicyLearningGuardrailError,
    _validate_proposed_changes,
    approved_policy_from_proposal,
    collect_normalized_evidence,
    propose_policy_change,
    record_operator_approval,
    summarize_evidence,
)
from build_coordinator.runner.routing import RoutingPolicy
from build_coordinator.service import upsert_task, utcnow
from build_coordinator.types import TaskSpec


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    initialize_schema()


def _task(task_id: str, *, risk: str = "MEDIUM", review_policy: str = "INDEPENDENT") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="policy learning evidence",
        acceptance_criteria=["passes"],
        risk_level=risk,
        review_policy=review_policy,
    )


def _execution(
    task_id: str,
    worker_id: str,
    *,
    provider: str,
    role: str = "BUILDER",
    status: str = "SUCCEEDED",
    seconds: int = 10,
    result_data: dict | None = None,
) -> BuildRunnerExecution:
    now = utcnow()
    return BuildRunnerExecution(
        execution_id=f"exec-{task_id}-{worker_id}",
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        provider=provider,
        adapter="fake",
        status=status,
        launched_at=now,
        completed_at=now + timedelta(seconds=seconds),
        result_data=result_data or {},
    )


def test_policy_learning_collects_normalized_evidence_and_trusts_only_verified_cost():
    with SessionLocal() as session:
        upsert_task(session, _task("PL-1", risk="HIGH", review_policy="TWO_PROVIDERS"))
        session.add(
            _execution(
                "PL-1",
                "builder-a",
                provider="openai",
                result_data={
                    "routing": {"capability_fit": 1.0},
                    "cost": {"amount": 0.42, "source": "operator_verified"},
                    "platform": "windows",
                    "compatibility": "SUPPORTED",
                },
            )
        )
        upsert_task(session, _task("PL-2"))
        session.add(
            _execution(
                "PL-2",
                "builder-b",
                provider="anthropic",
                result_data={"cost": {"amount": 0.01, "source": "model_guess"}},
            )
        )
        session.commit()

        observations = collect_normalized_evidence(session)

    observed = {item.worker_id: item for item in observations}
    assert observed["builder-a"].task_risk_class == "HIGH"
    assert observed["builder-a"].review_policy == "TWO_PROVIDERS"
    assert observed["builder-a"].latency_ms == 10000.0
    assert observed["builder-a"].capability_fit == 1.0
    assert observed["builder-a"].cost == 0.42
    assert observed["builder-a"].cost_trustworthy is True
    assert observed["builder-b"].cost == 0.01
    assert observed["builder-b"].cost_trustworthy is False


def test_policy_learning_proposes_explainable_unapproved_preference_changes_with_sparse_warning():
    observations = []
    for idx in range(10):
        observations.append(
            _synthetic_observation(f"a-{idx}", "builder-a", status="SUCCEEDED", latency_ms=8000)
        )
        observations.append(
            _synthetic_observation(
                f"b-{idx}",
                "builder-b",
                status="FAILED" if idx < 3 else "SUCCEEDED",
                latency_ms=20000,
            )
        )
    held_out = tuple(observations[:3])

    proposal = propose_policy_change(
        summarize_evidence(observations),
        current_policy=RoutingPolicy(policy_id="deterministic-v1", version="7"),
        held_out_evidence=held_out,
    )

    assert proposal.status == "PROPOSED_OPERATOR_APPROVAL_REQUIRED"
    assert proposal.base_policy_version == "7"
    assert proposal.proposed_policy_version == "8"
    assert proposal.changes[0]["field"] == "worker.preference"
    assert proposal.changes[0]["worker_id"] == "builder-a"
    assert "explicit_operator_approval_required" in proposal.guardrails
    assert proposal.rollback == {
        "restore_policy_id": "deterministic-v1",
        "restore_policy_version": "7",
        "proposal_id": proposal.proposal_id,
    }
    assert proposal.evaluation.method == "held_out_replay"
    assert proposal.evaluation.sample_size == 3
    assert proposal.evaluation.baseline_success_rate == pytest.approx(2 / 3)
    assert proposal.evaluation.proposed_success_rate == 1.0
    assert proposal.evaluation.baseline_mean_latency_ms == pytest.approx(12000)
    assert proposal.evaluation.proposed_mean_latency_ms == 8000
    assert proposal.to_dict()["changes"][0]["reason"]["success_rate"] == 1.0


def test_policy_learning_held_out_replay_selects_proposed_top_worker_per_role():
    training = []
    for idx in range(10):
        training.append(_synthetic_observation(f"a-train-{idx}", "builder-a", status="SUCCEEDED", latency_ms=5000))
        training.append(_synthetic_observation(f"b-train-{idx}", "builder-b", status="FAILED", latency_ms=20000))
    held_out = (
        _synthetic_observation("a-held-1", "builder-a", status="SUCCEEDED", latency_ms=7000),
        _synthetic_observation("b-held-1", "builder-b", status="SUCCEEDED", latency_ms=1000),
        _synthetic_observation("b-held-2", "builder-b", status="FAILED", latency_ms=1000),
    )

    proposal = propose_policy_change(
        summarize_evidence(training),
        current_policy=RoutingPolicy(),
        held_out_evidence=held_out,
    )

    assert proposal.changes[0]["worker_id"] == "builder-a"
    assert proposal.evaluation.baseline_success_rate == pytest.approx(2 / 3)
    assert proposal.evaluation.proposed_success_rate == 1.0
    assert proposal.evaluation.baseline_mean_latency_ms == 3000
    assert proposal.evaluation.proposed_mean_latency_ms == 7000


def test_policy_learning_collects_capability_fit_from_routing_audit_selected_candidate():
    with SessionLocal() as session:
        upsert_task(session, _task("PL-ROUTE"))
        session.add(
            _execution(
                "PL-ROUTE",
                "builder-a",
                provider="openai",
                result_data={
                    "routing": {
                        "selected_worker": "builder-a",
                        "candidates": [
                            {
                                "worker_id": "builder-a",
                                "eligible": True,
                                "evidence": {
                                    "reliability": 0.9,
                                    "latency_ms": 12000,
                                    "capability_fit": 0.5,
                                    "failure_rate": 0.1,
                                    "sample_size": 12,
                                    "source": "execution_history+configured",
                                },
                            },
                            {
                                "worker_id": "builder-b",
                                "eligible": True,
                                "evidence": {"capability_fit": 1.0},
                            },
                        ],
                    }
                },
            )
        )
        session.commit()

        observations = collect_normalized_evidence(session)

    assert len(observations) == 1
    assert observations[0].capability_fit == 0.5


def test_sparse_data_is_labeled_limited_and_not_ranked_as_strong_recommendation():
    observations = (
        _synthetic_observation("a-1", "builder-a", status="SUCCEEDED", latency_ms=5000),
        _synthetic_observation("b-1", "builder-b", status="FAILED", latency_ms=20000),
    )

    proposal = propose_policy_change(
        summarize_evidence(observations),
        current_policy=RoutingPolicy(),
    )

    assert proposal.changes == ()
    assert all(item.evidence_strength == "limited" for item in proposal.evidence)
    assert proposal.explanation == ("BUILDER: insufficient comparable evidence for a strong preference recommendation",)


def test_policy_learning_guardrails_reject_review_security_permission_weakening_and_autopromotion():
    with pytest.raises(PolicyLearningGuardrailError):
        _validate_proposed_changes([{"field": "review_policy", "value": "SELF"}])
    with pytest.raises(PolicyLearningGuardrailError):
        _validate_proposed_changes([{"field": "stage.required_permissions", "value": []}])

    proposal = propose_policy_change((), current_policy=RoutingPolicy())
    with pytest.raises(PolicyLearningGuardrailError):
        approved_policy_from_proposal(proposal)

    with pytest.raises(PolicyLearningGuardrailError):
        record_operator_approval(proposal, approved_by=" ")

    approval = record_operator_approval(proposal, approved_by="release-operator", audit_note="accepted in config PR")
    assert approval.proposal_id == proposal.proposal_id
    assert approval.approved_policy_version == proposal.proposed_policy_version
    assert approval.rollback == proposal.rollback


def _synthetic_observation(execution_id: str, worker_id: str, *, status: str, latency_ms: float):
    from build_coordinator.policy_learning import NormalizedEvidence

    return NormalizedEvidence(
        execution_id=execution_id,
        task_id=f"task-{execution_id}",
        worker_id=worker_id,
        provider="openai" if worker_id == "builder-a" else "anthropic",
        runtime="fake",
        role="BUILDER",
        status=status,
        latency_ms=latency_ms,
        capability_fit=1.0,
        task_risk_class="MEDIUM",
        review_policy="INDEPENDENT",
    )
