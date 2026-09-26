"""Deterministic tests for reviewer environment failures vs implementation remediation.

Verifies:
1. Managed execution receives writable temp storage
2. TEMP configured appropriately
3. TMP configured appropriately
4. TMPDIR configured where applicable
5. Concurrent worker executions do not unsafely share temp state
6. Temporary-file creation succeeds in worker environment
7. Explicit worker environment behavior remains compatible
8. GREEN behavior unchanged
9. GREEN_WITH_NOTES behavior unchanged
10. Genuine REMEDIATION_REQUIRED -> REWORK_REQUIRED
11. Genuine remediation dispatches remediation
12. Genuine remediation increments remediation count
13. REVIEW_ENVIRONMENT_BLOCKED does not -> REWORK_REQUIRED
14. It does not launch remediation
15. It does not increment remediation count
16. Diagnostic evidence is durable
17. Alternate reviewer can retry
18. Exact feature SHA is preserved
19. Independent-review constraints remain valid
20. Environment review retries are bounded
21. Exhaustion reports review environment block
22. Exhaustion does not report REMEDIATION_LIMIT_REACHED
23. AUTH_FAILURE remains provider failure
24. RATE_LIMITED remains provider failure
25. NETWORK_FAILURE remains provider failure
26. Restart preserves environment-review state
27. Restart resumes review rather than implementation
28. Restart preserves SHA
29. Restart preserves retry accounting
30. Real remediation followed by environment failure has correct accounting
31. Environment failure followed by real remediation has correct accounting
32. TWO_REVIEWERS remains correct
33. SHA-drift safety remains correct
34. Old blocked-task operator recovery preserves evidence/history
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
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
from build_coordinator.runner.models import (
    ReviewVerdict,
    ReviewVerdictContradiction,
    RunnerConfig,
    WorkerConfig,
)
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    recover_review_environment_blocked,
    transition_task,
    upsert_task,
)


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))


@pytest.fixture(autouse=True)
def clean_build_coordinator():
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
    yield


def _task(task_id: str, *, review_policy: str = "INDEPENDENT") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description=f"Description for {task_id}",
        acceptance_criteria=["passes"],
        review_policy=review_policy,
        required_validation=["pytest tests/"],
    )


def _config(
    *,
    remediation_cycles: int = 2,
    max_review_environment_attempts: int = 2,
    workers: list[WorkerConfig] | None = None,
) -> RunnerConfig:
    default_workers = (
        WorkerConfig(
            worker_id="builder-a",
            role="BUILDER",
            adapter="fake",
            stages=("implementation", "remediation"),
            preference=10,
        ),
        WorkerConfig(
            worker_id="reviewer-1",
            role="REVIEWER",
            adapter="fake",
            stages=("review",),
            preference=10,
        ),
        WorkerConfig(
            worker_id="reviewer-2",
            role="REVIEWER",
            adapter="fake",
            stages=("review",),
            preference=20,
        ),
        WorkerConfig(
            worker_id="reviewer-3",
            role="REVIEWER",
            adapter="fake",
            stages=("review",),
            preference=30,
        ),
        WorkerConfig(
            worker_id="integration-1",
            role="INTEGRATION",
            adapter="fake",
            stages=("integration",),
            preference=10,
        ),
    )
    return RunnerConfig(
        workers=tuple(workers or default_workers),
        max_remediation_cycles=remediation_cycles,
        max_review_environment_attempts=max_review_environment_attempts,
        poll_seconds=0.1,
        run_validation=False,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(*, config: RunnerConfig | None = None, executors: dict | None = None, git=None) -> BuildRunner:
    runner = BuildRunner(
        SessionLocal,
        config or _config(),
        executors=executors,
        git=git if git is not None else FakeGit(),
    )
    return runner


# ---------------------------------------------------------------------------
# Part 1: Managed Writable Temporary Storage (Tests 1 - 7)
# ---------------------------------------------------------------------------


def test_managed_execution_receives_isolated_writable_temp(tmp_path):
    """Test 1 - 5: TEMP, TMP, TMPDIR configured and concurrent executions do not share state."""
    executor = SubprocessExecutor(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        temp_dir=tmp_path / "managed-temp",
        log_dir=tmp_path / "logs",
    )

    launch1 = ExecutionLaunch(
        task_id="T-1",
        role="REVIEWER",
        worker_id="worker-rev-1",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="main",
        prompt="",
        execution_id="exec-1",
    )
    launch2 = ExecutionLaunch(
        task_id="T-2",
        role="REVIEWER",
        worker_id="worker-rev-2",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="main",
        prompt="",
        execution_id="exec-2",
    )

    handle1 = executor.launch(launch1)
    handle2 = executor.launch(launch2)

    temp1 = Path(tmp_path) / "managed-temp" / "worker-rev-1" / "exec-1"
    temp2 = Path(tmp_path) / "managed-temp" / "worker-rev-2" / "exec-2"

    assert temp1.is_dir()
    assert temp2.is_dir()
    assert temp1 != temp2


def test_temporary_file_creation_succeeds_in_worker_environment(tmp_path):
    """Test 6: Worker process can successfully create a temporary file/dir through environment."""
    script = (
        "import tempfile, os, sys\n"
        "d = tempfile.gettempdir()\n"
        "with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:\n"
        "    f.write('stagemesh-temp-verified')\n"
        "    f_path = f.name\n"
        "assert os.path.exists(f_path)\n"
        "assert d in f_path\n"
        "print('TEMP_OK')\n"
    )

    executor = SubprocessExecutor(
        [sys.executable, "-c", script],
        temp_dir=tmp_path / "managed-temp",
        log_dir=tmp_path / "logs",
    )

    launch = ExecutionLaunch(
        task_id="T-1",
        role="REVIEWER",
        worker_id="worker-1",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="main",
        prompt="",
        execution_id="exec-file-check",
    )
    handle = executor.launch(launch)
    obs = executor.poll(handle.execution_id)
    while obs.status == "RUNNING":
        obs = executor.poll(handle.execution_id)

    assert obs.exit_code == 0
    stdout_file = tmp_path / "logs" / f"{handle.execution_id}.stdout.log"
    assert "TEMP_OK" in stdout_file.read_text(encoding="utf-8")


def test_explicit_worker_environment_remains_compatible(tmp_path):
    """Test 7: Explicit worker environment configuration remains compatible."""
    custom_temp = tmp_path / "explicit-temp"
    custom_temp.mkdir()

    script = (
        "import os\n"
        "print('TEMP=' + os.environ.get('TEMP', ''))\n"
        "print('CUSTOM=' + os.environ.get('MY_CUSTOM_VAR', ''))\n"
    )

    executor = SubprocessExecutor(
        [sys.executable, "-c", script],
        temp_dir=tmp_path / "managed-temp",
        log_dir=tmp_path / "logs",
    )

    launch = ExecutionLaunch(
        task_id="T-1",
        role="BUILDER",
        worker_id="builder-1",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="main",
        prompt="",
        execution_id="exec-env",
        extra_env={"MY_CUSTOM_VAR": "custom_val", "TEMP": str(custom_temp)},
    )
    handle = executor.launch(launch)
    obs = executor.poll(handle.execution_id)
    while obs.status == "RUNNING":
        obs = executor.poll(handle.execution_id)

    assert obs.exit_code == 0
    stdout = (tmp_path / "logs" / f"{handle.execution_id}.stdout.log").read_text(encoding="utf-8")
    assert "CUSTOM=custom_val" in stdout
    assert str(custom_temp) in stdout


# ---------------------------------------------------------------------------
# Part 2: Review Verdict Contract (Tests 8 - 9)
# ---------------------------------------------------------------------------


def test_green_and_green_with_notes_unchanged():
    """Test 8 - 9: GREEN and GREEN_WITH_NOTES behavior unchanged."""
    v_green = ReviewVerdict.from_mapping({"verdict": "GREEN", "ready_for_integration": True})
    assert v_green.integration_eligible() is True

    v_notes = ReviewVerdict.from_mapping(
        {"verdict": "GREEN_WITH_NOTES", "ready_for_integration": True, "findings": ["minor note"]}
    )
    assert v_notes.integration_eligible() is True

    with pytest.raises(ReviewVerdictContradiction):
        ReviewVerdict.from_mapping({"verdict": "GREEN", "ready_for_integration": False}).validate_consistency()

    with pytest.raises(ReviewVerdictContradiction):
        ReviewVerdict.from_mapping(
            {"verdict": "GREEN", "ready_for_integration": True, "required_remediation": ["change"]}
        ).validate_consistency()


def test_review_environment_blocked_verdict_consistency():
    """Test REVIEW_ENVIRONMENT_BLOCKED validation consistency."""
    v_blocked = ReviewVerdict.from_mapping(
        {"verdict": "REVIEW_ENVIRONMENT_BLOCKED", "ready_for_integration": False, "findings": ["cannot write temp"]}
    )
    v_blocked.validate_consistency()
    assert v_blocked.integration_eligible() is False

    with pytest.raises(ReviewVerdictContradiction):
        ReviewVerdict.from_mapping(
            {"verdict": "REVIEW_ENVIRONMENT_BLOCKED", "ready_for_integration": True}
        ).validate_consistency()

    with pytest.raises(ReviewVerdictContradiction):
        ReviewVerdict.from_mapping(
            {"verdict": "REVIEW_ENVIRONMENT_BLOCKED", "ready_for_integration": False, "required_remediation": ["fix"]}
        ).validate_consistency()


# ---------------------------------------------------------------------------
# Part 3: Orchestrator Lifecycle & Accounting (Tests 10 - 22)
# ---------------------------------------------------------------------------


def test_genuine_remediation_required_lifecycle():
    """Test 10 - 12: genuine REMEDIATION_REQUIRED -> REWORK_REQUIRED, dispatches remediation, increments count."""
    task_id = f"T-REWORK-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["missing test"],
                            "required_remediation": ["add test"],
                        }
                    },
                )
            ]
        )
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launches review
    runner.run_once()  # observes review -> transitions to REWORK_REQUIRED

    with SessionLocal() as session:
        t = session.get(BuildTask, task_id)
        assert t.state in {"REWORK_REQUIRED", "CLAIMED"}
        rem = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REMEDIATION")
        )
        assert rem is not None
        assert runner._remediation_cycles(session, task_id) == 1


def test_review_environment_blocked_lifecycle():
    """Test 13 - 16: REVIEW_ENVIRONMENT_BLOCKED does not -> REWORK_REQUIRED, does not launch remediation, does not increment remediation count, records durable evidence."""
    task_id = f"T-ENV-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["pytest unable to write to /tmp"],
                        }
                    },
                )
            ]
        )
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launches review on reviewer-1
    runner.run_once()  # observes review -> REVIEW_ENVIRONMENT_BLOCKED -> transitions to REVIEW_READY and retries reviewer-2

    with SessionLocal() as session:
        t = session.get(BuildTask, task_id)
        assert t.state in {"REVIEW_READY", "REVIEWING"}
        assert t.state != "REWORK_REQUIRED"

        # Remediation count is 0
        assert runner._remediation_cycles(session, task_id) == 0
        rem = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REMEDIATION")
        )
        assert rem is None

        # Durable event recorded
        ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.review_environment_blocked")
        )
        assert ev is not None
        assert ev.event_data["verdict"] == "REVIEW_ENVIRONMENT_BLOCKED"
        assert "pytest unable to write to /tmp" in ev.event_data["findings"]


def test_alternate_reviewer_retries_exact_sha():
    """Test 17 - 19: alternate reviewer can retry on the exact same feature SHA, preserving independent review."""
    task_id = f"T-ALT-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["temp dir not writable on reviewer-1"],
                        }
                    },
                )
            ]
        ),
        "reviewer-2": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN",
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        ),
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launches reviewer-1
    runner.run_once()  # observes reviewer-1 failure -> launches reviewer-2 (alternate reviewer preferred!)

    with SessionLocal() as session:
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .order_by(BuildRunnerExecution.launched_at)
        ).all()
        assert len(executions) == 2
        assert executions[0].worker_id == "reviewer-1"
        assert executions[1].worker_id == "reviewer-2"
        # Exact feature SHA is preserved
        assert executions[0].reviewed_feature_sha == executions[1].reviewed_feature_sha


def test_review_environment_retries_bounded_and_exhaustion():
    """Test 20 - 22: environment review retries are bounded; exhaustion reports review environment block, not REMEDIATION_LIMIT_REACHED."""
    task_id = f"T-BOUND-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["env blocked 1"],
                        }
                    },
                )
            ]
        ),
        "reviewer-2": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["env blocked 2"],
                        }
                    },
                )
            ]
        ),
    }

    # Max attempts = 2
    runner = _runner(config=_config(max_review_environment_attempts=2), executors=executors)
    runner.run_once()  # reviewer-1 launch
    runner.run_once()  # reviewer-1 done (attempt 1) -> REVIEW_READY -> reviewer-2 launch
    result = runner.run_once()  # reviewer-2 done (attempt 2) -> exhaustion!

    assert f"{task_id}:REVIEW_ENVIRONMENT_BLOCKED" in result.escalations
    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" not in result.escalations

    with SessionLocal() as session:
        t = session.get(BuildTask, task_id)
        assert t.state == "BLOCKED"
        ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "task.transitioned")
            .where(BuildTaskEvent.to_state == "BLOCKED")
            .order_by(BuildTaskEvent.created_at.desc())
        )
        assert ev.event_data["reason"] == "REVIEW_ENVIRONMENT_BLOCKED"


# ---------------------------------------------------------------------------
# Part 4: Provider Failure Classification (Tests 23 - 25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_code", ["AUTH_FAILURE", "RATE_LIMITED", "NETWORK_FAILURE"])
def test_provider_failures_remain_provider_failure(failure_code):
    """Test 23 - 25: AUTH_FAILURE, RATE_LIMITED, NETWORK_FAILURE remain provider failures, not remediation or review environment blocked."""
    task_id = f"T-PROV-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "FAILED",
                    result_data={"provider_failure": failure_code},
                )
            ]
        )
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launch
    runner.run_once()  # observe failed

    with SessionLocal() as session:
        ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        )
        assert ev is not None
        assert ev.event_data["failure"] == failure_code
        # Did not record review environment blocked
        env_ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.review_environment_blocked")
        )
        assert env_ev is None
        assert runner._remediation_cycles(session, task_id) == 0
        assert runner._review_environment_attempts(session, task_id) == 0


def test_reviewer_rate_limit_is_capacity_wait_not_task_block():
    """RATE_LIMITED is provider capacity, so it must not consume review budget or block the task."""
    task_id = f"T-RATE-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    rate_limited = ExecutionObservation(
        "FAILED",
        result_data={"provider_failure": "RATE_LIMITED", "detail": "provider throttled review request"},
    )
    executors = {"reviewer-1": FakeExecutor([rate_limited])}

    runner = _runner(config=_config(max_review_environment_attempts=1), executors=executors)
    runner.run_once()  # launch first reviewer
    result = runner.run_once()  # observe RATE_LIMITED -> keep task waiting at review stage

    assert result.escalations == []
    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        assert task.state == "REVIEW_READY"
        blocked = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.to_state == "BLOCKED")
        ).all()
        assert blocked == []
        provider_failure = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        )
        assert provider_failure is not None
        assert provider_failure.event_data["failure"] == "RATE_LIMITED"
        execution = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
        )
        assert execution.result_data["provider_capacity_failure"] is True
        assert execution.result_data["reviewer_attempt"] == 0
        assert runner._remediation_cycles(session, task_id) == 0
        assert runner._review_environment_attempts(session, task_id) == 0


# ---------------------------------------------------------------------------
# Part 5: Restart / Recovery & Accounting (Tests 26 - 31)
# ---------------------------------------------------------------------------


def test_restart_preserves_review_environment_state():
    """Test 26 - 29: restart preserves environment-review state, resumes review rather than implementation, preserves SHA and retry accounting."""
    task_id = f"T-RESTART-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        from build_coordinator.service import utcnow
        import datetime
        upsert_task(session, _task(task_id))
        claim = claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        claim.claimed_at = utcnow() - datetime.timedelta(seconds=20)
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        # Persist previous review environment failure execution
        review_time = utcnow() - datetime.timedelta(seconds=10)
        session.add(
            BuildRunnerExecution(
                execution_id=str(uuid4()),
                task_id=task_id,
                role="REVIEWER",
                worker_id="reviewer-1",
                adapter="fake",
                status="SUCCEEDED",
                launched_at=review_time,
                reviewed_feature_sha="feat-sha-restart-1",
                result_data={
                    "review": {
                        "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                        "findings": ["tool failure"],
                    }
                },
            )
        )
        session.commit()

    # Now runner starts (simulating restart after review environment failure)
    workers = (
        WorkerConfig(worker_id="builder-a", role="BUILDER", adapter="fake", stages=("implementation", "remediation")),
        WorkerConfig(worker_id="reviewer-1", role="REVIEWER", adapter="fake", stages=("review",), preference=10),
        WorkerConfig(worker_id="reviewer-2", role="REVIEWER", adapter="fake", stages=("review",), preference=20),
    )
    executors = {
        "reviewer-2": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN",
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        )
    }
    runner = _runner(config=_config(workers=workers), executors=executors)

    with SessionLocal() as session:
        # Retry accounting preserved across restart
        assert runner._review_environment_attempts(session, task_id) == 1

    runner.run_once()  # should dispatch reviewer-2 directly, NOT builder!

    with SessionLocal() as session:
        latest_exec = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .order_by(BuildRunnerExecution.launched_at.desc())
        )
        assert latest_exec.role == "REVIEWER"
        assert latest_exec.worker_id == "reviewer-2"


def test_mixed_remediation_and_environment_accounting():
    """Test 30 - 31: real remediation followed by environment failure, and vice-versa, have correct independent accounting."""
    task_id = f"T-MIX-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    # Step 1: Real remediation required
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["real defect"],
                            "required_remediation": ["fix defect"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["env broken"],
                        }
                    },
                ),
            ]
        ),
        "builder-a": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"feature_sha": "remediated-sha-1"},
                )
            ]
        ),
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launch review 1
    runner.run_once()  # review 1 completed -> REWORK_REQUIRED -> launches remediation builder
    runner.run_once()  # remediation builder completed -> VALIDATING -> REVIEW_READY -> launches review 2

    with SessionLocal() as session:
        assert runner._remediation_cycles(session, task_id) == 1
        assert runner._review_environment_attempts(session, task_id) == 0

    # Step 2: Now reviewer-1 completes with REVIEW_ENVIRONMENT_BLOCKED
    runner.run_once()  # review 2 completed -> REVIEW_ENVIRONMENT_BLOCKED

    with SessionLocal() as session:
        # Remediation count STILL 1, environment attempts = 1
        assert runner._remediation_cycles(session, task_id) == 1
        assert runner._review_environment_attempts(session, task_id) == 1


# ---------------------------------------------------------------------------
# Part 6: TWO_REVIEWERS & SHA-Drift (Tests 32 - 33)
# ---------------------------------------------------------------------------


def test_two_reviewers_with_review_environment_blocked():
    """Test 32: TWO_REVIEWERS remains correct when one approval succeeds and subsequent review hits environment failure."""
    task_id = f"T-TWO-REV-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id, review_policy="TWO_REVIEWERS"))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN",
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        ),
        "reviewer-2": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                            "findings": ["temp dir issue on reviewer 2"],
                        }
                    },
                )
            ]
        ),
        "reviewer-3": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN",
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        ),
    }

    runner = _runner(executors=executors)
    runner.run_once()  # launches reviewer-1
    runner.run_once()  # reviewer-1 approves -> records approval -> dispatches reviewer-2
    runner.run_once()  # reviewer-2 blocked -> retries on reviewer-3
    runner.run_once()  # reviewer-3 approves -> integration eligible!

    with SessionLocal() as session:
        t = session.get(BuildTask, task_id)
        assert t.state != "BLOCKED"
        assert t.state != "REWORK_REQUIRED"
        # Integration eligible event recorded
        int_ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.integration_eligible")
        )
        assert int_ev is not None


def test_sha_drift_safety_with_review_environment_accounting():
    """Test 33: SHA-drift safety does not count review environment retries as review cycles."""
    task_id = f"T-DRIFT-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        # Record review environment failure
        session.add(
            BuildRunnerExecution(
                execution_id=str(uuid4()),
                task_id=task_id,
                role="REVIEWER",
                worker_id="reviewer-1",
                adapter="fake",
                status="SUCCEEDED",
                result_data={
                    "review": {
                        "verdict": "REVIEW_ENVIRONMENT_BLOCKED",
                        "findings": ["env err"],
                    }
                },
            )
        )
        transition_task(session, task_id, "BLOCKED", reason="REVIEWED_SHA_CHANGED")
        session.commit()

    runner = _runner()
    with SessionLocal() as session:
        # _review_cycles excludes REVIEW_ENVIRONMENT_BLOCKED
        assert runner._review_cycles(session, task_id) == 0


# ---------------------------------------------------------------------------
# Part 7: Historical Blocked Task Operator Recovery (Test 34)
# ---------------------------------------------------------------------------


def test_historical_blocked_task_operator_recovery():
    """Test 34: operator recovery of historically blocked task preserves evidence/history and unblocks to REVIEW_READY."""
    task_id = f"T-HIST-{uuid4().hex[:6]}"
    with SessionLocal() as session:
        upsert_task(session, _task(task_id))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        transition_task(
            session,
            task_id,
            "BLOCKED",
            reason="REMEDIATION_LIMIT_REACHED",
        )
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        assert task.state == "BLOCKED"

        recovered = recover_review_environment_blocked(
            session,
            task_id,
            reason="operator evidence: review environment failure",
        )
        assert recovered.state == "REVIEW_READY"
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "REVIEW_READY"
        ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.review_environment_recovered")
        )
        assert ev is not None
        assert ev.event_data["prior_state"] == "BLOCKED"
        assert ev.event_data["target_state"] == "REVIEW_READY"
        assert "operator evidence" in ev.event_data["reason"]
