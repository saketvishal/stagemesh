"""Replay interrupted publication before recovery, with real local Git and gates."""

import time
from dataclasses import replace

import pytest

from autonomy_support import git
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.persistence import Store
from stagemesh.run_window import WindowOptions
from stagemesh.survivor_wave import STAGING_REF, RunResult, _defer_pr_delivery
from test_landing_recovery import recovery_rig


def deferred_rig(tmp_path, monkeypatch):
    r = recovery_rig(tmp_path, monkeypatch, stacked=True)
    tail = r.records[-1]
    _defer_pr_delivery(r.store, tail, "previous window publication interrupted")
    del r.prs[2]
    original = r.hooks.run_task
    r.delivery_runs = []

    def resume_delivery(task_id, steps):
        worker = Store(r.repo / ".stagemesh/stagemesh.sqlite3")
        try:
            if task_id == "SECOND" and worker.get_task(task_id)["stage"] == Stage.INTEGRATE:
                assert steps == 1
                r.delivery_runs.append(task_id)
                worker.advance_task(task_id, Stage.DONE)
                return RunResult("DONE")
        finally:
            worker.close()
        return original(task_id, steps)

    r.hooks = replace(r.hooks, run_task=resume_delivery)
    return r


@pytest.mark.parametrize("done_before_publication", [False, True])
def test_interrupted_tail_is_published_then_full_suffix_refreshed_and_landed(tmp_path, monkeypatch, done_before_publication):
    r = deferred_rig(tmp_path, monkeypatch)
    if done_before_publication:
        r.store.advance_task("SECOND", Stage.DONE)
    deadlines = []
    r.hooks.open_pr.set_deadline = deadlines.append
    deadline = time.time() + 60
    result = r.merger.recover(r.records[:1], r.failure, deadline, r.hooks, WindowOptions())
    assert result["ok"], result["reason"]
    assert deadlines and all(value == deadline for value in deadlines)
    assert r.delivery_runs == ([] if done_before_publication else ["SECOND"])
    assert [item[1] for item in r.phases] == ["VALIDATE", "VALIDATE"]
    assert len(result["merged_prs"]) == 2
    assert len(result["waves"]) == 3
    tip = git(r.remote, "rev-parse", "main")
    assert tip == git(r.repo, "rev-parse", STAGING_REF) == git(r.repo, "rev-parse", "main")
    assert r.merger.active_recovery() is None
    for index, sha in enumerate(r.originals):
        assert git(r.remote, "rev-parse", f"wave/task-{r.records[index].task_id}") == sha


@pytest.mark.parametrize("problem", ["missing_audit", "stale_audit", "blocked", "failed_evidence", "selection", "budget", "unknown_tip", "invalid_base"])
def test_unknown_or_ineligible_tail_is_not_published_or_discarded(tmp_path, monkeypatch, problem):
    r = deferred_rig(tmp_path, monkeypatch)
    options = WindowOptions()
    if problem == "missing_audit":
        r.store.conn.execute("delete from audit_events where event_type='task.pr_delivery_pending'")
        r.store.conn.commit()
    elif problem == "stale_audit":
        r.store.add_audit_event("task.pr_delivery_pending", {"task_id": "SECOND", "candidate_sha": "f" * 40})
    elif problem == "blocked":
        r.store.conn.execute("update tasks set status='BLOCKED' where id='SECOND'")
        r.store.conn.commit()
    elif problem == "failed_evidence":
        r.store.add_evidence("SECOND", r.originals[-1], EvidenceKind.REVIEW, EvidenceStatus.FAILED, {})
    elif problem == "selection":
        options.tasks = ["FIRST"]
    elif problem == "budget":
        options.max_tasks = 1
    elif problem == "unknown_tip":
        git(r.repo, "update-ref", STAGING_REF, r.target)
    else:
        r.failure["base_sha"] = None
    before = git(r.repo, "rev-parse", STAGING_REF)
    result = r.merger.recover(r.records[:1], r.failure, time.time() + 30, r.hooks, options)
    assert not result["ok"]
    assert r.delivery_runs == [] and r.phases == []
    assert set(r.prs) == {1}
    assert git(r.repo, "rev-parse", STAGING_REF) == before
    assert git(r.remote, "rev-parse", "main") == r.target


def test_publication_failure_retains_pending_tail_for_next_resume(tmp_path, monkeypatch):
    r = deferred_rig(tmp_path, monkeypatch)
    r.hooks.open_pr = lambda record: (_ for _ in ()).throw(RuntimeError("publication interrupted again"))
    result = r.merger.recover(r.records[:1], r.failure, time.time() + 30, r.hooks, WindowOptions())
    assert not result["ok"] and "publication interrupted again" in result["reason"]
    assert r.store.get_task("SECOND")["stage"] == Stage.INTEGRATE
    assert r.store.latest_candidate("SECOND")["sha"] == r.originals[-1]
    assert git(r.repo, "rev-parse", STAGING_REF) == r.originals[-1]
    assert len(result["waves"]) == 1 and r.phases == []


def test_dispatch_guard_refuses_task_that_changed_to_implementation(tmp_path, monkeypatch):
    from stagemesh import landing_recovery

    r = deferred_rig(tmp_path, monkeypatch)
    original = landing_recovery.run_wave

    def changed(*args, **kwargs):
        r.store.advance_task("SECOND", Stage.IMPLEMENT)
        return original(*args, **kwargs)

    monkeypatch.setattr(landing_recovery, "run_wave", changed)
    result = r.merger.recover(r.records[:1], r.failure, time.time() + 30, r.hooks, WindowOptions())
    assert not result["ok"] and "identity, state or evidence changed" in result["reason"]
    assert r.delivery_runs == [] and r.phases == [] and set(r.prs) == {1}


def test_recovery_receives_remaining_window_budget_after_earlier_work(tmp_path):
    import stagemesh.run_window as rw
    from test_run_window import window

    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2", "3"], auto_merge_ff=True, concurrency=1, max_tasks=3, required_checks=("web",))
    observed = []

    def land(record, deadline):
        return ({"merged": True, "pr": 1} if record.task_id == "1" else
                {"merged": False, "remote_landed": False, "refresh_required": True})

    def recover(suffix, failure, deadline, hooks, remaining):
        observed.append(remaining.max_tasks)
        return {"ok": False, "reason": "bounded fixture", "waves": [], "merged_prs": [], "landing_results": []}

    land.recover = recover
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert not report.ok and observed == [2]
    assert rig.runs == ["1", "2"] and options.max_tasks == 3
