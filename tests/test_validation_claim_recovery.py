"""Regression coverage for validation-restart recovery, including the
healthy-live-validator acceptance case.

Root cause #1 (fixed previously): validation executions are launched
against the *same* claim_id as the BUILDER (or REMEDIATION) claim that
produced the feature SHA under validation -- there is no dedicated
VALIDATION claim_type in the schema. When that claim's lease expires (e.g.
the coordinator or the validation executor was down long enough to miss a
heartbeat), `recover_expired()`'s generic fallback used to unconditionally
flip the task to STALE -- a claimable state -- even though the task was
sitting in VALIDATING with an already-successful implementation. A fresh
builder could then claim the task and redo the implementation from
scratch.

Root cause #2 (this file's focus): fixing #1 alone was insufficient. The
service layer's claim-expiry bookkeeping (`recover_expired`,
`reconcile_stale_executions`) used to unconditionally *terminate* the
LAUNCHED/RUNNING validation execution row under the expiring claim, on the
theory that the claim's lease lapsing meant the run was "stale". A claim
lease expiring is evidence only that lease-heartbeat bookkeeping lapsed --
it is not evidence the underlying OS validation process actually exited. A
genuinely still-running, verified-live validator would be killed and
relaunched from scratch solely because its *inherited implementation
lease* (a bookkeeping artifact shared with the BUILDER/REMEDIATION claim)
expired, wasting the in-flight validation run.

Root cause #3 (this file's focus): `ValidationExecutor` (unlike
`SubprocessExecutor`, fixed for Problem 1) never participated in durable
process-identity reconciliation at all -- it had no
`remember_process_identity` method, and `poll()` fell straight through to
`_lost_observation()` for any execution absent from its process-local
`_runs` dict, which is exactly what happens after any coordinator/executor
restart. So even after root cause #2 was fixed at the service layer, the
orchestrator's own liveness-aware poll (the mechanism the service layer
now defers to) would still incorrectly report a genuinely-alive validation
subprocess as LOST on the very next reconciliation after a restart.

The fix (see build_coordinator/service.py `terminate_executions_for_claim`
/ `reconcile_stale_executions`, and build_coordinator/runner/validation.py
`ValidationExecutor`) makes the *actual OS process's durable identity* --
not claim/lease bookkeeping -- the sole authority for whether a validation
execution is reconciled to a terminal state. The service layer excludes
validation-adapter rows from claim-staleness-driven termination
unconditionally, deferring to the orchestrator's `_reconcile_active` sweep
(which always runs later in the same cycle), and `ValidationExecutor.poll`
now captures and checks durable process identity exactly like
`SubprocessExecutor` does.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import FakeExecutor
from build_coordinator.execution.base import ExecutionLaunch
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
from build_coordinator.runner.validation import ValidationExecutor
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    recover_expired,
    upsert_task,
    utcnow,
)
from sqlalchemy import delete


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


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="Validation-claim-recovery regression test task",
        acceptance_criteria=["passes"],
        review_policy="INDEPENDENT",
        required_validation=[f"{sys.executable} -c \"print('ok')\""],
        migration_allowed=False,
    )


def _config() -> RunnerConfig:
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        result_dir=None,
    )


def _runner(executors=None) -> BuildRunner:
    config = _config()
    config = RunnerConfig(**{**config.__dict__, "result_dir": os.getenv("BUILD_COORDINATOR_RESULT_DIR")})
    return BuildRunner(SessionLocal, config, executors=executors, git=FakeGit())


def _sleep_command(seconds: float) -> list[str]:
    return [f"{sys.executable} -c \"import time; time.sleep({seconds})\""]


def _quick_exit_command() -> list[str]:
    return [f"{sys.executable} -c \"pass\""]


def _wait_alive(pid: int, expected: bool, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = subprocess.run(["kill", "-0", str(pid)]).returncode == 0
        if alive == expected:
            return alive
        time.sleep(0.02)
    return subprocess.run(["kill", "-0", str(pid)]).returncode == 0


def _result_path(execution_id: str) -> str:
    return str(Path(os.environ["BUILD_COORDINATOR_RESULT_DIR"]) / f"{execution_id}.json")


def _seed_builder_succeeded(task_id: str, *, worker_id: str = "builder-a") -> str:
    """Seed the durable state of 'builder succeeded' and return the shared claim_id."""
    with SessionLocal() as session:
        task = upsert_task(session, _task(task_id))
        claim = claim_task(session, ClaimRequest(task_id, worker_id=worker_id))
        task.state = "VALIDATING"
        session.add(
            BuildRunnerExecution(
                execution_id=f"{task_id}-builder-done",
                task_id=task_id,
                role="BUILDER",
                worker_id=worker_id,
                provider="local",
                adapter="fake",
                claim_id=claim.claim_id,
                status="SUCCEEDED",
                worktree_path=str(Path.cwd()),
                result_data={"feature_sha": "feature-sha-original"},
                completed_at=utcnow(),
            )
        )
        session.commit()
        return claim.claim_id


def _launch_real_validation(task_id: str, claim_id: str, *, commands: list[str]) -> tuple[str, int, str | None]:
    """Launch a REAL validation subprocess through the actual ValidationExecutor
    (not a synthetic DB row), persist its durable identity on the execution
    row exactly as `orchestrator._launch_validation` does, and return
    (execution_id, pid, process_start_key)."""
    execution_id = f"{task_id}-validation-in-flight"
    result_path = _result_path(execution_id)
    launcher = ValidationExecutor()
    handle = launcher.launch(
        ExecutionLaunch(
            task_id=task_id,
            role="BUILDER",
            worker_id="runner-validation",
            provider="runner",
            worktree_path=str(Path.cwd()),
            branch_name=None,
            prompt="",
            execution_id=execution_id,
            result_path=result_path,
            reviewed_feature_sha="feature-sha-original",
            metadata={"commands": commands},
        )
    )
    with SessionLocal() as session:
        session.add(
            BuildRunnerExecution(
                execution_id=execution_id,
                task_id=task_id,
                role="BUILDER",
                worker_id="runner-validation",
                provider="runner",
                adapter="validation",
                claim_id=claim_id,
                status="LAUNCHED",
                worktree_path=str(Path.cwd()),
                process_id=handle.process_id,
                process_start_key=handle.process_start_key,
                result_path=handle.result_path or result_path,
                reviewed_feature_sha="feature-sha-original",
                result_data={
                    "commands": commands,
                    "workspace": str(Path.cwd()),
                    "source_execution_id": f"{task_id}-builder-done",
                    "feature_sha": "feature-sha-original",
                    "validated_sha": "feature-sha-original",
                    "next_state": "REVIEW_READY",
                },
            )
        )
        session.commit()
    return execution_id, int(handle.process_id), handle.process_start_key


def _expire_claim(claim_id: str) -> None:
    with SessionLocal() as session:
        claim = session.get(BuildTaskClaim, claim_id)
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()


# ---------------------------------------------------------------------------
# Case 1: healthy live validator -- the core acceptance requirement. Zero
# unintended terminations, zero replacement validators, zero replacement
# builders, across a *real* restart (a fresh BuildRunner/ValidationExecutor
# with no in-memory knowledge of the already-launched subprocess).
# ---------------------------------------------------------------------------


def test_expired_claim_preserves_live_validator_row_not_terminated():
    """Direct recover_expired() exercise: a verified-live validator must not
    be terminated by claim-expiry bookkeeping alone."""
    claim_id = _seed_builder_succeeded("VCR-1")
    execution_id, pid, _ = _launch_real_validation("VCR-1", claim_id, commands=_sleep_command(8))
    assert _wait_alive(pid, True)
    _expire_claim(claim_id)

    try:
        with SessionLocal() as session:
            recovered = recover_expired(session)
            task = session.get(BuildTask, "VCR-1")
            claim = session.get(BuildTaskClaim, claim_id)
            validation_row = session.get(BuildRunnerExecution, execution_id)

            assert task.task_id in {t.task_id for t in recovered}
            assert task.state == "VALIDATING"
            assert claim.status == "EXPIRED"
            assert task.current_claim_id is None
            # The core fix: claim-expiry bookkeeping alone must not
            # terminate a live validator.
            assert validation_row.status == "LAUNCHED"
        assert _wait_alive(pid, True)  # not killed by the reconciliation itself
    finally:
        subprocess.run(["kill", "-9", str(pid)])


def test_runner_cycle_healthy_live_validator_survives_full_cycle_with_zero_duplicates():
    """Full-cycle, real-restart exercise: a fresh runner/executor with no
    in-memory knowledge of the already-launched subprocess must, via
    durable process identity, observe it as still running -- not launch a
    replacement validator, not touch the successful builder execution, and
    not terminate the live process."""
    claim_id = _seed_builder_succeeded("VCR-2")
    execution_id, pid, start_key = _launch_real_validation("VCR-2", claim_id, commands=_sleep_command(8))
    assert _wait_alive(pid, True)
    assert start_key, "durable process identity must be captured at launch"
    _expire_claim(claim_id)

    try:
        # A brand-new runner == a brand-new ValidationExecutor with an
        # empty in-memory `_runs`/`_process_identities` -- exactly what a
        # coordinator/executor restart looks like.
        result = _runner(executors={"builder-a": FakeExecutor()}).run_once()

        assert "VCR-2" in result.recovered

        with SessionLocal() as session:
            task = session.get(BuildTask, "VCR-2")
            builder_rows = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == "VCR-2")
                .where(BuildRunnerExecution.adapter == "fake")
            ).all()
            validation_rows = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == "VCR-2")
                .where(BuildRunnerExecution.adapter == "validation")
            ).all()

            # Zero replacement builders.
            assert len(builder_rows) == 1
            assert builder_rows[0].status == "SUCCEEDED"
            assert builder_rows[0].execution_id == "VCR-2-builder-done"

            # Zero replacement validators: still exactly the one, original
            # execution row, still LAUNCHED/RUNNING (not terminated).
            assert len(validation_rows) == 1
            assert validation_rows[0].execution_id == execution_id
            assert validation_rows[0].status in ("LAUNCHED", "RUNNING")
            assert validation_rows[0].result_data.get("reconciliation_state") in ("MATCH", "ALIVE_UNVERIFIED")

            assert task.state == "VALIDATING"

        # Zero unintended terminations: the real OS process must still be alive.
        assert _wait_alive(pid, True)
    finally:
        subprocess.run(["kill", "-9", str(pid)])


# ---------------------------------------------------------------------------
# Case 2: confirmed interrupted validator -- safe recovery on the same
# candidate feature SHA, with no overlapping old execution.
# ---------------------------------------------------------------------------


def test_runner_cycle_confirmed_dead_validator_recovers_safely_without_overlap():
    """A validator whose OS process has genuinely exited (confirmed via
    durable identity MISMATCH, not merely claim staleness) must be
    reconciled to a terminal state and validation safely relaunched against
    the same, original feature SHA -- with no overlapping live execution
    (never two live validation rows at once)."""
    claim_id = _seed_builder_succeeded("VCR-3")
    execution_id, pid, _ = _launch_real_validation("VCR-3", claim_id, commands=_sleep_command(30))
    assert _wait_alive(pid, True)
    # Simulate a genuine crash -- the process is killed outright (SIGKILL,
    # not a normal exit), so it never writes a result file. This is the
    # "confirmed interrupted" case, distinct from a validator that finished
    # quickly and left a valid result to be consumed normally.
    subprocess.run(["kill", "-9", str(pid)])
    assert _wait_alive(pid, False, timeout=5.0) is False
    _expire_claim(claim_id)

    result = _runner(executors={"builder-a": FakeExecutor()}).run_once()
    assert "VCR-3" in result.recovered

    with SessionLocal() as session:
        old_row = session.get(BuildRunnerExecution, execution_id)
        assert old_row.status in ("LOST", "TERMINATED")

        validation_rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "VCR-3")
            .where(BuildRunnerExecution.adapter == "validation")
        ).all()
        live_validations = [row for row in validation_rows if row.status in ("LAUNCHED", "RUNNING")]

        # Safe recovery on the same candidate: exactly one live validation
        # afterward (no overlap with the old, dead one), tied to the
        # original feature SHA.
        assert len(live_validations) == 1
        assert live_validations[0].execution_id != execution_id
        assert live_validations[0].reviewed_feature_sha == "feature-sha-original"
        assert (live_validations[0].result_data or {}).get("validated_sha") == "feature-sha-original"

        builder_rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "VCR-3")
            .where(BuildRunnerExecution.adapter == "fake")
        ).all()
        assert len(builder_rows) == 1  # still no replacement builder

        task = session.get(BuildTask, "VCR-3")
        assert task.state == "VALIDATING"


# ---------------------------------------------------------------------------
# Case 3: ambiguous identity -- no destructive assumption, no duplicate
# dispatch.
# ---------------------------------------------------------------------------


def test_runner_cycle_ambiguous_identity_does_not_duplicate_dispatch(monkeypatch):
    """When the platform identity probe itself cannot produce a definitive
    answer (ALIVE_UNVERIFIED) for a pid that is nonetheless still alive,
    the runner must not assume the process is gone: no termination, no
    duplicate/replacement validation dispatch."""
    claim_id = _seed_builder_succeeded("VCR-4")
    execution_id, pid, _ = _launch_real_validation("VCR-4", claim_id, commands=_sleep_command(8))
    assert _wait_alive(pid, True)
    _expire_claim(claim_id)

    import build_coordinator.runner.validation as validation_module

    real_status = validation_module.process_identity_status

    def _forced_ambiguous(check_pid: int, remembered_start_key):
        if check_pid == pid:
            return "ALIVE_UNVERIFIED"
        return real_status(check_pid, remembered_start_key)

    monkeypatch.setattr(validation_module, "process_identity_status", _forced_ambiguous)

    try:
        result = _runner(executors={"builder-a": FakeExecutor()}).run_once()
        assert "VCR-4" in result.recovered

        with SessionLocal() as session:
            validation_rows = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == "VCR-4")
                .where(BuildRunnerExecution.adapter == "validation")
            ).all()
            live_validations = [row for row in validation_rows if row.status in ("LAUNCHED", "RUNNING")]

            # No destructive assumption: the ambiguous row is not terminated.
            assert len(validation_rows) == 1
            assert validation_rows[0].execution_id == execution_id
            # No duplicate dispatch: exactly one live validation, not two.
            assert len(live_validations) == 1
            assert live_validations[0].result_data.get("reconciliation_state") == "ALIVE_UNVERIFIED"

            task = session.get(BuildTask, "VCR-4")
            assert task.state == "VALIDATING"

        assert _wait_alive(pid, True)
    finally:
        subprocess.run(["kill", "-9", str(pid)])


# ---------------------------------------------------------------------------
# Case 4: stale/failed results -- existing acceptance gates remain
# enforced. This does not duplicate the dedicated STALE_VALIDATION_CONTEXT
# coverage already in tests/test_runner.py; it just proves this file's
# preservation fix did not weaken it for the one path this file directly
# touches (claim-expiry recovery followed by a validation result callback).
# ---------------------------------------------------------------------------


def test_stale_validated_sha_result_does_not_pass_even_after_claim_expiry_recovery(tmp_path, monkeypatch):
    """A validation result reporting `passed=True` for a SHA that no longer
    matches the workspace HEAD must still be rejected by the existing
    STALE_VALIDATION_CONTEXT gate (build_coordinator/runner/orchestrator.py
    `_apply_validation_result`), even for an execution that went through
    this file's claim-expiry-preservation path."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "f.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=tmp_path)
    (tmp_path / "f.txt").write_text("b")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "two"], cwd=tmp_path)
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    claim_id = _seed_builder_succeeded("VCR-5")
    with SessionLocal() as session:
        row = session.get(BuildRunnerExecution, "VCR-5-builder-done")
        row.worktree_path = str(tmp_path)
        session.commit()

    execution_id = "VCR-5-validation-in-flight"
    launcher = ValidationExecutor()
    launcher.launch(
        ExecutionLaunch(
            task_id="VCR-5",
            role="BUILDER",
            worker_id="runner-validation",
            provider="runner",
            worktree_path=str(tmp_path),
            branch_name=None,
            prompt="",
            execution_id=execution_id,
            result_path=_result_path(execution_id),
            metadata={"commands": []},  # empty commands -> synchronous SUCCEEDED
        )
    )
    with SessionLocal() as session:
        session.add(
            BuildRunnerExecution(
                execution_id=execution_id,
                task_id="VCR-5",
                role="BUILDER",
                worker_id="runner-validation",
                provider="runner",
                adapter="validation",
                claim_id=claim_id,
                status="LAUNCHED",
                worktree_path=str(tmp_path),
                result_path=_result_path(execution_id),
                reviewed_feature_sha="0" * 40,
                result_data={
                    "commands": [],
                    "workspace": str(tmp_path),
                    "source_execution_id": "VCR-5-builder-done",
                    "feature_sha": "0" * 40,
                    "validated_sha": "0" * 40,
                    "next_state": "REVIEW_READY",
                },
            )
        )
        session.commit()

    result = _runner(executors={"builder-a": FakeExecutor()}).run_once()

    with SessionLocal() as session:
        row = session.get(BuildRunnerExecution, execution_id)
        assert row.status == "FAILED"
        assert (row.result_data or {}).get("validation_terminal_type") == "STALE_VALIDATION_CONTEXT"
        task = session.get(BuildTask, "VCR-5")
        # The stale-SHA result must not have advanced the task.
        assert task.state != "REVIEW_READY"
        assert task.state != "DONE"
