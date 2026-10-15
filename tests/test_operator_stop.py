from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_bounded_execution import TASK, _setup

from stagemesh.coordinator import Coordinator
from stagemesh.domain import Stage
from stagemesh.execution import SubprocessExecutor
from stagemesh.parallel import request_queue_control
from stagemesh.persistence import Store
from stagemesh.run_ready import RunSummary, drive_task


@pytest.mark.parametrize("engine", ["subprocess", "runtime", "pooled", "child-cli"])
def test_terminate_stop_reaps_provider_tree_preserves_candidate_and_releases_claim(tmp_path, engine):
    project, store = _setup(tmp_path)
    marker = tmp_path / "provider-started.json"
    script = tmp_path / "provider.py"
    script.write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "Path('docs/a.md').write_text('unfinished work')\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{'parent':os.getpid(),'child':child.pid}}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    prior = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    store.add_candidate(TASK, prior, "previous-provider", True)
    executor = SubprocessExecutor([sys.executable, str(script)], name="sleeper", timeout_seconds=8)
    fallback_marker = tmp_path / "fallback-started"
    if engine in {"runtime", "pooled"}:
        from stagemesh.providers import RuntimeCommandAdapter
        executor = RuntimeCommandAdapter("sleeper", (sys.executable, str(script)), timeout_seconds=8)
        if engine == "pooled":
            from stagemesh.provider_pool import IMPLEMENT, PooledExecutor, ProviderPool
            fallback = RuntimeCommandAdapter("fallback", (sys.executable, "-c",
                f"from pathlib import Path;Path({str(fallback_marker)!r}).write_text('unexpected')"))
            executor = PooledExecutor(ProviderPool([executor, fallback],
                {IMPLEMENT: ("sleeper", "fallback")}, require_independent=False))
    if engine == "child-cli":
        (project / ".stagemesh/config.json").write_text(json.dumps({
            "providers": {"sleeper": {"command": f'"{sys.executable}" "{script}"'}},
            "routing": {"mode": "STAGED", "require_independent_review": False,
                "pools": {"IMPLEMENT": ["sleeper"], "REVIEW": ["sleeper"]}},
        }), encoding="utf-8")
    coordinator = Coordinator(store, project, executor=executor)
    failures = []
    requested_at = []
    def stop():
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), "disposable provider never started"
            other = Store(store.db_path)
            requested_at.append(time.monotonic())
            request_queue_control(other, "stopping", "test operator stop", terminate_running=True)
            other.close()
        except BaseException as exc:  # noqa: BLE001 -- propagate thread failure to the test
            failures.append(exc)
    thread = threading.Thread(target=stop)
    thread.start()
    summary = RunSummary(True, "UNSET", task_id=TASK)
    try:
        if engine == "child-cli":
            from stagemesh.survivor_wave import subprocess_task_runner
            result = subprocess_task_runner(project)(TASK, 1)
            summary.stop_reason = result.stop_reason
            summary.message = result.message
        else:
            drive_task(store, project, coordinator, TASK, summary, max_steps=1)
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive() and not failures
    assert time.monotonic() - requested_at[0] < 5, "stop waited for the provider timeout/natural exit"
    assert summary.stop_reason == "OPERATOR_STOP", summary
    from stagemesh.parallel import queue_control_state
    assert queue_control_state(store)["state"] == "stopped"
    assert not fallback_marker.exists()
    from stagemesh.workspaces import task_workspace
    assert (task_workspace(project, TASK) / "docs/a.md").read_text() == "unfinished work"
    assert not list(store.running_executions())
    assert not store.has_active_claim(TASK)
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    assert store.get_task(TASK)["status"] == "OPEN"
    assert store.latest_candidate(TASK)["sha"] == prior
    assert store.task_remediation_count(TASK, "IMPLEMENT") == 0
    assert store.task_remediation_count(TASK, "VALIDATE") == 0
    execution = store.conn.execute("SELECT result FROM executions ORDER BY started_at DESC LIMIT 1").fetchone()
    assert execution["result"] == "operator_stop"
    from stagemesh.process_identity import process_identity
    pids = json.loads(marker.read_text())
    def terminated(pid):
        identity = process_identity(pid)
        if identity is None:
            return True
        # Linux may briefly retain a terminated orphan as a zombie until init reaps it.
        stat = Path(f"/proc/{pid}/stat")
        return sys.platform.startswith("linux") and stat.exists() and stat.read_text().split()[2] == "Z"
    assert all(terminated(pid) for pid in pids.values()), pids
    store.close()


@pytest.mark.parametrize("state,terminate", [("paused", False), ("stopping", False)])
def test_pause_and_drain_stop_do_not_kill_the_active_provider(tmp_path, state, terminate):
    from stagemesh.execution import communicate_bounded, popen_session_kwargs
    from stagemesh.queue_control import QueueStopControl, provider_stop_scope
    _project, store = _setup(tmp_path)
    script = "import sys,time;sys.stdin.read();time.sleep(.2);print('finished')"
    control = QueueStopControl(store, TASK)
    request_queue_control(store, state, "drain or pause", terminate_running=terminate)
    proc = subprocess.Popen([sys.executable, "-c", script], text=True, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_session_kwargs())
    with provider_stop_scope(control):
        stdout, _stderr, timed_out = communicate_bounded(proc, "input", 5)
    assert proc.returncode == 0 and stdout.strip() == "finished" and not timed_out
    assert control.blocks_admission()
    store.close()


def test_stop_is_sticky_for_current_run_but_resume_allows_a_new_run(tmp_path):
    from stagemesh.queue_control import QueueStopControl
    _project, store = _setup(tmp_path)
    current = QueueStopControl(store, TASK)
    request_queue_control(store, "stopping", "stop this run", terminate_running=True)
    request_queue_control(store, "resumed", "future run")
    assert current.blocks_admission()
    assert current.poll()["terminate_running"]
    assert not QueueStopControl(store, TASK).blocks_admission()
    current.complete_if_idle()
    from stagemesh.parallel import queue_control_state
    assert queue_control_state(store)["state"] == "running", "late cleanup overwrote resume"
    store.close()


def test_cancel_does_not_kill_an_unverified_identity_or_unrelated_process(tmp_path, monkeypatch):
    from stagemesh import execution
    from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope
    _project, store = _setup(tmp_path)
    control = QueueStopControl(store, TASK)
    request_queue_control(store, "stopping", "unverified stop", terminate_running=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(.3)"],
        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **execution.popen_session_kwargs())
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        monkeypatch.setattr(execution, "classify_process", lambda saved, current: "UNKNOWN")
        monkeypatch.setattr(execution, "kill_process_tree", lambda proc: pytest.fail("unverified identity killed"))
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested):
            execution.communicate_bounded(proc, "", 5)
        assert proc.returncode == 0, "unknown identity was killed instead of allowed to finish"
        assert sentinel.poll() is None, "an unrelated process was terminated"
    finally:
        sentinel.kill()
        sentinel.wait(timeout=5)
        proc.wait(timeout=5)
    assert any(r["event_type"] == "queue.provider_stop_identity_unverified" for r in store.audit_events())
    store.close()


@pytest.mark.parametrize("max_steps", [1, 3])
def test_drain_stops_before_another_lifecycle_stage(tmp_path, max_steps):
    project, store = _setup(tmp_path)
    class DrainCoordinator:
        def __init__(self):
            self.calls = 0
        def tick(self):
            self.calls += 1
            request_queue_control(store, "stopping", "drain current stage")
            store.advance_task(TASK, Stage.VALIDATE)
            return 1
    coordinator = DrainCoordinator()
    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(store, project, coordinator, TASK, summary, max_steps=max_steps)
    assert summary.stop_reason == "OPERATOR_STOP" and coordinator.calls == 1
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    from stagemesh.parallel import queue_control_state
    assert queue_control_state(store)["state"] == "stopped"
    store.close()


def test_review_stop_reaps_provider_without_recording_a_verdict(tmp_path):
    from test_review_terminalization import TASK as review_task
    from test_review_terminalization import _project_with_candidate

    from stagemesh.domain import EvidenceKind
    from stagemesh.providers import RuntimeCommandAdapter
    from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope
    from stagemesh.review import Reviewer
    project, store, candidate = _project_with_candidate(tmp_path)
    marker = tmp_path / "review-started"
    script = tmp_path / "review.py"
    script.write_text("import time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\ntime.sleep(60)\n", encoding="utf-8")
    adapter = RuntimeCommandAdapter("reviewer", (sys.executable, str(script)), timeout_seconds=8)
    control = QueueStopControl(store, review_task)
    failures = []
    def stop():
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists()
            other = Store(store.db_path)
            request_queue_control(other, "stopping", "review stop", terminate_running=True)
            other.close()
        except Exception as exc:  # noqa: BLE001 -- propagate thread failure to test
            failures.append(exc)
    thread = threading.Thread(target=stop)
    thread.start()
    try:
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested):
            Reviewer(adapter=adapter, require_independent=True).review(store, review_task, candidate, project)
    finally:
        thread.join(timeout=20)
    assert not thread.is_alive() and not failures
    assert not list(store.running_executions())
    row = store.conn.execute("SELECT result FROM executions ORDER BY started_at DESC LIMIT 1").fetchone()
    assert row["result"] == "operator_stop"
    assert store.latest_candidate(review_task)["sha"] == candidate
    assert not store.conn.execute("SELECT 1 FROM evidence WHERE kind=?", (EvidenceKind.REVIEW,)).fetchall()
    assert not store.open_findings_for_candidate(review_task, candidate)
    store.close()


@pytest.mark.parametrize("resume", [False, True])
def test_wave_stop_blocks_publication_and_next_task(tmp_path, resume):
    from test_survivor_wave import Rig, make_store

    from stagemesh.survivor_wave import run_wave
    store, project = make_store(tmp_path, ["1", "2"])
    def stop_after_done(rig, task_id):
        result = rig.finish_done(task_id)
        other = Store(store.db_path)
        request_queue_control(other, "stopping", "stop before publishing")
        if resume:
            request_queue_control(other, "resumed", "future run")
        other.close()
        return result
    rig = Rig(project, {"1": stop_after_done})
    report = run_wave(store, ["1", "2"], rig.hooks(), concurrency=1)
    assert rig.runs == ["1"] and rig.prs == []
    assert report.tasks[0].landing == "staged_not_published"
    assert report.tasks[1].outcome == "not_started"
    assert report.stopped_early
    store.close()


@pytest.mark.parametrize("resume", [False, True])
def test_window_stop_does_not_admit_more_tasks_after_provider_returns(tmp_path, resume):
    from test_run_window import go

    from stagemesh.survivor_wave import RunResult
    def stopped(rig, task_id):
        other = rig.worker_store()
        request_queue_control(other, "stopping", "provider cancelled", terminate_running=True)
        if resume:
            request_queue_control(other, "resumed", "new window only")
        other.close()
        return RunResult("OPERATOR_STOP", "provider cancelled")
    report, store, rig, _options = go(tmp_path, ["1", "2"], {"1": stopped}, concurrency=1)
    assert rig.runs == ["1"] and not rig.prs
    assert report.stop_reason == "queue admission stopping"
    assert report.tasks[0].outcome == "stopped" and report.tasks[0].resumable
    assert not list(store.running_executions())
    store.close()


def test_stop_is_project_scoped_and_never_completes_over_an_active_execution(tmp_path):
    from stagemesh.domain import ExecutionKind
    from stagemesh.parallel import queue_control_state
    from stagemesh.queue_control import QueueStopControl
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    _project, store = _setup(tmp_path / "one")
    _other_project, other = _setup(tmp_path / "two")
    control, unrelated = QueueStopControl(store), QueueStopControl(other)
    execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.REVIEW)
    request_queue_control(store, "stopping", "active execution retained", terminate_running=True)
    control.complete_if_idle()
    assert queue_control_state(store)["state"] == "stopping"
    assert not unrelated.blocks_admission()
    assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution,)).fetchone()["status"] == "RUNNING"
    store.close()
    other.close()


def test_failed_pipe_cleanup_is_bounded_and_keeps_execution_ownership(tmp_path, monkeypatch):
    from stagemesh import execution
    from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope

    project, store = _setup(tmp_path)
    control = QueueStopControl(store, TASK)
    request_queue_control(store, "stopping", "cleanup test", terminate_running=True)

    class UnreapableProvider:
        pid = 123
        args = ("test-provider",)

        def poll(self):
            return 0

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(self.args, timeout)

    monkeypatch.setattr(execution, "classify_process", lambda saved, current: "UNKNOWN")
    monkeypatch.setattr(execution, "popen_identity", lambda proc: None)
    monkeypatch.setattr(execution, "process_identity", lambda pid: None)
    with provider_stop_scope(control), pytest.raises(OperatorStopRequested) as caught:
        execution.communicate_bounded(UnreapableProvider(), "", 1)
    assert not caught.value.cleanup_complete

    class IncompleteExecutor:
        name = "test-incomplete"

        def run(self, store, task_id, claim_id, project):
            from stagemesh.domain import ExecutionKind
            from stagemesh.process_identity import owner_execution_fields
            store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION,
                                  **owner_execution_fields())
            raise OperatorStopRequested("cleanup incomplete", cleanup_complete=False)

    request_queue_control(store, "resumed", "test next run")
    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(store, project, Coordinator(store, project, executor=IncompleteExecutor()), TASK, summary, max_steps=1)
    assert summary.stop_reason == "STOP_CLEANUP_INCOMPLETE"
    assert list(store.running_executions())
    assert store.has_active_claim(TASK)
    from stagemesh.operator_actions import recover_stale
    assert [action.action for action in recover_stale(store, TASK)] == ["SKIPPED_UNKNOWN"]
    store.close()


def test_drain_stop_preserves_a_task_blocked_verdict(tmp_path):
    project, store = _setup(tmp_path)

    class BlockingCoordinator:
        def tick(self):
            request_queue_control(store, "stopping", "stop after stage")
            store.block_task(TASK)
            return 1

    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(store, project, BlockingCoordinator(), TASK, summary, max_steps=1)
    assert summary.stop_reason == "BLOCKED"
    store.close()


def test_cancellation_does_not_consume_the_failure_budget(tmp_path):
    from stagemesh.diagnosis import diagnose
    from stagemesh.queue_control import OperatorStopRequested

    project, store = _setup(tmp_path)

    class CancelledExecutor:
        name = "test-cancelled"

        def run(self, store, task_id, claim_id, project):
            raise OperatorStopRequested("operator requested stop")

    coordinator = Coordinator(store, project, executor=CancelledExecutor())
    for _ in range(2):
        with pytest.raises(OperatorStopRequested):
            coordinator.tick()
    assert diagnose(store, TASK, project) is None
    assert not store.has_active_claim(TASK)
    assert sum(e["event_type"] == "task.implementation_cancelled" for e in store.audit_events()) == 2
    store.close()


def test_monitor_failure_cancels_the_owned_provider_instead_of_accepting_output(tmp_path, monkeypatch):
    from stagemesh import execution
    from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope

    _project, store = _setup(tmp_path)
    control = QueueStopControl(store, TASK)
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **execution.popen_session_kwargs())

    def unavailable_monitor():
        raise OSError("disposable monitor failure")

    monkeypatch.setattr(control, "poll", unavailable_monitor)
    started = time.monotonic()
    try:
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested, match="monitoring failed"):
            execution.communicate_bounded(proc, "", 8)
        assert time.monotonic() - started < 5
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            execution.kill_process_tree(proc)
        proc.wait(timeout=5)
        store.close()


def test_incomplete_review_cleanup_retains_the_running_execution(tmp_path):
    from test_review_terminalization import TASK as review_task
    from test_review_terminalization import _project_with_candidate

    from stagemesh.domain import EvidenceKind
    from stagemesh.providers import RuntimeCommandAdapter
    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.review import Reviewer

    project, store, candidate = _project_with_candidate(tmp_path)

    class IncompleteReviewer(RuntimeCommandAdapter):
        def review_candidate(self, *args):
            raise OperatorStopRequested("cleanup incomplete", cleanup_complete=False)

    adapter = IncompleteReviewer("reviewer", (sys.executable, "-c", "pass"))
    with pytest.raises(OperatorStopRequested):
        Reviewer(adapter=adapter, require_independent=True).review(store, review_task, candidate, project)
    assert list(store.running_executions())
    from stagemesh.operator_actions import recover_stale
    assert [action.action for action in recover_stale(store, review_task)] == ["SKIPPED_UNKNOWN"]
    assert not store.conn.execute("SELECT 1 FROM evidence WHERE kind=?", (EvidenceKind.REVIEW,)).fetchall()
    store.close()


@pytest.mark.parametrize("monitor_fails", [False, True])
def test_unread_large_stdin_has_an_independent_cleanup_deadline(tmp_path, monkeypatch, monitor_fails):
    from stagemesh import execution
    from stagemesh.queue_control import OperatorStopRequested, QueueStopControl, provider_stop_scope

    _project, store = _setup(tmp_path)
    control = QueueStopControl(store, TASK)
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **execution.popen_session_kwargs())
    monkeypatch.setattr(execution, "classify_process", lambda saved, current: "UNKNOWN")
    monkeypatch.setattr(execution, "STOP_CLEANUP_SECONDS", 0.2)
    if monitor_fails:
        def failed_monitor():
            raise OSError("disposable monitor failure")
        monkeypatch.setattr(control, "poll", failed_monitor)
    started = time.monotonic()
    try:
        with provider_stop_scope(control), pytest.raises(OperatorStopRequested) as caught:
            execution.communicate_bounded(proc, "x" * (2 * 1024 * 1024), 0.05)
        assert not caught.value.cleanup_complete
        assert time.monotonic() - started < 2
        assert proc.poll() is None, "unverified provider identity must not be killed"
    finally:
        execution.kill_process_tree(proc)
        proc.wait(timeout=5)
        for worker in threading.enumerate():
            if worker.name == f"provider-io-{proc.pid}":
                worker.join(timeout=5)
                assert not worker.is_alive()
        store.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group isolation")
def test_provider_without_own_process_group_cannot_kill_the_runner_group(monkeypatch):
    from stagemesh import execution
    from stagemesh.parallel import _kill_pid

    killed = []

    class Provider:
        pid = 123

        def kill(self):
            killed.append(self.pid)

    monkeypatch.setattr(execution.os, "getpgid", lambda pid: 456)
    monkeypatch.setattr(execution.os, "killpg", lambda *args: pytest.fail("shared runner process group killed"))
    execution.kill_process_tree(Provider())
    assert killed == [123]
    monkeypatch.setattr(execution.os, "kill", lambda pid, signal: killed.append(pid))
    _kill_pid(123)
    assert killed == [123, 123]


@pytest.mark.parametrize("resume", [False, True])
def test_parallel_finalization_keeps_incomplete_claim_and_never_overwrites_resume(tmp_path, resume):
    from stagemesh.domain import ExecutionKind
    from stagemesh.parallel import ParallelRunner, TaskLifecycle, queue_control_state, worker_id_for

    project, store = _setup(tmp_path)
    runner = ParallelRunner(store, project, lambda *args: None, concurrency=1, sweep_worktrees=False)
    runner._threads[TASK] = threading.Thread()
    runner.summary.tasks.append(TaskLifecycle(TASK, summary=RunSummary(True, "STOP_CLEANUP_INCOMPLETE", task_id=TASK)))
    claim = store.acquire_claim(TASK, worker_id_for(TASK))
    execution = store.start_execution(task_id=TASK, claim_id=claim, kind=ExecutionKind.IMPLEMENTATION)
    runner.stop_admission("test stop", terminate_running=True)
    if resume:
        request_queue_control(store, "resumed", "future run")
    runner._finalize(runner.summary)
    assert store.has_active_claim(TASK)
    assert runner.summary.stop_reason == "GLOBAL_SAFETY_FAILURE"
    runner._record_stopped("cleanup not yet complete", terminate_running=True)
    assert queue_control_state(store)["state"] == ("running" if resume else "stopping")
    from stagemesh.domain import ExecutionStatus
    store.finish_execution(execution, ExecutionStatus.FAILED, result="test cleanup")
    store.release_claim(claim)
    runner._record_stopped("cleanup complete", terminate_running=True)
    assert queue_control_state(store)["state"] == ("running" if resume else "stopped")
    store.close()
