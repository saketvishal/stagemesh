from dataclasses import replace
from pathlib import Path

import pytest

from stagemesh import execution
from stagemesh.queue_control import OperatorStopRequested

pytest_plugins = ("test_research_stage",)


def test_research_timeout_with_lost_cleanup_proof_retains_running_identity(rig, monkeypatch):
    project, bound, store, build = rig
    adapter = replace(build("ok"), timeout_seconds=0.1)
    Path(adapter.research.command[1]).write_text("import time; time.sleep(120)\n", encoding="utf-8")
    real_kill = execution.kill_process_tree
    started, confirmations = [], []

    def unconfirmed(process):
        started.append(process)
        confirmations.append(real_kill(process))
        process.wait(timeout=5)
        return False

    monkeypatch.setattr(execution, "kill_process_tree", unconfirmed)
    try:
        with pytest.raises(OperatorStopRequested) as caught:
            adapter._run_research(store, "t1", project, bound)
        assert not caught.value.cleanup_complete
        assert confirmations == [True]
        retained = list(store.running_executions())
        assert len(retained) == 1 and retained[0]["actor"] == "researcher"
        assert retained[0]["result"] == "cleanup_incomplete"
        assert retained[0]["process_create_time"] is None
        assert store.latest_candidate("t1") is None
    finally:
        try:
            for process in started:
                if process.poll() is None:
                    real_kill(process)
                process.communicate(timeout=5)
        finally:
            store.close()
