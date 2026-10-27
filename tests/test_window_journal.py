import json

from stagemesh import window_journal as journal
from stagemesh.persistence import Store


def test_started_event_persists_observed_runtime_and_cannot_be_overridden(tmp_path, monkeypatch):
    observed = {"interpreter": "owned-python", "provenance": {"source_commit": "a" * 40, "source_dirty": False, "status": "VERIFIED"}}
    monkeypatch.setattr(journal, "runtime_evidence", lambda: observed)
    path = tmp_path / "journal.sqlite3"
    store = Store(path)
    store.migrate()
    journal.record_started(store, "window-1", {"mode": "local", "runtime": {"forged": True}, "window_id": "forged"})
    journal.record_task_context(store, "window-1", "task-1", {"stage": "IMPLEMENT"})
    store.close()

    reopened = Store(path)
    try:
        started = reopened.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", (journal.STARTED,)).fetchone()
        payload = json.loads(started["payload"])
        assert payload == {"window_id": "window-1", "mode": "local", "runtime": observed}
        assert journal.latest_task_context(reopened, "task-1")["window_id"] == payload["window_id"]
        assert journal.last_window(reopened)["runtime"] == observed
    finally:
        reopened.close()
