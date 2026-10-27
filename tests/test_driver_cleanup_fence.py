import json
import subprocess
from pathlib import Path

import pytest

from stagemesh import continuous_driver as driver
from stagemesh.domain import ProcessIdentity
from stagemesh.persistence import Store


@pytest.mark.parametrize("failure", ["kill_false", "kill_raises", "wait_timeout"])
@pytest.mark.parametrize("owner_transition", ["republished", "removed"])
def test_uncertain_parent_cleanup_blocks_recovery_after_recorded_roots_die(
    tmp_path: Path, monkeypatch, failure, owner_transition,
):
    driver.save_config(tmp_path, driver.DriverConfig(project=str(tmp_path)))
    wrapper = ProcessIdentity(111, 1.0, "boot", "python.exe")
    child = ProcessIdentity(222, 2.0, "boot", "python.exe")
    roots_dead = False

    class Launched:
        calls = 0

        def communicate(self, *, timeout):
            self.calls += 1
            if self.calls == 1 or failure == "wait_timeout":
                raise subprocess.TimeoutExpired("fixture", timeout)
            return "", ""

    def launch(*args, **kwargs):
        driver.save_child_owner(tmp_path, wrapper, child)
        return Launched()

    def kill(_process):
        nonlocal roots_dead
        # The wrapper may publish again or remove its record concurrently with parent cleanup.
        driver.save_child_owner(tmp_path, wrapper, child)
        if owner_transition == "removed":
            driver.child_owner_path(tmp_path).unlink()
        roots_dead = True
        if failure == "kill_raises":
            raise OSError("fixture kill failure")
        return failure != "kill_false"

    monkeypatch.setattr(driver.subprocess, "Popen", launch)
    monkeypatch.setattr(driver, "popen_identity", lambda _: wrapper)
    monkeypatch.setattr(driver, "process_identity", lambda pid: None if roots_dead else {111: wrapper, 222: child}.get(pid))
    monkeypatch.setattr(driver, "kill_process_tree", kill)
    if failure == "kill_raises":
        with pytest.raises(OSError, match="fixture kill failure"):
            driver._default_runner(["unused"], tmp_path, 1)
    else:
        assert driver._default_runner(["unused"], tmp_path, 1) == 124

    assert not driver.child_owner_fully_dead(tmp_path)
    assert not driver.clear_dead_child_owner(tmp_path)
    store = Store(tmp_path / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    try:
        tick = driver.run_tick(store, tmp_path, runner=lambda *_: pytest.fail("dispatched with uncertain descendants"))
        assert tick.state == "blocked"
        assert driver.status(store, tmp_path)["active_window_child"] == "ORPHANED"
        assert driver.release_orphaned_child(tmp_path)
        assert driver.child_owner_state(tmp_path) == (None, None)

        def idle(argv, *_):
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps({"tasks": []}), encoding="utf-8")
            return 0

        assert driver.run_tick(store, tmp_path, runner=idle).state == "idle"
    finally:
        store.close()


def test_cleanup_fence_keeps_richer_live_child_identity_when_owner_is_republished(tmp_path: Path, monkeypatch):
    wrapper = ProcessIdentity(111, 1.0, "boot", "python.exe")
    child = ProcessIdentity(222, 2.0, "boot", "python.exe")
    driver.save_child_owner(tmp_path, wrapper, child)
    driver.begin_child_cleanup(tmp_path, wrapper)
    driver.save_child_owner(tmp_path, wrapper)
    monkeypatch.setattr(driver, "process_identity", lambda pid: child if pid == child.pid else None)
    assert driver.child_owner_state(tmp_path)[0] == "LIVE"
    with pytest.raises(driver.DriverConfigError, match="LIVE"):
        driver.release_orphaned_child(tmp_path)
    assert driver.cleanup_pending_path(tmp_path).exists()


def test_confirmed_parent_cleanup_removes_fence_only_after_wait(tmp_path: Path, monkeypatch):
    wrapper = ProcessIdentity(111, 1.0, "boot", "python.exe")
    child = ProcessIdentity(222, 2.0, "boot", "python.exe")

    class Launched:
        calls = 0

        def communicate(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            assert driver.cleanup_pending_path(tmp_path).exists()
            return "", ""

    def launch(*args, **kwargs):
        driver.save_child_owner(tmp_path, wrapper, child)
        return Launched()

    monkeypatch.setattr(driver.subprocess, "Popen", launch)
    monkeypatch.setattr(driver, "popen_identity", lambda _: wrapper)
    monkeypatch.setattr(driver, "process_identity", lambda _: None)
    monkeypatch.setattr(driver, "kill_process_tree", lambda _: True)
    assert driver._default_runner(["unused"], tmp_path, 1) == 124
    assert not driver.cleanup_pending_path(tmp_path).exists()
    assert driver.child_owner_state(tmp_path) == (None, None)


def test_wrapper_still_attempts_cleanup_when_owner_republication_fails(tmp_path: Path, monkeypatch):
    from stagemesh import driver_child

    wrapper = ProcessIdentity(111, 1.0, "boot", "python.exe")
    child = ProcessIdentity(222, 2.0, "boot", "python.exe")
    killed = []
    save = driver.save_child_owner

    class Interrupted:
        pid = 222
        calls = 0

        def wait(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return -1

    def publish(project, identity, nested=None):
        if identity == child:
            raise OSError("fixture owner publication failure")
        return save(project, identity, nested)

    monkeypatch.setattr(driver_child, "owner_identity", lambda: wrapper)
    monkeypatch.setattr(driver_child, "process_identity", lambda _: child)
    monkeypatch.setattr(driver_child.subprocess, "Popen", lambda *a, **kw: Interrupted())
    monkeypatch.setattr(driver_child, "save_child_owner", publish)
    monkeypatch.setattr(driver_child, "kill_process_tree", lambda proc: killed.append(proc) or False)
    monkeypatch.setattr(driver, "process_identity", lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        driver_child.main([str(tmp_path), "unused"])
    assert killed
    assert driver.cleanup_pending_path(tmp_path).is_file()
    assert not driver.child_owner_fully_dead(tmp_path)


@pytest.mark.parametrize("record_path", [driver.child_owner_path, driver.cleanup_pending_path])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("field,value", [
    ("pid", "invalid"), ("pid", True), ("pid", 0), ("pid", -1), ("pid", 10**400),
    ("create_time", "invalid"), ("create_time", None), ("create_time", float("nan")), ("create_time", 10**400),
    ("boot_id", None), ("boot_id", 42), ("executable", 42),
])
def test_malformed_recorded_identity_is_unknown_and_never_released(tmp_path: Path, record_path, nested, field, value):
    path = record_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    malformed = {"pid": 999999999, "create_time": 1.0, "boot_id": "boot", field: value}
    payload = {"pid": 999999998, "create_time": 1.0, "boot_id": "boot", "child": malformed} if nested else malformed
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert driver.child_owner_state(tmp_path)[0] == "UNKNOWN"
    assert not driver.clear_dead_child_owner(tmp_path)
    with pytest.raises(driver.DriverConfigError, match="UNKNOWN"):
        driver.release_orphaned_child(tmp_path)
