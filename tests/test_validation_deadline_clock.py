import json
from types import SimpleNamespace

import pytest

from stagemesh import survivor_wave
from stagemesh import validation_slot as slot
from stagemesh.queue_control import OperatorStopRequested


@pytest.fixture(autouse=True)
def isolated_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "slot.lock"))
    slot.set_window_deadline(None)
    yield
    slot.set_window_deadline(None)


def test_window_deadline_uses_the_clock_that_issued_it(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(slot.time, "time", lambda: 1000.0)
    slot.set_window_deadline(10.0, clock=lambda: now[0])
    assert not slot._deadline_exceeded()
    now[0] = 11.0
    assert slot._deadline_exceeded()
    slot.set_window_deadline(None)
    assert slot.current_window_deadline() is None


def test_subprocess_receives_remaining_allowance_in_its_wall_clock(tmp_path, monkeypatch):
    now = [5.0]
    monkeypatch.setattr(slot.time, "time", lambda: 1000.0)
    slot.set_window_deadline(10.0, clock=lambda: now[0])
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"]["STAGEMESH_WINDOW_DEADLINE"])
        return SimpleNamespace(stdout=json.dumps({"stop_reason": "DONE"}), returncode=0)

    monkeypatch.setattr(survivor_wave.subprocess, "run", run)
    runner = survivor_wave.subprocess_task_runner(tmp_path)
    assert runner("task", 1).stop_reason == "DONE"
    assert float(seen[-1]) == 1005.0
    now[0] = 11.0
    runner("task", 1)
    assert float(seen[-1]) < 1000.0


def test_environment_deadline_remains_an_absolute_wall_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(slot.time, "time", lambda: now[0])
    monkeypatch.setenv("STAGEMESH_WINDOW_DEADLINE", "1005")
    assert not slot._deadline_exceeded()
    now[0] = 1006.0
    assert slot._deadline_exceeded()


def test_window_clock_expiring_while_waiting_never_acquires_the_slot(monkeypatch):
    now = [0.0]
    slot.set_window_deadline(10.0, clock=lambda: now[0])

    def unavailable(_handle):
        now[0] = 11.0
        return False

    monkeypatch.setattr(slot, "_try_file_lock", unavailable)
    with pytest.raises(OperatorStopRequested, match="deadline"), slot.validation_slot():
        pytest.fail("expired window acquired the validation slot")
