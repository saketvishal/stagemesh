import pytest
from test_github_task_sources import _store

from stagemesh.persistence import Store
from stagemesh.scheduling import Scheduler
from stagemesh.task_sources import DiscoveredTask, sync_source


@pytest.mark.parametrize("source,retired", [("github", False), ("github", True), ("local-json", False)])
def test_observer_cannot_admit_a_source_task_before_dependencies_or_retirement(tmp_path, monkeypatch, source, retired):
    store = _store(tmp_path)
    observer = Store(store.db_path)
    store.upsert_task("unfinished prerequisite", source=source, source_id="248")
    upsert = store.upsert_task
    observed = []

    def paused_upsert(*args, **kwargs):
        task_id = upsert(*args, **kwargs)
        if task_id == "249":
            decision = Scheduler(observer).decision(task_id)
            observed.append(decision)
            assert not decision.eligible, "source sync published a runnable task before its prerequisite/retirement"
            assert observer.acquire_claim(task_id, "concurrent-observer") is None
        return task_id

    monkeypatch.setattr(store, "upsert_task", paused_upsert)
    try:
        sync_source(store, [DiscoveredTask(
            source, "249", "dependent work", dependencies=("248",),
            labels=("stagemesh:blocked",) if retired else ("stagemesh:ready",),
            eligible=not retired,
        )])
        assert observed
        assert not Scheduler(observer).decision("249").eligible
        assert not observer.has_active_claim("249")
    finally:
        observer.close()
        store.close()


@pytest.mark.parametrize("marker", [False, None, {}, "unknown"])
def test_unrecognized_pending_markers_refuse_stale_admission_without_releasing_claims(tmp_path, marker):
    store = _store(tmp_path)
    observer = Store(store.db_path)
    task_id = store.upsert_task("work", source="github", source_id="249")
    assert Scheduler(observer).decision(task_id).eligible  # a scheduler decision captured before sync starts
    claim = store.acquire_claim(task_id, "original-worker")
    store.conn.execute("UPDATE claims SET lease_expires_at=0 WHERE id=?", (claim,))
    store.conn.commit()
    store.cache_source("github", "249", {"state": "OPEN", "eligible": True, "admission_pending": marker}, "OPEN")
    try:
        assert Scheduler(observer).decision(task_id).reason == "source synchronization incomplete"
        assert observer.acquire_claim(task_id, "stale-observer") is None
        assert store.conn.execute("SELECT active FROM claims WHERE id=?", (claim,)).fetchone()[0] == 1
    finally:
        observer.close()
        store.close()


def test_failed_sync_and_unrelated_scan_cannot_publish_partial_dependencies(tmp_path, monkeypatch):
    from stagemesh.domain import Stage

    store = _store(tmp_path)
    observer = Store(store.db_path)
    store.upsert_task("prerequisite", source="github", source_id="248")
    tasks = [DiscoveredTask("github", "249", "dependent", dependencies=("248",))]
    add_dependency = store.add_dependency

    def fail(*args):
        raise OSError("fixture interrupted during dependency publication")

    monkeypatch.setattr(store, "add_dependency", fail)
    try:
        with pytest.raises(OSError, match="fixture interrupted"):
            sync_source(store, tasks)
        assert Scheduler(observer).decision("249").reason == "source synchronization incomplete"
        assert observer.acquire_claim("249", "observer") is None
        sync_source(store, [])
        sync_source(store, [DiscoveredTask("other-source", "unrelated", "unrelated")])
        assert ("admission_pending" in store.source_state("github", "249"))
        assert observer.acquire_claim("249", "observer") is None
        monkeypatch.setattr(store, "add_dependency", add_dependency)
        sync_source(store, tasks)
        assert not ("admission_pending" in store.source_state("github", "249"))
        assert Scheduler(observer).decision("249").reason == "waiting for 248"
        store.advance_task("248", Stage.DONE)
        assert Scheduler(observer).decision("249").eligible
        assert observer.acquire_claim("249", "observer")
    finally:
        observer.close()
        store.close()


def test_real_parent_exit_leaves_admission_fenced_and_releases_sync_lock(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import stagemesh

    store = _store(tmp_path)
    store.upsert_task("prerequisite", source="github", source_id="248")
    script = """
import os, sys
from pathlib import Path
from stagemesh.persistence import Store
from stagemesh.task_sources import DiscoveredTask, sync_source
store = Store(Path(sys.argv[1]))
original = store.upsert_task
def interrupted(*args, **kwargs):
    result = original(*args, **kwargs)
    if result == '249':
        os._exit(73)
    return result
store.upsert_task = interrupted
sync_source(store, [DiscoveredTask('github', '249', 'dependent', dependencies=('248',))])
"""
    env = {**os.environ, "PYTHONPATH": str(Path(stagemesh.__file__).parent.parent)}
    try:
        child = subprocess.run([sys.executable, "-c", script, str(store.db_path)], env=env, capture_output=True, text=True, timeout=15, check=False)
        assert child.returncode == 73, child.stderr
        assert Scheduler(store).decision("249").reason == "source synchronization incomplete"
        assert store.acquire_claim("249", "observer") is None
        sync_source(store, [DiscoveredTask("github", "249", "dependent", dependencies=("248",))])
        assert not ("admission_pending" in store.source_state("github", "249"))
        assert Scheduler(store).decision("249").reason == "waiting for 248"
    finally:
        store.close()


def test_concurrent_source_writers_cannot_publish_another_writers_partial_state(tmp_path):
    from threading import Event, Thread

    store = _store(tmp_path)
    for source_id in ("248", "250"):
        store.upsert_task("prerequisite", source="github", source_id=source_id)
    first_pending, release_first, second_started, second_mutation = (Event() for _ in range(4))
    errors = []

    def writer(dependency):
        worker = Store(store.db_path)
        original = worker.upsert_task

        def upsert(*args, **kwargs):
            result = original(*args, **kwargs)
            if result == "249":
                if dependency == "248":
                    first_pending.set()
                    assert release_first.wait(5)
                else:
                    second_mutation.set()
            return result

        worker.upsert_task = upsert
        try:
            if dependency == "250":
                second_started.set()
            sync_source(worker, [DiscoveredTask("github", "249", "dependent", dependencies=(dependency,))])
        except BaseException as exc:  # noqa: BLE001 -- marshal writer failures to the test thread
            errors.append(exc)
        finally:
            worker.close()

    first, second = Thread(target=writer, args=("248",)), Thread(target=writer, args=("250",))
    first.start()
    try:
        assert first_pending.wait(5)
        second.start()
        assert second_started.wait(5)
        assert not second_mutation.wait(0.2), "source writer bypassed database-scoped publication lock"
        assert Scheduler(store).decision("249").reason == "source synchronization incomplete"
        assert store.acquire_claim("249", "observer") is None
        release_first.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive() and not errors
        assert second_mutation.is_set()
        assert not ("admission_pending" in store.source_state("github", "249"))
        assert store.incomplete_dependencies("249") == ["250"]
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        store.close()


def test_memory_source_sync_does_not_create_an_invalid_lock_filename():
    from pathlib import Path

    store = Store(Path(":memory:"))
    store.migrate()
    try:
        assert sync_source(store, [DiscoveredTask("github", "249", "work")]) == ["249"]
        assert Scheduler(store).decision("249").eligible
        assert not ("admission_pending" in store.source_state("github", "249"))
    finally:
        store.close()


@pytest.mark.parametrize("change", ["dependency", "retirement", "objective"])
def test_stale_decision_cannot_claim_after_changed_source_state_is_fully_published(tmp_path, change):
    store = _store(tmp_path)
    observer = Store(store.db_path)
    store.upsert_task("prerequisite", source="github", source_id="248")
    store.upsert_task("previously runnable", source="github", source_id="249")
    assert Scheduler(observer).decision("249").eligible
    task = DiscoveredTask(
        "github", "249", "updated work", dependencies=("248",) if change == "dependency" else (),
        eligible=change != "retirement",
        labels=("stagemesh:objective",) if change == "objective" else (),
    )
    try:
        sync_source(store, [task])
        assert not ("admission_pending" in store.source_state("github", "249"))  # publication finished, so pending alone is insufficient
        assert not Scheduler(observer).decision("249").eligible
        assert observer.acquire_claim("249", "stale-worker") is None
        assert not observer.has_active_claim("249")
    finally:
        observer.close()
        store.close()


def test_restoring_a_source_retired_task_keeps_admission_fenced_until_dependencies_finish(tmp_path, monkeypatch):
    store = _store(tmp_path)
    observer = Store(store.db_path)
    store.upsert_task("prerequisite", source="github", source_id="248")
    store.upsert_task("previously retired", source="github", source_id="249")
    sync_source(store, [DiscoveredTask("github", "249", "closed", eligible=False, state="CLOSED")])
    restore = store.restore_source_task
    observed = []

    def paused_restore(*args, **kwargs):
        result = restore(*args, **kwargs)
        observed.append(Scheduler(observer).decision("249"))
        assert store.get_task("249")["status"] == "OPEN"
        assert not observed[-1].eligible
        assert observer.acquire_claim("249", "stale-worker") is None
        return result

    monkeypatch.setattr(store, "restore_source_task", paused_restore)
    try:
        sync_source(store, [DiscoveredTask("github", "249", "restored", dependencies=("248",))])
        assert observed and not ("admission_pending" in store.source_state("github", "249"))
        assert Scheduler(observer).decision("249").reason == "waiting for 248"
    finally:
        observer.close()
        store.close()
