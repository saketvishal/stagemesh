from pathlib import Path

from stagemesh.coordinator import Coordinator
from stagemesh.persistence import Store
from stagemesh.stage_dispatch import stage_dispatch_lock


def test_dispatch_uses_the_connected_database_after_working_directory_changes(tmp_path, monkeypatch):
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(original)
    store = Store(Path("queue.sqlite3"))
    store.migrate()
    task = store.upsert_task("controlled", source_id="ONE")
    coordinator = Coordinator(store, original)
    calls = []
    monkeypatch.setattr(coordinator, "_advance_task_owned", lambda task_id: calls.append(task_id) or 7)
    try:
        with stage_dispatch_lock(original / "queue.sqlite3", task).hold("existing-dispatch"):
            monkeypatch.chdir(other)
            assert coordinator._advance_task(task) == 0
        assert calls == []
        assert coordinator.last_execution_refusals[0]["reason_code"] == "stage_dispatch_unavailable"
        assert not (other / "queue.sqlite3.stage-locks").exists()
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert store.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    finally:
        store.close()
        monkeypatch.chdir(tmp_path)


def test_missing_malformed_or_failed_database_identity_never_becomes_memory_admission():
    import sqlite3

    import pytest

    from stagemesh.stage_dispatch import connected_dispatch_database
    class Result:
        def __init__(self, rows):self.rows=rows
        def fetchall(self):return self.rows
    class Connection:
        def __init__(self, rows):self.rows=rows
        def execute(self, query):
            assert query=="PRAGMA database_list"
            return Result(self.rows)
    bad_rows=[[],[(0,"temp","")],[(0,"main",None)],[(0,"main","relative.sqlite3")],
              [(0,"main","bad\0name")],[(0,"main","")]*2,[()]]
    for rows in bad_rows:
        with pytest.raises(OSError,match="database identity"):
            connected_dispatch_database(Connection(rows))
    class Unavailable:
        def execute(self, query):raise sqlite3.OperationalError("controlled unavailable database")
    with pytest.raises(OSError,match="database identity"):
        connected_dispatch_database(Unavailable())
    assert connected_dispatch_database(Connection([(0,"main","")])) is None


def test_identity_query_failure_is_an_explicit_public_dispatch_refusal(tmp_path,monkeypatch):
    from test_stage_admission_fence import TASK, _prepared

    from stagemesh import coordinator as module
    from stagemesh.run_ready import RunSummary, drive_task
    rig=_prepared(tmp_path/'setup')
    calls=[]
    def unavailable(_connection):raise OSError("cannot establish database identity")
    monkeypatch.setattr(module,"connected_dispatch_database",unavailable)
    monkeypatch.setattr(rig.coordinator,"_advance_task_owned",lambda task:calls.append(task) or 7)
    rig.coordinator.require_independent_review=False
    try:
        summary=RunSummary(True,"RUNNING",task_id=TASK)
        drive_task(rig.store,rig.project,rig.coordinator,TASK,summary,max_steps=1)
        assert summary.stop_reason=="REFUSED:stage_dispatch_unavailable" and calls==[]
        assert rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0]==0
    finally:rig.store.close()
