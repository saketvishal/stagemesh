"""Real dry-run CLI regression: a source sync that drops the selected task's label after selection must not start any stage."""
from __future__ import annotations

import importlib
import json

import pytest
from test_run_ready import _project
from test_task_selection import _continue

from stagemesh.auto_plan import task_labels
from stagemesh.execution import FakeExecutor
from stagemesh.parallel import ParallelRunner
from stagemesh.persistence import Store


@pytest.mark.parametrize("mode", ["serial", "parallel"])
def test_selected_label_removed_before_stage_dispatch_never_invokes_executor(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("stagemesh.config.detect_github_repository", lambda _project: None)
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "private-slot.lock"))
    project = _project(tmp_path, ["T-1"], labels={"T-1": ["focus"]})
    changed: list[str] = []
    calls: list[dict] = []

    def remove_label(path) -> None:
        other = Store(path)  # second connection, as a concurrent source sync would use
        try:
            task = other.get_task("T-1")
            assert task_labels(other, task) == ("focus",)
            row = other.conn.execute(
                "SELECT state FROM source_cache WHERE source=? AND source_id=?", (task["source"], task["source_id"])
            ).fetchone()
            state = json.loads(row["state"])
            state["labels"] = ["other"]
            other.conn.execute(
                "UPDATE source_cache SET state=? WHERE source=? AND source_id=?",
                (json.dumps(state), task["source"], task["source_id"]),
            )
            other.conn.commit()
            changed.append(str(task["stage"]))
        finally:
            other.close()

    original_run = FakeExecutor.run

    def record(self, store, task_id, *args, **kwargs):
        calls.append({"task": task_id, "labels": task_labels(store, store.get_task(task_id))})
        return original_run(self, store, task_id, *args, **kwargs)

    monkeypatch.setattr(FakeExecutor, "run", record)
    if mode == "serial":
        module = importlib.import_module("stagemesh.run_ready")
        original_choose = module.choose_task

        def choose(store, *args, **kwargs):
            result = original_choose(store, *args, **kwargs)
            if not changed:
                remove_label(store.db_path)
            return result

        monkeypatch.setattr(module, "choose_task", choose)
        code, result = _continue(project, "--label", "focus")
    else:
        original_select = ParallelRunner._select_batch

        def select(self, *args, **kwargs):
            result = original_select(self, *args, **kwargs)
            if result and not changed:
                remove_label(self.store.db_path)
            return result

        monkeypatch.setattr(ParallelRunner, "_select_batch", select)
        code, result = _continue(project, "--label", "focus", "--parallel", "2")
    store = Store(project / ".stagemesh/stagemesh.sqlite3")
    try:
        final = dict(store.get_task("T-1"))
    finally:
        store.close()
    assert changed == ["PLAN"], changed
    assert calls == [], {"mode": mode, "executor_invocations": calls, "code": code, "stop": result.get("stop_reason"), "task_stage": final["stage"]}
    assert final["stage"] == "PLAN" and final["status"] == "OPEN"
