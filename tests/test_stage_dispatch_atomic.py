from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stagemesh.execution import kill_process_tree, popen_session_kwargs
from stagemesh.persistence import Store

_WORKER = r"""
import json,sys,time
from pathlib import Path
from stagemesh.coordinator import Coordinator,TargetSelection
from stagemesh.domain import EvidenceStatus
from stagemesh.persistence import Store
project,markers,name,task_id=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3],sys.argv[4]
store=Store(project/'.stagemesh/stagemesh.sqlite3')
class Recorder:
    def validate(self,*args,**kwargs):
        (markers/(name+'.called')).write_text('admitted')
        return EvidenceStatus.UNKNOWN
coordinator=Coordinator(store,project,validator=Recorder(),integrator=object(),target=TargetSelection(task_id))
check=coordinator._workspace_intact
def gap(*args,**kwargs):
    checked=check(*args,**kwargs)
    if checked and args[2]=='VALIDATE:before_validation':
        (markers/(name+'.gap')).write_text('checked; no execution row yet')
        deadline=time.monotonic()+45
        while not (markers/'release').exists():
            if time.monotonic()>=deadline:raise RuntimeError('controlled gap expired')
            time.sleep(.02)
    return checked
coordinator._workspace_intact=gap
(markers/(name+'.attempt')).write_text('attempting')
progress=coordinator._advance_task(task_id)
(markers/(name+'.result')).write_text(json.dumps({'progress':progress,'refusals':getattr(coordinator,'last_execution_refusals',[])}))
store.close()
"""


def _wait(predicate, timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('controlled dispatcher did not reach expected point')


def test_two_processes_cannot_both_dispatch_before_execution_publication(tmp_path,monkeypatch):
    from test_stage_admission_fence import TASK, _prepared
    monkeypatch.setenv('STAGEMESH_VALIDATION_SLOT',str(tmp_path/'private-slot.lock'))
    rig=_prepared(tmp_path/'setup')
    candidate=rig.store.latest_candidate(TASK)['sha']
    rig.store.close()
    markers=tmp_path/'markers';markers.mkdir()
    source=Path(__file__).resolve().parents[1]
    environment={**os.environ,'PYTHONPATH':os.pathsep.join((str(source/'src'),str(source/'tests')))}
    workers=[]
    def start(name):
        proc=subprocess.Popen([sys.executable,'-c',_WORKER,str(rig.project),str(markers),name,TASK],
                              cwd=source,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                              **popen_session_kwargs())
        workers.append(proc)
        return proc
    try:
        start('one')
        _wait(lambda:(markers/'one.gap').exists())
        inspected=Store(rig.project/'.stagemesh/stagemesh.sqlite3')
        try:
            assert inspected.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0]==0
        finally:inspected.close()
        start('two')
        _wait(lambda:(markers/'two.gap').exists() or (markers/'two.result').exists())
        # Release only after the second process crossed the old race or refused.
        (markers/'release').touch()
        outputs=[proc.communicate(timeout=30) for proc in workers]
        assert all(proc.returncode==0 for proc in workers),outputs
        called=sorted(path.stem for path in markers.glob('*.called'))
        observed={'called':called,'second_crossed_gap':(markers/'two.gap').exists()}
        assert called==['one'],observed
        result=json.loads((markers/'two.result').read_text())
        assert result['progress']==0 and len(result['refusals'])==1
        assert result['refusals'][0]['reason_code']=='stage_dispatch_unavailable'
        assert not (markers/'two.gap').exists()
        store=Store(rig.project/'.stagemesh/stagemesh.sqlite3')
        try:
            assert store.latest_candidate(TASK)['sha']==candidate
            assert store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
            assert store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0]==0
        finally:store.close()
    finally:
        (markers/'release').touch()
        for proc in workers:
            try:proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc);proc.communicate(timeout=10)


def test_dispatch_key_is_boundary_safe_and_different_tasks_and_databases_are_independent(tmp_path):
    from stagemesh.stage_dispatch import stage_dispatch_lock
    first=stage_dispatch_lock(tmp_path/'a'/'store.sqlite3','../../task')
    assert first.path.parent==(tmp_path/'a'/'store.sqlite3.stage-locks').resolve()
    assert first.path.name.endswith('.lock') and len(first.path.stem)==64
    assert stage_dispatch_lock(tmp_path/'a'/'sub'/'..'/'store.sqlite3','../../task').path==first.path
    assert stage_dispatch_lock(tmp_path/'a'/'store.sqlite3','other').path!=first.path
    assert stage_dispatch_lock(tmp_path/'b'/'store.sqlite3','../../task').path!=first.path


def test_distinct_handles_and_threads_cannot_reenter_the_same_dispatch_resource(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from stagemesh.concurrency import IntegrationLockTimeout
    from stagemesh.stage_dispatch import stage_dispatch_lock
    database=tmp_path/'store.sqlite3'
    def attempt():
        try:
            with stage_dispatch_lock(database,'one').hold('second-thread'):
                return True
        except IntegrationLockTimeout:
            return False
    with stage_dispatch_lock(database,'one').hold('first-thread'):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(attempt).result(timeout=5) is False
        with stage_dispatch_lock(database,'other').hold('different-task'):
            pass
    assert attempt() is True


def test_busy_dispatch_does_not_stop_an_unrelated_task_or_fabricate_execution(tmp_path):
    from test_operator_workflow import TASK, _with_baseline

    from stagemesh.coordinator import Coordinator
    from stagemesh.domain import Stage
    from stagemesh.stage_dispatch import stage_dispatch_lock
    project,store,_=_with_baseline(tmp_path)
    other=store.upsert_task('independent',source_id='OTHER')
    original_stage=store.get_task(TASK)['stage']
    coordinator=Coordinator(store,project)
    try:
        with stage_dispatch_lock(store.db_path,TASK).hold('controlled-owner'):
            assert coordinator._advance_task(TASK)==0
            assert coordinator._advance_task(other)==1
            assert store.get_task(other)['stage']==Stage.IMPLEMENT
            assert store.get_task(TASK)['stage']==original_stage
            assert coordinator.last_execution_refusals[0]['execution_ids']==[]
            assert store.conn.execute('SELECT COUNT(*) FROM executions').fetchone()[0]==0
            assert store.conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0]==0
            assert store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
    finally:store.close()


def test_stage_exception_releases_dispatch_and_inner_integration_timeout_is_not_hidden(tmp_path,monkeypatch):
    from test_operator_workflow import _with_baseline

    from stagemesh.concurrency import IntegrationLockTimeout
    from stagemesh.coordinator import Coordinator
    project,store,_=_with_baseline(tmp_path)
    coordinator=Coordinator(store,project)
    try:
        def fail(_task):raise IntegrationLockTimeout('actual-stage-timeout')
        monkeypatch.setattr(coordinator,'_advance_task_owned',fail)
        with pytest.raises(IntegrationLockTimeout,match='actual-stage-timeout'):
            coordinator._advance_task('controlled-task')
        assert getattr(coordinator,'last_execution_refusals',[])==[]
        monkeypatch.setattr(coordinator,'_advance_task_owned',lambda _task:7)
        assert coordinator._advance_task('controlled-task')==7
    finally:store.close()


def test_owned_prepublication_process_death_releases_dispatch_without_releasing_other_authority(tmp_path,monkeypatch):
    from test_stage_admission_fence import TASK, _prepared
    monkeypatch.setenv('STAGEMESH_VALIDATION_SLOT',str(tmp_path/'private-slot.lock'))
    rig=_prepared(tmp_path/'setup');rig.store.close()
    markers=tmp_path/'markers';markers.mkdir()
    source=Path(__file__).resolve().parents[1]
    environment={**os.environ,'PYTHONPATH':os.pathsep.join((str(source/'src'),str(source/'tests')))}
    workers=[]
    def start(name):
        proc=subprocess.Popen([sys.executable,'-c',_WORKER,str(rig.project),str(markers),name,TASK],
                              cwd=source,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                              **popen_session_kwargs())
        workers.append(proc);return proc
    try:
        first=start('one');_wait(lambda:(markers/'one.gap').exists())
        assert not (markers/'one.called').exists()
        kill_process_tree(first);first.communicate(timeout=10)
        assert first.poll() is not None
        # No child or execution was launched at this controlled prepublication point.
        store=Store(rig.project/'.stagemesh/stagemesh.sqlite3')
        try:assert store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0]==0
        finally:store.close()
        (markers/'release').touch()
        second=start('two');output=second.communicate(timeout=30)
        assert second.returncode==0,output
        assert (markers/'two.called').exists() and not (markers/'one.called').exists()
        assert json.loads((markers/'two.result').read_text())['refusals']==[]
    finally:
        (markers/'release').touch()
        for proc in workers:
            try:proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc);proc.communicate(timeout=10)


@pytest.mark.parametrize("change",["blocked","done","dependency","source_ineligible","source_pending"])
def test_eligibility_is_rechecked_after_native_admission_before_stage_dispatch(tmp_path,monkeypatch,change):
    from contextlib import contextmanager

    from test_stage_admission_fence import TASK, _prepared

    from stagemesh import coordinator as module
    from stagemesh.domain import EvidenceStatus
    from stagemesh.scheduling import Scheduler
    monkeypatch.setenv('STAGEMESH_VALIDATION_SLOT',str(tmp_path/'private-slot.lock'))
    rig=_prepared(tmp_path/'setup')
    rig.store.conn.execute("UPDATE tasks SET source='github' WHERE id=?",(TASK,));rig.store.conn.commit()
    rig.store.cache_source('github',TASK,{'state':'OPEN','eligible':True},'OPEN')
    assert Scheduler(rig.store).decision(TASK).eligible
    calls=[];published=[]
    class Recorder:
        def validate(self,*_args,**_kwargs):calls.append(True);return EvidenceStatus.UNKNOWN
    rig.coordinator.validator=Recorder()
    original=module.stage_dispatch_lock
    def selected_before_publication(database,task):
        native=original(database,task)
        class Controlled:
            @contextmanager
            def hold(self,owner):
                with native.hold(owner):
                    writer=Store(database)
                    try:
                        if change=='blocked':writer.block_task(TASK)
                        elif change=='done':
                            writer.conn.execute("UPDATE tasks SET status='DONE' WHERE id=?",(TASK,));writer.conn.commit()
                        elif change=='dependency':writer.add_dependency(TASK,writer.upsert_task('blocker',source_id='BLOCKER'))
                        else:
                            state={'state':'OPEN','eligible':change!='source_ineligible'}
                            if change=='source_pending':state['admission_pending']=False
                            writer.cache_source('github',TASK,state,'OPEN')
                        published.append(tuple(writer.get_task(TASK)))
                    finally:writer.close()
                    yield
        return Controlled()
    monkeypatch.setattr(module,'stage_dispatch_lock',selected_before_publication)
    try:
        assert rig.coordinator._advance_task(TASK)==0
        assert calls==[]
        assert tuple(rig.store.get_task(TASK))==published[0]
        assert rig.store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
        refusal,=rig.coordinator.last_execution_refusals
        assert refusal['reason_code']=='stage_dispatch_ineligible'
        assert Scheduler(rig.store).decision(TASK).reason in refusal['reason']
    finally:rig.store.close()


def test_unavailable_anchor_is_an_explicit_refusal_without_stage_or_store_mutation(tmp_path):
    from test_operator_workflow import TASK, _with_baseline

    from stagemesh.coordinator import Coordinator
    from stagemesh.stage_dispatch import stage_dispatch_lock
    project,store,_=_with_baseline(tmp_path)
    anchor=stage_dispatch_lock(store.db_path,TASK).path.parent
    anchor.write_text('not a directory')
    before=tuple(store.get_task(TASK))
    try:
        coordinator=Coordinator(store,project)
        assert coordinator._advance_task(TASK)==0
        assert tuple(store.get_task(TASK))==before
        assert coordinator.last_execution_refusals[0]['reason_code']=='stage_dispatch_unavailable'
        assert store.conn.execute('SELECT COUNT(*) FROM executions').fetchone()[0]==0
    finally:store.close()


def test_inner_oserror_is_not_misreported_as_an_admission_refusal(tmp_path,monkeypatch):
    from test_operator_workflow import _with_baseline

    from stagemesh.coordinator import Coordinator
    project,store,_=_with_baseline(tmp_path);coordinator=Coordinator(store,project)
    def fail(_task):raise OSError('stage-internal-error')
    monkeypatch.setattr(coordinator,'_advance_task_owned',fail)
    try:
        with pytest.raises(OSError,match='stage-internal-error'):coordinator._advance_task('controlled-task')
        assert getattr(coordinator,'last_execution_refusals',[])==[]
    finally:store.close()


def test_private_memory_store_keeps_its_connection_local_behavior_without_invalid_native_filename(tmp_path):
    from test_operator_workflow import _with_baseline

    from stagemesh.coordinator import Coordinator
    from stagemesh.domain import Stage
    project,disk,_=_with_baseline(tmp_path);disk.close()
    memory=Store(Path(':memory:'));memory.migrate()
    task=memory.upsert_task('memory-only',source_id='MEMORY')
    try:
        assert Coordinator(memory,project)._advance_task(task)==1
        assert memory.get_task(task)['stage']==Stage.IMPLEMENT
    finally:memory.close()


@pytest.mark.parametrize("entrypoint",["drive","cli_json","cli_text"])
@pytest.mark.parametrize("reason",["stage_dispatch_unavailable","stage_dispatch_ineligible"])
def test_public_dispatch_refusal_does_not_claim_an_existing_execution(tmp_path,monkeypatch,entrypoint,reason):
    import contextlib
    import io

    from test_stage_admission_fence import TASK, _prepared

    from stagemesh import cli
    from stagemesh import coordinator as module
    from stagemesh.run_ready import RunSummary, drive_task
    rig=_prepared(tmp_path/'setup')
    original=module.stage_dispatch_lock
    @contextlib.contextmanager
    def situation():
        if reason=='stage_dispatch_unavailable':
            with original(rig.store.db_path,TASK).hold('controlled-dispatch'):
                yield
        else:
            class Changed:
                @contextlib.contextmanager
                def hold(self,owner):
                    with original(rig.store.db_path,TASK).hold(owner):
                        writer=Store(rig.store.db_path)
                        try:writer.block_task(TASK)
                        finally:writer.close()
                        yield
            monkeypatch.setattr(module,'stage_dispatch_lock',lambda *_:Changed())
            yield
    try:
        with situation():
            if entrypoint=='drive':
                rig.coordinator.require_independent_review=False
                summary=RunSummary(True,'RUNNING',task_id=TASK)
                drive_task(rig.store,rig.project,rig.coordinator,TASK,summary,max_steps=1)
                assert summary.stop_reason=='REFUSED:'+reason
                message=summary.message
            else:
                out,err=io.StringIO(),io.StringIO()
                argv=['--project',str(rig.project),'continue','--once','--task',TASK]
                if entrypoint=='cli_json':argv.append('--json')
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                    code=cli.main(argv)
                assert code==2,(out.getvalue(),err.getvalue())
                if entrypoint=='cli_json':
                    data=json.loads(out.getvalue())
                    assert data['stop_reason']=='REFUSED:'+reason
                    message=data['message']
                else:message=out.getvalue()+err.getvalue()
            assert 'existing stage' not in message.lower()
            assert ('lock' if reason=='stage_dispatch_unavailable' else 'eligible') in message.lower()
        assert rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0]==0
        assert rig.store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
    finally:rig.store.close()


def test_mixed_public_refusals_keep_per_task_causes_without_claiming_all_have_execution_owners(tmp_path):
    import contextlib
    import io

    from test_stage_admission_fence import TASK, _prepared

    from stagemesh import cli
    from stagemesh.domain import ExecutionKind
    from stagemesh.stage_dispatch import stage_dispatch_lock
    rig=_prepared(tmp_path/'setup')
    other=rig.store.upsert_task('other retained work',source_id='OTHER')
    retained=rig.store.start_execution(task_id=other,claim_id=None,kind=ExecutionKind.VALIDATION)
    try:
        out=io.StringIO()
        with stage_dispatch_lock(rig.store.db_path,TASK).hold('controlled-dispatch'),contextlib.redirect_stdout(out):
            # Exercise real dispatch/ownership without requiring any installed provider CLI.
            code=cli.main(['--project',str(rig.project),'continue','--once','--json','--dry-run'])
        data=json.loads(out.getvalue())
        assert code==2 and data['progressed']==0 and data['stop_reason']=='REFUSED:stage_dispatch'
        assert 'existing stage ownership' not in data['message']
        by_task={entry['task_id']:entry for entry in data['active_execution_refusals']}
        assert by_task[TASK]['reason_code']=='stage_dispatch_unavailable' and by_task[TASK]['execution_ids']==[]
        assert by_task[other]['execution_ids']==[retained]
        assert rig.store.conn.execute('SELECT status FROM executions WHERE id=?',(retained,)).fetchone()[0]=='RUNNING'
        assert rig.store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
    finally:rig.store.close()
