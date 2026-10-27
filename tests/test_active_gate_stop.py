from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from stagemesh.contracts import GateCommand, run_gate
from stagemesh.domain import ProcessIdentity
from stagemesh.parallel import request_queue_control
from stagemesh.persistence import Store
from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope
from stagemesh.validation import Validator

TASK = "GATE-1"


@pytest.fixture(autouse=True)
def _private_gate_controls(tmp_path, monkeypatch):
    from stagemesh import validation_slot
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "slot.lock"))
    monkeypatch.delenv("STAGEMESH_WINDOW_DEADLINE", raising=False)
    validation_slot.set_window_deadline(None)
    yield
    validation_slot.set_window_deadline(None)


def _store(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("controlled gate", source_id=TASK)
    return store


def _publisher(store, marker, state="stopping", *, terminate=True, resume=False):
    errors = []
    def publish():
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), "gate did not start"
            other = Store(store.db_path)
            try:
                request_queue_control(other, state, "controlled active-gate request", terminate_running=terminate)
                if resume:
                    request_queue_control(other, "resumed", "resume cannot retract an observed run stop")
            finally:
                other.close()
        except Exception as exc:  # noqa: BLE001 -- publish thread errors to the calling test
            errors.append(exc)
    thread = threading.Thread(target=publish)
    thread.start()
    return thread, errors


def _join(thread, errors):
    thread.join(timeout=10)
    assert not thread.is_alive() and not errors


def _gate(marker, seconds=2):
    code = "import pathlib,time\n" + f"pathlib.Path({str(marker)!r}).write_text('started')\n" + f"time.sleep({seconds})\nprint('controlled complete')\n"
    return GateCommand("controlled", [sys.executable, "-c", code], timeout_seconds=30)


@pytest.mark.parametrize("resume", [False, True])
def test_active_queue_stop_interrupts_gate(tmp_path, resume):
    store = _store(tmp_path)
    marker = tmp_path / "started"
    control = QueueStopControl(store, TASK)
    thread, errors = _publisher(store, marker, resume=resume)
    started = time.monotonic()
    try:
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested) as caught:
            run_gate(tmp_path, _gate(marker, 30))
        assert caught.value.cleanup_complete is True and not caught.value.monitor_failed
        assert time.monotonic() - started < 10
        assert control.request["terminate_running"] is True
    finally:
        _join(thread, errors)
        store.close()


@pytest.mark.parametrize(("state", "terminate"), [("paused", False), ("stopping", False)])
def test_pause_or_drain_allows_active_gate_to_finish(tmp_path, state, terminate):
    store = _store(tmp_path)
    marker = tmp_path / "started"
    control = QueueStopControl(store, TASK)
    thread, errors = _publisher(store, marker, state, terminate=terminate)
    try:
        with provider_stop_scope(control):
            result = run_gate(tmp_path, _gate(marker, 0.4))
        assert result.status == "PASSED" and "controlled complete" in result.stdout
    finally:
        _join(thread, errors)
        store.close()


@pytest.mark.parametrize("monitor_failed", [False, True])
def test_confirmed_validation_stop_terminalizes_without_verdict(tmp_path, monkeypatch, monitor_failed):
    from stagemesh import validation
    store = _store(tmp_path)
    def stopped(*_args):
        raise OperatorStopRequested("controlled confirmed stop", monitor_failed=monitor_failed)
    monkeypatch.setattr(validation, "contract_for_candidate", stopped)
    try:
        with pytest.raises(OperatorStopRequested):
            Validator().validate(store, TASK, "candidate", tmp_path)
        assert not list(store.running_executions())
        row = store.conn.execute("SELECT status,result FROM executions").fetchone()
        assert row["status"] == "FAILED" and row["result"] == ("stop_monitor_failed" if monitor_failed else "operator_stop")
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert store.task_remediation_count(TASK, "VALIDATE") == 0
    finally:
        store.close()


def test_finished_gate_stop_race_cannot_accept_pass(tmp_path, monkeypatch):
    from stagemesh import contracts, gate_process
    store = _store(tmp_path)
    control = QueueStopControl(store, TASK)
    class Completed:
        pid, returncode = 12345, 0
        def communicate(self, timeout):
            request_queue_control(store, "stopping", "stop after output", terminate_running=True)
            return "success", ""
    monkeypatch.setattr(gate_process, "spawn_gate_process", lambda *_: (Completed(), ProcessIdentity(12345, 1.0, "fixture")))
    def unexpected(*_args):
        raise AssertionError("already completed gate requires no signal")
    monkeypatch.setattr(gate_process, "terminate_owned_gate", unexpected)
    try:
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested) as caught:
            contracts._run_gate_process(["controlled"], tmp_path, None, 30)
        assert caught.value.cleanup_complete is True
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["cancel", "window", "monitor"])
def test_active_gate_observes_cancel_deadline_or_monitor_failure(tmp_path, kind):
    from stagemesh import validation_slot as slot
    marker = tmp_path / "started"
    class Monitor:
        request = None
        def poll(self):
            if marker.exists():
                raise RuntimeError("controlled unavailable control store")
    tokens = slot.bind_validation_wait(cancel=marker.exists if kind == "cancel" else None)
    if kind == "window":
        slot.set_window_deadline(time.monotonic() + 0.5, clock=time.monotonic)
    try:
        with provider_stop_scope(Monitor() if kind == "monitor" else None), pytest.raises(OperatorStopRequested) as caught:
            run_gate(tmp_path, _gate(marker))
        assert marker.exists() and caught.value.cleanup_complete is True
        assert caught.value.monitor_failed == (kind == "monitor")
    finally:
        slot.reset_validation_wait(tokens)
        slot.set_window_deadline(None)


def _validation_case(tmp_path):
    from test_bounded_execution import TASK as task_id
    from test_bounded_execution import _setup
    from test_operator_workflow import _commit

    from stagemesh.domain import Stage
    from stagemesh.git import GitWorkspace
    marker = tmp_path / "gate-started"
    command = _gate(marker).command
    project, store = _setup(tmp_path, {"objective": "controlled docs", "allowed_files": ["docs/**"],
                                      "required_tests": [{"name": "controlled", "command": list(command), "timeout_seconds": 30}]})
    store.set_task_baseline(task_id, GitWorkspace(project).head())
    sha = _commit(project, {"docs/a.md": "candidate"})
    store.add_candidate(task_id, sha, "controlled", True)
    store.advance_task(task_id, Stage.VALIDATE)
    return project, store, task_id, sha, marker


def test_real_validator_stop_preserves_candidate_and_resume_earns_new_verdict(tmp_path, monkeypatch):
    from stagemesh import contracts
    from stagemesh.domain import EvidenceStatus
    project, store, task_id, sha, marker = _validation_case(tmp_path)
    captured = []
    original = contracts._run_gate_process
    def observe(command, cwd, env, timeout):
        captured.append((Path(cwd), Path(env["TMP"])))
        return original(command, cwd, env, timeout)
    monkeypatch.setattr(contracts, "_run_gate_process", observe)
    thread, errors = _publisher(store, marker)
    try:
        with provider_stop_scope(QueueStopControl(store, task_id)), pytest.raises(OperatorStopRequested) as caught:
            Validator().validate(store, task_id, sha, project)
        assert caught.value.cleanup_complete and not list(store.running_executions())
        assert store.latest_candidate(task_id)["sha"] == sha
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert not store.open_findings_for_candidate(task_id, sha)
        assert store.task_remediation_count(task_id, "VALIDATE") == 0
        assert captured and all(not path.exists() for pair in captured for path in pair)
        _join(thread, errors)
        request_queue_control(store, "resumed", "controlled new run after confirmed cleanup")
        with provider_stop_scope(QueueStopControl(store, task_id)):
            assert Validator().validate(store, task_id, sha, project) == EvidenceStatus.PASSED
        rows = store.conn.execute("SELECT result,status FROM executions ORDER BY started_at,rowid").fetchall()
        assert len(rows) == 2 and rows[0]["result"] == "operator_stop" and rows[1]["status"] == "SUCCEEDED"
        assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE kind='VALIDATION'").fetchone()[0] == 1
    finally:
        _join(thread, errors)
        store.close()


def test_cancelled_unknown_cleanup_retains_paths_and_blocks_reopened_run(tmp_path, monkeypatch):
    from stagemesh import gate_process, operator_actions
    from stagemesh.git import GitWorkspace
    from stagemesh.run_ready import run_ready
    project, store, task_id, sha, marker = _validation_case(tmp_path)
    original = gate_process.terminate_owned_gate
    known = []
    def withhold_proof(proc, identity):
        result = original(proc, identity)
        assert result.cleanup_confirmed  # actual child is dead; only the receipt is fault-injected
        known.append(True)
        return replace(result, cleanup_confirmed=False, note="controlled proof unavailable")
    monkeypatch.setattr(gate_process, "terminate_owned_gate", withhold_proof)
    thread, errors = _publisher(store, marker)
    held = []
    try:
        with provider_stop_scope(QueueStopControl(store, task_id)), pytest.raises(OperatorStopRequested) as caught:
            Validator().validate(store, task_id, sha, project)
        assert not caught.value.cleanup_complete and known == [True]
        held = [Path(caught.value.retained_workspace), Path(caught.value.retained_gate_temp)]
        assert all(path.is_dir() for path in held)
        execution = next(iter(store.running_executions()))
        assert execution["result"] == "cleanup_incomplete" and execution["process_create_time"] is None
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert store.task_remediation_count(task_id, "VALIDATE") == 0
        path = store.db_path
        store.close()
        store = Store(path)
        request_queue_control(store, "resumed", "cannot clear unknown cleanup")
        monkeypatch.setattr(operator_actions, "process_identity", lambda _pid: None)
        def forbidden(_target):
            raise AssertionError("unknown cleanup must prevent another coordinator")
        summary = run_ready(store, project, forbidden, task_id=task_id, max_steps=1)
        assert summary.stop_reason == "REFUSED:active_execution" and not summary.started
        assert len(list(store.running_executions())) == 1
        assert store.latest_candidate(task_id)["sha"] == sha
    finally:
        _join(thread, errors)
        store.close()
        if held and known == [True]:
            checkout, temp = [path.resolve() for path in held]
            assert checkout.name == "checkout" and checkout.parent.name.startswith("stagemesh-candidate-")
            assert temp.name.startswith("stagemesh-gate-")
            GitWorkspace(project).run("worktree", "remove", "--force", str(checkout))
            shutil.rmtree(checkout.parent)
            shutil.rmtree(temp)


def test_active_gate_stop_reaps_grandchild_and_preserves_unrelated_process(tmp_path):
    from test_gate_temp_isolation import _alive
    store = _store(tmp_path)
    marker = tmp_path / "grandchild.pid"
    code = "import pathlib,subprocess,sys,time\n" + "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n" + f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n" + "time.sleep(30)\n"
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    thread, errors = _publisher(store, marker)
    try:
        with provider_stop_scope(QueueStopControl(store, TASK)), pytest.raises(OperatorStopRequested) as caught:
            run_gate(tmp_path, GateCommand("tree", [sys.executable, "-c", code], timeout_seconds=10))
        assert caught.value.cleanup_complete
        assert not _alive(int(marker.read_text())) and sentinel.poll() is None
    finally:
        _join(thread, errors)
        sentinel.kill()
        sentinel.wait(timeout=5)
        store.close()


def test_validation_stop_storage_failure_preserves_control_without_verdict(tmp_path, monkeypatch):
    from stagemesh import validation
    store = _store(tmp_path)
    error = OperatorStopRequested("confirmed stop", monitor_failed=True)
    def stopped(*_args):
        raise error
    def unavailable(*_args, **_kwargs):
        raise OSError("controlled completion storage unavailable")
    monkeypatch.setattr(validation, "contract_for_candidate", stopped)
    monkeypatch.setattr(store, "finish_execution", unavailable)
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            Validator().validate(store, TASK, "candidate", tmp_path)
        assert caught.value is error and caught.value.monitor_failed and caught.value.cleanup_complete
        assert isinstance(caught.value.__cause__, OSError)
        assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        store.close()
