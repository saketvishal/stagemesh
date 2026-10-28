from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stagemesh.domain import ProcessIdentity
from stagemesh.execution import kill_process_tree
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process, process_identity
from stagemesh.run_ready import run_ready

_CHILD = r'''
import json,os,subprocess,sys,time
from dataclasses import asdict
from pathlib import Path
from test_research_stage import rig
from stagemesh import providers
from stagemesh.domain import Stage
from stagemesh.process_identity import process_identity
from stagemesh.providers import ResearchRole,RuntimeCommandAdapter
root=Path(sys.argv[1]);phase=sys.argv[2]
project,bound,store,_=rig.__wrapped__(root)
from stagemesh.contract_binding import bind_task_contract
runtime=project/'.stagemesh';(runtime/'contracts').mkdir(parents=True)
(runtime/'check-service.py').write_text("from pathlib import Path\nassert Path('src/service.py').is_file()\n")
(runtime/'contracts/t1.json').write_text(json.dumps({'objective':'controlled researcher ownership','allowed_files':['src/service.py'],'required_tests':[{'name':'service-exists','command':[sys.executable,'.stagemesh/check-service.py']}]}))
bound=bind_task_contract(store,project,'t1',bound.baseline_sha)
store.set_task_baseline('t1',bound.baseline_sha)
store.advance_task('t1',Stage.IMPLEMENT)
claim=store.acquire_claim('t1','controlled-research-parent')
marker=root/'researcher-started'
code='import pathlib,time\n'+f'pathlib.Path({str(marker)!r}).write_text("started")\n'+'time.sleep(90)\n'
command=(sys.executable,'-c',code)
role=ResearchRole('controlled-researcher',command)
adapter=RuntimeCommandAdapter('unused-implementation',('unused-no-provider',),research=role,timeout_seconds=120)
(root/'case.json').write_text(json.dumps({'project':str(project),'database':str(store.db_path),'task_id':'t1','claim_id':claim,'owner':asdict(process_identity(os.getpid()))}))
spawn=providers.subprocess.Popen;identity=providers.popen_identity
def before(argv,*args,**kwargs):
    if phase=='before_launch' and len(argv)>=3 and argv[1]=='-c' and 'researcher-started' in argv[2]:
        (root/'witness.json').write_text(json.dumps({'child':None}))
        time.sleep(90)
    return spawn(argv,*args,**kwargs)
def after(proc):
    saved=identity(proc)
    if phase=='after_launch':
        (root/'witness.json').write_text(json.dumps({'child':asdict(saved)}))
        time.sleep(90)
    return saved
providers.subprocess.Popen=before
providers.popen_identity=after
adapter._run_research(store,'t1',project,bound)
'''


def _wait(predicate, timeout=25):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('controlled research parent did not reach crash point')


@pytest.mark.skipif(sys.platform!='win32',reason='native Windows exact-parent kill and researcher survival')
@pytest.mark.parametrize('phase',['before_launch','after_launch'])
def test_parent_loss_cannot_release_unconfirmed_research_tree(tmp_path,phase):
    from test_validation_crash_fence import _cleanup_gate

    from stagemesh.gate_process import _terminate_windows_pid

    source=Path(__file__).resolve().parents[1]
    env={**os.environ,'PYTHONPATH':os.pathsep.join((str(source/'src'),str(source/'tests')))}
    parent=subprocess.Popen([sys.executable,'-c',_CHILD,str(tmp_path),phase],cwd=source,env=env,
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    store=None;child=None;owner=None
    calls=[]
    try:
        _wait(lambda:(tmp_path/'witness.json').exists() and (phase=='before_launch' or (tmp_path/'researcher-started').exists()))
        case=json.loads((tmp_path/'case.json').read_text(encoding='utf-8'))
        witness=json.loads((tmp_path/'witness.json').read_text(encoding='utf-8'))
        owner=ProcessIdentity(**case['owner'])
        child=ProcessIdentity(**witness['child']) if witness['child'] else None
        assert owner.is_known and classify_process(owner,process_identity(owner.pid))=='LIVE'
        if child:
            assert child.is_known and classify_process(child,process_identity(child.pid))=='LIVE'
        store=Store(Path(case['database']))
        row=next(iter(store.running_executions()))
        assert row['kind']=='REVIEW' and row['pid']==owner.pid and row['actor']=='controlled-researcher'
        assert row['process_tree_scope']=='research/v1'
        assert store.execution_process_identity(row['id']).matches(owner)
        execution_id=row['id']
        assert _terminate_windows_pid(owner.pid,owner.create_time)
        parent.wait(timeout=10)
        assert classify_process(owner,process_identity(owner.pid))=='DEAD'
        if child:
            assert classify_process(child,process_identity(child.pid))=='LIVE'
        store.close();store=Store(Path(case['database']))
        with store.conn:
            store.conn.execute('UPDATE claims SET lease_expires_at=0 WHERE id=?',(case['claim_id'],))
        class ReplacementReached(Exception):
            pass
        def forbidden(_target):
            calls.append(True)
            raise ReplacementReached('replacement admitted over unconfirmed researcher')
        try:
            summary=run_ready(store,Path(case['project']),forbidden,task_id='t1',max_steps=1)
        except ReplacementReached:
            summary=None
        row=store.conn.execute('SELECT * FROM executions WHERE id=?',(execution_id,)).fetchone()
        observed={'phase':phase,'row_status':row['status'],'replacement_factories':len(calls),
                  'researcher_live':bool(child and classify_process(child,process_identity(child.pid))=='LIVE')}
        assert row['status']=='RUNNING' and calls==[],observed
        assert summary is not None and summary.stop_reason=='REFUSED:active_execution' and not summary.started
        assert store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
    finally:
        if owner is None and (tmp_path/'case.json').exists():
            owner=ProcessIdentity(**json.loads((tmp_path/'case.json').read_text(encoding='utf-8'))['owner'])
        if parent.poll() is None:
            assert kill_process_tree(parent), 'owned research fixture tree cleanup unconfirmed'
        if owner and classify_process(owner,process_identity(owner.pid))=='LIVE':
            _terminate_windows_pid(owner.pid,owner.create_time)
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=15)
        if child:
            _cleanup_gate(child)
        if store:
            store.close()
