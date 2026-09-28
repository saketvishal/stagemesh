"""Regression tests for the "missing execution identity" acceptance gap:

A claim's lease expiring (a missed heartbeat/renewal -- coordinator or
executor was down, or simply slow) is bookkeeping evidence only. On its
own it says nothing about whether the OS process a worker actually
launched under that claim has exited. Before this fix,
`terminate_executions_for_claim` (called from `recover_expired`)
unconditionally terminated every LAUNCHED/RUNNING execution row under an
expired claim for every role except validation (which had already been
fixed separately) -- with *no* durable-process-identity check at all, even
when the row had a captured process_id/process_start_key that would prove
the underlying subprocess was still alive. The task was simultaneously
flipped to a claimable state (STALE), so a fresh worker could be -- and,
per `active_implementation_tasks`'s task-vs-task-only comparisons, was not
even blocked from being -- dispatched against the same task while the
original worker might still be running: unrelated termination, unsafe
worker-lease release, and replacement dispatch, all at once.

The fix generalizes the already-proven validation-only protection: before
terminating any LAUNCHED/RUNNING row on claim-lease-expiry,
`_confirmed_alive_execution_ids` checks durable process identity
(`process_identity_status`, never bare pid) for every row that has a
captured `process_id`, for every role. A confirmed-alive (or ambiguously
alive) row is excluded from termination, its worker lease is not released,
and the task's state is left unchanged (not claimable) -- exactly mirroring
what VALIDATING already did for validation. A row with no captured
process_id (a FakeExecutor-based test double, or a legacy row) is
unaffected and keeps today's existing behavior.

These tests specifically reproduce the "identity capture returned None at
launch" case named in the acceptance requirements: a successful child
launch whose `process_start_key` is None (a real, reachable
platform-probe-failure outcome -- see process_tree.capture_process_identity
and test_executor_restart_identity.py), then executor/controller
reconstruction (a fresh runner instance, no in-memory Popen handle) *and*
claim-lease recovery (`recover_expired`) firing together.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import FakeExecutor
from build_coordinator.execution.process_tree import _process_exists, kill_process_tree
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
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    reconcile_stale_executions,
    recover_expired,
    request_task_input,
    upsert_task,
    utcnow,
)


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildWorkerLease,
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
        description="Claim-expiry live-worker preservation regression test task",
        acceptance_criteria=["passes"],
        review_policy="NONE",
        migration_allowed=False,
    )


def _config() -> RunnerConfig:
    return RunnerConfig(
        workers=(WorkerConfig("builder-a", "BUILDER", adapter="fake"),),
        result_dir=None,
    )


def _runner(executors=None) -> BuildRunner:
    return BuildRunner(SessionLocal, _config(), executors=executors, git=FakeGit())


def _sleep_command(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _wait_alive(pid: int, expected: bool, timeout: float = 2.0) -> bool:
    # Portable liveness check: reuses the project's own durable
    # process-existence probe (process_tree._process_exists), which is
    # correct on both POSIX and Windows and already distinguishes a
    # genuinely-exited process from a reused pid slot -- `kill -0` is a
    # POSIX-only shell command and does not exist on Windows.
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = _process_exists(pid)
        if alive == expected:
            return alive
        time.sleep(0.02)
    return _process_exists(pid)


def _seed_claimed_builder_with_real_process(
    task_id: str, proc: subprocess.Popen, *, process_start_key: str | None, worker_id: str = "builder-a"
) -> tuple[str, str]:
    """Build the durable state of "a BUILDER claimed the task and launched a
    real (still-alive) child process, then the claim's lease expired" --
    without touching any operational database; this is a disposable
    per-test sqlite DB. `process_start_key=None` reproduces the acceptance
    requirement's "identity capture returning None" case."""
    with SessionLocal() as session:
        task = upsert_task(session, _task(task_id))
        claim = claim_task(session, ClaimRequest(task_id, worker_id=worker_id))
        task.state = "CLAIMED"
        execution_id = f"{task_id}-builder-live"
        session.add(
            BuildRunnerExecution(
                execution_id=execution_id,
                task_id=task_id,
                role="BUILDER",
                worker_id=worker_id,
                provider="local",
                adapter="fake",
                claim_id=claim.claim_id,
                status="RUNNING",
                worktree_path=str(Path.cwd()),
                process_id=str(proc.pid),
                process_start_key=process_start_key,
            )
        )
        session.add(
            BuildWorkerLease(
                worker_id=worker_id,
                provider="local",
                execution_id=execution_id,
                status="ACTIVE",
                lease_expires_at=utcnow() + timedelta(hours=1),
            )
        )
        # Simulate the coordinator having been down long enough that the
        # claim's heartbeat lease expired while the real child process was
        # still outstanding.
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
        return claim.claim_id, execution_id


def test_expired_claim_with_confirmed_alive_process_preserves_row_and_task_state():
    """Direct unit-level exercise of recover_expired(): a claim whose lease
    expired while its BUILDER execution's real OS process is confirmed
    alive (via durable identity, MATCH case) must not terminate that row,
    must not release its worker lease, and must not flip the task to a
    claimable state."""
    proc = subprocess.Popen(_sleep_command(5))
    try:
        assert _wait_alive(proc.pid, True)
        from build_coordinator.execution.process_tree import capture_process_identity

        start_key = capture_process_identity(proc.pid)
        assert start_key is not None
        claim_id, execution_id = _seed_claimed_builder_with_real_process(
            "CEP-1", proc, process_start_key=start_key
        )

        with SessionLocal() as session:
            recover_expired(session)
            task = session.get(BuildTask, "CEP-1")
            claim = session.get(BuildTaskClaim, claim_id)
            execution = session.get(BuildRunnerExecution, execution_id)
            lease = session.scalar(
                select(BuildWorkerLease).where(BuildWorkerLease.execution_id == execution_id)
            )

            # The claim's lease bookkeeping still correctly expires...
            assert claim.status == "EXPIRED"
            # ...but the live worker underneath it is untouched:
            assert execution.status == "RUNNING"
            assert lease.status == "ACTIVE"
            # ...and the task was never made claimable, so no replacement
            # builder can be dispatched against it.
            assert task.state == "CLAIMED"

        assert _wait_alive(proc.pid, True)  # not killed by recovery itself
    finally:
        kill_process_tree(proc.pid)


def test_missing_identity_capture_at_launch_still_preserves_live_worker_through_lease_recovery():
    """Reproduces the acceptance requirement's exact scenario: a successful
    child launch whose durable identity capture returned None (the
    ALIVE_UNVERIFIED case for process_identity_status), followed by
    executor/controller reconstruction *and* claim-lease recovery firing
    together. There must be no replacement dispatch, no unsafe worker
    -lease release, and no unrelated termination while the child may still
    be alive."""
    proc = subprocess.Popen(_sleep_command(5))
    try:
        assert _wait_alive(proc.pid, True)
        # process_start_key=None reproduces "identity capture returned None
        # at launch" -- capture_process_identity's platform probe failed,
        # or was never attempted, for this execution.
        claim_id, execution_id = _seed_claimed_builder_with_real_process(
            "CEP-2", proc, process_start_key=None
        )

        # "Executor/controller reconstruction" -- a brand-new runner
        # instance, no in-memory Popen handle anywhere, exactly what a
        # coordinator/executor restart looks like -- runs a full cycle,
        # which itself calls recover_expired() ("lease recovery") as its
        # first step.
        result = _runner(executors={"builder-a": FakeExecutor()}).run_once()

        with SessionLocal() as session:
            task = session.get(BuildTask, "CEP-2")
            claim = session.get(BuildTaskClaim, claim_id)
            execution = session.get(BuildRunnerExecution, execution_id)
            lease = session.scalar(
                select(BuildWorkerLease).where(BuildWorkerLease.execution_id == execution_id)
            )
            all_builder_rows = session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "CEP-2")
            ).all()

            assert claim.status == "EXPIRED"
            # Not terminated, not released, not made claimable, and no
            # replacement builder was ever dispatched -- still exactly the
            # one, original execution row.
            assert execution.status in ("RUNNING", "LAUNCHED")
            assert lease.status == "ACTIVE"
            assert task.state == "CLAIMED"
            assert len(all_builder_rows) == 1

        assert _wait_alive(proc.pid, True)  # not killed by the cycle
        # recover_expired() legitimately records that this claim's lease
        # expired and was processed -- that bookkeeping fact is true and
        # expected. What must NOT happen (and is verified above) is any
        # unsafe consequence of that processing: no termination, no lease
        # release, no state change to claimable, no replacement dispatch.
        assert "CEP-2" in result.recovered
    finally:
        kill_process_tree(proc.pid)


def test_confirmed_dead_process_still_recovers_normally_and_allows_redispatch():
    """The inverse case, proving the fix does not simply block recovery
    forever: once the original process is genuinely, confirmably gone
    (MISMATCH), claim-lease recovery proceeds exactly as before -- the row
    terminates, its worker lease releases, the task becomes claimable, and
    a full runner cycle can safely dispatch a fresh builder."""
    proc = subprocess.Popen(_sleep_command(0.1))
    proc.wait()
    assert _wait_alive(proc.pid, False) is False

    claim_id, execution_id = _seed_claimed_builder_with_real_process(
        "CEP-3", proc, process_start_key="proc:1"  # forged/stale, will MISMATCH
    )

    with SessionLocal() as session:
        recover_expired(session)
        task = session.get(BuildTask, "CEP-3")
        claim = session.get(BuildTaskClaim, claim_id)
        execution = session.get(BuildRunnerExecution, execution_id)
        lease = session.scalar(
            select(BuildWorkerLease).where(BuildWorkerLease.execution_id == execution_id)
        )

        assert claim.status == "EXPIRED"
        assert execution.status == "TERMINATED"
        assert lease.status == "EXPIRED"
        assert task.state == "STALE"

    # A fresh runner cycle can now legitimately dispatch a replacement.
    result = _runner(executors={"builder-a": FakeExecutor()}).run_once()
    assert len(result.launched) == 1
    with SessionLocal() as session:
        all_builder_rows = session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "CEP-3")
        ).all()
        assert len(all_builder_rows) == 2  # original (terminated) + replacement

def test_reconcile_stale_executions_preserves_confirmed_alive_claimless_row():
    """Independent-review-flagged gap: `reconcile_stale_executions`'s
    no-claim branch (a row whose claim_id was cleared entirely, e.g. a
    fully-deleted claim rather than merely an expired one) terminated
    unconditionally with no process-identity check at all -- unlike the
    claim-present branch a few lines below it, which this fix already
    protects. A claimless row is stronger ownership evidence than a merely
    -stale claim, but it is still not process evidence: the same
    confirmed-alive process must not be killed just because its claim_id
    is gone."""
    proc = subprocess.Popen(_sleep_command(5))
    try:
        assert _wait_alive(proc.pid, True)
        with SessionLocal() as session:
            upsert_task(session, _task("CEP-4"))
            session.add(
                BuildRunnerExecution(
                    execution_id="CEP-4-claimless-live",
                    task_id="CEP-4",
                    role="BUILDER",
                    worker_id="builder-a",
                    provider="local",
                    adapter="fake",
                    claim_id=None,
                    status="RUNNING",
                    worktree_path=str(Path.cwd()),
                    process_id=str(proc.pid),
                    process_start_key=None,
                )
            )
            session.commit()

        with SessionLocal() as session:
            terminated = reconcile_stale_executions(session)
            execution = session.get(BuildRunnerExecution, "CEP-4-claimless-live")

            assert "CEP-4-claimless-live" not in {row.execution_id for row in terminated}
            assert execution.status == "RUNNING"

        assert _wait_alive(proc.pid, True)  # not killed by reconciliation
    finally:
        kill_process_tree(proc.pid)


def test_request_task_input_preserves_confirmed_alive_execution_under_same_claim():
    """Independent-review-flagged gap: `request_task_input` releases the
    implementation claim/lease when a task moves to WAITING_FOR_INPUT, and
    called `terminate_executions_for_claim` with no `exclude_execution_ids`
    -- so any other LAUNCHED/RUNNING row sharing that claim_id (validation
    shares the BUILDER/REMEDIATION claim_id; there is no dedicated
    VALIDATION claim_type) would be killed with no process-identity check,
    purely because of this state transition, even if it was confirmed or
    ambiguously still alive."""
    proc = subprocess.Popen(_sleep_command(5))
    try:
        assert _wait_alive(proc.pid, True)
        claim_id, execution_id = _seed_claimed_builder_with_real_process(
            "CEP-5", proc, process_start_key=None
        )

        with SessionLocal() as session:
            task = request_task_input(session, "CEP-5", "need a decision", claim_id=claim_id)
            execution = session.get(BuildRunnerExecution, execution_id)
            lease = session.scalar(
                select(BuildWorkerLease).where(BuildWorkerLease.execution_id == execution_id)
            )

            assert task.state == "WAITING_FOR_INPUT"
            # Not terminated and not released: the confirmed/ambiguously
            # alive execution and its worker lease are left exactly as
            # they were, even though claim/lease ownership itself moved on.
            assert execution.status == "RUNNING"
            assert lease.status == "ACTIVE"

        assert _wait_alive(proc.pid, True)  # not killed by the transition
    finally:
        kill_process_tree(proc.pid)

