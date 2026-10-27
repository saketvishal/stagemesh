from __future__ import annotations

import pytest
from test_provider_pool import TASK, Rig
from test_run_ready import _continue

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceStatus, ExecutionKind, Stage
from stagemesh.parallel import ParallelRunner
from stagemesh.persistence import Store
from stagemesh.process_identity import owner_execution_fields
from stagemesh.validation import Validator


@pytest.fixture(autouse=True)
def _private_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "slot.lock"))


def _prepared(tmp_path):
    rig = Rig(tmp_path, {"codex": "ok", "claude": "ok", "grok": "ok"})
    rig.tick(2)
    assert rig.stage == "VALIDATE"
    return rig


def _retain(store, sha, kind=ExecutionKind.VALIDATION):
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=kind,
                                         candidate_sha=sha, actor="controlled-validator", **owner_execution_fields())
    store.retain_execution_for_cleanup(execution_id, "controlled gate cleanup unconfirmed")
    return execution_id


@pytest.mark.parametrize("kind", list(ExecutionKind))
def test_raw_tick_refuses_retained_stage_owner(tmp_path, monkeypatch, kind):
    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    held = _retain(rig.store, sha, kind)
    calls = []
    monkeypatch.setattr(Validator, "validate", lambda *_args, **_kwargs: calls.append(True) or EvidenceStatus.UNKNOWN)
    try:
        assert rig.coordinator.tick() == 0
        assert calls == []
        assert rig.stage == "VALIDATE"
        assert rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (held,)).fetchone()[0] == "RUNNING"
        assert rig.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        rig.store.close()


def test_continue_once_refuses_retained_owner_after_parent_death(tmp_path, monkeypatch):
    from stagemesh import coordinator

    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    held = _retain(rig.store, sha)
    calls = []
    monkeypatch.setattr(Validator, "validate", lambda *_args, **_kwargs: calls.append(True) or EvidenceStatus.UNKNOWN)
    monkeypatch.setattr(coordinator, "process_identity", lambda _pid: None)
    try:
        code, result = _continue(rig.project, "--once", "--task", TASK)
        assert calls == []
        assert code == 2 and result["stop_reason"] == "REFUSED:active_execution"
        assert result["progressed"] == 0 and rig.stage == "VALIDATE"
        row = rig.store.conn.execute("SELECT * FROM executions WHERE id=?", (held,)).fetchone()
        assert row["status"] == "RUNNING" and row["result"] == "cleanup_incomplete"
        assert row["pid"] is not None and row["process_create_time"] is None
    finally:
        rig.store.close()


def test_parallel_handoff_rechecks_new_owner_after_selection(tmp_path):
    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    calls, fences = [], []

    class Recorder(Validator):
        def validate(self, *_args, **_kwargs):
            calls.append(True)
            return EvidenceStatus.UNKNOWN

    def make(target, worker_store, task_id):
        assert task_id == TASK
        # This independent connection inserts the fence only after batch selection.
        other = Store(worker_store.db_path)
        try:
            fences.append(_retain(other, sha))
        finally:
            other.close()
        return Coordinator(worker_store, rig.project, executor=rig.coordinator.executor,
                           validator=Recorder(), reviewer=rig.coordinator.reviewer,
                           integrator=rig.coordinator.integrator, target=target,
                           require_independent_review=True)

    try:
        runner = ParallelRunner(rig.store, rig.project, make, concurrency=1, max_steps=1,
                                poll_seconds=0.01, sweep_worktrees=False)
        summary = runner.run()
        assert len(fences) == 1
        assert calls == []
        assert rig.stage == "VALIDATE"
        item = summary.tasks[0].summary
        assert item.stop_reason == "REFUSED:active_execution"
        assert rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (fences[0],)).fetchone()[0] == "RUNNING"
        assert rig.store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    finally:
        rig.store.close()


def test_continue_once_without_owner_still_advances_one_stage(tmp_path):
    rig = _prepared(tmp_path)
    try:
        code, result = _continue(rig.project, "--once", "--task", TASK)
        assert code == 0 and result["progressed"] == 1
        assert rig.stage == "REVIEW"
    finally:
        rig.store.close()


def test_known_live_claimless_owner_is_not_replayed(tmp_path, monkeypatch):
    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    held = rig.store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION,
                                     candidate_sha=sha, actor="controlled-live-validator", **owner_execution_fields())
    calls = []
    monkeypatch.setattr(Validator, "validate", lambda *_args, **_kwargs: calls.append(True) or EvidenceStatus.UNKNOWN)
    try:
        assert rig.coordinator.tick() == 0 and calls == []
        assert rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (held,)).fetchone()[0] == "RUNNING"
    finally:
        rig.store.close()


def test_known_dead_claimless_owner_is_recovered_then_validation_runs(tmp_path, monkeypatch):
    from stagemesh import coordinator

    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    held = rig.store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION,
                                     candidate_sha=sha, actor="controlled-dead-validator", **owner_execution_fields())
    monkeypatch.setattr(coordinator, "process_identity", lambda _pid: None)
    try:
        assert rig.coordinator.tick() == 1
        assert rig.stage == "REVIEW"
        assert rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (held,)).fetchone()[0] == "FAILED"
        assert rig.store.conn.execute("SELECT COUNT(*) FROM evidence WHERE status='PASSED'").fetchone()[0] == 1
    finally:
        rig.store.close()


def test_fenced_task_does_not_prevent_unrelated_task_progress(tmp_path, monkeypatch):
    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    _retain(rig.store, sha)
    other = rig.store.upsert_task("unrelated task", source_id="OTHER-UNFENCED")
    calls = []
    monkeypatch.setattr(Validator, "validate", lambda *_args, **_kwargs: calls.append(True) or EvidenceStatus.UNKNOWN)
    try:
        assert rig.coordinator.tick() == 1
        assert rig.stage == "VALIDATE" and rig.store.get_task(other)["stage"] == Stage.IMPLEMENT
        assert calls == []
        assert [item["task_id"] for item in rig.coordinator.last_execution_refusals] == [TASK]
        assert rig.store.task_remediation_count(TASK, "VALIDATE") == 0
    finally:
        rig.store.close()


def test_untargeted_once_keeps_unrelated_work_and_reports_fenced_task(tmp_path):
    rig = _prepared(tmp_path)
    sha = rig.store.latest_candidate(TASK)["sha"]
    _retain(rig.store, sha)
    other = rig.store.upsert_task("unrelated task", source_id="OTHER-UNFENCED")
    try:
        code, result = _continue(rig.project, "--once")
        assert code == 0 and result["progressed"] == 1
        assert result["active_execution_refusals"][0]["task_id"] == TASK
        assert rig.stage == "VALIDATE" and rig.store.get_task(other)["stage"] == Stage.IMPLEMENT
    finally:
        rig.store.close()
