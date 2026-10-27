from contextlib import contextmanager
from pathlib import Path

import pytest

from stagemesh import continuous_driver as driver
from stagemesh.persistence import Store


@pytest.mark.parametrize("state", ["paused", "stopped"])
@pytest.mark.parametrize("boundary", ["lock", "journal"])
def test_control_change_before_dispatch_prevents_new_window(tmp_path: Path, monkeypatch, state, boundary):
    driver.save_config(tmp_path, driver.DriverConfig(project=str(tmp_path)))
    store = Store(tmp_path / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    real_hold = driver.IntegrationLock.hold
    real_audit = driver.record_audit

    @contextmanager
    def hold(lock, owner):
        with real_hold(lock, owner):
            if boundary == "lock":
                driver.set_state(tmp_path, state)
            yield

    def audit(store, event, payload):
        real_audit(store, event, payload)
        if boundary == "journal" and event == driver.TICK_STARTED:
            driver.set_state(tmp_path, state)

    monkeypatch.setattr(driver.IntegrationLock, "hold", hold)
    monkeypatch.setattr(driver, "record_audit", audit)
    try:
        result = driver.run_tick(store, tmp_path, runner=lambda *_: pytest.fail("dispatched after control change"))
        assert result.state == state
        assert driver.interrupted_tick(store) is None
        assert not driver.child_owner_path(tmp_path).exists()
    finally:
        store.close()


def test_concurrent_control_publications_do_not_share_a_temporary_file(tmp_path: Path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    path = driver.config_path(tmp_path)
    replace = Path.replace
    publishers = Barrier(2)
    first_renamed = Event()
    publications = []

    def replace_together(temporary, target):
        if Path(target).name == path.name:
            first = not publications
            publications.append(temporary)
            publishers.wait(timeout=10)
            if first:
                try:
                    return replace(temporary, target)
                finally:
                    first_renamed.set()
            assert first_renamed.wait(timeout=10)
        return replace(temporary, target)

    monkeypatch.setattr(Path, "replace", replace_together)
    configs = [driver.DriverConfig(project=str(tmp_path), state=state) for state in ("paused", "stopped")]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [workers.submit(driver.save_config, tmp_path, config) for config in configs]
        for result in results:
            assert result.result(timeout=15) == path
    assert driver.load_config(tmp_path) in configs
    assert len(publications) == 2
    assert not list(path.parent.glob("*.tmp"))
