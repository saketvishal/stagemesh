from threading import Event, Thread, current_thread

import pytest

from stagemesh import execution
from stagemesh.domain import ProcessIdentity
from stagemesh.queue_control import OperatorStopRequested


@pytest.mark.parametrize("first", ["external", "worker"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_worker_and_external_stop_share_one_tree_termination(monkeypatch, first, confirmed):
    reading, killing, release_tree, release_io, finished = (Event() for _ in range(5))
    identity = ProcessIdentity(123, 1.0, "boot")
    calls, errors, counts = [], [], []

    class Process:
        pid = 123
        exited = False

        def communicate(self, *, input):
            reading.set()
            assert release_io.wait(5)
            return "partial", ""

        def poll(self):
            return 0 if self.exited else None

    class Control:
        def poll(self):
            return {"terminate_running": True, "reason": "fixture stop", "rowid": 1} if first == "worker" or killing.is_set() else None

        def note(self, *args, **kwargs):
            pass

    proc = Process()

    def kill(process):
        calls.append(current_thread().name)
        if len(calls) > 1:
            raise OSError("fixture duplicate taskkill lost race")
        killing.set()
        assert release_tree.wait(5)
        process.exited = True
        release_io.set()
        return confirmed

    def communicate():
        try:
            execution.communicate_bounded(proc, "", 60)
        except BaseException as exc:  # noqa: BLE001 -- assert worker outcomes in the test thread
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(execution, "popen_identity", lambda _: identity)
    monkeypatch.setattr(execution, "process_identity", lambda _: None if proc.exited else identity)
    monkeypatch.setattr(execution, "current_provider_stop", Control)
    monkeypatch.setattr(execution, "kill_process_tree", kill)
    owner = Thread(target=communicate, name="fixture-owner")
    controller = Thread(target=lambda: counts.append(execution.kill_active_provider_processes()), name="fixture-controller")
    owner.start()
    try:
        assert reading.wait(5)
        if first == "worker":
            assert killing.wait(5)
        controller.start()
        assert killing.wait(5)
        assert not finished.wait(0.2), "a competing kill failure escaped before the shared tree result"
        release_tree.set()
        owner.join(5)
        controller.join(5)
        assert not owner.is_alive() and not controller.is_alive()
        assert len(calls) == 1
        assert len(errors) == 1 and isinstance(errors[0], OperatorStopRequested)
        assert errors[0].cleanup_complete is confirmed
        assert counts == [int(confirmed and first == "external")]
    finally:
        release_tree.set()
        release_io.set()
        owner.join(5)
        if controller.ident is not None:
            controller.join(5)
