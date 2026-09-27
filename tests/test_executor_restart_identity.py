"""Regression coverage for the durable process-identity restart fix.

Root cause: SubprocessExecutor.poll() previously classified an execution as
LOST whenever this process instance had no in-memory Popen handle for it --
which is exactly what happens after any coordinator/executor restart or
reconstruction, since `_processes` is process-local. A genuinely live
external worker (e.g. a long-running Codex/Claude subprocess) could be
declared LOST mid-run, its claim released, and a duplicate worker
dispatched against the same task/worktree.

The fix persists a durable start-identity token (pid + platform-specific
process-creation identity) alongside process_id, captured at launch and
reattached across restarts (see BuildRunnerExecution.process_start_key,
SubprocessExecutor.remember_process_identity, and
orchestrator._executor_for_execution). PID alone is never trusted, because
pids get reused.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
from build_coordinator.execution.process_tree import (
    capture_process_identity,
    process_identity_status,
)


def _launch(tmp_path: Path, *, execution_id="exec-1") -> ExecutionLaunch:
    return ExecutionLaunch(
        task_id="RESTART-1",
        role="BUILDER",
        worker_id="builder-a",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="task/RESTART-1",
        prompt="do the thing",
        execution_id=execution_id,
        result_path=str(tmp_path / f"{execution_id}.json"),
    )


def _sleep_command(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _wait_alive(pid: int, expected: bool, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = subprocess.run(["kill", "-0", str(pid)]).returncode == 0
        if alive == expected:
            return alive
        time.sleep(0.02)
    return subprocess.run(["kill", "-0", str(pid)]).returncode == 0


def _spawn_and_wait_dead() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    _wait_alive(proc.pid, False)
    return proc.pid


def test_restart_while_worker_alive_reports_running_not_lost(tmp_path: Path):
    """Coordinator/executor restart while the worker is genuinely alive."""
    original = SubprocessExecutor(_sleep_command(5), log_dir=tmp_path / "logs")
    handle = original.launch(_launch(tmp_path))
    assert _wait_alive(int(handle.process_id), True)

    # Simulate a full restart: a brand-new executor instance with no
    # in-memory Popen handle, reattached purely from durable storage (as
    # orchestrator._executor_for_execution does after `--once` or a
    # coordinator restart).
    reattached = SubprocessExecutor([sys.executable], log_dir=tmp_path / "logs")
    reattached.remember_result_path(handle.execution_id, str(tmp_path / f"{handle.execution_id}.json"))
    reattached.remember_process_identity(handle.execution_id, handle.process_id, handle.process_start_key)

    observation = reattached.poll(handle.execution_id)

    assert observation.status == "RUNNING"
    assert observation.result_data["reconciliation_state"] in ("MATCH", "ALIVE_UNVERIFIED")
    assert _wait_alive(int(handle.process_id), True)  # not killed by the reconciliation itself

    subprocess.run(["kill", "-9", handle.process_id])


def test_restart_with_valid_result_already_written_is_consumed_normally(tmp_path: Path):
    """A result file that exists takes priority over identity reconciliation."""
    result_path = tmp_path / "exec-2.json"
    original = SubprocessExecutor(
        [sys.executable, "-c", "import sys, os, json; open(os.environ['BUILD_COORDINATOR_RESULT_PATH'], 'w').write(json.dumps({'status': 'SUCCEEDED', 'passed': True}))"],
        log_dir=tmp_path / "logs",
    )
    handle = original.launch(_launch(tmp_path, execution_id="exec-2"))
    for _ in range(100):
        if result_path.is_file():
            break
        time.sleep(0.05)
    assert result_path.is_file()

    reattached = SubprocessExecutor([sys.executable], log_dir=tmp_path / "logs")
    reattached.remember_result_path("exec-2", str(result_path))
    reattached.remember_process_identity("exec-2", handle.process_id, handle.process_start_key)

    observation = reattached.poll("exec-2")
    assert observation.status == "SUCCEEDED"


def test_restart_with_dead_worker_and_no_result_is_lost(tmp_path: Path):
    """The original terminal/lost handling is unchanged for a genuinely dead worker."""
    original = SubprocessExecutor([sys.executable, "-c", "pass"], log_dir=tmp_path / "logs")
    handle = original.launch(_launch(tmp_path, execution_id="exec-3"))
    assert _wait_alive(int(handle.process_id), False)

    reattached = SubprocessExecutor([sys.executable], log_dir=tmp_path / "logs")
    reattached.remember_result_path("exec-3", str(tmp_path / "exec-3.json"))
    reattached.remember_process_identity("exec-3", handle.process_id, handle.process_start_key)

    observation = reattached.poll("exec-3")
    assert observation.status == "LOST"
    assert observation.result_data["reconciliation_state"] == "LOST"


def test_pid_reuse_is_not_mistaken_for_the_original_process(tmp_path: Path):
    """PID alone must never be trusted: a different process now holding the
    same pid must not be treated as the original worker still running."""
    original = SubprocessExecutor(_sleep_command(5), log_dir=tmp_path / "logs")
    handle = original.launch(_launch(tmp_path, execution_id="exec-4"))
    real_pid = int(handle.process_id)
    assert _wait_alive(real_pid, True)

    reattached = SubprocessExecutor([sys.executable], log_dir=tmp_path / "logs")
    reattached.remember_result_path("exec-4", str(tmp_path / "exec-4.json"))
    # A forged/stale start-identity token that does not match the real
    # process currently holding this pid.
    reattached.remember_process_identity("exec-4", handle.process_id, "proc:1")

    observation = reattached.poll("exec-4")
    assert observation.status == "LOST"

    subprocess.run(["kill", "-9", str(real_pid)])


def test_no_durable_identity_falls_back_to_legacy_behavior(tmp_path: Path):
    """A row with no captured identity (predates this fix, or capture ever
    failed) gets exactly today's pre-fix LOST classification -- not worse,
    not silently assumed alive."""
    reattached = SubprocessExecutor([sys.executable], log_dir=tmp_path / "logs")
    reattached.remember_result_path("exec-5", str(tmp_path / "exec-5.json"))
    # No remember_process_identity call at all -- e.g. a legacy DB row.

    observation = reattached.poll("exec-5")
    assert observation.status == "LOST"
    assert observation.result_data["reconciliation_state"] == "LOST"


def test_process_identity_status_contract():
    import os

    pid = os.getpid()
    key = capture_process_identity(pid)
    assert key is not None
    assert process_identity_status(pid, key) == "MATCH"
    # A currently-alive pid with no remembered identity to compare against
    # (e.g. capture_process_identity() returned None right at launch time,
    # a real, reachable platform-probe-failure case -- see
    # test_missing_identity_capture_at_launch_does_not_get_treated_as_dead
    # below) must be reported as ALIVE_UNVERIFIED, never as a status a
    # caller could mistake for "confirmed gone": this process (the test
    # runner itself) is unambiguously still running.
    assert process_identity_status(pid, None) == "ALIVE_UNVERIFIED"
    assert process_identity_status(pid, "not-a-real-key") == "MISMATCH"


def test_unknown_only_when_no_remembered_identity_and_pid_is_actually_gone():
    """UNKNOWN is now reserved for the case that actually cannot be
    resolved either way: no identity was ever captured, AND the pid is not
    currently alive. It must never be returned for a pid that is alive."""
    dead = _spawn_and_wait_dead()
    assert process_identity_status(dead, None) == "UNKNOWN"
