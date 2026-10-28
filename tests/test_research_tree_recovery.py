from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_research_stage import rig as _research_rig

from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage
from stagemesh.execution_ownership import (
    RESEARCH_PROCESS_TREE,
    execution_tree_unresolved,
    recovery_identity_state,
)
from stagemesh.operator_actions import (
    OperatorActionError,
    recover_stale,
    release_unknown_execution,
    task_details,
)
from stagemesh.persistence import Store, StoreValidationError


@pytest.fixture
def research_case(tmp_path):
    return _research_rig.__wrapped__(tmp_path)


@pytest.fixture
def store(tmp_path):
    state=Store(tmp_path/'state.sqlite3');state.migrate()
    state.upsert_task('research ownership',source_id='T')
    state.advance_task('T',Stage.IMPLEMENT)
    try:
        yield state
    finally:
        state.close()


def _scoped(store):
    return store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,
                                 actor='arbitrary-configured-name',process_tree_scope=RESEARCH_PROCESS_TREE)


def _row(store,identity):
    return store.conn.execute('SELECT * FROM executions WHERE id=?',(identity,)).fetchone()


def _v4(store):
    store.conn.execute('ALTER TABLE executions DROP COLUMN process_tree_scope')
    store.conn.execute('DELETE FROM schema_migrations WHERE version>=5')
    store.conn.execute('DELETE FROM migration_audit WHERE version>=5')
    store.conn.commit()


def test_fresh_and_v4_upgrade_preserve_legacy_rows_and_audit_and_are_idempotent(store):
    assert store.schema_version()>=5
    old=store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,actor='old')
    store.add_audit_event('keep',{'task_id':'T'})
    _v4(store)
    before=dict(_row(store,old))
    store.migrate();store.migrate()
    after=dict(_row(store,old));scope=after.pop('process_tree_scope')
    assert scope is None and before==after
    assert store.conn.execute("SELECT COUNT(*) FROM migration_audit WHERE version=5").fetchone()[0]==1
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='keep'").fetchone()[0]==1


def test_v4_upgrade_serializes_two_independent_connections(store):
    _v4(store)
    path=store.db_path
    def upgrade(_):
        other=Store(path)
        try:
            other.migrate()
            return other.schema_version()
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(v>=5 for v in pool.map(upgrade,range(2)))
    assert store.conn.execute("SELECT COUNT(*) FROM migration_audit WHERE version=5").fetchone()[0]==1
    assert [r['name'] for r in store.conn.execute('PRAGMA table_info(executions)')].count('process_tree_scope')==1


def test_interrupted_column_publication_recovers_without_discarding_tree_authority(store):
    identity=_scoped(store)
    store.conn.execute('DELETE FROM schema_migrations WHERE version=5')
    store.conn.execute('DELETE FROM migration_audit WHERE version=5')
    store.conn.commit()
    store.migrate()
    assert execution_tree_unresolved(_row(store,identity))
    assert store.schema_version()>=5


@pytest.mark.parametrize('scope',['','future/v999',True,3])
def test_start_rejects_unsupported_scope_before_any_row_is_published(store,scope):
    with pytest.raises(StoreValidationError,match='scope'):
        store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,process_tree_scope=scope)
    assert list(store.running_executions())==[]


def test_scope_and_running_row_publish_together_and_survive_cleanup_result_changes(store):
    identity=_scoped(store)
    other=Store(store.db_path)
    try:
        assert _row(other,identity)['process_tree_scope']==RESEARCH_PROCESS_TREE
        store.retain_execution_for_cleanup(identity,'controlled unconfirmed child cleanup')
        row=_row(other,identity)
        assert row['result']=='cleanup_incomplete' and row['process_tree_scope']==RESEARCH_PROCESS_TREE
        assert execution_tree_unresolved(row) and recovery_identity_state(row,'DEAD')=='UNKNOWN'
        assert recovery_identity_state(row,'LIVE')=='LIVE'
    finally:
        other.close()


def test_orphan_fence_coordinator_and_expired_claim_recovery_preserve_scope(store,tmp_path):
    claim=store.acquire_claim('T','parallel-12345-controlled',lease_seconds=-1)
    identity=_scoped(store)
    assert store.acquire_claim('T','replacement') is None
    assert store.conn.execute('SELECT active FROM claims WHERE id=?',(claim,)).fetchone()[0]==1
    assert not store.mark_orphan_running_execution_failed(identity,'controlled orphan')
    assert not store.fence_unknown_execution(identity,'controlled fence')
    Coordinator(store,tmp_path).recover()
    from stagemesh.parallel import recover_orphaned_claims
    assert recover_orphaned_claims(store,is_dead=lambda _:True)==[]
    assert _row(store,identity)['status']=='RUNNING'
    assert store.conn.execute('SELECT active FROM claims WHERE id=?',(claim,)).fetchone()[0]==1


def test_workspace_refuses_before_preparing_or_replacing_the_task_checkout(store,tmp_path,monkeypatch):
    from stagemesh import workspace_guard
    identity=_scoped(store);calls=[]
    monkeypatch.setattr(workspace_guard,'prepare_task_workspace',lambda *_:calls.append(True))
    with pytest.raises(workspace_guard.WorkspaceMutation,match='EXTERNAL_WORKSPACE_MUTATION'):
        workspace_guard.acquire_workspace(store,tmp_path/'project','T','IMPLEMENTATION')
    assert calls==[] and _row(store,identity)['status']=='RUNNING'
    assert not (tmp_path/'project').exists()


def test_reports_project_scoped_unknown_authority_and_advise_inspection(store):
    from stagemesh.handoff import _active_executions, _next_action
    from stagemesh.observability import health
    from stagemesh.operator import operator_report
    from stagemesh.queue_visibility import queue_control_report
    from stagemesh.run_ready import current_problems
    identity=_scoped(store)
    h=health(store)
    assert not h.ok and h.unresolved_process_tree_count==1 and 'unresolved_process_trees' in h.current_problems
    assert 'unresolved_process_trees' not in current_problems(store)  # unknown identity remains task-local
    details=task_details(store,'T')['active_executions'][0]
    queue=queue_control_report(store)['active_executions'][0]
    rows=next(s.rows for s in operator_report(store).sections if s.name=='Active Executions')
    active=_active_executions(store,time.time())
    for row in (details,queue,rows[0],active[0]):
        assert row['process_state']=='UNKNOWN' and row['cleanup_state']=='UNCONFIRMED_RESEARCH_TREE'
    assert queue['process_tree_scope']==RESEARCH_PROCESS_TREE
    next_action=_next_action([],{'health':{'unresolved_process_trees':1}},active,[])
    assert next_action['command'] is None and 'researcher' in next_action['reason']
    assert _row(store,identity)['status']=='RUNNING'


def test_autonomy_holds_and_refuses_fencing_scoped_research_without_touching_workspace(store):
    from stagemesh.autonomy.decisions import Action
    from stagemesh.autonomy.supervisor import Supervisor
    identity=_scoped(store)
    supervisor=SimpleNamespace(store=store)
    decision=Supervisor._assess_execution(supervisor,'T',identity,999)
    assert decision.action==Action.HOLD_FAIL_CLOSED and 'research tree' in decision.detail['reason']
    with pytest.raises(ValueError,match='research tree'):
        Supervisor._fence(supervisor,'T',identity)
    assert _row(store,identity)['status']=='RUNNING'


def test_explicit_inspected_release_is_audited_and_normal_completion_ends_authority(store):
    identity=_scoped(store)
    action=recover_stale(store,'T')[0]
    assert action.action=='SKIPPED_UNKNOWN'
    assert release_unknown_execution(store,'T',identity,'inspected all controlled child processes are stopped').action=='RELEASED_BY_OPERATOR'
    assert not execution_tree_unresolved(_row(store,identity))
    audit=json.loads(store.conn.execute("SELECT payload FROM audit_events WHERE event_type='recovery.operator_release_unknown'").fetchone()[0])
    assert audit['process_tree_scope']==RESEARCH_PROCESS_TREE and audit['execution_id']==identity
    second=_scoped(store)
    store.finish_execution(second,ExecutionStatus.SUCCEEDED)
    assert not execution_tree_unresolved(_row(store,second))
    assert store.acquire_claim('T','after-confirmed-completion') is not None


def test_inspected_release_transaction_rolls_back_if_audit_write_fails(store):
    identity=_scoped(store)
    store.conn.executescript("""CREATE TRIGGER reject_tree_release BEFORE INSERT ON audit_events
    WHEN NEW.event_type='recovery.operator_release_unknown'
    BEGIN SELECT RAISE(ABORT,'controlled audit failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError,match='controlled audit failure'):
        release_unknown_execution(store,'T',identity,'inspected controlled fixture')
    assert _row(store,identity)['status']=='RUNNING'
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.orphan_execution_failed'").fetchone()[0]==0


def test_live_tree_root_cannot_be_released_by_inspected_override(store,monkeypatch):
    from stagemesh import operator_actions
    identity=_scoped(store)
    monkeypatch.setattr(operator_actions,'_process_state',lambda *_:'LIVE')
    with pytest.raises(OperatorActionError,match='LIVE'):
        release_unknown_execution(store,'T',identity,'inspection never overrides a live root')
    assert _row(store,identity)['status']=='RUNNING'


def test_unknown_non_null_scope_holds_without_guessing_actor_or_future_contract(store):
    identity=_scoped(store)
    store.conn.execute("UPDATE executions SET process_tree_scope='future/v999' WHERE id=?",(identity,));store.conn.commit()
    assert execution_tree_unresolved(_row(store,identity))
    assert not store.mark_orphan_running_execution_failed(identity,'future scope is not cleanup evidence')



def test_popen_failure_finishes_scope_and_live_parent_cannot_be_inspected_away(research_case,monkeypatch):
    from stagemesh import providers
    from stagemesh.process_identity import process_identity
    from stagemesh.research_context import ResearchBlocked
    project,bound,state,adapter=research_case
    original=providers.subprocess.Popen
    calls=[]
    def refused(argv,*args,**kwargs):
        if len(argv)>1 and str(argv[1]).endswith('researcher.py'):
            pending=next(iter(state.running_executions()));calls.append(pending['id'])
            assert pending['process_tree_scope']==RESEARCH_PROCESS_TREE
            saved=state.execution_process_identity(pending['id'])
            assert saved.is_known and saved.matches(process_identity(saved.pid))
            with pytest.raises(OperatorActionError,match='LIVE'):
                release_unknown_execution(state,'t1',pending['id'],'a live parent cannot be inspected away')
            raise OSError('controlled prelaunch failure; no process was created')
        return original(argv,*args,**kwargs)
    monkeypatch.setattr(providers.subprocess,'Popen',refused)
    try:
        with pytest.raises(ResearchBlocked,match='research_failed'):
            adapter('ok')._run_research(state,'t1',project,bound)
        assert len(calls)==1
        row=_row(state,calls[0])
        assert row['status']=='FAILED' and row['process_tree_scope']==RESEARCH_PROCESS_TREE
        assert not execution_tree_unresolved(row)
    finally:
        state.close()


def test_task_done_does_not_prove_scoped_child_cleanup(store,tmp_path):
    identity=_scoped(store)
    store.advance_task('T',Stage.DONE)
    Coordinator(store,tmp_path).recover()
    assert _row(store,identity)['status']=='RUNNING'


def test_unscoped_legacy_review_is_not_reclassified_from_provider_name(store):
    from stagemesh.run_ready import _recover_dead
    identity=store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,actor='arbitrary-configured-name')
    assert _row(store,identity)['process_tree_scope'] is None
    assert _recover_dead(store,'T',own_task=True)[0]['execution_id']==identity
    assert _row(store,identity)['status']=='FAILED'


def test_store_inspected_release_binds_scope_and_kind_with_the_selected_row(store):
    identity=_scoped(store)
    audit={'task_id':'T','execution_id':identity,'execution_kind':'REVIEW','process_tree_scope':'wrong/v1',
           'operator_action':'RELEASE_UNKNOWN_EXECUTION','operator':'controlled','reason':'inspected fixture'}
    with pytest.raises(StoreValidationError,match='selected execution tree'):
        store.release_inspected_execution_tree(identity,'T',audit)
    assert _row(store,identity)['status']=='RUNNING'
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.operator_release_unknown'").fetchone()[0]==0



def test_inspected_release_refuses_if_a_live_child_identity_publishes_after_unknown_probe(store,monkeypatch):
    from stagemesh.process_identity import owner_execution_fields
    identity=_scoped(store)
    original=store.release_inspected_execution_tree
    def publish_then_release(execution_id,task_id,audit):
        saved=owner_execution_fields()
        store.conn.execute("UPDATE executions SET pid=?,process_create_time=?,boot_id=?,executable=? WHERE id=?",
                           (saved['pid'],saved['process_create_time'],saved['boot_id'],saved['executable'],identity))
        store.conn.commit()
        return original(execution_id,task_id,audit)
    monkeypatch.setattr(store,'release_inspected_execution_tree',publish_then_release)
    action=release_unknown_execution(store,'T',identity,'inspection of the previously unidentified root')
    assert action.action=='NOT_RECOVERABLE' and _row(store,identity)['status']=='RUNNING'
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.orphan_execution_failed'").fetchone()[0]==0


def test_identity_probe_uses_the_fresh_saved_root_not_a_stale_outer_pid(store):
    from stagemesh.operator_actions import _process_state
    from stagemesh.process_identity import owner_execution_fields
    identity=_scoped(store);saved=owner_execution_fields()
    store.conn.execute("UPDATE executions SET pid=?,process_create_time=?,boot_id=?,executable=? WHERE id=?",
                       (saved['pid'],saved['process_create_time'],saved['boot_id'],saved['executable'],identity))
    store.conn.commit()
    assert _process_state(store,identity,None)=='LIVE'


def test_inspected_claimless_research_release_does_not_release_an_unproven_implementation_claim(store):
    claim=store.acquire_claim('T','controlled-implementation-parent',lease_seconds=300)
    identity=_scoped(store)
    assert release_unknown_execution(store,'T',identity,'controlled child cleanup inspected').action=='RELEASED_BY_OPERATOR'
    assert store.conn.execute('SELECT active FROM claims WHERE id=?',(claim,)).fetchone()[0]==1
    assert store.acquire_claim('T','replacement') is None
    store.conn.execute('UPDATE claims SET lease_expires_at=0 WHERE id=?',(claim,));store.conn.commit()
    assert store.acquire_claim('T','after-fixture-claim-expiry') is not None


def test_cli_health_fields_and_dashboard_summary_include_scoped_tree_count(store):
    from stagemesh.cli import _health_scope_fields
    from stagemesh.dashboard import dashboard_summary
    from stagemesh.observability import health
    _scoped(store)
    assert _health_scope_fields(health(store))['unresolved_process_tree_count']==1
    assert dashboard_summary(store)['unresolved process trees']=='1'



def test_inner_store_live_recheck_refuses_an_unchanged_inspected_identity(store,monkeypatch):
    from stagemesh import operator_actions
    from stagemesh.process_identity import owner_execution_fields
    identity=store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,
                                   process_tree_scope=RESEARCH_PROCESS_TREE,**owner_execution_fields())
    monkeypatch.setattr(operator_actions,'_process_state',lambda *_:'UNKNOWN')
    assert release_unknown_execution(store,'T',identity,'outer probe unavailable; inner probe is live').action=='NOT_RECOVERABLE'
    assert _row(store,identity)['status']=='RUNNING'
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.orphan_execution_failed'").fetchone()[0]==0


def test_scoped_claimless_abandon_and_finalize_keep_claim_work_and_honest_safety_status(store,tmp_path,monkeypatch):
    import os
    import threading

    from stagemesh import parallel
    claim=store.acquire_claim('T',f'parallel-{os.getpid()}-T')
    identity=_scoped(store);mutations=[]
    monkeypatch.setattr(parallel,'_kill_pid',lambda *_:mutations.append('kill') or True)
    monkeypatch.setattr(parallel,'discard_partial_edits',lambda *_:mutations.append('discard') or True)
    runner=parallel.ParallelRunner(store,tmp_path,lambda *_:None,concurrency=1,max_steps=1,sweep_worktrees=False)
    runner._threads['T']=threading.Thread(target=lambda:None)
    runner._abandon(store,'T','controlled interrupt')
    summary=parallel.ParallelSummary(1)
    runner._finalize(summary)
    assert mutations==[] and claim in runner._protected_claims
    assert summary.stop_reason=='GLOBAL_SAFETY_FAILURE'
    assert store.conn.execute('SELECT active FROM claims WHERE id=?',(claim,)).fetchone()[0]==1
    assert _row(store,identity)['status']=='RUNNING'


def test_dead_scoped_stop_snapshot_records_unknown_ownership_without_inventing_a_kill(store,tmp_path,monkeypatch):
    import threading

    from stagemesh import parallel
    identity=store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW,
                                   process_tree_scope=RESEARCH_PROCESS_TREE,pid=999991,
                                   process_create_time=1,boot_id='controlled',executable='controlled')
    monkeypatch.setattr(parallel,'process_identity',lambda _:None)
    runner=parallel.ParallelRunner(store,tmp_path,lambda *_:None,concurrency=1,max_steps=1,sweep_worktrees=False)
    runner._threads['T']=threading.Thread(target=lambda:None)
    snapshot=runner._execution_stop_snapshot(store)
    assert snapshot[0]['process_state']=='UNKNOWN' and snapshot[0]['parent_process_state']=='DEAD'
    assert snapshot[0]['process_tree_scope']==RESEARCH_PROCESS_TREE
    assert snapshot[0]['cleanup_state']=='UNCONFIRMED_RESEARCH_TREE'
    assert [e['id'] for e in snapshot if e['process_state']=='UNKNOWN']==[identity]
    assert runner._terminate_live_known_executions(store,{identity})==[]
    assert _row(store,identity)['status']=='RUNNING'


def test_read_only_v4_queue_projection_does_not_require_a_scope_column(store):
    from stagemesh.queue_visibility import queue_control_report
    identity=store.start_execution(task_id='T',claim_id=None,kind=ExecutionKind.REVIEW)
    _v4(store)
    assert queue_control_report(store)['active_executions'][0]['process_tree_scope'] is None
    assert _row(store,identity)['status']=='RUNNING'
    assert store.schema_version()==4
