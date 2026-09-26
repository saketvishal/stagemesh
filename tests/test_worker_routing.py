from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import record_event
from build_coordinator.execution import ExecutionHandle, FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
    BuildWorkerLease,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.orchestrator import RunnerCycleResult
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import (
    CAP_CODE_REVIEW,
    CAP_CODING,
    CAP_SCM_OPERATOR,
    PROVIDER_FAILURES,
    RETRYABLE_PROVIDER_FAILURES,
    ProviderConfig,
    StageRequirement,
    WorkerEvidence,
    approving_providers,
    approving_reviewers,
    route_worker,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.service import (
    CheckpointInput,
    ClaimRequest,
    checkpoint,
    claim_review,
    claim_task,
    reconcile_stale_executions,
    recover_expired,
    recover_lost_execution_claims,
    upsert_task,
    utcnow,
)
from build_coordinator.types import EventInput, TaskSpec


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildWorkerLease,
            BuildTask,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="routing test task",
        acceptance_criteria=["passes"],
        review_policy="INDEPENDENT",
    )


def _approval(task_id: str, worker_id: str, provider: str, sha: str = "feature-sha") -> BuildRunnerExecution:
    return BuildRunnerExecution(
        execution_id=f"exec-{task_id}-{worker_id}",
        task_id=task_id,
        role="REVIEWER",
        worker_id=worker_id,
        provider=provider,
        adapter="fake",
        reviewed_feature_sha=sha,
        status="SUCCEEDED",
        result_data={
            "review": {
                "verdict": "GREEN",
                "ready_for_integration": True,
                "required_remediation": [],
            }
        },
    )


def _config(tmp_path: Path, workers: tuple[WorkerConfig, ...]) -> RunnerConfig:
    return RunnerConfig(
        workers=workers,
        result_dir=str(tmp_path / "results"),
    )


def test_yaml_worker_config_loads_provider_runtime_model_and_redacts_auth(tmp_path: Path):
    config_path = tmp_path / "workers.yaml"
    config_path.write_text(
        """
routing:
  policy_id: deterministic-v1
  version: "1"
poll_seconds: 1.5
max_remediation_cycles: 4
auto_push_allowed: true
main_ref: trunk
remote_name: upstream
upstream_remote: deploy
push_upstream: true
run_validation: false
validation_timeout_seconds: 123
max_execution_attempts: 5
cleanup_branches: true
task_branches: true
providers:
  xai:
    enabled: true
    availability: AVAILABLE
    auth:
      source: environment
      variable: XAI_API_KEY
      value: must-not-be-public
runtimes:
  grok-cli:
    harness: subprocess
models:
  grok-code-fast:
    provider: xai
    capabilities: [FAST, CODING]
stages:
  implementation:
    capabilities: [CODING]
workers:
  - id: builder-xai
    enabled: true
    runtime: grok-cli
    provider: xai
    model: grok-code-fast
    adapter: fake
    capabilities: [CODING, FAST]
    stages: [implementation]
    max_concurrency: 2
    env:
      XAI_API_KEY:
        source: environment
        variable: XAI_API_KEY
""",
        encoding="utf-8",
    )

    config = RunnerConfig.from_file(config_path)
    public = config.public_summary()

    assert config.workers[0].worker_id == "builder-xai"
    assert config.workers[0].max_concurrency == 2
    assert config.poll_seconds == 1.5
    assert config.max_remediation_cycles == 4
    assert config.auto_push_allowed is True
    assert config.main_ref == "trunk"
    assert config.remote_name == "upstream"
    assert config.upstream_remote == "deploy"
    assert config.push_upstream is True
    assert config.run_validation is False
    assert config.validation_timeout_seconds == 123
    assert config.max_execution_attempts == 5
    assert config.cleanup_branches is True
    assert config.task_branches is True
    assert public["providers"]["xai"]["auth"]["value"] == "<redacted-ref>"
    assert public["workers"][0]["env"]["XAI_API_KEY"] == "<redacted-ref>"
    assert "must-not-be-public" not in json.dumps(public)


def test_provider_mode_alias_loads_active_fallback_disabled_policy(tmp_path: Path):
    config_path = tmp_path / "workers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {"mode": "ACTIVE"},
                    "anthropic": {"consumption_mode": "FALLBACK"},
                    "xai": {"mode": "DISABLED"},
                },
                "workers": [{"id": "builder-a", "role": "BUILDER", "adapter": "fake"}],
            }
        ),
        encoding="utf-8",
    )

    config = RunnerConfig.from_file(config_path)
    public = config.public_summary()

    assert config.providers["openai"].consumption_mode == "ACTIVE"
    assert config.providers["anthropic"].consumption_mode == "FALLBACK"
    assert config.providers["xai"].consumption_mode == "DISABLED"
    assert public["providers"]["anthropic"]["mode"] == "FALLBACK"


def test_route_explain_reports_eligible_and_ineligible_reasons():
    workers = (
        WorkerConfig("builder-fast", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=20),
        WorkerConfig("reviewer", "REVIEWER", adapter="fake", capabilities=(CAP_CODE_REVIEW,), stages=("review",)),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    assert decision.selected_worker_id == "builder-fast"
    reasons = {item.worker_id: item.reasons for item in decision.candidates}
    assert reasons["builder-fast"] == ("eligible",)
    assert "stage_not_allowed" in reasons["reviewer"]


def test_evidence_score_prefers_more_reliable_faster_worker_when_policy_ties():
    workers = (
        WorkerConfig("builder-a", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=10),
        WorkerConfig("builder-b", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=10),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
        evidence_by_worker={
            "builder-a": WorkerEvidence(reliability=0.80, latency_ms=120000, capability_fit=1.0, failure_rate=0.20, cost=1.0, sample_size=10, source="test"),
            "builder-b": WorkerEvidence(reliability=0.95, latency_ms=30000, capability_fit=1.0, failure_rate=0.05, cost=0.2, sample_size=10, source="test"),
        },
    )

    assert decision.selected_worker_id == "builder-b"
    candidates = {candidate.worker_id: candidate.to_dict() for candidate in decision.candidates}
    assert candidates["builder-b"]["routing_score"] > candidates["builder-a"]["routing_score"]
    assert "reliability:0.950" in candidates["builder-b"]["score_reasons"]


def test_operator_preferred_worker_wins_before_evidence_score():
    workers = (
        WorkerConfig("builder-a", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=10),
        WorkerConfig("builder-b", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=10),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,), preferred_workers=("builder-a",)),
        providers={},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
        evidence_by_worker={
            "builder-a": WorkerEvidence(reliability=0.50, latency_ms=120000, capability_fit=1.0, failure_rate=0.50, cost=5.0, sample_size=10, source="test"),
            "builder-b": WorkerEvidence(reliability=0.99, latency_ms=1000, capability_fit=1.0, failure_rate=0.01, cost=0.1, sample_size=10, source="test"),
        },
    )

    assert decision.selected_worker_id == "builder-a"


def test_fallback_provider_preserves_capacity_while_active_provider_is_eligible():
    workers = (
        WorkerConfig("builder-fallback", "BUILDER", provider="anthropic", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
        WorkerConfig("builder-active", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=99),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={
            "anthropic": ProviderConfig("anthropic", consumption_mode="FALLBACK"),
            "openai": ProviderConfig("openai", consumption_mode="ACTIVE"),
        },
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    assert decision.selected_worker_id == "builder-active"
    audit = decision.to_audit_dict(next(worker for worker in workers if worker.worker_id == decision.selected_worker_id))
    candidates = {candidate["worker_id"]: candidate for candidate in audit["candidates"]}
    assert candidates["builder-fallback"]["eligible"] is True
    assert candidates["builder-fallback"]["provider_mode"] == "FALLBACK"
    assert candidates["builder-active"]["provider_mode"] == "ACTIVE"


def test_quota_exhaustion_excludes_provider_and_falls_back_to_other_provider():
    workers = (
        WorkerConfig("builder-xai", "BUILDER", provider="xai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
        WorkerConfig("builder-openai", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={"xai": ProviderConfig("xai", availability="QUOTA_EXHAUSTED")},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    assert decision.selected_worker_id == "builder-openai"
    assert any(
        item.worker_id == "builder-xai" and "provider_QUOTA_EXHAUSTED" in item.reasons
        for item in decision.candidates
    )


def test_no_change_deprioritization_prefers_different_worker_same_provider():
    workers = (
        WorkerConfig("builder-openai-a", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
        WorkerConfig("builder-openai-b", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE")},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
        deprioritized_workers={"builder-openai-a"},
    )

    assert decision.selected_worker_id == "builder-openai-b"


def test_no_change_deprioritization_prefers_different_provider_when_available():
    workers = (
        WorkerConfig("builder-openai-a", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
        WorkerConfig("builder-openai-b", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
        WorkerConfig("builder-anthropic", "BUILDER", provider="anthropic", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=99),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={
            "openai": ProviderConfig("openai", availability="AVAILABLE"),
            "anthropic": ProviderConfig("anthropic", availability="AVAILABLE"),
        },
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
        deprioritized_workers={"builder-openai-a"},
        deprioritized_providers={"openai"},
    )

    assert decision.selected_worker_id == "builder-anthropic"


def test_no_change_event_deprioritizes_worker_on_next_attempt(tmp_path: Path):
    config = _config(
        tmp_path,
        (
            WorkerConfig("builder-openai-a", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
            WorkerConfig("builder-openai-b", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
        ),
    )
    runner = BuildRunner(SessionLocal, config, executors={}, git=FakeGit())
    with SessionLocal() as session:
        upsert_task(session, _task("NOCHANGE-RETRY"))
        record_event(
            session,
            EventInput(
                task_id="NOCHANGE-RETRY",
                event_type="runner.no_changes_produced",
                actor="runner",
                event_data={
                    "provider": "openai",
                    "worker_id": "builder-openai-a",
                    "retry_generation": 0,
                    "attempt": 1,
                },
            ),
        )
        session.commit()

        worker, availability, _decision = runner._select_worker(
            "BUILDER",
            task_id="NOCHANGE-RETRY",
            session=session,
            deprioritized_workers=runner._no_change_workers(session, "NOCHANGE-RETRY"),
        )

    assert availability == "selected"
    assert worker is not None
    assert worker.worker_id == "builder-openai-b"


def test_auth_failure_blocks_fallback_to_other_provider():
    workers = (
        WorkerConfig("builder-xai", "BUILDER", provider="xai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
        WorkerConfig("builder-openai", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={"xai": ProviderConfig("xai", availability="AUTH_FAILURE")},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    assert decision.selected_worker_id is None
    assert decision.availability == "blocked_no_fallback"
    assert any(
        item.worker_id == "builder-xai" and "provider_AUTH_FAILURE" in item.reasons
        for item in decision.candidates
    )


def test_independent_review_prefers_different_provider_and_audits_reason(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("REVIEW-XPROVIDER"))
        claim_task(session, ClaimRequest("REVIEW-XPROVIDER", worker_id="builder-xai", provider="xai"))
        session.commit()

        workers = (
            WorkerConfig("builder-xai", "BUILDER", provider="xai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
            WorkerConfig("reviewer-xai", "REVIEWER", provider="xai", adapter="fake", capabilities=(CAP_CODE_REVIEW,), stages=("review",), preference=1),
            WorkerConfig("reviewer-openai", "REVIEWER", provider="openai", adapter="fake", capabilities=(CAP_CODE_REVIEW,), stages=("review",), preference=99),
        )

        decision = route_worker(
            workers,
            stage="review",
            stage_requirement=StageRequirement("review", capabilities=(CAP_CODE_REVIEW,)),
            providers={},
            runtimes={},
            routing_policy=RunnerConfig().routing_policy,
            session=session,
            task_id="REVIEW-XPROVIDER",
            excluded_workers={"builder-xai"},
        )

    assert decision.selected_worker_id == "reviewer-openai"
    audit = decision.to_audit_dict(next(worker for worker in workers if worker.worker_id == decision.selected_worker_id))
    assert audit["selected_provider"] == "openai"
    reasons = {candidate["worker_id"]: candidate["reasons"] for candidate in audit["candidates"]}
    assert "preferred_different_provider_than_builder" in reasons["reviewer-openai"]
    assert "preferred_different_provider_than_builder" not in reasons["reviewer-xai"]


def test_independent_review_still_routes_when_only_builders_provider_exists(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("REVIEW-ONE-PROVIDER"))
        claim_task(session, ClaimRequest("REVIEW-ONE-PROVIDER", worker_id="builder-xai", provider="xai"))
        session.commit()

        workers = (
            WorkerConfig("builder-xai", "BUILDER", provider="xai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
            WorkerConfig("reviewer-xai", "REVIEWER", provider="xai", adapter="fake", capabilities=(CAP_CODE_REVIEW,), stages=("review",), preference=1),
        )

        decision = route_worker(
            workers,
            stage="review",
            stage_requirement=StageRequirement("review", capabilities=(CAP_CODE_REVIEW,)),
            providers={},
            runtimes={},
            routing_policy=RunnerConfig().routing_policy,
            session=session,
            task_id="REVIEW-ONE-PROVIDER",
            excluded_workers={"builder-xai"},
        )

    assert decision.selected_worker_id == "reviewer-xai"
    reasons = {candidate.worker_id: candidate.reasons for candidate in decision.candidates}
    assert reasons["reviewer-xai"] == ("eligible",)


def test_provider_independent_review_rejects_builders_provider(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="REVIEW-PROVIDER",
                title="Task REVIEW-PROVIDER",
                description="provider independence",
                acceptance_criteria=["passes"],
                review_policy="INDEPENDENT_PROVIDER",
            ),
        )
        claim_task(session, ClaimRequest("REVIEW-PROVIDER", worker_id="builder-a", provider="openai"))
        session.get(BuildTask, "REVIEW-PROVIDER").state = "REVIEW_READY"

        try:
            claim_review(session, ClaimRequest("REVIEW-PROVIDER", worker_id="reviewer-b", provider="openai"))
        except CoordinatorPolicyError as exc:
            assert "Provider-independent review" in str(exc)
        else:
            raise AssertionError("same-provider reviewer should be rejected")

        claim = claim_review(session, ClaimRequest("REVIEW-PROVIDER", worker_id="reviewer-c", provider="anthropic"))

    assert claim.worker_id == "reviewer-c"


def test_two_provider_approvals_count_distinct_providers_and_exact_sha(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="TWO-PROVIDERS",
                title="Task TWO-PROVIDERS",
                description="approval counting",
                acceptance_criteria=["passes"],
                review_policy="TWO_PROVIDERS",
            ),
        )
        claim_task(session, ClaimRequest("TWO-PROVIDERS", worker_id="builder-a", provider="openai"))
        session.add(_approval("TWO-PROVIDERS", "reviewer-a", "anthropic"))
        session.add(_approval("TWO-PROVIDERS", "reviewer-b", "anthropic"))
        session.add(_approval("TWO-PROVIDERS", "reviewer-c", "xai", sha="old-sha"))
        session.commit()

        assert approving_reviewers(session, "TWO-PROVIDERS", reviewed_feature_sha="feature-sha") == {
            "reviewer-a",
            "reviewer-b",
        }
        assert approving_providers(session, "TWO-PROVIDERS", reviewed_feature_sha="feature-sha") == {"anthropic"}


def test_cross_provider_resume_state_can_route_to_different_eligible_worker(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("RESUME-XPROVIDER"))
        claim = claim_task(session, ClaimRequest("RESUME-XPROVIDER", worker_id="builder-xai", provider="xai"))
        checkpoint(
            session,
            claim.claim_id,
            worker_id="builder-xai",
            data=CheckpointInput(current_step="partial work", current_head_sha="abc123"),
        )
        claim.lease_expires_at = utcnow()
        session.commit()
        recover_expired(session)
        session.commit()

    config = _config(
        tmp_path,
        (
            WorkerConfig("builder-xai", "BUILDER", provider="xai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
            WorkerConfig("builder-openai", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
        ),
    )
    config = RunnerConfig(
        workers=config.workers,
        providers={"xai": ProviderConfig("xai", availability="QUOTA_EXHAUSTED")},
        result_dir=config.result_dir,
    )

    result = BuildRunner(SessionLocal, config, executors={"builder-openai": FakeExecutor()}, git=FakeGit()).run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        execution = session.scalar(select(BuildRunnerExecution))
        assert execution.worker_id == "builder-openai"
        assert execution.provider == "openai"
        assert execution.result_data["routing"]["selected_worker"] == "builder-openai"


def test_routing_audit_reports_active_worker_and_provider_usage(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("ACTIVE-USAGE"))
        claim_task(session, ClaimRequest("ACTIVE-USAGE", worker_id="builder-openai", provider="openai"))
        session.commit()

        workers = (
            WorkerConfig("builder-openai", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), max_concurrency=2),
            WorkerConfig("builder-openai-2", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
        )
        decision = route_worker(
            workers,
            stage="implementation",
            stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
            providers={"openai": ProviderConfig("openai", consumption_mode="ACTIVE")},
            runtimes={},
            routing_policy=RunnerConfig().routing_policy,
            session=session,
        )

    candidates = {candidate.worker_id: candidate.to_dict() for candidate in decision.candidates}
    assert candidates["builder-openai"]["active_workers"] == 1
    assert candidates["builder-openai"]["active_provider_workers"] == 1
    assert candidates["builder-openai"]["provider_mode"] == "ACTIVE"


def test_active_sql_worker_lease_occupies_distributed_worker_slot(tmp_path: Path):
    with SessionLocal() as session:
        session.add(
            BuildWorkerLease(
                worker_id="builder-openai",
                provider="openai",
                slot_index=0,
                machine_id="remote-host",
                process_id="1234",
                lease_expires_at=utcnow() + timedelta(minutes=5),
                status="ACTIVE",
            )
        )
        session.commit()

        workers = (
            WorkerConfig("builder-openai", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), max_concurrency=1),
            WorkerConfig("builder-openai-2", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), max_concurrency=1),
        )
        decision = route_worker(
            workers,
            stage="implementation",
            stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
            providers={"openai": ProviderConfig("openai", consumption_mode="ACTIVE")},
            runtimes={},
            routing_policy=RunnerConfig().routing_policy,
            session=session,
        )

    assert decision.selected_worker_id == "builder-openai-2"
    candidates = {candidate.worker_id: candidate.to_dict() for candidate in decision.candidates}
    assert "max_concurrency_reached" in candidates["builder-openai"]["reasons"]
    assert candidates["builder-openai"]["active_workers"] == 1


def test_launch_reserves_worker_slot_before_external_executor_starts(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("ATOMIC-SLOT"))
        claim = claim_task(session, ClaimRequest("ATOMIC-SLOT", worker_id="builder-openai", provider="openai"))
        claim_id = claim.claim_id
        session.add(
            BuildWorkerLease(
                worker_id="builder-openai",
                provider="openai",
                slot_index=0,
                machine_id="remote-host",
                process_id="1234",
                lease_expires_at=utcnow() + timedelta(minutes=5),
                status="ACTIVE",
            )
        )
        session.commit()

    class ShouldNotLaunch:
        adapter_name = "fake"

        def launch(self, launch):
            raise AssertionError("executor launch must not run when SQL worker slots are full")

        def poll(self, execution_id):
            raise AssertionError("poll not expected")

    worker = WorkerConfig(
        "builder-openai",
        "BUILDER",
        provider="openai",
        adapter="fake",
        capabilities=(CAP_CODING,),
        stages=("implementation",),
        max_concurrency=1,
    )
    runner = BuildRunner(SessionLocal, _config(tmp_path, (worker,)), executors={"builder-openai": ShouldNotLaunch()}, git=FakeGit())
    result = RunnerCycleResult(mode="RUNNING")
    with SessionLocal() as session:
        runner._launch(
            session,
            result,
            "ATOMIC-SLOT",
            "BUILDER",
            worker,
            claim_id,
            "do work",
        )
        session.commit()

    assert result.launched == []
    assert result.capacity_full is True
    with SessionLocal() as session:
        assert session.scalar(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "ATOMIC-SLOT")) is None
        active_leases = session.scalars(
            select(BuildWorkerLease).where(
                BuildWorkerLease.worker_id == "builder-openai",
                BuildWorkerLease.status == "ACTIVE",
            )
        ).all()
        assert len(active_leases) == 1
        assert active_leases[0].task_id is None


def test_execution_and_worker_lease_are_durable_before_executor_launch(tmp_path: Path):
    with SessionLocal() as session:
        upsert_task(session, _task("DURABLE-LAUNCH"))
        session.commit()

    class ObservingExecutor:
        adapter_name = "fake"

        def launch(self, launch):
            with SessionLocal() as verify:
                execution = verify.get(BuildRunnerExecution, launch.execution_id)
                lease = verify.scalar(
                    select(BuildWorkerLease).where(
                        BuildWorkerLease.execution_id == launch.execution_id,
                        BuildWorkerLease.status == "ACTIVE",
                    )
                )
                assert execution is not None
                assert execution.task_id == "DURABLE-LAUNCH"
                assert execution.status == "LAUNCHED"
                assert lease is not None
                assert lease.worker_id == launch.worker_id
            return ExecutionHandle(
                execution_id=launch.execution_id,
                process_id="observed",
                result_path=launch.result_path,
            )

        def poll(self, execution_id):
            raise AssertionError("poll not expected")

    worker = WorkerConfig(
        "builder-openai",
        "BUILDER",
        provider="openai",
        adapter="fake",
        capabilities=(CAP_CODING,),
        stages=("implementation",),
        max_concurrency=1,
    )
    runner = BuildRunner(
        SessionLocal,
        _config(tmp_path, (worker,)),
        executors={"builder-openai": ObservingExecutor()},
        git=FakeGit(),
    )

    result = runner.run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        execution = session.get(BuildRunnerExecution, result.launched[0])
        lease = session.scalar(
            select(BuildWorkerLease).where(BuildWorkerLease.execution_id == result.launched[0])
        )
        assert execution is not None
        assert execution.process_id == "observed"
        assert lease is not None
        assert lease.status == "ACTIVE"


def test_expired_active_worker_lease_does_not_block_slot_reuse(tmp_path: Path):
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("STALE-SLOT"))
        session.add(
            BuildWorkerLease(
                worker_id="builder-openai",
                provider="openai",
                slot_index=0,
                machine_id="dead-host",
                process_id="dead",
                task_id="OLD-TASK",
                execution_id="old-exec",
                lease_expires_at=now - timedelta(minutes=5),
                status="ACTIVE",
            )
        )
        session.commit()

    worker = WorkerConfig(
        "builder-openai",
        "BUILDER",
        provider="openai",
        adapter="fake",
        capabilities=(CAP_CODING,),
        stages=("implementation",),
        max_concurrency=1,
    )
    executor = FakeExecutor()
    runner = BuildRunner(SessionLocal, _config(tmp_path, (worker,)), executors={"builder-openai": executor}, git=FakeGit())

    result = runner.run_once()

    assert len(result.launched) == 1
    assert result.capacity_full is False
    with SessionLocal() as session:
        old_lease = session.scalar(select(BuildWorkerLease).where(BuildWorkerLease.execution_id == "old-exec"))
        new_lease = session.scalar(select(BuildWorkerLease).where(BuildWorkerLease.execution_id == result.launched[0]))
        assert old_lease is not None
        assert old_lease.status == "EXPIRED"
        assert new_lease is not None
        assert new_lease.status == "ACTIVE"
        assert new_lease.task_id == "STALE-SLOT"


def test_launch_failure_releases_reserved_worker_slot(tmp_path: Path):
    class FailingExecutor:
        adapter_name = "fake"

        def launch(self, launch):
            raise RuntimeError("boom")

        def poll(self, execution_id):
            raise AssertionError("poll not expected")

    with SessionLocal() as session:
        upsert_task(session, _task("LAUNCH-FAIL"))
        session.commit()

    worker = WorkerConfig(
        "builder-openai",
        "BUILDER",
        provider="openai",
        adapter="fake",
        capabilities=(CAP_CODING,),
        stages=("implementation",),
        max_concurrency=1,
    )
    runner = BuildRunner(SessionLocal, _config(tmp_path, (worker,)), executors={"builder-openai": FailingExecutor()}, git=FakeGit())

    try:
        runner.run_once()
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected executor launch failure")

    with SessionLocal() as session:
        assert session.scalars(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "LAUNCH-FAIL")).all() == []
        leases = session.scalars(select(BuildWorkerLease).where(BuildWorkerLease.worker_id == "builder-openai")).all()
        assert leases == []


def test_recovery_paths_release_worker_leases(tmp_path: Path):
    now = utcnow()
    with SessionLocal() as session:
        upsert_task(session, _task("LEASE-EXPIRED"))
        expired_claim = claim_task(session, ClaimRequest("LEASE-EXPIRED", worker_id="builder-a"))
        expired_claim.lease_expires_at = now - timedelta(seconds=1)
        session.add(
            BuildRunnerExecution(
                execution_id="exec-expired",
                task_id="LEASE-EXPIRED",
                role="BUILDER",
                worker_id="builder-a",
                provider="openai",
                adapter="fake",
                claim_id=expired_claim.claim_id,
                status="RUNNING",
            )
        )
        session.add(
            BuildWorkerLease(
                worker_id="builder-a",
                provider="openai",
                slot_index=0,
                task_id="LEASE-EXPIRED",
                execution_id="exec-expired",
                lease_expires_at=now + timedelta(minutes=5),
                status="ACTIVE",
            )
        )

        upsert_task(session, _task("LEASE-STALE"))
        stale_claim = claim_task(session, ClaimRequest("LEASE-STALE", worker_id="builder-b"))
        stale_claim.status = "EXPIRED"
        stale_claim.lease_expires_at = now - timedelta(seconds=1)
        session.add(
            BuildRunnerExecution(
                execution_id="exec-stale",
                task_id="LEASE-STALE",
                role="BUILDER",
                worker_id="builder-b",
                provider="openai",
                adapter="fake",
                claim_id=stale_claim.claim_id,
                status="RUNNING",
            )
        )
        session.add(
            BuildWorkerLease(
                worker_id="builder-b",
                provider="openai",
                slot_index=0,
                task_id="LEASE-STALE",
                execution_id="exec-stale",
                lease_expires_at=now + timedelta(minutes=5),
                status="ACTIVE",
            )
        )

        upsert_task(session, _task("LEASE-LOST"))
        lost_claim = claim_task(session, ClaimRequest("LEASE-LOST", worker_id="builder-c"))
        session.add(
            BuildRunnerExecution(
                execution_id="exec-lost",
                task_id="LEASE-LOST",
                role="BUILDER",
                worker_id="builder-c",
                provider="openai",
                adapter="fake",
                claim_id=lost_claim.claim_id,
                status="LOST",
                completed_at=now,
                result_data={"reconciliation_state": "LOST"},
            )
        )
        session.add(
            BuildWorkerLease(
                worker_id="builder-c",
                provider="openai",
                slot_index=0,
                task_id="LEASE-LOST",
                execution_id="exec-lost",
                lease_expires_at=now + timedelta(minutes=5),
                status="ACTIVE",
            )
        )
        session.commit()

        recover_expired(session)
        reconcile_stale_executions(session)
        recover_lost_execution_claims(session)
        session.commit()

    with SessionLocal() as session:
        statuses = {
            lease.execution_id: lease.status
            for lease in session.scalars(select(BuildWorkerLease)).all()
        }
        assert statuses == {
            "exec-expired": "EXPIRED",
            "exec-stale": "EXPIRED",
            "exec-lost": "RELEASED",
        }


def test_runner_diagnostics_reports_provider_modes_usage_and_failure_reset(tmp_path: Path):
    config = _config(
        tmp_path,
        (
            WorkerConfig("builder-active", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
            WorkerConfig("builder-fallback", "BUILDER", provider="anthropic", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
        ),
    )
    config = RunnerConfig(
        workers=config.workers,
        providers={
            "openai": ProviderConfig("openai", consumption_mode="ACTIVE"),
            "anthropic": ProviderConfig("anthropic", consumption_mode="FALLBACK"),
        },
        result_dir=config.result_dir,
    )
    reset_at = utcnow() + timedelta(minutes=10)
    with SessionLocal() as session:
        upsert_task(session, _task("DIAG-USAGE"))
        claim_task(session, ClaimRequest("DIAG-USAGE", worker_id="builder-active", provider="openai"))
        record_event(
            session,
            EventInput(
                task_id="DIAG-USAGE",
                event_type="runner.provider_failure",
                actor="runner",
                event_data={
                    "provider": "anthropic",
                    "failure": "RATE_LIMITED",
                    "until": reset_at.isoformat(),
                },
            ),
        )
        session.commit()
        diagnostics = BuildRunner(SessionLocal, config, executors={}, git=FakeGit()).diagnostics(session)

    assert diagnostics["providers"]["openai"]["mode"] == "ACTIVE"
    assert diagnostics["providers"]["openai"]["active_workers"] == 1
    assert diagnostics["providers"]["anthropic"]["mode"] == "FALLBACK"
    assert diagnostics["providers"]["anthropic"]["availability"] == "RATE_LIMITED"
    assert diagnostics["providers"]["anthropic"]["active_failure"]["failure"] == "RATE_LIMITED"
    assert diagnostics["workers"][0]["active_provider_workers"] == 1


def test_runner_diagnostics_reports_cumulative_launches_per_worker_and_provider(tmp_path: Path):
    config = _config(
        tmp_path,
        (WorkerConfig("builder-a", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),),
    )
    runner = BuildRunner(SessionLocal, config, executors={"builder-a": FakeExecutor()}, git=FakeGit())
    with SessionLocal() as session:
        upsert_task(session, _task("DIAG-LAUNCHES-1"))
        session.commit()
    runner.run_once()

    with SessionLocal() as session:
        upsert_task(session, _task("DIAG-LAUNCHES-2"))
        session.commit()
    runner.run_once()

    with SessionLocal() as session:
        diagnostics = runner.diagnostics(session)

    worker = next(w for w in diagnostics["workers"] if w["worker_id"] == "builder-a")
    assert worker["launches"] == 2
    assert diagnostics["providers"]["openai"]["launches"] == 2


def test_historical_unknown_provider_failure_is_ignored_for_provider_health(tmp_path: Path):
    config = _config(
        tmp_path,
        (
            WorkerConfig("builder-openai-a", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=1),
            WorkerConfig("builder-openai-b", "BUILDER", provider="openai", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",), preference=2),
        ),
    )
    runner = BuildRunner(SessionLocal, config, executors={}, git=FakeGit())
    with SessionLocal() as session:
        record_event(
            session,
            EventInput(
                task_id=None,
                event_type="runner.provider_failure",
                actor="test",
                event_data={
                    "provider": "openai",
                    "worker_id": "builder-openai-a",
                    "failure": "NO_CHANGES_PRODUCED",
                    "until": (utcnow() + timedelta(minutes=30)).isoformat(),
                },
            ),
        )
        session.commit()

        providers = runner._effective_providers(session)
        worker, availability, decision = runner._select_worker("BUILDER", session=session)

    assert providers["openai"].availability == "AVAILABLE"
    assert availability == "selected"
    assert worker is not None
    assert worker.worker_id == "builder-openai-a"
    assert all(
        "provider_NO_CHANGES_PRODUCED" not in candidate.reasons
        for candidate in decision.candidates
    )


def test_runner_audit_event_records_routing_without_secrets(tmp_path: Path):
    os.environ["BUILD_COORDINATOR_RESULT_DIR"] = str(tmp_path / "results")
    with SessionLocal() as session:
        upsert_task(session, _task("AUDIT-ROUTE"))
        session.commit()

    config = _config(
        tmp_path,
        (
            WorkerConfig(
                "builder-a",
                "BUILDER",
                provider="xai",
                adapter="fake",
                capabilities=(CAP_CODING,),
                stages=("implementation",),
                env={"XAI_API_KEY": {"source": "environment", "variable": "XAI_API_KEY"}},
            ),
        ),
    )

    BuildRunner(SessionLocal, config, executors={"builder-a": FakeExecutor()}, git=FakeGit()).run_once()

    with SessionLocal() as session:
        event = session.scalar(select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.execution_launched"))
        lease = session.scalar(select(BuildWorkerLease).where(BuildWorkerLease.worker_id == "builder-a"))
        payload = event.event_data
        assert payload["routing"]["required_capabilities"] == [CAP_CODING]
        assert payload["provider"] == "xai"
        assert lease is not None
        assert lease.status == "ACTIVE"
        assert "XAI_API_KEY" not in json.dumps(payload)


def test_worker_env_refs_are_resolved_only_for_launch(monkeypatch, tmp_path: Path):
    class CapturingExecutor:
        adapter_name = "fake"

        def __init__(self):
            self.extra_env = None

        def launch(self, launch):
            self.extra_env = dict(launch.extra_env)
            return ExecutionHandle(execution_id=launch.execution_id, result_path=launch.result_path)

        def poll(self, execution_id):
            raise AssertionError("poll not expected")

        def terminate(self, execution_id):
            raise AssertionError("terminate not expected")

    monkeypatch.setenv("XAI_API_KEY", "secret-value")
    with SessionLocal() as session:
        upsert_task(session, _task("ENV-ROUTE"))
        session.commit()

    executor = CapturingExecutor()
    config = _config(
        tmp_path,
        (
            WorkerConfig(
                "builder-a",
                "BUILDER",
                provider="xai",
                adapter="fake",
                capabilities=(CAP_CODING,),
                stages=("implementation",),
                env={
                    "XAI_API_KEY": {"source": "environment", "variable": "XAI_API_KEY"},
                    "GROK_HOME": {"source": "literal_path", "path": "C:/runtime"},
                },
            ),
        ),
    )

    BuildRunner(SessionLocal, config, executors={"builder-a": executor}, git=FakeGit()).run_once()

    assert executor.extra_env == {"XAI_API_KEY": "secret-value", "GROK_HOME": "C:/runtime"}
    with SessionLocal() as session:
        execution = session.scalar(select(BuildRunnerExecution))
        event = session.scalar(select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.execution_launched"))
        persisted = json.dumps({"execution": execution.result_data, "event": event.event_data})
        assert "secret-value" not in persisted
        assert "XAI_API_KEY" not in persisted


def test_stage_permissions_gate_integration_workers():
    workers = (
        WorkerConfig("integration-readonly", "CUSTOM", adapter="fake", capabilities=(CAP_SCM_OPERATOR,), stages=("integration",), permissions=()),
        WorkerConfig("integration-writer", "CUSTOM", adapter="fake", capabilities=(CAP_SCM_OPERATOR,), stages=("integration",), permissions=("SCM_WRITE",)),
    )

    decision = route_worker(
        workers,
        stage="integration",
        stage_requirement=StageRequirement("integration", capabilities=(CAP_SCM_OPERATOR,), permissions=("SCM_WRITE",)),
        providers={},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    assert decision.selected_worker_id == "integration-writer"
    assert any(
        item.worker_id == "integration-readonly" and "missing_permissions:SCM_WRITE" in item.reasons
        for item in decision.candidates
    )


def test_json_config_does_not_require_yaml_parser(tmp_path: Path):
    config_path = tmp_path / "workers.json"
    config_path.write_text(
        json.dumps({"workers": [{"worker_id": "builder-a", "role": "BUILDER", "adapter": "fake"}]}),
        encoding="utf-8",
    )

    config = RunnerConfig.from_file(config_path)

    assert config.workers[0].worker_id == "builder-a"


def test_routing_explanation_can_include_stage_ineligible_workers():
    workers = (
        WorkerConfig("builder-a", "BUILDER", adapter="fake", capabilities=(CAP_CODING,), stages=("implementation",)),
        WorkerConfig("reviewer-1", "REVIEWER", adapter="fake", capabilities=(CAP_CODE_REVIEW,), stages=("review",)),
    )

    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={},
        runtimes={},
        routing_policy=RunnerConfig().routing_policy,
    )

    reasons = {candidate.worker_id: candidate.reasons for candidate in decision.candidates}
    assert "stage_not_allowed" in reasons["reviewer-1"]


def test_retryable_provider_failures_are_a_subset_of_the_routing_taxonomy():
    assert RETRYABLE_PROVIDER_FAILURES == {"RATE_LIMITED", "UNAVAILABLE", "NETWORK_FAILURE"}
    assert RETRYABLE_PROVIDER_FAILURES <= PROVIDER_FAILURES
    # Failures that need a credential fix, quota reset, or investigation are
    # deliberately excluded from automatic retry.
    assert not RETRYABLE_PROVIDER_FAILURES & {"AUTH_FAILURE", "QUOTA_EXHAUSTED", "EXECUTION_FAILURE"}
