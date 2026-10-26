from __future__ import annotations

from pathlib import Path

from test_completion_integrity import TASK, FakeReviewAdapter, Rig

from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store
from stagemesh.scheduling import Scheduler
from stagemesh.task_sources import DiscoveredTask, sync_source


def _targeted(rig: Rig) -> Coordinator:
    rig.coordinator.target = TargetSelection(TASK)
    return rig.coordinator


def _runs(rig: Rig) -> int:
    return rig.log.read_text().count("run") if rig.log.exists() else 0


def test_sync_and_targeted_continue_do_not_reacquire_implement_after_validation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeReviewAdapter("claude"), integration_ref="refs/heads/integration")
    rig.tick(2)  # implement, validate
    assert rig.stage == Stage.REVIEW
    assert rig.evidence(EvidenceKind.VALIDATION)[0][0] == EvidenceStatus.PASSED
    candidate = rig.store.latest_candidate(TASK)["sha"]

    for _ in range(2):  # repeated source sync of the still-OPEN external task
        sync_source(rig.store, [DiscoveredTask("local", TASK, "completion renamed")])
        task = rig.store.get_task(TASK)
        assert task["stage"] == Stage.REVIEW and task["status"] == "OPEN"
        assert task["title"] == "completion renamed"

    coordinator = _targeted(rig)
    coordinator.tick()  # targeted single step: review, not implementation
    assert _runs(rig) == 1
    assert rig.stage != Stage.IMPLEMENT
    assert rig.store.latest_candidate(TASK)["sha"] == candidate
    assert len(rig.store.conn.execute("SELECT 1 FROM candidates WHERE task_id=?", (TASK,)).fetchall()) == 1
    assert rig.store.conn.execute("SELECT 1 FROM claims WHERE task_id=? AND active=1", (TASK,)).fetchone() is None


def test_failed_independent_review_is_the_only_path_back_to_implement(tmp_path: Path) -> None:
    adapter = FakeReviewAdapter("claude", '{"decision":"FAIL","findings":[{"severity":"error","message":"bad"}]}')
    rig = Rig(tmp_path, adapter, integration_ref="refs/heads/integration")
    rig.tick(3)  # implement, validate, review (fails)
    assert rig.evidence(EvidenceKind.REVIEW)[0][0] == EvidenceStatus.FAILED
    assert rig.stage == Stage.IMPLEMENT
    # history is preserved: candidate, passing validation and failed review evidence remain
    assert rig.evidence(EvidenceKind.VALIDATION)[0][0] == EvidenceStatus.PASSED
    assert rig.store.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] >= 1


def test_background_sync_preserves_registered_prerequisites_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "stagemesh.sqlite3"
    declarations = [DiscoveredTask("local", "1", "prerequisite"), DiscoveredTask("local", "2", "dependent")]
    store = Store(path)
    store.migrate()
    sync_source(store, declarations)
    store.add_dependency("2", "1")
    sync_source(store, declarations, preserve_existing_dependencies=True)
    store.close()

    reopened = Store(path)
    try:
        sync_source(reopened, declarations, preserve_existing_dependencies=True)
        assert reopened.incomplete_dependencies("2") == ["1"]
        assert not Scheduler(reopened).decision("2").eligible
        # Explicit source reconciliation retains its existing ability to remove obsolete declarations.
        sync_source(reopened, declarations)
        assert reopened.incomplete_dependencies("2") == []
        assert Scheduler(reopened).decision("2").eligible
    finally:
        reopened.close()


def test_background_sync_adds_new_dependencies_without_replacing_existing_ones(tmp_path: Path) -> None:
    store = Store(tmp_path / "stagemesh.sqlite3")
    try:
        store.migrate()
        sync_source(store, [DiscoveredTask("local", name, name) for name in ("1", "2", "3")])
        store.add_dependency("3", "1")
        sync_source(store, [DiscoveredTask("local", "3", "dependent", dependencies=("2",))],
                    preserve_existing_dependencies=True)
        assert set(store.incomplete_dependencies("3")) == {"1", "2"}
    finally:
        store.close()
