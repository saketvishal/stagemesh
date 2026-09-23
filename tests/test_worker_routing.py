from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionHandle, FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import (
    CAP_CODE_REVIEW,
    CAP_CODING,
    CAP_SCM_OPERATOR,
    ProviderConfig,
    StageRequirement,
    route_worker,
)
from build_coordinator.service import CheckpointInput, ClaimRequest, checkpoint, claim_task, recover_expired, upsert_task, utcnow
from build_coordinator.types import TaskSpec


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
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


def _config(tmp_path: Path, workers: tuple[WorkerConfig, ...]) -> RunnerConfig:
    return RunnerConfig(
        workers=workers,
        q_records_dir=str(tmp_path / "q"),
        result_dir=str(tmp_path / "results"),
    )


def test_yaml_worker_config_loads_provider_runtime_model_and_redacts_auth(tmp_path: Path):
    config_path = tmp_path / "workers.yaml"
    config_path.write_text(
        """
routing:
  policy_id: deterministic-v1
  version: "1"
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
    assert public["providers"]["xai"]["auth"]["value"] == "<redacted-ref>"
    assert public["workers"][0]["env"]["XAI_API_KEY"] == "<redacted-ref>"
    assert "must-not-be-public" not in json.dumps(public)


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
        q_records_dir=config.q_records_dir,
        result_dir=config.result_dir,
    )

    result = BuildRunner(SessionLocal, config, executors={"builder-openai": FakeExecutor()}, git=FakeGit()).run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        execution = session.scalar(select(BuildRunnerExecution))
        assert execution.worker_id == "builder-openai"
        assert execution.provider == "openai"
        assert execution.result_data["routing"]["selected_worker"] == "builder-openai"


def test_runner_audit_event_records_routing_without_secrets(tmp_path: Path):
    os.environ["BUILD_COORDINATOR_Q_RECORDS_DIR"] = str(tmp_path / "q")
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
        payload = event.event_data
        assert payload["routing"]["required_capabilities"] == [CAP_CODING]
        assert payload["provider"] == "xai"
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
