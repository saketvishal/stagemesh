from __future__ import annotations

from pathlib import Path

from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.task_sources import DiscoveredTask, sync_source

from test_completion_integrity import TASK, FakeReviewAdapter, Rig


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
