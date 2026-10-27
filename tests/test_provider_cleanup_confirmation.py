from threading import Event

import pytest

from stagemesh import execution
from stagemesh.domain import ProcessIdentity
from stagemesh.queue_control import OperatorStopRequested


@pytest.mark.parametrize("reason", ["timeout", "stop", "monitor"])
@pytest.mark.parametrize("kill_result", ["false", "raises", "confirmed"])
def test_root_exit_and_reaped_pipes_do_not_substitute_for_tree_cleanup(monkeypatch, reason, kill_result):
    released = Event()
    identity = ProcessIdentity(123, 1.0, "boot", "python.exe")

    class Process:
        pid = identity.pid
        exited = False

        def communicate(self, *, input):
            assert released.wait(timeout=5)
            return "partial output", ""

        def poll(self):
            return 0 if self.exited else None

    class Control:
        def poll(self):
            if reason == "monitor":
                raise OSError("fixture monitor failure")
            return {"terminate_running": True, "reason": "fixture stop", "rowid": 1}

        def note(self, *args, **kwargs):
            pass

    proc = Process()

    def kill(process):
        process.exited = True
        released.set()
        if kill_result == "raises":
            raise OSError("fixture termination failure")
        return kill_result == "confirmed"

    monkeypatch.setattr(execution, "popen_identity", lambda _: identity)
    monkeypatch.setattr(execution, "process_identity", lambda _: None if proc.exited else identity)
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    control = None if reason == "timeout" else Control()
    try:
        if kill_result == "confirmed" and reason == "timeout":
            assert execution._communicate_bounded(proc, "", 0, control) == ("partial output", "", True)
        else:
            with pytest.raises(OperatorStopRequested) as caught:
                execution._communicate_bounded(proc, "", 0 if reason == "timeout" else 60, control)
            assert caught.value.cleanup_complete is (kill_result == "confirmed")
            assert caught.value.monitor_failed is (reason == "monitor")
    finally:
        released.set()


def test_unconfirmed_timeout_retains_durable_implementation_ownership(tmp_path, monkeypatch):
    import sys

    from test_bounded_execution import TASK, _setup

    from stagemesh.coordinator import Coordinator, TargetSelection

    project, store = _setup(tmp_path)
    real_kill = execution.kill_process_tree

    def unconfirmed(process):
        real_kill(process)  # stop the actual disposable tree, then inject lost confirmation
        return False

    monkeypatch.setattr(execution, "kill_process_tree", unconfirmed)
    executor = execution.SubprocessExecutor(
        [sys.executable, "-c", "import time; time.sleep(120)"], name="stand-in", timeout_seconds=0.1,
    )
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            Coordinator(store, project, executor=executor, target=TargetSelection(TASK)).tick()
        assert not caught.value.cleanup_complete
        retained = list(store.running_executions())
        assert len(retained) == 1
        assert retained[0]["result"] == "cleanup_incomplete"
        assert retained[0]["process_create_time"] is None
        assert store.has_active_claim(TASK)
        assert store.latest_candidate(TASK) is None
    finally:
        store.close()
