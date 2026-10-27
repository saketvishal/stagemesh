from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stagemesh.contracts import GateCommand, candidate_workspace, run_gate
from stagemesh.domain import ProcessIdentity
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.queue_control import OperatorStopRequested
from stagemesh.validation import Validator


@pytest.fixture(autouse=True)
def _private_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "slot.lock"))


class _UnreapedProcess:
    pid = 12345
    returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        raise OSError("termination denied")

    def wait(self, timeout):
        raise subprocess.TimeoutExpired("controlled-gate", timeout)

    def communicate(self, timeout):
        raise subprocess.TimeoutExpired("controlled-gate", timeout, output="partial")


def test_failed_kill_and_reap_cannot_report_termination(monkeypatch):
    from stagemesh import gate_process

    proc = _UnreapedProcess()
    saved = ProcessIdentity(proc.pid, 1.0, "test")
    monkeypatch.setattr(gate_process, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(gate_process, "process_identity", lambda _pid: saved)
    monkeypatch.setattr(gate_process, "_terminate_windows_descendants", lambda *_: True)
    result = gate_process.terminate_owned_gate(proc, saved)
    assert result.terminated is False


def test_unconfirmed_timeout_is_control_flow_not_failed_gate(tmp_path, monkeypatch):
    from stagemesh import gate_process

    proc = _UnreapedProcess()
    saved = ProcessIdentity(proc.pid, 1.0, "test")
    monkeypatch.setattr(gate_process, "spawn_gate_process", lambda *_: (proc, saved))
    monkeypatch.setattr(gate_process, "terminate_owned_gate", lambda *_: SimpleNamespace(
        terminated=False, cleanup_confirmed=False, note="tree still unknown", returncode=None,
    ))
    with pytest.raises(OperatorStopRequested) as caught:
        run_gate(tmp_path, GateCommand("controlled", [sys.executable], timeout_seconds=1))
    assert caught.value.cleanup_complete is False
    # Only a fake process was used, so this test can dispose of its retained temp safely.
    retained = getattr(caught.value, "retained_gate_temp", None)
    if retained:
        path = Path(retained).resolve()
        assert path.name.startswith("stagemesh-gate-")
        shutil.rmtree(path)


def test_candidate_checkout_survives_unknown_cleanup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    (project / "file.txt").write_text("candidate", encoding="utf-8")
    sha = git.commit_all("initial")
    held = None
    try:
        with pytest.raises(OperatorStopRequested), candidate_workspace(project, sha) as checkout:
            held = checkout
            (checkout / "partial.txt").write_text("unfinished gate", encoding="utf-8")
            raise OperatorStopRequested("unknown gate cleanup", cleanup_complete=False)
        assert held is not None and held.is_dir()
        assert (held / "partial.txt").read_text(encoding="utf-8") == "unfinished gate"
    finally:
        if held is not None and held.exists():
            parent = held.resolve().parent
            assert held.name == "checkout" and parent.name.startswith("stagemesh-candidate-")
            git.run("worktree", "remove", "--force", str(held))
            shutil.rmtree(parent)


def test_gate_temp_survives_unknown_cleanup(tmp_path, monkeypatch):
    from stagemesh import contracts

    captured = {}

    def uncertain(_command, _cwd, env, _timeout):
        captured["path"] = Path(env["TMP"])
        (captured["path"] / "partial.txt").write_text("held", encoding="utf-8")
        raise OperatorStopRequested("unknown gate cleanup", cleanup_complete=False)

    monkeypatch.setattr(contracts, "_run_gate_process", uncertain)
    try:
        with pytest.raises(OperatorStopRequested):
            run_gate(tmp_path, GateCommand("controlled", [sys.executable]))
        assert captured["path"].is_dir()
        assert (captured["path"] / "partial.txt").read_text(encoding="utf-8") == "held"
    finally:
        path = captured.get("path")
        if path is not None and path.exists():
            assert path.resolve().name.startswith("stagemesh-gate-")
            shutil.rmtree(path)


def test_validator_retains_claimless_execution_without_failure_evidence(tmp_path, monkeypatch):
    from stagemesh import validation

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task = store.upsert_task("controlled validation")
    error = OperatorStopRequested("unknown gate cleanup", cleanup_complete=False,
                                  retained_workspace=str(tmp_path / "held-checkout"))
    error.gate_identity = ProcessIdentity(12345, 1.0, "test", "controlled-gate")
    error.retained_gate_temp = str(tmp_path / "held-temp")

    def uncertain(*_args):
        raise error

    monkeypatch.setattr(validation, "contract_for_candidate", uncertain)
    try:
        with pytest.raises(OperatorStopRequested):
            Validator().validate(store, task, "candidate", tmp_path)
        rows = list(store.running_executions())
        assert len(rows) == 1 and rows[0]["claim_id"] is None
        assert rows[0]["result"] == "cleanup_incomplete"
        assert rows[0]["process_create_time"] is None
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert store.task_remediation_count(task, "VALIDATE") == 0
        details = [json.loads(r["payload"]) for r in store.audit_events()
                   if r["event_type"] == "validation.cleanup_incomplete"]
        assert details and details[-1]["candidate_sha"] == "candidate"
        assert details[-1]["retained_workspace"] == error.retained_workspace
        assert details[-1]["retained_gate_temp"] == error.retained_gate_temp
    finally:
        store.close()


@pytest.mark.parametrize("cleanup_error", [OSError("denied"), KeyboardInterrupt()])
def test_cleanup_exception_preserves_ownership(tmp_path, monkeypatch, cleanup_error):
    from stagemesh import contracts, gate_process

    saved = ProcessIdentity(12345, 1.0, "test")
    monkeypatch.setattr(gate_process, "spawn_gate_process", lambda *_: (_UnreapedProcess(), saved))

    def fails(*_args):
        raise cleanup_error

    monkeypatch.setattr(gate_process, "terminate_owned_gate", fails)
    with pytest.raises(OperatorStopRequested) as caught:
        contracts._run_gate_process(["controlled"], tmp_path, None, 1)
    assert caught.value.cleanup_complete is False
    assert caught.value.gate_identity == saved


def test_reaped_root_cannot_erase_unknown_descendant_cleanup(tmp_path, monkeypatch):
    from stagemesh import contracts, gate_process

    class RootExits(_UnreapedProcess):
        def kill(self):
            self.returncode = -9

        def wait(self, timeout):
            return self.returncode

    proc = RootExits()
    saved = ProcessIdentity(proc.pid, 1.0, "test")
    monkeypatch.setattr(gate_process, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(gate_process, "process_identity", lambda _pid: saved)
    monkeypatch.setattr(gate_process, "_terminate_windows_descendants", lambda *_: False)
    monkeypatch.setattr(gate_process, "spawn_gate_process", lambda *_: (proc, saved))
    with pytest.raises(OperatorStopRequested) as caught:
        contracts._run_gate_process(["controlled"], tmp_path, None, 1)
    assert proc.poll() == -9
    assert caught.value.cleanup_complete is False


def test_failed_windows_snapshot_is_not_an_empty_owned_tree(monkeypatch):
    from stagemesh import gate_process

    monkeypatch.setattr(gate_process, "_windows_parent_snapshot", lambda: None)
    assert gate_process._terminate_windows_descendants(12345, 1.0) is False


def test_unknown_child_identity_is_not_signalled_or_confirmed(monkeypatch):
    from stagemesh import gate_process

    monkeypatch.setattr(gate_process, "_windows_parent_snapshot", lambda: [(20, 10)])
    monkeypatch.setattr(gate_process, "process_identity", lambda pid: ProcessIdentity(pid, None, "test"))
    calls = []
    monkeypatch.setattr(gate_process, "_terminate_windows_pid", lambda *args: calls.append(args))
    assert gate_process._terminate_windows_descendants(10, 1.0) is False
    assert not calls


def test_retained_validation_refuses_restart_after_parent_death(tmp_path, monkeypatch):
    from test_bounded_execution import TASK
    from test_operator_workflow import _commit, _with_baseline

    from stagemesh import contracts, operator_actions
    from stagemesh.domain import Stage
    from stagemesh.gate_process import GateCleanupUnconfirmed
    from stagemesh.run_ready import run_ready

    project, store, _ = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/change.md": "candidate"})
    store.add_candidate(TASK, sha, "controlled", True)
    store.advance_task(TASK, Stage.VALIDATE)
    held = {}

    def uncertain(_command, cwd, env, _timeout):
        held["checkout"] = Path(cwd)
        held["temp"] = Path(env["TMP"])
        (held["checkout"] / "partial.txt").write_text("retained", encoding="utf-8")
        (held["temp"] / "partial.txt").write_text("retained", encoding="utf-8")
        raise GateCleanupUnconfirmed("controlled failure", ProcessIdentity(12345, 1.0, "test"))

    monkeypatch.setattr(contracts, "_run_gate_process", uncertain)
    try:
        with pytest.raises(OperatorStopRequested):
            Validator().validate(store, TASK, sha, project)
        execution = next(iter(store.running_executions()))
        assert execution["claim_id"] is None and execution["pid"] is not None
        execution_id = execution["id"]
        store.close()
        store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
        monkeypatch.setattr(operator_actions, "process_identity", lambda _pid: None)
        started = []

        def factory(_target):
            started.append(True)
            raise AssertionError("a retained gate must prevent another coordinator")

        summary = run_ready(store, project, factory, task_id=TASK, max_steps=1)
        assert summary.started is False and summary.stop_reason == "REFUSED:active_execution"
        assert summary.recovered == [] and started == []
        saved = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        assert saved["status"] == "RUNNING" and saved["result"] == "cleanup_incomplete"
        assert saved["process_create_time"] is None
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert all(path.is_dir() and (path / "partial.txt").exists() for path in held.values())
    finally:
        store.close()
        # All subprocess operations above were controlled failures, with no live child.
        checkout = held.get("checkout")
        if checkout is not None and checkout.exists():
            parent = checkout.resolve().parent
            assert checkout.name == "checkout" and parent.name.startswith("stagemesh-candidate-")
            GitWorkspace(project).run("worktree", "remove", "--force", str(checkout))
            shutil.rmtree(parent)
        temp = held.get("temp")
        if temp is not None and temp.exists():
            assert temp.resolve().name.startswith("stagemesh-gate-")
            shutil.rmtree(temp)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows handle confirmation")
def test_native_windows_descendant_handle_is_confirmed_before_release():
    from stagemesh.gate_process import _terminate_windows_pid
    from stagemesh.process_identity import popen_identity

    proc = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"])
    try:
        saved = popen_identity(proc)
        assert saved.is_known
        assert _terminate_windows_pid(proc.pid, saved.create_time) is True
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize(
    "bound_kind,terminated,wait_result,expected,signals",
    [("same", False, 258, False, 1), ("same", True, 258, False, 1),
     ("same", False, 0, True, 1), ("unknown", True, 0, False, 0),
     ("replacement", True, 0, True, 0)],
)
def test_windows_handle_failures_never_become_tree_proof(
    monkeypatch, bound_kind, terminated, wait_result, expected, signals,
):
    import ctypes

    from stagemesh import gate_process
    from stagemesh import process_identity as identities

    saved = ProcessIdentity(12345, 1.0, "test")
    bound = {"same": saved, "unknown": ProcessIdentity(12345, None, "test"),
             "replacement": ProcessIdentity(12345, 2.0, "test")}[bound_kind]

    class Api:
        def __init__(self, result):
            self.result, self.calls = result, []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    kernel = SimpleNamespace(OpenProcess=Api(123), TerminateProcess=Api(terminated),
                             WaitForSingleObject=Api(wait_result), CloseHandle=Api(True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(gate_process, "process_identity", lambda _pid: saved)
    captures = []

    def observe(_pid, *, owned_handle):
        captures.append(owned_handle)
        return bound

    monkeypatch.setattr(identities, "_windows_process_identity", observe)
    assert gate_process._terminate_windows_pid(saved.pid, saved.create_time) is expected
    assert captures == [123] and len(kernel.TerminateProcess.calls) == signals
    if signals:
        assert kernel.WaitForSingleObject.calls == [(123, 5000)]
    assert kernel.CloseHandle.calls == [(123,)]


@pytest.mark.parametrize("intermediate,expected", [(None, False), (ProcessIdentity(20, 0.5, "test"), True)])
def test_missing_and_proven_unrelated_snapshot_branches_are_distinguished(monkeypatch, intermediate, expected):
    from stagemesh import gate_process

    monkeypatch.setattr(gate_process, "_windows_parent_snapshot", lambda: [(20, 10), (30, 20)])
    monkeypatch.setattr(gate_process, "process_identity", lambda pid: intermediate if pid == 20 else ProcessIdentity(pid, 3.0, "test"))
    calls = []
    monkeypatch.setattr(gate_process, "_terminate_windows_pid", lambda *args, **kwargs: calls.append(args))
    assert gate_process._terminate_windows_descendants(10, 1.0) is expected
    assert not calls  # never signal a missing or proven-unrelated snapshot lineage


def test_unknown_root_creation_cannot_authorize_descendant_signal(monkeypatch):
    from stagemesh import gate_process

    monkeypatch.setattr(gate_process, "_windows_parent_snapshot", lambda: [(20, 10)])
    monkeypatch.setattr(gate_process, "process_identity", lambda pid: ProcessIdentity(pid, 2.0, "test"))
    calls = []
    monkeypatch.setattr(gate_process, "_terminate_windows_pid", lambda *args, **kwargs: calls.append(args))
    assert gate_process._terminate_windows_descendants(10, None) is False
    assert not calls


def test_interrupted_drain_keeps_typed_cleanup_and_monitor_flag(tmp_path, monkeypatch):
    from stagemesh import contracts, gate_process

    class InterruptedDrain(_UnreapedProcess):
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("controlled", timeout)
            raise OperatorStopRequested("monitor unavailable", monitor_failed=True)

    proc = InterruptedDrain()
    identity = ProcessIdentity(proc.pid, 1.0, "test")
    monkeypatch.setattr(gate_process, "spawn_gate_process", lambda *_: (proc, identity))
    monkeypatch.setattr(gate_process, "terminate_owned_gate", lambda *_: SimpleNamespace(cleanup_confirmed=True, note="terminated"))
    with pytest.raises(OperatorStopRequested) as caught:
        contracts._run_gate_process(["controlled"], tmp_path, None, 1)
    assert caught.value.cleanup_complete is False
    assert caught.value.monitor_failed is True
    assert caught.value.gate_identity == identity and caught.value.gate_cwd == str(tmp_path)


def test_storage_failure_preserves_uncertain_control_flow(tmp_path, monkeypatch):
    from stagemesh import validation

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task = store.upsert_task("controlled validation")
    error = OperatorStopRequested("unknown cleanup", cleanup_complete=False,
                                  retained_workspace=str(tmp_path / "held"))

    def fail_contract(*_args):
        raise error

    def fail_storage(*_args):
        raise OSError("controlled storage failure")

    monkeypatch.setattr(validation, "contract_for_candidate", fail_contract)
    monkeypatch.setattr(store, "retain_execution_for_cleanup", fail_storage)
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            Validator().validate(store, task, "candidate", tmp_path)
        assert caught.value is error and caught.value.cleanup_complete is False
        assert isinstance(caught.value.__cause__, OSError)
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert list(store.running_executions())
    finally:
        store.close()


def test_detail_audit_failure_keeps_original_control_and_unknown_fence(tmp_path, monkeypatch):
    from stagemesh import validation

    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    task = store.upsert_task("controlled validation")
    error = OperatorStopRequested("unknown cleanup", cleanup_complete=False)

    def fail_contract(*_args):
        raise error

    def fail_audit(*_args):
        raise OSError("controlled detail audit failure")

    monkeypatch.setattr(validation, "contract_for_candidate", fail_contract)
    monkeypatch.setattr(validation, "record_audit", fail_audit)
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            Validator().validate(store, task, "candidate", tmp_path)
        assert caught.value is error and caught.value.cleanup_complete is False
        assert isinstance(caught.value.__cause__, OSError)
        saved = next(iter(store.running_executions()))
        assert saved["result"] == "cleanup_incomplete" and saved["process_create_time"] is None
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        store.close()


def test_unrelated_old_branch_does_not_block_cleanup_of_known_owned_child(monkeypatch):
    from stagemesh import gate_process

    monkeypatch.setattr(gate_process, "_windows_parent_snapshot", lambda: [(20, 10), (30, 20), (40, 10)])
    identities = {20: ProcessIdentity(20, 0.5, "test"), 40: ProcessIdentity(40, 2.0, "test")}
    monkeypatch.setattr(gate_process, "process_identity", lambda pid: identities[pid])
    calls = []

    def terminate(pid, created, **_kwargs):
        calls.append((pid, created))
        return True

    monkeypatch.setattr(gate_process, "_terminate_windows_pid", terminate)
    assert gate_process._terminate_windows_descendants(10, 1.0) is True
    assert calls == [(40, 2.0)]
