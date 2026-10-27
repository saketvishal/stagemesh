from types import SimpleNamespace

import pytest
from test_bounded_execution import TASK, _setup

from stagemesh import parallel
from stagemesh.domain import ExecutionKind, ProcessIdentity
from stagemesh.parallel import ParallelRunner, worker_id_for


@pytest.mark.parametrize("entry", ["abandon", "stop_snapshot"])
@pytest.mark.parametrize("research", [False, True])
@pytest.mark.parametrize("outcome", ["false", "raises", "confirmed"])
def test_pid_adapter_cleanup_keeps_ownership_until_tree_proof(tmp_path, monkeypatch, entry, outcome, research):
    project, store = _setup(tmp_path)
    claim = store.acquire_claim(TASK, worker_id_for(TASK))
    identity = ProcessIdentity(123, 1.0, "boot", "python.exe")
    execution_id = store.start_execution(
        task_id=TASK, claim_id=None if research else claim,
        kind=ExecutionKind.REVIEW if research else ExecutionKind.IMPLEMENTATION, actor="custom-stand-in",
        pid=identity.pid, process_create_time=identity.create_time, boot_id=identity.boot_id, executable=identity.executable,
    )
    attempts, discarded = [], []

    def kill(*args):
        attempts.append(args[0])
        if outcome == "raises":
            raise OSError("fixture termination failed")
        return outcome == "confirmed"

    monkeypatch.setattr(parallel, "process_identity", lambda _: identity)
    monkeypatch.setattr(parallel, "_kill_pid", kill)
    monkeypatch.setattr(parallel, "discard_partial_edits", lambda *args: discarded.append(args))
    runner = ParallelRunner(store, project, lambda *_: None, concurrency=1)
    try:
        if entry == "stop_snapshot":
            killed = runner._terminate_live_known_executions(store, {execution_id})
            assert killed == ([identity.pid] if outcome == "confirmed" else [])
        runner._abandon(store, TASK, "fixture_stop")
        retained = list(store.running_executions())
        if outcome == "confirmed":
            assert not retained and not store.has_active_claim(TASK)
            assert discarded
        else:
            assert len(retained) == 1 and retained[0]["id"] == execution_id
            assert retained[0]["result"] == "cleanup_incomplete" and retained[0]["process_create_time"] is None
            assert store.has_active_claim(TASK) and not discarded
            assert attempts == [identity.pid]  # unknown ownership must never be signalled again
    finally:
        store.close()


@pytest.mark.parametrize("result", ["success", "failure", "timeout", "error"])
def test_windows_pid_cleanup_is_bounded_and_reports_tree_result(monkeypatch, result):
    import subprocess

    identity = ProcessIdentity(123, 1.0, "boot")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["timeout"] == 5
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 5)
        if result == "error":
            raise OSError("fixture taskkill unavailable")
        return SimpleNamespace(returncode=0 if result == "success" else 1)

    monkeypatch.setattr(parallel, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(parallel, "process_identity", lambda _: None if calls and result == "success" else identity)
    monkeypatch.setattr(parallel.subprocess, "run", run)
    assert parallel._kill_pid(identity.pid, identity) is (result == "success")
    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", "123"]


def test_pid_cleanup_refuses_own_runner_and_reused_identity(monkeypatch):
    import os

    saved = ProcessIdentity(123, 1.0, "boot")
    monkeypatch.setattr(parallel, "process_identity", lambda _: ProcessIdentity(123, 2.0, "boot"))
    monkeypatch.setattr(parallel.subprocess, "run", lambda *args, **kwargs: pytest.fail("foreign process signalled"))
    assert parallel._kill_pid(123, saved) is False
    assert parallel._kill_pid(os.getpid(), saved) is False


@pytest.mark.parametrize("entry", ["abandon", "stop_snapshot"])
@pytest.mark.parametrize("second_executable", [None, "D:/tools/PYTHON.EXE"])
def test_failed_launch_cleanup_fences_duplicate_rows_even_after_root_exit(tmp_path, monkeypatch, entry, second_executable):
    project, store = _setup(tmp_path)
    claim = store.acquire_claim(TASK, worker_id_for(TASK))
    identity = ProcessIdentity(123, 1.0, "boot")
    ids = [store.start_execution(
        task_id=TASK, claim_id=claim if kind == ExecutionKind.IMPLEMENTATION else None, kind=kind,
        actor="stand-in", pid=identity.pid, process_create_time=identity.create_time, boot_id=identity.boot_id,
        executable="C:/runtime/python.exe" if kind == ExecutionKind.IMPLEMENTATION else second_executable,
    ) for kind in (ExecutionKind.IMPLEMENTATION, ExecutionKind.REVIEW)]
    attempts, discarded = [], []

    def kill(*args):
        attempts.append(args[0])
        return False  # root exits, but its tree is unconfirmed

    monkeypatch.setattr(parallel, "_kill_pid", kill)
    monkeypatch.setattr(parallel, "process_identity", lambda _: None if attempts else identity)
    monkeypatch.setattr(parallel, "discard_partial_edits", lambda *args: discarded.append(args))
    runner = ParallelRunner(store, project, lambda *_: None, concurrency=1)
    try:
        if entry == "stop_snapshot":
            assert runner._terminate_live_known_executions(store, set(ids)) == []
        runner._abandon(store, TASK, "fixture_stop")
        rows = list(store.running_executions())
        assert {row["id"] for row in rows} == set(ids)
        assert all(row["result"] == "cleanup_incomplete" and row["process_create_time"] is None for row in rows)
        assert attempts == [123] and store.has_active_claim(TASK) and not discarded
    finally:
        store.close()


def test_confirmed_signal_without_root_death_is_not_cleanup_proof(monkeypatch):
    import itertools

    identity = ProcessIdentity(123, 1.0, "boot")
    ticks = itertools.count(step=2)
    monkeypatch.setattr(parallel, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(parallel, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    monkeypatch.setattr(parallel, "process_identity", lambda _: identity)
    monkeypatch.setattr(parallel.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert parallel._kill_pid(identity.pid, identity) is False


def test_native_pid_tree_cleanup_waits_for_standin_root_exit():
    import subprocess
    import sys
    from threading import Thread

    from stagemesh.execution import kill_process_tree, popen_session_kwargs
    from stagemesh.process_identity import popen_identity

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **popen_session_kwargs())
    identity = popen_identity(proc)
    reaper = Thread(target=proc.wait)
    reaper.start()
    try:
        assert parallel._kill_pid(proc.pid, identity) is True
        reaper.join(5)
        assert not reaper.is_alive() and proc.poll() is not None
    finally:
        if proc.poll() is None:
            kill_process_tree(proc)
        reaper.join(5)
