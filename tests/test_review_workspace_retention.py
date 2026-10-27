import json
import sys

import pytest
from test_review_terminalization import TASK, _project_with_candidate

from stagemesh import providers
from stagemesh.execution import kill_process_tree
from stagemesh.providers import RuntimeCommandAdapter, RuntimeReviewAdapter
from stagemesh.queue_control import OperatorStopRequested
from stagemesh.review import Reviewer


@pytest.mark.parametrize("outcome", ["unconfirmed", "confirmed_stop", "completed", "start_failed", "transport_error", "start_interrupted"])
def test_review_checkout_is_retained_until_provider_cleanup_is_confirmed(tmp_path, monkeypatch, outcome):
    project, store, candidate = _project_with_candidate(tmp_path)
    temp_dir = tmp_path / "review-runtime"
    temp_dir.mkdir()
    review_path = temp_dir / "candidate"
    started = []
    real_popen = providers.subprocess.Popen

    def popen(command, *args, **kwargs):
        if command[0] == sys.executable:
            if outcome == "start_failed":
                raise FileNotFoundError("fixture executable disappeared")
            proc = real_popen(command, *args, **kwargs)
            started.append(proc)
            if outcome == "start_interrupted":
                raise KeyboardInterrupt("fixture interrupted after child creation")
            return proc
        return real_popen(command, *args, **kwargs)

    def communicate(proc, *args):
        assert review_path.exists()
        assert kill_process_tree(proc)
        proc.communicate(timeout=5)
        if outcome == "transport_error":
            raise OSError("fixture transport lost cleanup status")
        if outcome != "completed":
            raise OperatorStopRequested("fixture operator stop", cleanup_complete=outcome == "confirmed_stop", monitor_failed=True)
        return '{"decision":"PASS"}', "", False

    monkeypatch.setattr(providers.tempfile, "mkdtemp", lambda **kwargs: str(temp_dir))
    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    monkeypatch.setattr(providers, "communicate_bounded", communicate)
    adapter = RuntimeCommandAdapter("stand-in", (sys.executable, "-c", "import time; time.sleep(120)"))
    review = Reviewer(adapter=RuntimeReviewAdapter(adapter, project, candidate), require_independent=True)
    incomplete = outcome in {"unconfirmed", "transport_error", "start_interrupted"}
    try:
        if outcome in {"unconfirmed", "confirmed_stop", "transport_error", "start_interrupted"}:
            with pytest.raises(OperatorStopRequested) as caught:
                review.review(store, TASK, candidate, project)
            assert caught.value.cleanup_complete is not incomplete
            if incomplete:
                assert review_path.exists(), "cleanup deleted a workspace still owned by an unconfirmed process tree"
                assert str(review_path) in str(caught.value)
                assert caught.value.retained_workspace == str(review_path)
            if outcome == "unconfirmed":
                assert caught.value.monitor_failed
        else:
            review.review(store, TASK, candidate, project)
        row = store.conn.execute("SELECT * FROM executions WHERE kind='REVIEW' ORDER BY started_at DESC LIMIT 1").fetchone()
        if incomplete:
            assert row["status"] == "RUNNING" and row["result"] == "cleanup_incomplete"
            assert row["process_create_time"] is None
            assert (review_path / "README.md").read_text().strip() == "changed"
            events = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='review.workspace_retained'").fetchall()
            assert [json.loads(event[0])["path"] for event in events] == [str(review_path)]
        else:
            assert row["status"] != "RUNNING"
            assert not temp_dir.exists()
    finally:
        for proc in started:
            if proc.poll() is None:
                kill_process_tree(proc)
            proc.communicate(timeout=5)
        store.close()


def test_long_retained_workspace_path_does_not_break_durable_review_fencing(tmp_path):
    project, store, candidate = _project_with_candidate(tmp_path)
    retained = "C:/" + "nested/" * 50 + "candidate"

    class Adapter:
        name = "stand-in"

        def review(self, prompt):
            raise OperatorStopRequested(
                "workspace retained at " + retained, cleanup_complete=False, retained_workspace=retained,
            )

    try:
        with pytest.raises(OperatorStopRequested):
            Reviewer(adapter=Adapter(), require_independent=True).review(store, TASK, candidate, project)
        row = store.conn.execute("SELECT * FROM executions WHERE kind='REVIEW'").fetchone()
        assert row["status"] == "RUNNING" and row["result"] == "cleanup_incomplete"
        events = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='review.workspace_retained'").fetchall()
        assert json.loads(events[0][0])["path"] == retained
    finally:
        store.close()
