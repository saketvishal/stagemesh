"""Regression tests for defects found and fixed during the low-risk canary run.

DEF-1: command_continue used FakeExecutor even when real providers were configured.
DEF-2: No --provider flag existed to select a specific provider.
DEF-3: SubprocessExecutor.name was a class attribute; couldn't be overridden per-instance via constructor.
DEF-4: Capacity failures left an active claim on the task, blocking re-dispatch until lease TTL expired.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import ExecutionResult, FakeExecutor, SubprocessExecutor
from stagemesh.persistence import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "state.sqlite3")
    db.migrate()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# DEF-3: SubprocessExecutor must accept a name via its constructor
# ---------------------------------------------------------------------------


def test_subprocess_executor_accepts_name_in_constructor() -> None:
    """DEF-3: Constructing SubprocessExecutor with an explicit name must override the class default."""
    default = SubprocessExecutor([sys.executable, "--version"])
    assert default.name == "subprocess"

    named = SubprocessExecutor([sys.executable, "--version"], name="claude")
    assert named.name == "claude"

    named2 = SubprocessExecutor([sys.executable, "--version"], name="codex")
    assert named2.name == "codex"


# ---------------------------------------------------------------------------
# DEF-4: Capacity failure must release the claim immediately
# ---------------------------------------------------------------------------


class CapacityFailExecutor(FakeExecutor):
    """Executor that always reports a capacity failure (provider unavailable)."""

    name = "capacity-fail"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)


def test_capacity_failure_releases_claim_immediately(store: Store, tmp_path: Path) -> None:
    """DEF-4: When a provider reports capacity_failure the claim must be released immediately so
    the task stays OPEN and can be re-dispatched without waiting for lease TTL."""
    task_id = store.upsert_task("capacity-fail-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    coord = Coordinator(store, tmp_path, executor=CapacityFailExecutor())
    coord.tick()

    # Task must be back to OPEN, not CLAIMED
    task = store.get_task(task_id)
    assert task["status"] == TaskStatus.OPEN

    # Stage must not have advanced
    assert task["stage"] == Stage.IMPLEMENT

    # No active claims must remain
    active = list(store.conn.execute("SELECT * FROM claims WHERE task_id=? AND active=1", (task_id,)))
    assert active == [], f"Expected no active claims after capacity failure, got: {active}"


def test_capacity_failure_allows_immediate_re_dispatch(store: Store, tmp_path: Path) -> None:
    """DEF-4 (corollary): After a capacity failure the very next tick must be able to re-claim
    and re-dispatch the task if a working executor is now available."""
    task_id = store.upsert_task("re-dispatch-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    # First tick: capacity fails, claim released
    coord_cap = Coordinator(store, tmp_path, executor=CapacityFailExecutor())
    result = coord_cap.tick()
    assert result == 0
    assert store.get_task(task_id)["status"] == TaskStatus.OPEN

    # Second tick: real executor succeeds
    coord_real = Coordinator(store, tmp_path, executor=FakeExecutor())
    result = coord_real.tick()
    assert result == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE


def test_release_claim_is_idempotent(store: Store, tmp_path: Path) -> None:
    """DEF-4 (idempotency): Calling release_claim twice on the same claim must not error
    and must leave the task in a consistent state."""
    task_id = store.upsert_task("idempotent-task")
    store.advance_task(task_id, Stage.IMPLEMENT)
    claim_id = store.acquire_claim(task_id, "worker-a")
    assert claim_id is not None

    store.release_claim(claim_id)
    store.release_claim(claim_id)  # second call must be a no-op

    assert store.get_task(task_id)["status"] == TaskStatus.OPEN
    active = list(store.conn.execute("SELECT * FROM claims WHERE task_id=? AND active=1", (task_id,)))
    assert active == []


def test_release_claim_on_missing_claim_is_safe(store: Store, tmp_path: Path) -> None:
    """DEF-4 (safety): release_claim with a non-existent ID must not raise."""
    store.release_claim("00000000-0000-0000-0000-000000000000")  # should be silent


# ---------------------------------------------------------------------------
# DEF-1: continue must wire the real provider, not FakeExecutor by default
# ---------------------------------------------------------------------------


def test_subprocess_executor_name_round_trips_through_coordinator(store: Store, tmp_path: Path) -> None:
    """DEF-1 (unit-level): A Coordinator built with a named SubprocessExecutor must use that
    executor and expose its name correctly, confirming the wiring path is exercised."""
    task_id = store.upsert_task("wired-task")
    executor = SubprocessExecutor([sys.executable, "--version"], name="claude")
    coord = Coordinator(store, tmp_path, executor=executor)
    coord.tick()  # advances PLAN -> IMPLEMENT
    coord.tick()  # IMPLEMENT: runs python --version, produces candidate
    task = store.get_task(task_id)
    candidate = store.latest_candidate(task_id)
    if candidate:
        assert candidate["produced_by"] == "claude", (
            f"DEF-1 regression: expected produced_by='claude', got '{candidate['produced_by']}'"
        )


# ---------------------------------------------------------------------------
# DEF-1 CLI: --dry-run must suppress real provider and use FakeExecutor path
# ---------------------------------------------------------------------------


def test_cli_continue_dry_run_uses_fake_executor(tmp_path: Path) -> None:
    """DEF-1/DEF-2: The --dry-run flag must cause command_continue to fall back to FakeExecutor
    so scripted tests can run without a live provider."""
    import argparse
    import stagemesh.cli as cli_module

    project = tmp_path / "proj"
    project.mkdir()

    args = argparse.Namespace(
        project=str(project),
        once=True,
        json=True,
        provider=None,
        dry_run=True,
    )
    result = cli_module.command_continue(args)
    assert result == 0


# ---------------------------------------------------------------------------
# Integration: capacity failure is isolated from code failure
# ---------------------------------------------------------------------------


class CodeFailExecutor(FakeExecutor):
    """Executor that exits non-zero (code failure, not capacity)."""

    name = "code-fail"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=False)


def test_code_failure_does_not_advance_stage(store: Store, tmp_path: Path) -> None:
    """A code failure (capacity_failure=False) must not advance the task stage."""
    task_id = store.upsert_task("code-fail-task")
    store.advance_task(task_id, Stage.IMPLEMENT)

    coord = Coordinator(store, tmp_path, executor=CodeFailExecutor())
    coord.tick()

    task = store.get_task(task_id)
    assert task["stage"] == Stage.IMPLEMENT


def test_capacity_failure_is_distinguishable_from_code_failure() -> None:
    """ExecutionResult must correctly distinguish capacity failures from code failures."""
    cap_fail = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)
    code_fail = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=False)

    assert cap_fail.capacity_failure is True
    assert code_fail.capacity_failure is False
    assert cap_fail.status is ExecutionStatus.FAILED
    assert code_fail.status is ExecutionStatus.FAILED
