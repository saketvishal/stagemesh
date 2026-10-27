from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stagemesh.domain import ProcessIdentity
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process, process_identity
from stagemesh.run_ready import run_ready

_CHILD = r'''
import json,os,sys,time
from dataclasses import asdict
from pathlib import Path
from test_bounded_execution import TASK,_setup
from test_operator_workflow import _commit
from stagemesh import gate_process
from stagemesh.domain import Stage
from stagemesh.git import GitWorkspace
from stagemesh.validation import Validator
root=Path(sys.argv[1]);phase=sys.argv[2]
marker=root/'gate-started'
code='import pathlib,time\n'+f'pathlib.Path({str(marker)!r}).write_text("started")\n'+'time.sleep(60)\n'
project,store=_setup(root,{'objective':'controlled crash','allowed_files':['docs/**'],'required_tests':[{'name':'controlled','command':[sys.executable,'-c',code],'timeout_seconds':60}]})
store.set_task_baseline(TASK,GitWorkspace(project).head())
sha=_commit(project,{'docs/a.md':'candidate'})
store.add_candidate(TASK,sha,'controlled',True);store.advance_task(TASK,Stage.VALIDATE)
(root/'case.json').write_text(json.dumps({'project':str(project),'task_id':TASK,'candidate':sha,'owner_pid':os.getpid()}))
spawn=gate_process.spawn_gate_process

def witness(command,cwd,env):
    data={'cwd':str(cwd),'temp':env['TMP'],'identity':None}
    if phase=='before_launch':
        (root/'witness.json').write_text(json.dumps(data));time.sleep(60)
    proc,identity=spawn(command,cwd,env)
    data['identity']=asdict(identity)
    (root/'witness.json').write_text(json.dumps(data))
    return proc,identity

gate_process.spawn_gate_process=witness
Validator().validate(store,TASK,sha,project)
'''


def _wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('controlled parent/gate did not reach crash point')


def _cleanup_gate(saved):
    from stagemesh.gate_process import _terminate_windows_descendants, _terminate_windows_pid
    if classify_process(saved, process_identity(saved.pid)) == 'LIVE':
        assert _terminate_windows_descendants(saved.pid, saved.create_time)
        if classify_process(saved, process_identity(saved.pid)) == 'LIVE':
            assert _terminate_windows_pid(saved.pid, saved.create_time)
    assert classify_process(saved, process_identity(saved.pid)) == 'DEAD'


@pytest.mark.skipif(sys.platform != 'win32', reason='native Windows parent-only process kill and child survival')
@pytest.mark.parametrize('phase', ['before_launch', 'active_gate'])
def test_parent_death_cannot_release_unverified_validation_tree(tmp_path, phase):
    from stagemesh.gate_process import _terminate_windows_pid
    source = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(source/'src'), str(source/'tests'))),
           'STAGEMESH_VALIDATION_SLOT': str(tmp_path/'private-slot.lock')}
    parent = subprocess.Popen([sys.executable, '-c', _CHILD, str(tmp_path), phase], cwd=source,
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    store = None
    witness = None
    saved = None
    admissions = []
    try:
        _wait(lambda: (tmp_path/'witness.json').exists() and (phase=='before_launch' or (tmp_path/'gate-started').exists()))
        witness = json.loads((tmp_path/'witness.json').read_text())
        case = json.loads((tmp_path/'case.json').read_text())
        project = Path(case['project'])
        store = Store(project/'.stagemesh/stagemesh.sqlite3')
        row = next(iter(store.running_executions()))
        owner = store.execution_process_identity(row['id'])
        assert owner.is_known and owner.pid == case['owner_pid']
        assert classify_process(owner, process_identity(owner.pid)) == 'LIVE'
        saved = ProcessIdentity(**witness['identity']) if witness['identity'] else None
        if saved:
            assert saved.is_known and classify_process(saved, process_identity(saved.pid)) == 'LIVE'
        assert _terminate_windows_pid(owner.pid, owner.create_time)  # exact validator parent, not its tree or venv launcher
        parent.wait(timeout=10)
        assert classify_process(owner, process_identity(owner.pid)) == 'DEAD'
        if saved:
            assert classify_process(saved, process_identity(saved.pid)) == 'LIVE'
        store.close()
        store = Store(project/'.stagemesh/stagemesh.sqlite3')
        class AdmissionReached(Exception):
            pass
        def forbidden(_target):
            admissions.append(True)
            raise AdmissionReached('a new coordinator was admitted over the old gate')
        try:
            summary = run_ready(store, project, forbidden, task_id=case['task_id'], max_steps=1)
        except AdmissionReached:
            summary = None
        row = store.conn.execute('SELECT * FROM executions ORDER BY started_at LIMIT 1').fetchone()
        observed = {'phase':phase, 'row_status':row['status'], 'factory_calls':len(admissions),
                    'old_gate_still_live':saved is not None and classify_process(saved,process_identity(saved.pid))=='LIVE'}
        assert row['status']=='RUNNING' and admissions==[], observed
        assert summary is not None and summary.stop_reason=='REFUSED:active_execution' and not summary.started
        assert store.latest_candidate(case['task_id'])['sha']==case['candidate']
        assert store.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0]==0
        assert Path(witness['cwd']).is_dir() and Path(witness['temp']).is_dir()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if saved:
            _cleanup_gate(saved)
        if store is not None:
            store.close()
        if witness:
            checkout = Path(witness['cwd']).resolve()
            temp = Path(witness['temp']).resolve()
            assert checkout.name=='checkout' and checkout.parent.name.startswith('stagemesh-candidate-')
            assert temp.name.startswith('stagemesh-gate-')
            project = Path(json.loads((tmp_path/'case.json').read_text())['project'])
            GitWorkspace(project).run('worktree','remove','--force',str(checkout))
            shutil.rmtree(checkout.parent)
            shutil.rmtree(temp)
