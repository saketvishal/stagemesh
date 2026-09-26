from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from build_coordinator.agents.profiles import (
    DISABLED,
    NOT_HEADLESS,
    NOT_INSTALLED,
    PROFILES,
    READY,
    RuntimeProfile,
    RuntimeStatus,
    probe_runtime,
)
from build_coordinator.agents.wrapper import classify_failure, sanitize_diagnostic
from build_coordinator.claims import task_is_claimable
from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import EventInput, record_event
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.execution.process_tree import attach_started_process
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.policy import VALID_TRANSITIONS, require_transition
from build_coordinator.project.backlog import SYNC_EVENT
from build_coordinator.project.commands import _continue_human_summary, _provider_capacity_waiting
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.orchestrator import RunnerCycleResult
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import (
    CAP_CODE_REVIEW,
    CAP_CODING,
    ProviderConfig,
    RETRYABLE_PROVIDER_FAILURES,
    RoutingPolicy,
    StageRequirement,
    route_worker,
)
from build_coordinator.service import (
    ClaimRequest,
    claim_task,
    recover_lost_execution_claims,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.task_source.github import GitHubTaskSource
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


def _task(task_id: str, *, deps: list[str] | None = None, priority: int = 100) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="test task",
        acceptance_criteria=["passes"],
        dependencies=deps or [],
        review_policy="INDEPENDENT",
    )


def _seed_priority(session, task_id: str, priority: int) -> None:
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type=SYNC_EVENT,
            actor="test",
            event_data={"revision": 1, "priority": priority, "action": "CREATED"},
        ),
    )


# 1. Missing worker configuration diagnostics and preflight
def test_missing_worker_configuration_diagnostics():
    config = RunnerConfig(workers=(), result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"))
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    diag = runner.diagnostics()
    assert diag["configured_concurrency"] == 0
    assert diag["has_configured_builders"] is False
    assert diag["executable_builders"] == []


# 2. Invalid external executor configuration
def test_invalid_external_executor_configuration():
    workers = [WorkerConfig("unconfigured-1", "BUILDER", adapter="unconfigured", provider="local")]
    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers={},
        runtimes={},
        routing_policy=RoutingPolicy(),
    )
    assert decision.selected_worker_id is None
    assert decision.candidates[0].reasons == ("worker_unconfigured",)


# 3. Executor discovery for all 4 runtimes
def test_executor_discovery_all_four_providers():
    assert "codex" in PROFILES
    assert "claude" in PROFILES
    assert "antigravity" in PROFILES
    assert "grok" in PROFILES

    assert PROFILES["codex"].provider == "openai"
    assert PROFILES["claude"].provider == "anthropic"
    assert PROFILES["antigravity"].provider == "google"
    assert PROFILES["grok"].provider == "xai"

    assert PROFILES["codex"].headless is True
    assert PROFILES["claude"].headless is True
    assert PROFILES["antigravity"].headless is False
    assert PROFILES["grok"].headless is True
    assert PROFILES["grok"].prompt_transport == "arg"
    assert PROFILES["codex"].prompt_transport == "stdin"
    assert PROFILES["claude"].prompt_transport == "stdin"


def test_grok_prompt_transport_and_builder_command():
    profile = PROFILES["grok"]
    assert profile.prompt_transport == "arg"
    prompt = "Implement feature X in this worktree"
    cwd = "C:/fake/worktree"
    cmd, stdin = profile.build_invocation("BUILDER", cwd, prompt)

    assert stdin is None
    assert "--no-auto-update" in cmd
    assert "-p" in cmd
    p_idx = cmd.index("-p")
    assert cmd[p_idx + 1] == prompt
    assert cmd[p_idx + 1] != "-"
    assert "--cwd" in cmd
    cwd_idx = cmd.index("--cwd")
    assert cmd[cwd_idx + 1] == cwd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "plain"
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "auto"
    assert "--always-approve" in cmd
    assert "--allow" in cmd
    assert cmd[cmd.index("--allow") + 1] == "*"


def test_grok_reviewer_command_and_permissions():
    profile = PROFILES["grok"]
    prompt = "Review commit abc123"
    cwd = "C:/fake/worktree"
    cmd, stdin = profile.build_invocation("REVIEWER", cwd, prompt)

    assert stdin is None
    assert "--no-auto-update" in cmd
    assert "-p" in cmd
    p_idx = cmd.index("-p")
    assert cmd[p_idx + 1] == prompt
    assert cmd[p_idx + 1] != "-"
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "auto"
    assert "--deny" in cmd
    assert cmd[cmd.index("--deny") + 1] == "Edit,Write"
    assert "--always-approve" not in cmd


def test_grok_model_override():
    profile = PROFILES["grok"]
    cmd = profile.command("BUILDER", "C:/fake", prompt="test", model="grok-beta")
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "grok-beta"


def test_codex_and_claude_stdin_prompt_transport():
    codex = PROFILES["codex"]
    claude = PROFILES["claude"]
    assert codex.prompt_transport == "stdin"
    assert claude.prompt_transport == "stdin"

    cmd, stdin = codex.build_invocation("BUILDER", "C:/fake", "do codex task")
    assert stdin == "do codex task"
    assert cmd[-1] == "-"

    cmd, stdin = claude.build_invocation("BUILDER", "C:/fake", "do claude task")
    assert stdin == "do claude task"
    assert "-p" in cmd


def test_grok_reviewer_verdict_parsing():
    from build_coordinator.agents.wrapper import parse_verdict
    grok_output = """
I have completed the review of commit 4220f3b224a6.
Here is the structured assessment:

```json
{
  "verdict": "GREEN",
  "findings": [],
  "required_remediation": [],
  "architecture_notes": ["Good separation of concerns"],
  "ready_for_integration": true
}
```

Everything looks solid.
"""
    parsed = parse_verdict(grok_output)
    assert parsed is not None
    assert parsed["verdict"] == "GREEN"
    assert parsed["ready_for_integration"] is True
    assert parsed["architecture_notes"] == ["Good separation of concerns"]


# 4. Provider-neutral candidate generation
def test_provider_neutral_candidate_generation():
    workers = [
        WorkerConfig("builder-claude-1", "BUILDER", adapter="subprocess", provider="anthropic", capabilities=(CAP_CODING,)),
        WorkerConfig("builder-codex-1", "BUILDER", adapter="subprocess", provider="openai", capabilities=(CAP_CODING,)),
    ]
    providers = {
        "anthropic": ProviderConfig("anthropic", availability="AVAILABLE", consumption_mode="ACTIVE"),
        "openai": ProviderConfig("openai", availability="AVAILABLE", consumption_mode="FALLBACK"),
    }
    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers=providers,
        runtimes={},
        routing_policy=RoutingPolicy(),
    )
    assert decision.availability == "selected"
    assert decision.selected_worker_id == "builder-claude-1"
    assert len(decision.candidates) == 2


# 5. ACTIVE / FALLBACK / DISABLED behavior
def test_consumption_policy_active_fallback_disabled():
    workers = [
        WorkerConfig("worker-disabled", "BUILDER", adapter="subprocess", provider="disabled-p", capabilities=(CAP_CODING,)),
        WorkerConfig("worker-fallback", "BUILDER", adapter="subprocess", provider="fallback-p", capabilities=(CAP_CODING,)),
        WorkerConfig("worker-active", "BUILDER", adapter="subprocess", provider="active-p", capabilities=(CAP_CODING,)),
    ]
    providers = {
        "disabled-p": ProviderConfig("disabled-p", consumption_mode="DISABLED"),
        "fallback-p": ProviderConfig("fallback-p", consumption_mode="FALLBACK"),
        "active-p": ProviderConfig("active-p", consumption_mode="ACTIVE"),
    }
    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers=providers,
        runtimes={},
        routing_policy=RoutingPolicy(),
    )
    assert decision.selected_worker_id == "worker-active"
    disabled_cand = next(c for c in decision.candidates if c.worker_id == "worker-disabled")
    assert "provider_disabled" in disabled_cand.reasons


# 6. Unavailable provider
def test_unavailable_provider_routing():
    workers = [
        WorkerConfig("worker-down", "BUILDER", adapter="subprocess", provider="down-p", capabilities=(CAP_CODING,)),
        WorkerConfig("worker-up", "BUILDER", adapter="subprocess", provider="up-p", capabilities=(CAP_CODING,)),
    ]
    providers = {
        "down-p": ProviderConfig("down-p", availability="UNAVAILABLE"),
        "up-p": ProviderConfig("up-p", availability="AVAILABLE"),
    }
    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers=providers,
        runtimes={},
        routing_policy=RoutingPolicy(),
    )
    assert decision.selected_worker_id == "worker-up"
    down_cand = next(c for c in decision.candidates if c.worker_id == "worker-down")
    assert "provider_UNAVAILABLE" in down_cand.reasons


# 7. Non-headless provider
def test_non_headless_provider_exclusion():
    workers = [
        WorkerConfig("builder-antigravity", "BUILDER", adapter="subprocess", provider="google", capabilities=(CAP_CODING,)),
    ]
    providers = {
        "google": ProviderConfig("google", availability="NOT_HEADLESS", consumption_mode="DISABLED"),
    }
    decision = route_worker(
        workers,
        stage="implementation",
        stage_requirement=StageRequirement("implementation", capabilities=(CAP_CODING,)),
        providers=providers,
        runtimes={},
        routing_policy=RoutingPolicy(),
    )
    assert decision.selected_worker_id is None
    assert decision.candidates[0].reasons == ("provider_disabled",)


# 8. Quota exhaustion classification
def test_quota_exhaustion_classification():
    assert classify_failure("ERROR: Grok Build usage balance exhausted") == "QUOTA_EXHAUSTED"
    assert classify_failure("ERROR: status 402 Payment Required") == "QUOTA_EXHAUSTED"
    assert classify_failure("ERROR: You’ve hit your usage limit. Upgrade to Pro") == "QUOTA_EXHAUSTED"


# 9. Rate limiting classification
def test_rate_limiting_classification():
    assert classify_failure("You've hit your session limit · resets 5:50pm") == "RATE_LIMITED"
    assert classify_failure("HTTP 429 Too Many Requests") == "RATE_LIMITED"


def test_ambiguous_retry_or_reset_text_is_not_rate_limited():
    assert classify_failure("Claude status: resets 5:50pm") == "EXECUTION_FAILURE"
    assert classify_failure("Provider reported an informational reset window; try again later if needed") == "EXECUTION_FAILURE"


def test_provider_overload_is_unavailable_not_rate_limited():
    assert classify_failure("Anthropic overloaded, please try again later") == "UNAVAILABLE"
    assert classify_failure("HTTP 503 Service Unavailable") == "UNAVAILABLE"


def test_transient_provider_failures_are_retryable_taxonomy():
    assert RETRYABLE_PROVIDER_FAILURES == frozenset(
        {"RATE_LIMITED", "UNAVAILABLE", "NETWORK_FAILURE"}
    )


def test_provider_diagnostics_are_sanitized_before_persistence():
    detail = sanitize_diagnostic("request failed authorization: Bearer sk-test-secret-token-1234567890")
    assert "sk-test-secret-token" not in detail
    assert "Bearer <redacted>" in detail


# 10. Execution failure classification
def test_execution_failure_classification():
    assert classify_failure("SyntaxError: unexpected token") == "EXECUTION_FAILURE"


# 11. Fallback to another provider
def test_fallback_to_another_provider():
    executors = {
        "builder-claude-1": FakeExecutor([
            ExecutionObservation("FAILED", result_data={"provider_failure": "RATE_LIMITED", "detail": "session limit"})
        ]),
        "builder-codex-1": FakeExecutor([
            ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "abc123"})
        ]),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-claude-1", "BUILDER", adapter="fake", provider="anthropic", capabilities=(CAP_CODING,)),
            WorkerConfig("builder-codex-1", "BUILDER", adapter="fake", provider="openai", capabilities=(CAP_CODING,)),
        ),
        providers={
            "anthropic": ProviderConfig("anthropic", availability="AVAILABLE", consumption_mode="ACTIVE"),
            "openai": ProviderConfig("openai", availability="AVAILABLE", consumption_mode="FALLBACK"),
        },
        max_execution_attempts=1,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-FALLBACK"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    # Cycle 1: dispatches to Claude, which fails with RATE_LIMITED
    res1 = runner.run_once()
    assert len(res1.launched) == 1

    # Cycle 2: runner recovers lost claim and routes to Codex
    res2 = runner.run_once()
    assert len(res2.launched) == 1

    # Cycle 3: runner observes Codex succeeding
    res3 = runner.run_once()
    assert len(res3.observed) == 1
    with SessionLocal() as session:
        execs = session.scalars(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "TASK-FALLBACK")).all()
        assert len(execs) == 2
        assert execs[0].worker_id == "builder-claude-1" and execs[0].status == "LOST"
        assert execs[1].worker_id == "builder-codex-1" and execs[1].status == "SUCCEEDED"


def test_task_scoped_no_changes_failure_does_not_emit_provider_failure():
    executors = {
        "builder-codex-1": FakeExecutor([
            ExecutionObservation(
                "FAILED",
                result_data={
                    "provider_failure": "NO_CHANGES_PRODUCED",
                    "detail": "no diff after attempting task",
                },
            )
        ]),
        "builder-codex-2": FakeExecutor(),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-codex-1", "BUILDER", adapter="fake", provider="openai", capabilities=(CAP_CODING,), preference=1),
            WorkerConfig("builder-codex-2", "BUILDER", adapter="fake", provider="openai", capabilities=(CAP_CODING,), preference=2),
        ),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE", consumption_mode="ACTIVE")},
        max_execution_attempts=2,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-NO-CHANGES-FAILURE"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    assert len(runner.run_once().launched) == 1
    observed = runner.run_once()
    assert len(observed.observed) == 1
    with SessionLocal() as session:
        observed_execution = session.get(BuildRunnerExecution, observed.observed[0])
        assert observed_execution is not None
        assert observed_execution.task_id == "TASK-NO-CHANGES-FAILURE"
    assert observed.escalations == []
    # NO_CHANGES recovery may dispatch the replacement builder in the same
    # cycle that observes the failed execution. Assert the retry exists
    # without coupling the test to a one-cycle scheduling delay.
    if observed.launched:
        retry_execution_id = observed.launched[0]
    else:
        retry = runner.run_once()
        assert len(retry.launched) == 1
        retry_execution_id = retry.launched[0]

    with SessionLocal() as session:
        retry_execution = session.get(BuildRunnerExecution, retry_execution_id)
        assert retry_execution is not None
        assert retry_execution.task_id == "TASK-NO-CHANGES-FAILURE"
        assert retry_execution.worker_id == "builder-codex-2"
        provider_failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-NO-CHANGES-FAILURE")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).all()
        no_changes = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-NO-CHANGES-FAILURE")
            .where(BuildTaskEvent.event_type == "runner.no_changes_produced")
        ).all()
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "TASK-NO-CHANGES-FAILURE")
            .order_by(BuildRunnerExecution.execution_id)
        ).all()

    assert provider_failures == []
    assert len(no_changes) == 1
    assert no_changes[0].event_data["worker_id"] == "builder-codex-1"
    assert no_changes[0].event_data["provider"] == "openai"
    assert no_changes[0].event_data["retry_generation"] == 0
    assert no_changes[0].event_data["attempt"] == 1
    assert no_changes[0].event_data["detail"] == "no diff after attempting task"
    assert [execution.worker_id for execution in executions] == ["builder-codex-1", "builder-codex-2"]
    with SessionLocal() as session:
        reconciliations = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-NO-CHANGES-FAILURE")
            .where(BuildTaskEvent.event_type == "runner.no_changes_reconciled")
        ).all()
    assert len(reconciliations) == 1
    assert reconciliations[0].event_data["outcome"] == "UNRESOLVED"
    assert reconciliations[0].event_data["deterministic_recheck"] is True
    assert reconciliations[0].event_data["retry_generation"] == 0


def test_task_scoped_no_changes_failure_exhaustion_preserves_no_changes_semantics():
    executors = {
        "builder-codex-1": FakeExecutor([
            ExecutionObservation("FAILED", result_data={"provider_failure": "NO_CHANGES_PRODUCED"})
        ]),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-codex-1", "BUILDER", adapter="fake", provider="openai", capabilities=(CAP_CODING,)),
        ),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE", consumption_mode="ACTIVE")},
        max_execution_attempts=1,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-NO-CHANGES-EXHAUSTED"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    assert len(runner.run_once().launched) == 1
    result = runner.run_once()

    assert len(result.observed) == 1
    with SessionLocal() as session:
        observed_execution = session.get(BuildRunnerExecution, result.observed[0])
        assert observed_execution is not None
        assert observed_execution.task_id == "TASK-NO-CHANGES-EXHAUSTED"
    assert result.escalations == ["TASK-NO-CHANGES-EXHAUSTED:NO_CHANGES_PRODUCED"]
    with SessionLocal() as session:
        task = session.get(BuildTask, "TASK-NO-CHANGES-EXHAUSTED")
        provider_failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-NO-CHANGES-EXHAUSTED")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).all()
        blocked_event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-NO-CHANGES-EXHAUSTED")
            .where(BuildTaskEvent.to_state == "BLOCKED")
        )

    assert task is not None
    assert task.state == "BLOCKED"
    assert provider_failures == []
    assert blocked_event is not None
    assert blocked_event.event_data["reason"] == "NO_CHANGES_PRODUCED"


# 12. No infinite fallback respects max attempts
def test_no_infinite_fallback_respects_max_attempts():
    executors = {
        "builder-a": FakeExecutor([
            ExecutionObservation("FAILED", result_data={"provider_failure": "UNAVAILABLE"})
        ]),
        "builder-b": FakeExecutor([
            ExecutionObservation("FAILED", result_data={"provider_failure": "UNAVAILABLE"})
        ]),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake", provider="p-a", capabilities=(CAP_CODING,)),
            WorkerConfig("builder-b", "BUILDER", adapter="fake", provider="p-b", capabilities=(CAP_CODING,)),
        ),
        providers={
            "p-a": ProviderConfig("p-a", availability="AVAILABLE", consumption_mode="ACTIVE"),
            "p-b": ProviderConfig("p-b", availability="AVAILABLE", consumption_mode="FALLBACK"),
        },
        max_execution_attempts=2,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-MAX"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    r1 = runner.run_once()  # attempt 1 launched on builder-a
    assert len(r1.launched) == 1
    r2 = runner.run_once()  # attempt 1 failed & recovered, attempt 2 launched on builder-b
    assert len(r2.launched) == 1
    r3 = runner.run_once()  # attempt 2 failed, max attempts (2) reached -> blocked
    assert r3.escalations == ["TASK-MAX:PROVIDER_FAILURE_RETRIES_EXHAUSTED:UNAVAILABLE"]
    with SessionLocal() as session:
        task = session.get(BuildTask, "TASK-MAX")
        assert task.state == "BLOCKED"
        checkpoints = session.scalars(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "TASK-MAX")
        ).all()
        assert len(checkpoints) >= 1


def test_retryable_failure_relaunches_with_backoff_and_records_attempts():
    executors = {
        "builder-a": FakeExecutor(
            [
                ExecutionObservation("FAILED", result_data={"provider_failure": "UNAVAILABLE"}),
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "abc123"}),
            ]
        ),
    }
    config = RunnerConfig(
        workers=(WorkerConfig("builder-a", "BUILDER", adapter="fake", provider="p-a", capabilities=(CAP_CODING,)),),
        providers={"p-a": ProviderConfig("p-a", availability="AVAILABLE", consumption_mode="ACTIVE")},
        max_execution_attempts=3,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-RETRY-BACKOFF"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    r1 = runner.run_once()  # attempt 1 launched
    assert len(r1.launched) == 1
    first_execution_id = r1.launched[0]
    r2 = runner.run_once()  # attempt 1 observed as UNAVAILABLE, retried without a human gate
    assert r2.escalations == []

    with SessionLocal() as session:
        task = session.get(BuildTask, "TASK-RETRY-BACKOFF")
        assert task.state != "BLOCKED"
        # Backdate the recorded backoff window to simulate its expiry without
        # sleeping in the test.
        failure_event = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-RETRY-BACKOFF")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).one()
        failure_event.event_data = {
            **failure_event.event_data,
            "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        session.commit()

    r3 = runner.run_once()  # attempt 2 launched on the same worker once backoff has elapsed
    assert len(r3.launched) == 1
    second_execution_id = r3.launched[0]

    with SessionLocal() as session:
        first = session.get(BuildRunnerExecution, first_execution_id)
        second = session.get(BuildRunnerExecution, second_execution_id)
        assert first is not None
        assert second is not None
        assert first.status == "LOST"
        assert first.result_data["retryable_failure"] is True
        assert first.result_data["retry_attempt"] == 1
        assert first.result_data["retry_backoff_seconds"] > 0
        assert second.worker_id == "builder-a"


def test_reviewer_capacity_failures_fall_through_to_healthy_provider_without_blocking():
    executors = {
        "reviewer-claude-1": FakeExecutor([
            ExecutionObservation(
                "FAILED",
                result_data={"provider_failure": "RATE_LIMITED", "detail": "session limit"},
            )
        ]),
        "reviewer-grok-1": FakeExecutor([
            ExecutionObservation(
                "FAILED",
                result_data={"provider_failure": "QUOTA_EXHAUSTED", "detail": "weekly quota exhausted"},
            )
        ]),
        "reviewer-codex-1": FakeExecutor([
            ExecutionObservation(
                "SUCCEEDED",
                result_data={
                    "review": {"verdict": "GREEN", "ready_for_integration": True},
                },
            )
        ]),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig(
                "reviewer-claude-1",
                "REVIEWER",
                adapter="fake",
                provider="anthropic",
                capabilities=(CAP_CODE_REVIEW,),
                preference=1,
            ),
            WorkerConfig(
                "reviewer-grok-1",
                "REVIEWER",
                adapter="fake",
                provider="xai",
                capabilities=(CAP_CODE_REVIEW,),
                preference=2,
            ),
            WorkerConfig(
                "reviewer-codex-1",
                "REVIEWER",
                adapter="fake",
                provider="openai",
                capabilities=(CAP_CODE_REVIEW,),
                preference=3,
            ),
        ),
        providers={
            "anthropic": ProviderConfig("anthropic", availability="AVAILABLE"),
            "xai": ProviderConfig("xai", availability="AVAILABLE"),
            "openai": ProviderConfig("openai", availability="AVAILABLE"),
        },
        max_review_environment_attempts=1,
        run_validation=False,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        task = upsert_task(session, _task("TASK-REVIEW-CAPACITY"))
        task.state = "REVIEW_READY"
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())

    first = runner.run_once()
    assert len(first.launched) == 1

    second = runner.run_once()
    assert len(second.launched) == 1

    third = runner.run_once()
    assert len(third.launched) == 1

    with SessionLocal() as session:
        task = session.get(BuildTask, "TASK-REVIEW-CAPACITY")
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "TASK-REVIEW-CAPACITY")
            .where(BuildRunnerExecution.role == "REVIEWER")
            .order_by(BuildRunnerExecution.launched_at)
        ).all()
        assert task.state != "BLOCKED"
        assert [row.worker_id for row in executions] == [
            "reviewer-claude-1",
            "reviewer-grok-1",
            "reviewer-codex-1",
        ]
        assert executions[0].result_data["provider_capacity_failure"] is True
        assert executions[1].result_data["provider_capacity_failure"] is True
        assert executions[0].result_data["reviewer_attempt"] == 0
        assert executions[1].result_data["reviewer_attempt"] == 0


def test_legacy_provider_capacity_block_auto_recovers_to_review_and_dispatches():
    config = RunnerConfig(
        workers=(
            WorkerConfig(
                "reviewer-codex-1",
                "REVIEWER",
                adapter="fake",
                provider="openai",
                capabilities=(CAP_CODE_REVIEW,),
            ),
        ),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE")},
        run_validation=False,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-LEGACY-CAPACITY-BLOCK"))
        transition_task(
            session,
            "TASK-LEGACY-CAPACITY-BLOCK",
            "BLOCKED",
            actor="test",
            reason="PROVIDER_FAILURE:RATE_LIMITED",
        )
        session.add(
            BuildRunnerExecution(
                execution_id="legacy-review-capacity-failure",
                task_id="TASK-LEGACY-CAPACITY-BLOCK",
                role="REVIEWER",
                worker_id="reviewer-claude-1",
                provider="anthropic",
                adapter="fake",
                status="LOST",
                completed_at=utcnow(),
                result_data={"provider_failure": "RATE_LIMITED"},
            )
        )
        session.commit()

    runner = BuildRunner(
        SessionLocal,
        config=config,
        executors={"reviewer-codex-1": FakeExecutor()},
        git=FakeGit(),
        target_task_ids={"TASK-LEGACY-CAPACITY-BLOCK"},
    )
    result = runner.run_once()

    assert "TASK-LEGACY-CAPACITY-BLOCK" in result.recovered
    assert len(result.launched) == 1
    with SessionLocal() as session:
        launched = session.get(BuildRunnerExecution, result.launched[0])
        assert launched is not None
        assert launched.task_id == "TASK-LEGACY-CAPACITY-BLOCK"
        assert launched.role == "REVIEWER"
        task = session.get(BuildTask, "TASK-LEGACY-CAPACITY-BLOCK")
        assert task.state == "REVIEWING"
        recovery = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-LEGACY-CAPACITY-BLOCK")
            .where(BuildTaskEvent.event_type == "runner.provider_capacity_blocker_recovered")
        )
        assert recovery is not None
        assert recovery.event_data["resumed_to"] == "REVIEW_READY"


def test_legacy_builder_capacity_block_auto_recovers_to_resumable_and_dispatches():
    config = RunnerConfig(
        workers=(
            WorkerConfig(
                "builder-codex-1",
                "BUILDER",
                adapter="fake",
                provider="openai",
                capabilities=(CAP_CODING,),
            ),
        ),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE")},
        max_execution_attempts=1,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TASK-LEGACY-BUILDER-CAPACITY"))
        transition_task(
            session,
            "TASK-LEGACY-BUILDER-CAPACITY",
            "BLOCKED",
            actor="test",
            reason="PROVIDER_FAILURE_RETRIES_EXHAUSTED:RATE_LIMITED",
        )
        session.add(
            BuildRunnerExecution(
                execution_id="legacy-builder-capacity-failure",
                task_id="TASK-LEGACY-BUILDER-CAPACITY",
                role="BUILDER",
                worker_id="builder-claude-1",
                provider="anthropic",
                adapter="fake",
                status="LOST",
                completed_at=utcnow(),
                result_data={
                    "provider_failure": "RATE_LIMITED",
                    "retry_generation": 0,
                },
            )
        )
        session.commit()

    runner = BuildRunner(
        SessionLocal,
        config=config,
        executors={"builder-codex-1": FakeExecutor()},
        git=FakeGit(),
        target_task_ids={"TASK-LEGACY-BUILDER-CAPACITY"},
    )
    result = runner.run_once()

    assert "TASK-LEGACY-BUILDER-CAPACITY" in result.recovered
    assert len(result.launched) == 1
    with SessionLocal() as session:
        launched = session.get(BuildRunnerExecution, result.launched[0])
        assert launched is not None
        assert launched.task_id == "TASK-LEGACY-BUILDER-CAPACITY"
        assert launched.role == "BUILDER"
        task = session.get(BuildTask, "TASK-LEGACY-BUILDER-CAPACITY")
        assert task.state == "CLAIMED"
        recovery = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-LEGACY-BUILDER-CAPACITY")
            .where(BuildTaskEvent.event_type == "runner.provider_capacity_blocker_recovered")
        )
        assert recovery is not None
        assert recovery.event_data["resumed_to"] == "RESUMABLE"


def test_provider_automatically_reenters_after_capacity_cooldown_expires():
    config = RunnerConfig(
        workers=(
            WorkerConfig(
                "reviewer-codex-1",
                "REVIEWER",
                adapter="fake",
                provider="openai",
                capabilities=(CAP_CODE_REVIEW,),
            ),
        ),
        providers={"openai": ProviderConfig("openai", availability="AVAILABLE")},
        run_validation=False,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        task = upsert_task(session, _task("TASK-CAPACITY-REENTRY"))
        task.state = "REVIEW_READY"
        record_event(
            session,
            EventInput(
                task_id="TASK-CAPACITY-REENTRY",
                event_type="runner.provider_failure",
                actor="test",
                event_data={
                    "provider": "openai",
                    "worker_id": "reviewer-codex-1",
                    "failure": "QUOTA_EXHAUSTED",
                    "until": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                    "detail": "quota exhausted",
                },
            ),
        )
        session.commit()

    runner = BuildRunner(
        SessionLocal,
        config=config,
        executors={"reviewer-codex-1": FakeExecutor()},
        git=FakeGit(),
        target_task_ids={"TASK-CAPACITY-REENTRY"},
    )
    waiting = runner.run_once()
    assert waiting.launched == []
    assert waiting.scheduling_reasons["TASK-CAPACITY-REENTRY"] == "provider_capacity_wait"

    with SessionLocal() as session:
        failure_event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "TASK-CAPACITY-REENTRY")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        )
        failure_event.event_data = {
            **failure_event.event_data,
            "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        session.commit()

    restarted = BuildRunner(
        SessionLocal,
        config=config,
        executors={"reviewer-codex-1": FakeExecutor()},
        git=FakeGit(),
        target_task_ids={"TASK-CAPACITY-REENTRY"},
    )
    resumed = restarted.run_once()
    assert len(resumed.launched) == 1
    with SessionLocal() as session:
        launched = session.get(BuildRunnerExecution, resumed.launched[0])
        assert launched is not None
        assert launched.task_id == "TASK-CAPACITY-REENTRY"
        assert launched.role == "REVIEWER"


def test_provider_capacity_wait_is_not_terminal_idle():
    result = RunnerCycleResult(
        mode="RUNNING",
        scheduling_reasons={"TASK-WAIT": "provider_capacity_wait"},
    )
    assert _provider_capacity_waiting(result) is True
    assert _provider_capacity_waiting(RunnerCycleResult(mode="RUNNING")) is False


def test_targeted_human_summary_filters_unrelated_project_attention():
    project = type("Project", (), {"project_id": "stagemesh"})()
    final = {
        "executions": [],
        "tasks_by_state": {"READY": 2, "BLOCKED": 2},
        "tasks": [
            {"task_id": "TARGET", "state": "BLOCKED"},
            {"task_id": "OTHER", "state": "BLOCKED"},
        ],
        "blocked": [
            {"task_id": "TARGET", "reason": "TARGET_REASON"},
            {"task_id": "OTHER", "reason": "OTHER_REASON"},
        ],
    }
    summary = _continue_human_summary(
        project,
        [],
        final,
        {"TARGET": "TARGET_REASON", "OTHER": "OTHER_REASON"},
        target_task_ids=frozenset({"TARGET"}),
    )

    assert "Target: TARGET" in summary
    assert "Target reason" in summary
    assert "OTHER" not in summary
    assert "Other reason" not in summary
    assert "Needs action: 1" in summary


# 13. Configurable concurrency
def test_configurable_concurrency():
    workers = [
        WorkerConfig("builder-1", "BUILDER", adapter="fake", max_concurrency=2),
    ]
    with SessionLocal() as session:
        for tid in ("C-1", "C-2", "C-3"):
            upsert_task(session, _task(tid))
        session.commit()

    config = RunnerConfig(workers=tuple(workers), result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"))
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res = runner.run_once()
    assert len(res.launched) == 2


# 14. Multiple independent P0 builders run concurrently
def test_multiple_independent_p0_builders():
    workers = [
        WorkerConfig("builder-1", "BUILDER", adapter="fake"),
        WorkerConfig("builder-2", "BUILDER", adapter="fake"),
    ]
    with SessionLocal() as session:
        for tid in ("P0-A", "P0-B"):
            upsert_task(session, _task(tid))
            _seed_priority(session, tid, 0)
        session.commit()

    config = RunnerConfig(workers=tuple(workers), result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"))
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res = runner.run_once()
    assert len(res.launched) == 2


# 15. Dependency overrides priority
def test_dependency_overrides_priority():
    with SessionLocal() as session:
        upsert_task(session, _task("DEP-1"))
        _seed_priority(session, "DEP-1", 100)
        upsert_task(session, _task("P0-BLOCKED", deps=["DEP-1"]))
        _seed_priority(session, "P0-BLOCKED", 0)
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-1", "BUILDER", adapter="fake"),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res = runner.run_once()
    # P0 is blocked on DEP-1, so DEP-1 launches first
    assert len(res.launched) == 1
    with SessionLocal() as session:
        execution = session.get(BuildRunnerExecution, res.launched[0])
        assert execution.task_id == "DEP-1"


# 16. Complete GH dependency extraction (unicode, mojibake, hyphens)
def test_complete_gh_dependency_extraction():
    gh = GitHubTaskSource("saketvishal/stagemesh")
    body1 = "Run after issues #55–#61."
    assert gh._parse_dependencies(body1) == [f"GH-{i}" for i in range(55, 62)]

    body2 = "depends on #55 to #61"
    assert gh._parse_dependencies(body2) == [f"GH-{i}" for i in range(55, 62)]

    body3 = "blocked by issues #55â€“#61."
    assert gh._parse_dependencies(body3) == [f"GH-{i}" for i in range(55, 62)]


# 17. GH-62 style seven task dependency gate
def test_gh_62_seven_task_dependency_gate():
    with SessionLocal() as session:
        for num in range(55, 62):
            upsert_task(session, _task(f"GH-{num}"))
        upsert_task(session, _task("GH-62", deps=[f"GH-{i}" for i in range(55, 62)]))
        session.commit()

    now = utcnow()
    with SessionLocal() as session:
        t62 = session.get(BuildTask, "GH-62")
        assert task_is_claimable(session, t62, now) is False

        # Mark 55 through 60 done; 61 still ready
        for num in range(55, 61):
            t = session.get(BuildTask, f"GH-{num}")
            t.state = "DONE"
        session.commit()

        assert task_is_claimable(session, t62, now) is False

        # Mark 61 done
        t61 = session.get(BuildTask, "GH-61")
        t61.state = "DONE"
        session.commit()

        assert task_is_claimable(session, t62, now) is True


# 18. LOST execution recovery
def test_lost_execution_recovery():
    with SessionLocal() as session:
        upsert_task(session, _task("LOST-1"))
        claim = claim_task(session, ClaimRequest("LOST-1", worker_id="builder-1"))
        transition_task(session, "LOST-1", "IN_PROGRESS")
        execution = BuildRunnerExecution(
            execution_id="exec-lost-1",
            task_id="LOST-1",
            role="BUILDER",
            worker_id="builder-1",
            claim_id=claim.claim_id,
            adapter="fake",
            status="LOST",
            completed_at=utcnow(),
        )
        session.add(execution)
        session.commit()

        recovered = recover_lost_execution_claims(session, actor="test")
        assert len(recovered) == 1
        assert recovered[0].task_id == "LOST-1"
        assert recovered[0].state == "STALE"


# 19. STALE task recovery and re-execution
def test_stale_task_recovery_and_reexecution():
    with SessionLocal() as session:
        task = upsert_task(session, _task("STALE-1"))
        task.state = "STALE"
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-1", "BUILDER", adapter="fake"),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res = runner.run_once()
    assert len(res.launched) == 1
    with SessionLocal() as session:
        assert session.get(BuildTask, "STALE-1").state == "CLAIMED"


# 20. Restart / resume preserves state
def test_restart_preserves_state():
    with SessionLocal() as session:
        upsert_task(session, _task("RESUME-1"))
        _seed_priority(session, "RESUME-1", 0)
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-1", "BUILDER", adapter="fake"),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    runner1 = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res1 = runner1.run_once()
    assert len(res1.launched) == 1

    # Simulate process restart with fresh runner
    runner2 = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res2 = runner2.run_once()
    assert len(res2.observed) == 1  # Observes in-flight execution from before restart


# 21. Process cleanup
def test_process_cleanup():
    # Verify attach_started_process can cleanly terminate an external process tree
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        tree = attach_started_process(proc.pid)
        tree.terminate()
        tree.close()
        proc.wait(timeout=3)
    finally:
        if proc.poll() is None:
            proc.kill()


# 22. Result channel handling
def test_result_channel_handling():
    observation = ExecutionObservation(
        status="SUCCEEDED",
        result_data={
            "schema_version": 1,
            "feature_sha": "abc456",
            "runtime": "claude",
        },
    )
    assert observation.status == "SUCCEEDED"
    assert observation.result_data["runtime"] == "claude"


# 23. Preflight failure before task claim
def test_preflight_failure_before_task_claim():
    config = RunnerConfig(workers=(), result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"))
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    with SessionLocal() as session:
        upsert_task(session, _task("PREFLIGHT-1"))
        session.commit()

    res = runner.run_once()
    assert len(res.launched) == 0
    with SessionLocal() as session:
        # Task remains READY and was never claimed
        assert session.get(BuildTask, "PREFLIGHT-1").state == "READY"


# 24. Preservation of P0 behavior
def test_preservation_of_p0_behavior():
    with SessionLocal() as session:
        upsert_task(session, _task("BACKLOG-1"))
        _seed_priority(session, "BACKLOG-1", 100)
        upsert_task(session, _task("P0-FIRST"))
        _seed_priority(session, "P0-FIRST", 0)
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-1", "BUILDER", adapter="fake"),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    res = runner.run_once()
    assert len(res.launched) == 1
    with SessionLocal() as session:
        exec_row = session.get(BuildRunnerExecution, res.launched[0])
        assert exec_row.task_id == "P0-FIRST"
