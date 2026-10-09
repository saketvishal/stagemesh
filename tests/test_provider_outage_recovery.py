from __future__ import annotations

import sys
import time

import pytest

from stagemesh.domain import ExecutionStatus, ExecutionKind
from stagemesh.execution import ExecutionResult
from stagemesh.provider_pool import IMPLEMENT, REVIEW, ProviderLog, ProviderPool, PooledExecutor
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.queue_run import QueueRunner
from stagemesh.workspace_guard import owned_workspace
from test_parallel import Rig, ScriptedExecutor


def pool_for(cooldown=0.2):
    adapter = RuntimeCommandAdapter(name="scripted", command=(sys.executable,), capabilities=("code", "review"))
    return ProviderPool([adapter], {IMPLEMENT: ("scripted",), REVIEW: ("scripted",)},
                        cooldown_seconds=cooldown, log=ProviderLog(echo=False))


@pytest.mark.parametrize("reason,temporary", [
    ("transient_provider_failure", True), ("quota_rate_limit", True),
    ("authentication_failure", False), ("provider_unavailable", False),
    ("no_implementation_change", False),
])
def test_only_temporary_capacity_has_retry_deadline(tmp_path, reason, temporary):
    rig = Rig(tmp_path, ["A"])
    pool = pool_for()
    pool.record_failure(rig.store, IMPLEMENT, "A", "scripted", reason)
    deadline = pool.next_retry_at(rig.store, IMPLEMENT, "A")
    assert (deadline is not None) == temporary
    assert pool.next_retry_at(rig.store, REVIEW, "A", "scripted") is None


def test_cooldown_ends_at_exact_deadline(tmp_path, monkeypatch):
    rig = Rig(tmp_path, ["A"])
    pool = pool_for()
    monkeypatch.setattr("stagemesh.provider_pool.time.time", lambda: 1000.0)
    pool.record_failure(rig.store, IMPLEMENT, "A", "scripted", "transient_provider_failure")
    assert not pool.evaluate(rig.store, IMPLEMENT, "A")[0].eligible
    monkeypatch.setattr("stagemesh.provider_pool.time.time", lambda: 1000.2)
    assert pool.evaluate(rig.store, IMPLEMENT, "A")[0].eligible


def test_shared_capacity_wait_crosses_stages_without_breaking_independence(tmp_path):
    from stagemesh.concurrency import ProviderLimiter
    rig = Rig(tmp_path, ["A"])
    pool = pool_for()
    pool.limiter = ProviderLimiter()
    pool.limiter.cool_down("scripted", 60, "transient_provider_failure")
    assert pool.next_retry_at(rig.store, REVIEW, "A", "other") > time.time()
    pool.record_failure(rig.store, REVIEW, "A", "scripted", "transient_provider_failure")
    assert pool.next_retry_at(rig.store, REVIEW, "A", "other") == pool.limiter.temporary_retry_at("scripted")
    assert pool.next_retry_at(rig.store, REVIEW, "A", "scripted") is None
    pool.limiter.cool_down("scripted", 60, "authentication_failure")
    pool.record_failure(rig.store, REVIEW, "A", "scripted", "authentication_failure")
    assert pool.next_retry_at(rig.store, REVIEW, "A", "other") is None


@pytest.mark.parametrize("control", ["recover", "existing", "pause", "stop", "finite"])
def test_queue_recovers_or_honors_control_during_outage(tmp_path, monkeypatch, control):
    rig = Rig(tmp_path, ["A"])
    scripted = ScriptedExecutor(rig.files)
    pool = pool_for(2 if control == "existing" else 0.2 if control == "recover" else 60)
    calls = []

    def execute(adapter, store, task_id, claim_id, project):
        calls.append(task_id)
        if len(calls) == 1 and control != "existing":
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True,
                                   failure_reason="transient_provider_failure")
        with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
            result = scripted.run(store, task_id, claim_id, project)
            lease.seal(result.candidate_sha)
            return result

    monkeypatch.setattr(RuntimeCommandAdapter, "execute", execute)
    runner = rig.runner(PooledExecutor(pool), runner_class=QueueRunner,
                        wait_for_providers=control != "finite")
    if control == "existing":
        pool.record_failure(rig.store, IMPLEMENT, "A", "scripted", "transient_provider_failure")
    original_note = runner.note

    def note(task_id, event, **detail):
        original_note(task_id, event, **detail)
        if event == "waiting_for_provider":
            if control == "pause":
                runner.pause_admission()
            if control == "stop":
                runner.stop_admission()

    runner.note = note
    start = time.monotonic()
    summary = runner.run()
    assert time.monotonic() - start < 15
    assert len(summary.tasks) == 1
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    if control in {"recover", "existing"}:
        assert calls == (["A"] if control == "existing" else ["A", "A"])
        assert summary.succeeded, summary.to_dict()
        assert rig.store.get_task("A")["status"] == "DONE"
        assert summary.tasks[0].summary.detail["provider_wait_history"]
    else:
        assert calls == ["A"]
        assert rig.store.get_task("A")["status"] == "OPEN"


def test_queue_recovers_independent_review_without_reimplementing(tmp_path, monkeypatch):
    from stagemesh.review import Reviewer
    rig = Rig(tmp_path, ["A"])
    executor = ScriptedExecutor(rig.files)
    adapter = RuntimeCommandAdapter(name="independent", command=(sys.executable,), capabilities=("review",))
    pool = ProviderPool([adapter], {REVIEW: ("independent",)}, cooldown_seconds=0.2,
                        log=ProviderLog(echo=False))
    reviews = []

    def review(adapter, prompt, project, candidate_sha):
        reviews.append(candidate_sha)
        if len(reviews) == 1:
            return '{"decision":"INFRASTRUCTURE_FAILURE","reason":"transient_provider_failure"}'
        return '{"decision":"PASS","findings":[]}'

    monkeypatch.setattr(RuntimeCommandAdapter, "review_candidate", review)
    runner = rig.runner(executor, runner_class=QueueRunner)
    make = runner.make_coordinator

    def coordinator(target, store, task_id):
        result = make(target, store, task_id)
        result.reviewer = Reviewer(require_independent=True, review_pool=pool)
        return result

    runner.make_coordinator = coordinator
    summary = runner.run()
    assert summary.succeeded, summary.to_dict()
    assert len(reviews) == 2 and reviews[0] == reviews[1]
    assert len([entry for entry in executor.log if entry[0] == "start"]) == 1
