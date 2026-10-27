from __future__ import annotations

import json

import pytest
from test_review_terminalization import TASK, JsonReviewAdapter, _project_with_candidate

from stagemesh.capacity import CapacityKind
from stagemesh.config import AdverseReviewPolicy
from stagemesh.contract_binding import contract_for_candidate
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage
from stagemesh.provider_pool import IMPLEMENT, REVIEW, ProviderLog, ProviderPool
from stagemesh.review import Reviewer, adverse_gate
from stagemesh.review_identity import (
    IMPLEMENTATION_IDENTITY,
    command_snapshot,
    implementation_identity,
    note_review_identity,
    record_implementation_identity,
)


def test_one_executed_adverse_review_is_not_a_distinct_normal_adverse_pair(tmp_path):
    project, store, candidate = _project_with_candidate(tmp_path)
    policy = AdverseReviewPolicy()
    try:
        reviewer = Reviewer(adapter=JsonReviewAdapter('{"decision":"PASS"}'), require_independent=True, adverse_policy=policy)
        assert reviewer.review(store, TASK, candidate, project) == EvidenceStatus.PASSED
        bound = contract_for_candidate(store, TASK, candidate, project)
        assert not adverse_gate(store, TASK, candidate, bound.digest, policy).allowed
    finally:
        store.close()


class ControlledAdapter:
    capabilities = frozenset({"code", "review"})

    def __init__(self, name, response='{"decision":"PASS"}', command=None, denied=None):
        self.name = name
        self.command = command or ("controlled-review", name)
        self.response = response
        self.denied = denied
        self.calls = []

    def command_for(self, _capability):
        return self.command

    def all_commands(self):
        return (self.command,)

    def check_stage_capacity(self, _capability):
        return CapacityKind.AVAILABLE

    def review_candidate(self, prompt, _project, _candidate):
        self.calls.append(prompt)
        note_review_identity(command_snapshot(self.name, self.command, self.all_commands()))
        return self.response


@pytest.fixture
def role_rig(tmp_path):
    project, store, candidate = _project_with_candidate(tmp_path)
    policy = AdverseReviewPolicy()
    adapters = [ControlledAdapter(name) for name in ("codex", "claude", "grok")]
    implementation = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.IMPLEMENTATION,
                                           candidate_sha=candidate, actor="codex")
    store.finish_execution(implementation, ExecutionStatus.SUCCEEDED, candidate)
    record_implementation_identity(store, TASK, candidate, implementation, command_snapshot("codex", adapters[0].command))
    pool = ProviderPool(adapters, {IMPLEMENT: ("codex",), REVIEW: ("claude", "grok")},
                        policy="priority", log=ProviderLog(echo=False))
    reviewer = Reviewer(review_pool=pool, require_independent=True, adverse_policy=policy, separate_roles=True)
    bound = contract_for_candidate(store, TASK, candidate, project)
    try:
        yield project, store, candidate, policy, pool, reviewer, bound
    finally:
        store.close()


def _run(rig):
    project, store, candidate, _, _, reviewer, _ = rig
    return reviewer.review(store, TASK, candidate, project)


def _gate(rig):
    _, store, candidate, policy, _, _, bound = rig
    return adverse_gate(store, TASK, candidate, bound.digest, policy)


def test_two_distinct_role_invocations_earn_bound_pair(role_rig):
    project, store, candidate, policy, pool, reviewer, bound = role_rig
    coordinator = Coordinator(store, project, reviewer=reviewer, require_independent_review=True, adverse_policy=policy)
    assert _run(role_rig) == EvidenceStatus.PASSED
    assert len(pool.adapters["claude"].calls) == 1 and not pool.adapters["grok"].calls
    assert "Normal review:" in pool.adapters["claude"].calls[0]
    assert "Adverse review gate:" not in pool.adapters["claude"].calls[0]
    assert not _gate(role_rig).allowed and not coordinator._review_satisfied(TASK, candidate, bound.digest)
    assert _run(role_rig) == EvidenceStatus.PASSED
    assert len(pool.adapters["claude"].calls) == len(pool.adapters["grok"].calls) == 1
    assert "Adverse review gate:" in pool.adapters["grok"].calls[0]
    assert _gate(role_rig).allowed and coordinator._review_satisfied(TASK, candidate, bound.digest)
    executions = store.conn.execute("SELECT * FROM executions WHERE kind='REVIEW'").fetchall()
    assert len(executions) == 2 and all(e["status"] == "SUCCEEDED" for e in executions)


def test_missing_second_actor_stays_unavailable_and_resumes_without_repeating_normal(role_rig):
    _, _, _, _, pool, _, _ = role_rig
    pool.pools[REVIEW] = ("claude",)
    assert _run(role_rig) == EvidenceStatus.PASSED
    assert _run(role_rig) == EvidenceStatus.CAPACITY
    assert not _gate(role_rig).allowed and len(pool.adapters["claude"].calls) == 1
    pool.pools[REVIEW] = ("claude", "grok")  # controlled configured availability, no real authorization change
    assert _run(role_rig) == EvidenceStatus.PASSED and _gate(role_rig).allowed
    assert len(pool.adapters["claude"].calls) == len(pool.adapters["grok"].calls) == 1


def test_normal_finding_needs_remediation_not_an_adverse_fallback(role_rig):
    _, store, candidate, _, pool, _, _ = role_rig
    pool.adapters["claude"].response = '{"decision":"FAIL_WITH_FINDINGS","findings":[{"severity":"high","message":"unchecked path"}]}'
    assert _run(role_rig) == EvidenceStatus.FAILED
    assert not _gate(role_rig).allowed and not pool.adapters["grok"].calls
    assert store.open_findings_for_candidate(TASK, candidate)[0]["message"] == "unchecked path"


@pytest.mark.parametrize(("severity", "expected"), [("high", EvidenceStatus.FAILED), ("nit", EvidenceStatus.PASSED), ("custom-advisory", EvidenceStatus.PASSED)])
def test_adverse_threshold_is_enforced_and_advisory_is_retained(role_rig, severity, expected):
    _, store, candidate, _, pool, _, bound = role_rig
    pool.adapters["grok"].response = json.dumps({"decision": "FAIL_WITH_FINDINGS", "findings": [{"severity": severity, "message": "concrete concern"}]})
    assert _run(role_rig) == EvidenceStatus.PASSED
    assert _run(role_rig) == expected
    assert _gate(role_rig).allowed == (expected == EvidenceStatus.PASSED)
    if severity == "nit":
        payloads = store.bound_evidence_payloads(TASK, candidate, EvidenceKind.REVIEW, bound.digest)
        assert payloads[-1]["advisory_findings"][0]["message"] == "concrete concern"
        assert not store.open_findings_for_candidate(TASK, candidate)


def test_alias_of_normal_actor_and_unauthorized_adverse_are_not_invoked(role_rig):
    _, _, _, _, pool, _, _ = role_rig
    alias = ControlledAdapter("alias", command=tuple(p.upper() for p in pool.adapters["claude"].command))
    denied = ControlledAdapter("denied", denied="not authorized")
    pool.adapters.update(alias=alias, denied=denied)
    pool.pools[REVIEW] = ("claude", "alias", "denied", "grok")
    assert _run(role_rig) == _run(role_rig) == EvidenceStatus.PASSED
    assert _gate(role_rig).allowed and not alias.calls and not denied.calls


def test_fallback_excludes_the_actual_normal_actor(role_rig):
    _, _, _, _, pool, _, _ = role_rig
    pool.adapters["claude"].response = '{"decision":"INFRASTRUCTURE_FAILURE","reason":"quota_rate_limit"}'
    third = ControlledAdapter("third")
    pool.adapters["third"] = third
    pool.pools[REVIEW] = ("claude", "grok", "third")
    assert _run(role_rig) == EvidenceStatus.PASSED  # grok supplies the normal answer
    assert _run(role_rig) == EvidenceStatus.PASSED  # third supplies adverse; grok is excluded
    assert _gate(role_rig).allowed
    assert len(pool.adapters["grok"].calls) == len(third.calls) == 1


@pytest.mark.parametrize("corruption", ["candidate", "policy", "contract", "provider_alias", "command_alias", "not_executed", "same_execution", "missing_execution", "running_execution", "wrong_role"])
def test_pair_binding_and_actual_execution_proofs_fail_closed(role_rig, corruption):
    _, store, candidate, _, _, _, bound = role_rig
    assert _run(role_rig) == _run(role_rig) == EvidenceStatus.PASSED
    row = store.conn.execute("SELECT id,payload FROM evidence WHERE kind='REVIEW' ORDER BY rowid DESC LIMIT 1").fetchone()
    payload = json.loads(row["payload"])
    normal, adverse = payload["review_roles"]["normal"], payload["review_roles"]["adverse"]
    if corruption == "candidate":
        normal["candidate_sha"] = "0" * 40
    elif corruption == "policy":
        normal["policy_digest"] = "0" * 64
    elif corruption == "contract":
        normal["contract_hash"] = "0" * 64
    elif corruption == "provider_alias":
        normal["provider"] = adverse["provider"]
    elif corruption == "command_alias":
        normal["command"] = adverse["command"]
        normal["command_digest"] = adverse["command_digest"]
        normal["alias_digests"] = adverse["alias_digests"]
    elif corruption == "not_executed":
        normal["executed"] = False
    elif corruption == "same_execution":
        normal["execution_id"] = adverse["execution_id"]
    elif corruption == "missing_execution":
        normal["execution_id"] = "unknown"
    elif corruption == "running_execution":
        store.conn.execute("UPDATE executions SET status='RUNNING' WHERE id=?", (normal["execution_id"],))
    else:
        normal["role"] = "adverse"
    store.conn.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    store.conn.commit()
    assert not _gate(role_rig).allowed, corruption
    assert not adverse_gate(store, TASK, candidate, bound.digest, AdverseReviewPolicy(("critical",), "hold")).allowed


def _coordinator(rig):
    project, store, _, policy, _, reviewer, _ = rig
    return Coordinator(store, project, reviewer=reviewer, require_independent_review=True, adverse_policy=policy)


def test_coordinator_ticks_reopen_between_roles_and_advance_only_after_pair(tmp_path):
    from test_provider_pool import TASK as POOL_TASK
    from test_provider_pool import Rig

    from stagemesh.integration import Integrator
    from stagemesh.persistence import Store

    rig = Rig(tmp_path, {"codex": "ok", "claude": "ok", "grok": "ok"})
    store = rig.store
    policy = AdverseReviewPolicy()
    coordinator = rig.coordinator
    coordinator.adverse_policy = policy
    coordinator.reviewer = Reviewer(review_pool=rig.pool, require_independent=True, separate_roles=True, adverse_policy=policy)
    try:
        assert store.get_task(POOL_TASK)["stage"] == Stage.PLAN
        coordinator.tick()
        assert store.get_task(POOL_TASK)["stage"] == Stage.IMPLEMENT
        coordinator.tick()  # actual controlled Python implementation subprocess
        assert store.get_task(POOL_TASK)["stage"] == Stage.VALIDATE
        candidate = store.latest_candidate(POOL_TASK)["sha"]
        assert implementation_identity(store, POOL_TASK, candidate) is not None
        coordinator.tick()  # actual declared validation
        assert store.get_task(POOL_TASK)["stage"] == Stage.REVIEW
        assert coordinator.tick() == 1  # actual normal reviewer subprocess
        assert store.get_task(POOL_TASK)["stage"] == Stage.REVIEW
        db = store.db_path
        store.close()
        store = Store(db)
        coordinator = Coordinator(store, rig.project,
            reviewer=Reviewer(review_pool=rig.pool, require_independent=True, separate_roles=True, adverse_policy=policy),
            integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True),
            require_independent_review=True, adverse_policy=policy)
        assert coordinator.tick() == 1  # actual adverse subprocess, normal reused after reopen
        assert store.get_task(POOL_TASK)["stage"] == Stage.INTEGRATE
        reviews = store.conn.execute("SELECT * FROM executions WHERE kind='REVIEW'").fetchall()
        assert len(reviews) == 2 and all(r["status"] == "SUCCEEDED" for r in reviews)
    finally:
        store.close()


@pytest.mark.parametrize("boundary", ["budget", "stop"])
def test_run_ready_checks_budget_and_stop_between_roles(role_rig, boundary, monkeypatch):
    from stagemesh import run_ready
    from stagemesh.efficiency import BudgetPolicy
    from stagemesh.queue_control import QUEUE_CONTROL_EVENT

    project, store, _candidate, policy, pool, _, _ = role_rig
    assert _run(role_rig) == EvidenceStatus.PASSED
    store.advance_task(TASK, Stage.REVIEW)
    coordinator = _coordinator(role_rig)
    if boundary == "budget":
        coordinator.budget_policy = BudgetPolicy(max_provider_calls_per_task=1)
    else:
        store.add_audit_event(QUEUE_CONTROL_EVENT, {"state": "stopped", "terminate_running": False})
    monkeypatch.setattr(run_ready, "workspace_info", lambda *a: {})
    summary = run_ready.RunSummary(True, "UNSET", task_id=TASK)
    run_ready.drive_task(store, project, coordinator, TASK, summary, max_steps=2, adverse_policy=policy)
    assert summary.stop_reason == ("BLOCKED" if boundary == "budget" else "OPERATOR_STOP")
    assert not pool.adapters["grok"].calls and len(pool.adapters["claude"].calls) == 1
    assert store.get_task(TASK)["stage"] == Stage.REVIEW and not _gate(role_rig).allowed


def test_legacy_single_role_cannot_report_endless_normal_progress(role_rig, monkeypatch):
    project, store, _, policy, pool, _, _bound = role_rig
    store.advance_task(TASK, Stage.REVIEW)
    reviewer = Reviewer(review_pool=pool, require_independent=True, adverse_policy=policy)
    coordinator = Coordinator(store, project, reviewer=reviewer, require_independent_review=True, adverse_policy=policy)
    monkeypatch.setattr(coordinator, "_workspace_intact", lambda *a, **k: True)
    assert coordinator.tick() == 0
    assert store.get_task(TASK)["stage"] == Stage.REVIEW and not _gate(role_rig).allowed


def test_malformed_role_history_is_not_reused_or_a_crash(role_rig):
    _, store, _, _, pool, _, _ = role_rig
    assert _run(role_rig) == EvidenceStatus.PASSED
    row = store.conn.execute("SELECT id,payload FROM evidence WHERE kind='REVIEW' ORDER BY rowid DESC LIMIT 1").fetchone()
    payload = json.loads(row["payload"])
    payload["review_roles"] = []
    store.conn.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    store.conn.commit()
    assert not _gate(role_rig).allowed
    assert _run(role_rig) == EvidenceStatus.PASSED  # new normal proof, not an adverse invocation
    assert len(pool.adapters["claude"].calls) == 2 and not pool.adapters["grok"].calls
    assert _run(role_rig) == EvidenceStatus.PASSED and _gate(role_rig).allowed


def test_frozen_implementer_command_survives_configuration_change(role_rig):
    _, store, candidate, _, pool, _, _ = role_rig
    original = pool.adapters["codex"].command
    frozen = implementation_identity(store, TASK, candidate)
    pool.adapters["codex"].command = ("controlled-review", "new-implementer-command")
    pool.adapters["claude"].command = original  # alias of the actual old implementer command
    third = ControlledAdapter("third")
    pool.adapters["third"] = third
    pool.pools[REVIEW] = ("claude", "grok", "third")
    assert _run(role_rig) == _run(role_rig) == EvidenceStatus.PASSED
    assert not pool.adapters["claude"].calls and _gate(role_rig).allowed
    assert implementation_identity(store, TASK, candidate) == frozen


@pytest.mark.parametrize("problem", ["missing", "conflicting", "implementation_running"])
def test_unknown_or_conflicting_implementation_identity_blocks_before_review(role_rig, problem):
    _, store, candidate, _, pool, _, _ = role_rig
    if problem == "missing":
        store.conn.execute("DELETE FROM audit_events WHERE event_type=?", (IMPLEMENTATION_IDENTITY,))
    elif problem == "implementation_running":
        store.conn.execute("UPDATE executions SET status='RUNNING' WHERE kind='IMPLEMENTATION'")
    else:
        execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.IMPLEMENTATION, candidate_sha=candidate, actor="codex")
        store.finish_execution(execution, ExecutionStatus.SUCCEEDED, candidate)
        record_implementation_identity(store, TASK, candidate, execution, command_snapshot("codex", ("other-command",)))
    store.conn.commit()
    assert _run(role_rig) == EvidenceStatus.CAPACITY and not _gate(role_rig).allowed
    assert not pool.adapters["claude"].calls and not pool.adapters["grok"].calls


def test_failed_fallback_identity_cannot_authenticate_an_unnoted_answer(role_rig):
    _, _, _, _, pool, _, _ = role_rig
    pool.adapters["claude"].response = '{"decision":"INFRASTRUCTURE_FAILURE","reason":"quota_rate_limit"}'
    pool.adapters["grok"].review_candidate = lambda *a: '{"decision":"PASS"}'
    assert _run(role_rig) == EvidenceStatus.CAPACITY and not _gate(role_rig).allowed


def test_provenance_receipt_never_persists_raw_command_arguments(role_rig):
    _, store, candidate, _, _, _, _ = role_rig
    execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.IMPLEMENTATION, candidate_sha=candidate, actor="codex")
    store.finish_execution(execution, ExecutionStatus.SUCCEEDED, candidate)
    record_implementation_identity(store, TASK, candidate, execution,
        command_snapshot("codex", ("private-command", "--token=fixture-secret", "prompt-shaped-argument")))
    blob = "\n".join(row["payload"] for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (IMPLEMENTATION_IDENTITY,)))
    assert "fixture-secret" not in blob and "private-command" not in blob and "prompt-shaped-argument" not in blob


@pytest.mark.parametrize(("verdict", "findings"), [
    ("FAIL_WITH_FINDINGS", [None]),
    ("FAIL_WITH_FINDINGS", [{"severity": "custom", "message": ""}]),
    ("FAIL_WITH_FINDINGS", [{"severity": None, "message": "concrete defect"}]),
    ("PASS", [{"severity": "nit", "message": "concrete concern"}]),
])
def test_malformed_adverse_verdict_cannot_become_advisory_pass(role_rig, verdict, findings):
    _, store, _, _, _, _, _ = role_rig
    assert _run(role_rig) == _run(role_rig) == EvidenceStatus.PASSED
    row = store.conn.execute("SELECT id,payload FROM evidence WHERE kind='REVIEW' ORDER BY rowid DESC LIMIT 1").fetchone()
    payload = json.loads(row["payload"])
    payload["review_roles"]["adverse"].update(verdict=verdict, findings=findings)
    store.conn.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    store.conn.commit()
    assert not _gate(role_rig).allowed


def test_malformed_identity_note_is_capacity_not_normal_pass_or_crash(role_rig):
    _, store, _, _, pool, _, _ = role_rig

    def malformed(*_args):
        note_review_identity({"provider": "claude", "command_digest": None, "alias_digests": []})
        return '{"decision":"PASS"}'

    pool.adapters["claude"].review_candidate = malformed
    assert _run(role_rig) == EvidenceStatus.CAPACITY
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE kind='REVIEW' AND status='PASSED'").fetchone()[0] == 0
    assert not _gate(role_rig).allowed


def test_generic_subprocess_candidate_gets_frozen_execution_identity(tmp_path):
    from test_provider_pool import TASK as POOL_TASK
    from test_provider_pool import Rig

    from stagemesh.execution import SubprocessExecutor

    rig = Rig(tmp_path, {"codex": "ok", "claude": "ok", "grok": "ok"})
    rig.coordinator.executor = SubprocessExecutor(list(rig.pool.adapters["codex"].command), name="generic-controlled")
    try:
        rig.coordinator.tick()  # PLAN -> IMPLEMENT
        rig.coordinator.tick()  # actual local controlled Python subprocess
        assert rig.store.get_task(POOL_TASK)["stage"] == Stage.VALIDATE
        candidate = rig.store.latest_candidate(POOL_TASK)["sha"]
        identity = implementation_identity(rig.store, POOL_TASK, candidate)
        assert identity is not None and identity["provider"] == "generic-controlled"
        assert identity["command_digest"] == command_snapshot("generic-controlled", rig.pool.adapters["codex"].command)["command_digest"]
    finally:
        rig.store.close()


def test_interrupted_role_clears_identity_and_retains_unknown_cleanup(role_rig):
    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.review_identity import take_review_identity

    _, store, _, _, pool, _, _ = role_rig

    def interrupted(*_args):
        note_review_identity(command_snapshot("claude", pool.adapters["claude"].command))
        raise OperatorStopRequested("injected unknown cleanup", cleanup_complete=False)

    pool.adapters["claude"].review_candidate = interrupted
    with pytest.raises(OperatorStopRequested):
        _run(role_rig)
    assert take_review_identity() is None
    row = store.conn.execute("SELECT status FROM executions WHERE kind='REVIEW' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["status"] == "RUNNING"
    assert store.conn.execute("SELECT COUNT(*) FROM evidence WHERE kind='REVIEW' AND status='PASSED'").fetchone()[0] == 0


@pytest.mark.parametrize("limit", ["task", "window", "incomplete_retry"])
def test_shared_configured_budget_is_checked_before_each_role_call(role_rig, limit):
    from stagemesh.window_journal import record_started, record_task_context

    project, store, _, _, pool, _, _ = role_rig
    budgets = {"max_provider_calls_per_task": 1} if limit != "window" else {"max_provider_calls_per_window": 1}
    (project / ".stagemesh/config.json").write_text(json.dumps({"budgets": budgets}), encoding="utf-8")
    if limit == "window":
        record_started(store, "controlled-window", {})
        record_task_context(store, "controlled-window", TASK, {})
    if limit == "incomplete_retry":
        pool.adapters["claude"].response = "Review in progress"
        assert _run(role_rig) == EvidenceStatus.CAPACITY
    else:
        assert _run(role_rig) == EvidenceStatus.PASSED
        assert _run(role_rig) == EvidenceStatus.CAPACITY
    assert len(pool.adapters["claude"].calls) == 1 and not pool.adapters["grok"].calls
    payload = json.loads(store.conn.execute("SELECT payload FROM evidence WHERE kind='REVIEW' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert payload["review_infrastructure_failure"] == "budget_exhausted"
    assert "budget_exhausted" in payload["review_budget_reason"]
    assert not _gate(role_rig).allowed


def test_each_fallback_is_counted_once_and_usage_does_not_leak(role_rig):
    from stagemesh.efficiency import Usage, note_call_usage, provider_calls

    _, store, _, _, pool, _, _ = role_rig
    first = pool.adapters["claude"]
    first.response = '{"decision":"INFRASTRUCTURE_FAILURE","reason":"quota_rate_limit"}'
    original = first.review_candidate

    def reported(*args):
        note_call_usage(Usage(input=100, output=10))
        return original(*args)

    first.review_candidate = reported
    assert _run(role_rig) == EvidenceStatus.PASSED
    calls = provider_calls(store, TASK)
    assert [(c["provider"], c["outcome"]) for c in calls] == [("claude", "failed"), ("grok", "succeeded")]
    assert calls[0]["usage"]["input"] == 100 and calls[0]["usage"]["output"] == 10
    assert calls[1]["usage"]["reported"] is False and calls[1]["usage"]["input"] is None
