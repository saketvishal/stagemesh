"""Run-window behaviour for the real failure classes: recoverable blocks are reconsidered, step-capped tasks resume from their state."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_run_window import (  # noqa: F401 - FRESH keeps the autouse runtime probe fixture's import path stable
    FRESH,
    _no_real_runtime_probe,
    go,
    window,
)
from test_survivor_wave import make_store

import stagemesh.run_window as rw
from stagemesh.audit import record_audit
from stagemesh.block_kinds import (
    MAX_REENTRIES,
    BlockKind,
    classify_block,
    record_block,
    reenter_blocked_task,
    reentries_used,
    reentry_refusal,
)
from stagemesh.domain import EvidenceKind, EvidenceStatus, TaskStatus
from stagemesh.survivor_wave import RunResult


def block_legacy(store, task_id: str, event: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
    """A block as the shipped code wrote it before kinds existed: the task is BLOCKED and one audit event says why."""
    store.block_task(task_id)
    record_audit(store, event, {"task_id": task_id, **payload})


# --- classification of the blocks that are in the ledger today (tasks 143, 172, 173) -----------------------------------------------


def test_the_real_integration_blocks_are_recoverable_operational(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["143", "172"])
    for task_id, sha in (("143", "b4026b617504"), ("172", "f9e79959fb7d")):  # as recorded in the Caventra ledger
        block_legacy(store, task_id, "task.remediation_exhausted", {"candidate_sha": sha, "stage": "INTEGRATE", "reason": "integration_ref_state"})
        info = classify_block(store, task_id)
        assert info is not None and info.kind is BlockKind.RECOVERABLE_OPERATIONAL and info.source == "legacy:task.remediation_exhausted"


def test_a_review_budget_block_without_a_stated_decision_is_recoverable_not_a_founder_decision(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["173"])
    block_legacy(store, "173", "task.remediation_exhausted", {"candidate_sha": "c" * 40, "stage": "REVIEW"})  # task 173
    assert classify_block(store, "173").kind is BlockKind.RECOVERABLE_OPERATIONAL


def test_safety_and_scope_blocks_are_not_recoverable(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["S", "C", "U"])
    block_legacy(store, "S", "task.blocked", {"reason": "external_workspace_mutation", "stage": "REVIEW"})
    block_legacy(store, "C", "task.diagnosis_stop", {"category": "contract_scope"})
    store.block_task("U")  # blocked by hand, no recorded cause
    assert classify_block(store, "S").kind is BlockKind.POLICY_SAFETY
    assert classify_block(store, "C").kind is BlockKind.HUMAN_DECISION
    assert classify_block(store, "U") is None


# --- re-entry ------------------------------------------------------------------------------------------------------------------------


def test_only_recoverable_operational_blocks_are_reconsidered_and_only_after_a_runtime_change(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["R", "H", "P", "X"])
    record_block(store, "R", BlockKind.RECOVERABLE_OPERATIONAL, "budget spent", runtime="old")
    record_block(store, "H", BlockKind.HUMAN_DECISION, "which side wins in shared.txt?", runtime="old")
    record_block(store, "P", BlockKind.POLICY_SAFETY, "workspace mutated", runtime="old")
    record_block(store, "X", BlockKind.EXTERNAL_DEPENDENCY, "integration branch missing", runtime="old")

    assert reentry_refusal(store, "R", "old") == "the runtime and configuration are unchanged since the last attempt; waiting for a change"
    assert reentry_refusal(store, "R", "new") is None
    for task_id, kind in (("H", "BLOCKED_HUMAN_DECISION"), ("P", "BLOCKED_POLICY_SAFETY"), ("X", "BLOCKED_EXTERNAL_DEPENDENCY")):
        assert reentry_refusal(store, task_id, "new").startswith(kind)
        assert reenter_blocked_task(store, task_id, "new") is None and store.get_task(task_id)["status"] == TaskStatus.BLOCKED


def test_reentry_returns_the_task_to_open_keeps_its_history_and_is_bounded(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["R"])
    store.add_candidate("R", "d" * 40, "codex", True)
    store.add_evidence("R", "d" * 40, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
    for number in range(1, MAX_REENTRIES + 1):
        record_block(store, "R", BlockKind.RECOVERABLE_OPERATIONAL, "budget spent", runtime=f"fp{number - 1}")
        entry = reenter_blocked_task(store, "R", f"fp{number}")
        assert entry["attempt"] == number and entry["of"] == MAX_REENTRIES and store.get_task("R")["status"] == TaskStatus.OPEN
        assert reentries_used(store, "R") == number
    record_block(store, "R", BlockKind.RECOVERABLE_OPERATIONAL, "budget spent", runtime="fp-last")
    assert "automatic re-entries are spent" in reentry_refusal(store, "R", "fp-new")
    assert store.latest_candidate("R")["sha"] == "d" * 40  # candidates and evidence are never discarded by a re-entry
    assert store.unblock_task("R")  # an operator's retry-task grants a fresh budget
    assert reentries_used(store, "R") == 0


def test_a_recorded_block_with_the_same_runtime_as_the_last_attempt_waits(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path, ["R"])
    record_block(store, "R", BlockKind.RECOVERABLE_OPERATIONAL, "budget spent", runtime="fp1")
    assert reenter_blocked_task(store, "R", "fp2") is not None
    record_block(store, "R", BlockKind.RECOVERABLE_OPERATIONAL, "failed again with fp2 code", runtime="fp2")
    assert reentry_refusal(store, "R", "fp2") is not None  # it already ran on this runtime: no loop without a change


def test_run_window_reconsiders_a_recoverable_block_and_explains_every_block_it_leaves_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw, "runtime_fingerprint", lambda project: "fp-new")

    def hooks_edit(hooks, rig, store):  # type: ignore[no-untyped-def]
        block_legacy(store, "143", "task.remediation_exhausted", {"candidate_sha": "b" * 40, "stage": "INTEGRATE", "reason": "integration_ref_state"})
        record_block(store, "7", BlockKind.HUMAN_DECISION, "Which behaviour wins in shared.txt: the candidate's or main's?", runtime="fp-new")

    report, _store, rig, _ = go(tmp_path, ["143", "7"], hooks_edit=hooks_edit)

    assert rig.runs == ["143"]  # the recoverable block ran again without `retry-task`
    (entry,) = report.reentered
    assert entry["task_id"] == "143" and entry["attempt"] == 1 and "integration_ref_state" in entry["was_blocked_because"]
    assert {t.task_id: t.outcome for t in report.tasks} == {"143": "done"}
    assert report.not_selected["7"].startswith("BLOCKED_HUMAN_DECISION: Which behaviour wins in shared.txt")  # a concrete question
    assert "reconsidered blocked task 143" in rw.format_window_report(report)


def test_run_window_leaves_a_recoverable_block_alone_when_nothing_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw, "runtime_fingerprint", lambda project: "fp-same")

    def hooks_edit(hooks, rig, store):  # type: ignore[no-untyped-def]
        record_block(store, "143", BlockKind.RECOVERABLE_OPERATIONAL, "integration budget spent", runtime="fp-same")

    report, _store, rig, _ = go(tmp_path, ["143"], hooks_edit=hooks_edit)

    assert rig.runs == [] and not report.reentered
    assert "waiting for a change" in report.not_selected["143"]


# --- MAX_STEPS (task 148) ------------------------------------------------------------------------------------------------------------


def progress(rig, task_id: str) -> None:  # type: ignore[no-untyped-def]
    """What a capped run leaves behind: a validated candidate that has not been reviewed yet."""
    store = rig.worker_store()
    try:
        sha = (task_id + "e" * 40)[:40]
        store.add_candidate(task_id, sha, "codex", True)
        store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
    finally:
        store.close()


def test_a_task_that_hits_the_step_cap_with_progress_is_resumed_from_its_state_and_finishes(tmp_path: Path) -> None:
    def behaviour(rig, task_id: str) -> RunResult:  # type: ignore[no-untyped-def]
        if len(rig.runs) == 1:
            progress(rig, task_id)
            return RunResult("MAX_STEPS", "stopped after 8 steps")
        return rig.finish_done(task_id)

    report, store, rig, _ = go(tmp_path, ["148"], {"148": behaviour})

    assert rig.runs == ["148", "148"]
    (note,) = report.continued
    assert note["task_id"] == "148" and note["validation"] == "PASSED" and note["candidate_sha"] and note["continue"] is True
    (record,) = report.tasks
    assert record.outcome == "done" and record.runs == 2  # one auditable record, two runs
    assert rig.prs == ["148"]  # opened once, for the finished task
    assert report.counts() == {"done": 1}
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='task.max_steps_continuation'").fetchone()[0] == 1
    first = ("148" + "e" * 40)[:40]  # the candidate the capped run produced: still in the ledger with its validation evidence
    assert store.conn.execute("SELECT COUNT(*) FROM candidates WHERE task_id='148' AND sha=?", (first,)).fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE task_id='148' AND candidate_sha=? AND kind='VALIDATION'", (first,)).fetchone()[0] == 1


def test_a_step_capped_run_that_made_no_progress_is_not_repeated(tmp_path: Path) -> None:
    report, _store, rig, _ = go(tmp_path, ["148"], {"148": RunResult("MAX_STEPS", "stopped after 8 steps")})

    assert rig.runs == ["148"] and not report.continued
    (record,) = report.tasks
    assert record.outcome == "open" and "not continued: the run made no progress" in record.reason


def test_continuations_are_bounded(tmp_path: Path) -> None:
    def behaviour(rig, task_id: str) -> RunResult:  # type: ignore[no-untyped-def]
        progress(rig, task_id)  # always progressing, never finishing
        return RunResult("MAX_STEPS", "stopped after 8 steps")

    report, _store, rig, _ = go(tmp_path, ["148"], {"148": behaviour}, max_continuations=2)

    assert rig.runs == ["148"] * 3  # the first run plus two continuations
    (record,) = report.tasks
    assert record.outcome == "open" and record.runs == 3 and "2 continuation(s) already used (limit 2)" in record.reason


def test_a_continued_task_does_not_count_against_the_task_limit(tmp_path: Path) -> None:
    def behaviour(rig, task_id: str) -> RunResult:  # type: ignore[no-untyped-def]
        if rig.runs.count(task_id) == 1:
            progress(rig, task_id)
            return RunResult("MAX_STEPS", "stopped after 8 steps")
        return rig.finish_done(task_id)

    report, _store, rig, _ = go(tmp_path, ["148", "149"], {"148": behaviour}, max_tasks=2, concurrency=1)

    assert sorted({t.task_id for t in report.tasks}) == ["148", "149"] and rig.runs.count("148") == 2
    assert report.counts() == {"done": 2}
