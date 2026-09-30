"""Wave 4 — OWNERSHIP-004: process identity / hierarchy verification.

Legacy contract (test_git_identity_and_blocker_recovery.py,
build_coordinator/runner/process_identity.py):
  - PID reuse cannot impersonate a prior worker (different boot_id/create_time)
  - boot_id mismatch → DEAD, not LIVE
  - unknown identity (None pid/boot_id) → UNKNOWN
  - mismatched executable → classified correctly
  - classify_process is symmetric: only matches when all fields agree
  - restart/recovery reads saved identity, not a stale live PID
"""

from __future__ import annotations

import os

import pytest

from stagemesh.domain import ProcessIdentity
from stagemesh.process_identity import (
    boot_id,
    classify_process,
    current_process_identity,
    popen_identity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identity(pid=1234, create_time=100.0, bid="boot-abc", exe="python") -> ProcessIdentity:
    return ProcessIdentity(pid=pid, create_time=create_time, boot_id=bid, executable=exe)


def _unknown() -> ProcessIdentity:
    return ProcessIdentity(pid=None, create_time=None, boot_id=None, executable=None)


# ---------------------------------------------------------------------------
# Unknown identity stays UNKNOWN
# ---------------------------------------------------------------------------

def test_classify_unknown_saved_returns_unknown():
    saved = _unknown()
    observed = _identity()
    assert classify_process(saved, observed) == "UNKNOWN"


def test_classify_none_observed_returns_unknown():
    saved = _identity()
    assert classify_process(saved, None) == "UNKNOWN"


def test_classify_unknown_observed_returns_unknown():
    saved = _identity()
    observed = _unknown()
    assert classify_process(saved, observed) == "UNKNOWN"


def test_classify_both_unknown_returns_unknown():
    assert classify_process(_unknown(), _unknown()) == "UNKNOWN"


def test_classify_partial_saved_missing_create_time_returns_unknown():
    saved = ProcessIdentity(pid=1234, create_time=None, boot_id="boot-abc", executable="python")
    observed = _identity()
    assert classify_process(saved, observed) == "UNKNOWN"


def test_classify_partial_saved_missing_boot_id_returns_unknown():
    saved = ProcessIdentity(pid=1234, create_time=100.0, boot_id=None, executable="python")
    observed = _identity()
    assert classify_process(saved, observed) == "UNKNOWN"


# ---------------------------------------------------------------------------
# PID reuse cannot impersonate prior worker — different boot_id
# ---------------------------------------------------------------------------

def test_pid_reuse_with_different_boot_id_is_dead():
    """Same PID, different boot_id → boot has restarted → must be DEAD."""
    saved = _identity(pid=999, create_time=100.0, bid="boot-original")
    observed = _identity(pid=999, create_time=100.0, bid="boot-rebooted")
    assert classify_process(saved, observed) == "DEAD"


def test_pid_reuse_with_different_create_time_is_dead():
    """Same PID, same boot_id, different create_time → new process reused PID."""
    saved = _identity(pid=999, create_time=100.0, bid="boot-same")
    observed = _identity(pid=999, create_time=200.0, bid="boot-same")
    assert classify_process(saved, observed) == "DEAD"


def test_pid_reuse_with_different_executable_is_dead():
    """Same PID+boot_id+create_time, different executable → mismatch → DEAD."""
    saved = _identity(pid=999, create_time=100.0, bid="boot-same", exe="python")
    observed = _identity(pid=999, create_time=100.0, bid="boot-same", exe="bash")
    assert classify_process(saved, observed) == "DEAD"


# ---------------------------------------------------------------------------
# Matching identity → LIVE
# ---------------------------------------------------------------------------

def test_exact_match_returns_live():
    ident = _identity()
    assert classify_process(ident, ident) == "LIVE"


def test_exact_copy_returns_live():
    saved = _identity(pid=7777, create_time=55.5, bid="boot-x", exe="python3")
    observed = _identity(pid=7777, create_time=55.5, bid="boot-x", exe="python3")
    assert classify_process(saved, observed) == "LIVE"


# ---------------------------------------------------------------------------
# current_process_identity / boot_id
# ---------------------------------------------------------------------------

def test_current_process_identity_has_own_pid():
    ident = current_process_identity()
    assert ident.pid == os.getpid()


def test_current_process_identity_has_boot_id():
    ident = current_process_identity()
    assert isinstance(ident.boot_id, str)
    assert ident.boot_id  # non-empty


def test_boot_id_is_stable_within_process():
    """Two calls in the same process must return the same boot_id."""
    assert boot_id() == boot_id()


# ---------------------------------------------------------------------------
# popen_identity — subprocess spawned in this test
# ---------------------------------------------------------------------------

def test_popen_identity_captures_subprocess_pid():
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ident = popen_identity(proc)
        assert ident.pid == proc.pid
        assert isinstance(ident.boot_id, str)
    finally:
        proc.kill()
        proc.wait()


def test_popen_identity_boot_id_matches_parent():
    """Child launched in same OS session inherits the same boot_id."""
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ident = popen_identity(proc)
        assert ident.boot_id == boot_id()
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Restart / recovery does not inherit stale identity
# ---------------------------------------------------------------------------

def test_current_process_identity_is_known():
    ident = current_process_identity()
    assert ident.pid == os.getpid()
    assert ident.create_time is not None
    assert ident.create_time > 0
    assert ident.boot_id is not None
    assert ident.is_known


def test_popen_identity_has_real_create_time_and_is_known():
    import subprocess, sys
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ident = popen_identity(proc)
        assert ident.pid == proc.pid
        assert ident.create_time is not None
        assert ident.create_time > 0
        assert ident.boot_id == boot_id()
        assert ident.is_known
    finally:
        proc.kill()
        proc.wait()


def test_observe_process_identity_matches_popen_identity():
    import subprocess, sys
    from stagemesh.process_identity import observe_process_identity
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ident = popen_identity(proc)
        observed = observe_process_identity(proc.pid)
        assert observed.pid == proc.pid
        assert observed.is_known
        assert classify_process(ident, observed) == "LIVE"
    finally:
        proc.kill()
        proc.wait()


def test_coordinator_recovery_detects_pid_reuse_and_preserves_unrelated_process(tmp_path):
    """
    Test real Coordinator.recover() path:
    When a saved execution has a different create_time or boot_id than the currently running
    process at that PID, Coordinator.recover() treats it as DEAD (PID reuse detected),
    reclaims the claim, and NEVER kills or mutates the running process.
    """
    import subprocess, sys
    from stagemesh.coordinator import Coordinator
    from stagemesh.domain import Stage, TaskStatus
    from stagemesh.persistence import Store

    db = tmp_path / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()

    # Launch a real running subprocess
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Create a task and claim
        from stagemesh.workers import register_worker
        task_id = store.upsert_task("Test task for recovery", source="local")
        worker_id = "worker-1"
        register_worker(store, worker_id, "codex", {"code"}, current_process_identity(), 300)
        claim_id = store.acquire_claim(task_id, worker_id, lease_seconds=300)

        # Simulate PID reuse: record execution with proc.pid but a DIFFERENT create_time (500 seconds earlier)
        real_ident = popen_identity(proc)
        reused_ident = ProcessIdentity(
            pid=proc.pid,
            create_time=(real_ident.create_time or 100.0) - 500.0,
            boot_id=boot_id(),
            executable=sys.executable,
        )
        from stagemesh.domain import ExecutionKind
        exec_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            pid=reused_ident.pid,
            process_create_time=reused_ident.create_time,
            boot_id=reused_ident.boot_id,
            executable=reused_ident.executable,
        )

        coordinator = Coordinator(store, tmp_path)
        recovered = coordinator.recover()

        # The PID-reused execution was recovered (reclaimed)
        assert recovered == 1
        # The running process was NOT killed or mutated
        assert proc.poll() is None

        # The claim was released/deactivated
        claims = list(store.conn.execute("SELECT active FROM claims WHERE id=?", (claim_id,)))
        assert claims[0][0] == 0
    finally:
        proc.kill()
        proc.wait()

