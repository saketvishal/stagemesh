from __future__ import annotations

import json

import pytest
from test_autonomy_recovery import TASK, _repo_with_claimed_task

from stagemesh.autonomy.decisions import Action
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage
from stagemesh.operator_actions import (
    OperatorActionError,
    recover_stale,
    release_unknown_execution,
    task_details,
)
from stagemesh.parallel import ParallelRunner, recover_orphaned_claims
from stagemesh.process_identity import owner_execution_fields
from stagemesh.validation import Validator


@pytest.fixture
def case(tmp_path):
    project, store, _ = _repo_with_claimed_task(tmp_path)
    store.advance_task(TASK, Stage.VALIDATE)
    yield project, store
    store.close()


def _row(store, *, kind=ExecutionKind.VALIDATION, claim=None, legacy=False, live=False):
    fields = owner_execution_fields() if live else ({} if legacy else {"pid": 123456789, "process_create_time": 1.0, "boot_id": "fixture"})
    return store.start_execution(task_id=TASK, claim_id=claim, kind=kind, candidate_sha="candidate", actor="controlled", **fields)


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_holds_dead_or_ownerless_validation_without_erasing_parent(case, monkeypatch, legacy):
    from stagemesh import operator_actions
    _project, store = case
    eid = _row(store, legacy=legacy)
    original = dict(store.conn.execute("SELECT * FROM executions WHERE id=?", (eid,)).fetchone())
    monkeypatch.setattr(operator_actions, "process_identity", lambda _pid: None)
    actions = recover_stale(store, TASK)
    assert len(actions)==1 and actions[0].action=="SKIPPED_UNKNOWN" and actions[0].process_state=="UNKNOWN"
    assert dict(store.conn.execute("SELECT * FROM executions WHERE id=?", (eid,)).fetchone()) == original
    details = task_details(store, TASK)["active_executions"][0]
    assert details["process_state"]=="UNKNOWN" and details["cleanup_state"]=="UNCONFIRMED_VALIDATION_TREE"


def test_live_parent_stays_live_and_cannot_be_manually_released(case):
    _project, store = case
    eid = _row(store, live=True)
    assert task_details(store,TASK)["active_executions"][0]["process_state"]=="LIVE"
    assert recover_stale(store,TASK)[0].action=="SKIPPED_LIVE"
    with pytest.raises(OperatorActionError,match="LIVE"):
        release_unknown_execution(store,TASK,eid,"controlled inspection")
    assert len(list(store.running_executions()))==1


@pytest.mark.parametrize("done", [False, True])
def test_raw_coordinator_recovery_cannot_bypass_tree_even_when_task_done(case, monkeypatch, done):
    from stagemesh import coordinator
    project, store = case
    eid = _row(store)
    if done:
        store.advance_task(TASK, Stage.DONE)
    monkeypatch.setattr(coordinator,"process_identity",lambda _pid: None)
    Coordinator(store,project).recover()
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"
    assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]==0


@pytest.mark.parametrize("api", ["orphan", "fence", "claim"])
def test_store_mutation_backstops_refuse_unresolved_validation(case, api):
    _project, store = case
    claim = store.acquire_claim(TASK,"controlled-worker")
    eid = _row(store,claim=claim)
    if api=="orphan":
        changed=store.mark_orphan_running_execution_failed(eid,"TASK_ALREADY_DONE")
    elif api=="fence":
        changed=store.fence_unknown_execution(eid,"configured automatic replacement")
    else:
        changed=store.recover_stale_execution_claim(eid,"DEAD_PROCESS_IDENTITY")
    assert changed is False and store.has_active_claim_for_execution(eid)
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"


def test_parallel_orphan_claim_recovery_sees_claimless_validation(case):
    project, store = case
    claim = store.acquire_claim(TASK,"parallel-12345678-1")
    eid = _row(store)
    assert recover_orphaned_claims(store,project,is_dead=lambda _pid: True)==[]
    assert store.conn.execute("SELECT active FROM claims WHERE id=?",(claim,)).fetchone()[0]==1
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"


def test_parallel_abandon_does_not_kill_parent_release_or_discard(case, monkeypatch):
    from stagemesh import parallel
    project, store = case
    claim = store.acquire_claim(TASK,"parallel-12345678-1")
    eid = _row(store)
    mutations=[]
    monkeypatch.setattr(parallel,"_kill_pid",lambda *_a: mutations.append("kill") or True)
    monkeypatch.setattr(parallel,"discard_partial_edits",lambda *_a: mutations.append("discard") or True)
    runner=ParallelRunner(store,project,lambda *_a: None,concurrency=1,max_steps=1,sweep_worktrees=False)
    runner._abandon(store,TASK,"controlled interruption")
    assert mutations==[] and claim in runner._protected_claims
    assert store.conn.execute("SELECT active FROM claims WHERE id=?",(claim,)).fetchone()[0]==1
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"


@pytest.mark.parametrize("legacy", [False, True])
def test_autonomy_fence_policy_cannot_release_or_replace_validation(case, monkeypatch, legacy):
    from stagemesh.autonomy import supervisor as module
    project, store = case
    eid=_row(store,legacy=legacy)
    monkeypatch.setattr(module,"process_identity",lambda _pid: None)
    supervisor=Supervisor(store,project,integration_ref="main")
    changed=[]
    monkeypatch.setattr(module,"advance_worktree_generation",lambda *_a: changed.append(True))
    decision=supervisor.recover_execution(TASK,eid,age_seconds=100000)
    assert decision.action is Action.HOLD_FAIL_CLOSED and changed==[]
    assert decision.detail["automatic_replacement_allowed"] is False
    with pytest.raises(ValueError,match="validation tree"):
        supervisor._fence(TASK,eid)
    assert len(list(store.running_executions()))==1


def test_explicit_inspected_release_is_specific_and_audited(case, monkeypatch):
    from stagemesh import operator_actions
    _project, store = case
    first,second=_row(store),_row(store)
    monkeypatch.setattr(operator_actions,"process_identity",lambda _pid: None)
    with pytest.raises(OperatorActionError,match="non-empty"):
        release_unknown_execution(store,TASK,first,"")
    action=release_unknown_execution(store,TASK,first,"controlled fixture proves no child remains")
    assert action.action=="RELEASED_BY_OPERATOR"
    assert [r["id"] for r in store.running_executions()]==[second]
    events=[json.loads(e["payload"]) for e in store.audit_events() if e["event_type"]=="recovery.operator_release_unknown"]
    assert len(events)==1 and events[0]["execution_id"]==first and events[0]["reason"]=="controlled fixture proves no child remains"


@pytest.mark.parametrize("kind", [ExecutionKind.REVIEW,ExecutionKind.INTEGRATION])
def test_provably_dead_other_builtins_still_recover(case, monkeypatch, kind):
    from stagemesh import operator_actions
    _project,store=case
    eid=_row(store,kind=kind)
    monkeypatch.setattr(operator_actions,"process_identity",lambda _pid: None)
    assert recover_stale(store,TASK)[0].action=="RELEASED"
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="FAILED"


@pytest.mark.parametrize("status", [ExecutionStatus.SUCCEEDED,ExecutionStatus.FAILED])
def test_actual_owner_completion_ends_validation_authority(case,status):
    _project,store=case
    eid=_row(store)
    store.finish_execution(eid,status,"candidate",result="controlled confirmed completion")
    assert not list(store.running_executions())
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]==status


def test_start_storage_failure_prevents_any_gate_dispatch(case, monkeypatch):
    from stagemesh import validation
    project,store=case
    def unavailable(**_kwargs):
        raise OSError("controlled execution storage unavailable")
    def forbidden(*_args):
        raise AssertionError("no contract/gate work before durable owner")
    monkeypatch.setattr(store,"start_execution",unavailable)
    monkeypatch.setattr(validation,"contract_for_candidate",forbidden)
    with pytest.raises(OSError,match="storage unavailable"):
        Validator().validate(store,TASK,"candidate",project)
    assert not list(store.running_executions())


def test_inspected_release_audit_failure_rolls_back_authority(case,monkeypatch):
    from stagemesh import operator_actions
    _project,store=case
    eid=_row(store)
    monkeypatch.setattr(operator_actions,"process_identity",lambda _pid: None)
    before=dict(store.conn.execute("SELECT * FROM executions WHERE id=?",(eid,)).fetchone())
    store.conn.execute("CREATE TRIGGER reject_validation_release_audit BEFORE INSERT ON audit_events "
                       "WHEN NEW.event_type='recovery.operator_release_unknown' "
                       "BEGIN SELECT RAISE(ABORT, 'controlled audit failure'); END")
    store.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match="controlled audit failure"):
        release_unknown_execution(store,TASK,eid,"controlled inspected fixture")
    assert dict(store.conn.execute("SELECT * FROM executions WHERE id=?",(eid,)).fetchone())==before
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type LIKE 'recovery.%'").fetchone()[0]==0


@pytest.mark.parametrize("mixed", [False, True])
def test_implementation_policy_never_runs_over_a_validation_owner(case,mixed):
    project,store=case
    store.advance_task(TASK,Stage.IMPLEMENT)
    validation=_row(store)
    if mixed:
        _row(store,kind=ExecutionKind.IMPLEMENTATION,legacy=True)
    calls=[]
    class Guard:
        def recover_unknown(self,*_args):
            calls.append(True)
            return True
    coordinator=Coordinator(store,project,guard=Guard())
    assert coordinator.tick()==0 and calls==[]
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(validation,)).fetchone()[0]=="RUNNING"


def test_implementation_policy_boolean_cannot_bypass_unchanged_owner(case):
    project,store=case
    store.advance_task(TASK,Stage.IMPLEMENT)
    eid=_row(store,kind=ExecutionKind.IMPLEMENTATION,legacy=True)
    calls=[]
    class Guard:
        def recover_unknown(self,*_args):
            calls.append(True)
            return True
    coordinator=Coordinator(store,project,guard=Guard())
    assert coordinator.tick()==0 and calls==[True]
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"


def test_implementation_policy_handoff_rechecks_new_validation_owner(case):
    project,store=case
    store.advance_task(TASK,Stage.IMPLEMENT)
    eid=_row(store,kind=ExecutionKind.IMPLEMENTATION,legacy=True)
    published=[]
    class Guard:
        def recover_unknown(self,*_args):
            assert store.fence_unknown_execution(eid,"controlled implementation quarantine")
            published.append(_row(store))
            return True
    coordinator=Coordinator(store,project,guard=Guard())
    assert coordinator.tick()==0 and len(published)==1
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="UNKNOWN"
    assert [row["id"] for row in store.running_executions()]==published


def test_implementation_policy_never_fences_known_live_parent(case):
    project,store=case
    store.advance_task(TASK,Stage.IMPLEMENT)
    eid=_row(store,kind=ExecutionKind.IMPLEMENTATION,live=True)
    calls=[]
    class Guard:
        def recover_unknown(self,*_args):
            calls.append(True)
            return True
    coordinator=Coordinator(store,project,guard=Guard())
    assert coordinator.tick()==0 and calls==[]
    assert store.conn.execute("SELECT status FROM executions WHERE id=?",(eid,)).fetchone()[0]=="RUNNING"
