"""Scenario 6: MAX_STEPS resumes from the persisted stage without replaying work, and unproductive caps end as one typed blocker."""

from __future__ import annotations

import json
from pathlib import Path

from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.run_ready import RunSummary, drive_task
from stagemesh.step_progress import (
    STEP_CAP_STALL_LIMIT,
    made_progress,
    progress_state,
    stalled,
    unproductive_step_caps,
)

from test_bounded_execution import TASK, _setup

SHA = ("a" * 39)


class Scripted:
    """A coordinator whose ticks are scripted state changes; counts how often implementation work is (re)done."""

    def __init__(self, store, script):
        self.store, self.script, self.implementations = store, list(script), 0
        self.diagnosis_policy = type("P", (), {"repeat_threshold": 2})()

    def tick(self) -> int:
        action = self.script.pop(0) if self.script else "idle"
        if action == "implement":
            self.implementations += 1
            sha = f"{SHA}{self.implementations}"
            self.store.add_candidate(TASK, sha, "p", durable_handoff=True)
            self.store.advance_task(TASK, Stage.VALIDATE)
        elif action == "validate":
            sha = str(self.store.latest_candidate(TASK)["sha"])
            self.store.add_evidence(TASK, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {})
            self.store.advance_task(TASK, Stage.REVIEW)
        elif action == "review":
            sha = str(self.store.latest_candidate(TASK)["sha"])
            self.store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED, {})
            self.store.advance_task(TASK, Stage.INTEGRATE)
        elif action == "fail-review":  # same failing loop: REVIEW -> IMPLEMENT -> VALIDATE -> REVIEW on an equivalent finding
            self.implementations += 1
            sha = f"{SHA}{self.implementations}"
            self.store.add_candidate(TASK, sha, "p", durable_handoff=True)
            self.store.add_evidence(TASK, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "review", "message": "the same concern at line 41"}]})
            self.store.advance_task(TASK, Stage.REVIEW)
        return 1


def _run(store, project: Path, script: list[str], steps: int) -> tuple[RunSummary, Scripted]:
    coordinator = Scripted(store, script)
    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(store, project, coordinator, TASK, summary, max_steps=steps, global_health=False)
    return summary, coordinator


def _events(store, event_type: str) -> list[dict]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,))]


def test_a_capped_run_with_progress_resumes_from_its_stage_without_replaying_implementation(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)

    first, coordinator = _run(store, project, ["implement", "validate"], steps=2)
    assert first.stop_reason == "MAX_STEPS" and store.get_task(TASK)["stage"] == Stage.REVIEW
    assert first.detail["step_cap"]["progressed"] and "advanced from" in first.detail["step_cap"]["reason"]
    assert coordinator.implementations == 1

    second, resumed = _run(store, project, ["review"], steps=1)  # a new window: the same task, the persisted state
    assert second.stop_reason == "MAX_STEPS" and store.get_task(TASK)["stage"] == Stage.INTEGRATE
    assert resumed.implementations == 0  # nothing already completed was done again
    assert unproductive_step_caps(store, TASK) == 0 and store.get_task(TASK)["status"] == TaskStatus.OPEN
    assert [e["progressed"] for e in _events(store, "task.step_cap")] == [True, True]


def test_repeated_unproductive_caps_end_as_one_typed_blocker_across_windows(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    store.advance_task(TASK, Stage.REVIEW)
    store.add_candidate(TASK, SHA + "0", "p", durable_handoff=True)  # the loop is already under way: a review failure exists
    store.add_evidence(TASK, SHA + "0", EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "review", "message": "the same concern at line 3"}]})

    first, _ = _run(store, project, ["fail-review", "fail-review"], steps=2)
    assert first.stop_reason == "MAX_STEPS" and store.get_task(TASK)["status"] == TaskStatus.OPEN  # one capped run is still resumable
    assert not first.detail["step_cap"]["progressed"] and unproductive_step_caps(store, TASK) == 1 < STEP_CAP_STALL_LIMIT

    second, _ = _run(store, project, ["fail-review", "fail-review"], steps=2)  # re-admitted by the next window

    assert second.stop_reason == "BLOCKED" and "step_cap_no_progress" in second.message
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED and stalled(store, TASK)
    block, = _events(store, "task.block_classified")
    assert block["kind"] == "BLOCKED_RECOVERABLE_OPERATIONAL" and block["failure_code"] == "step_cap_no_progress"
    assert block["unproductive_step_caps"] == 2 and "stagemesh diagnose" in block["reason"]


def test_activity_without_a_resolved_cause_is_not_progress(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    store.advance_task(TASK, Stage.REVIEW)
    store.add_candidate(TASK, SHA + "0", "p", durable_handoff=True)
    before = progress_state(store, TASK)
    store.add_evidence(TASK, SHA + "0", EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "review", "message": "concern at line 3"}]})
    store.add_evidence(TASK, SHA + "0", EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {})  # more evidence rows are only activity
    assert made_progress(before, progress_state(store, TASK))[0] is False

    mid = progress_state(store, TASK)
    store.add_candidate(TASK, SHA + "1", "p", durable_handoff=True)
    store.add_evidence(TASK, SHA + "1", EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "review", "message": "concern at line 77"}]})
    assert made_progress(mid, progress_state(store, TASK)) == (False, "a new candidate failed the same way as before")

    store.add_candidate(TASK, SHA + "2", "p", durable_handoff=True)
    store.add_evidence(TASK, SHA + "2", EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "other", "message": "a different problem"}]})
    assert made_progress(mid, progress_state(store, TASK))[0] is True


def test_an_operator_fresh_start_ends_the_streak(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    store.advance_task(TASK, Stage.REVIEW)
    store.add_candidate(TASK, SHA + "0", "p", durable_handoff=True)
    store.add_evidence(TASK, SHA + "0", EvidenceKind.REVIEW, EvidenceStatus.FAILED, {"findings": [{"code": "review", "message": "the same concern at line 3"}]})
    _run(store, project, ["fail-review"], steps=1)
    assert unproductive_step_caps(store, TASK) == 1
    from stagemesh.audit import record_audit

    record_audit(store, "task.unblocked", {"task_id": TASK})
    assert unproductive_step_caps(store, TASK) == 0
