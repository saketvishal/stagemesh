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

def test_restart_uses_new_identity_not_stale_saved():
    """
    Simulates: worker saved identity before restart; after restart,
    current_process_identity() returns a different pid — classify
    correctly shows DEAD (old), LIVE (new).
    """
    old_saved = _identity(pid=os.getpid() + 100_000, create_time=1.0, bid="old-boot")
    new_current = current_process_identity()

    # Old saved identity against new observed → UNKNOWN (create_time=None in current)
    # or DEAD if the PIDs/boot-ids differ.  Either way, NOT LIVE.
    result = classify_process(old_saved, new_current)
    assert result in ("DEAD", "UNKNOWN")


def test_identity_mismatch_does_not_kill_unrelated_processes():
    """
    classify_process must not execute any system calls that could affect
    unrelated processes. Verify it is a pure classification function.
    """
    saved = _identity(pid=1, create_time=1.0, bid="boot-1")  # PID 1 = system init
    observed = _identity(pid=1, create_time=2.0, bid="boot-2")  # different boot
    # Should return DEAD, not raise, not kill anything
    result = classify_process(saved, observed)
    assert result == "DEAD"
