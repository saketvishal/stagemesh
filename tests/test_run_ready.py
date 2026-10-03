from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import stagemesh.cli as cli_module
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store

from test_bounded_execution import SLEEPER, TASK, _running_execution, _setup

FAKE_CONTRACT = {
    "objective": "fake task",
    "allowed_files": ["stagemesh-task-*.txt"],
    "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
}


def _project(tmp_path: Path, task_ids: list[str], contracts: list[str] | None = None) -> Path:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    backlog = {"objective": "o", "tasks": [{"id": t, "title": f"task {t}", "eligible": True, "state": "OPEN"} for t in task_ids]}
    (runtime / "backlog.json").write_text(json.dumps(backlog), encoding="utf-8")
    for task_id in task_ids if contracts is None else contracts:
        (runtime / "contracts" / f"{task_id}.json").write_text(json.dumps(FAKE_CONTRACT), encoding="utf-8")
    return project


def _run(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "run-ready", "--dry-run", "--json", *argv])
    return code, json.loads(out.getvalue())


def test_completes_a_fake_task_through_done(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    code, result = _run(project)
    assert code == 0, result
    assert result["stop_reason"] == "DONE" and result["succeeded"] is True
    assert result["task_id"] == "T-1" and result["final"]["stage"] == "DONE"
    steps = result["steps"]
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0]["previous"]["stage"] == "PLAN"
    assert result["message"] == ""
    assert steps[-1]["new"]["latest_validation"] == "PASSED" and steps[-1]["new"]["latest_review"] == "PASSED"
    assert steps[-1]["new"]["latest_candidate"]


def test_refuses_when_no_eligible_task(tmp_path: Path) -> None:
    code, result = _run(_project(tmp_path, []))
    assert code == 2 and result["stop_reason"] == "REFUSED:no_eligible_task" and result["started"] is False


def test_refuses_multiple_eligible_tasks_without_task_flag(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A-1", "B-1"])
    code, result = _run(project)
    assert code == 2 and result["stop_reason"] == "REFUSED:multiple_eligible_tasks"
    assert sorted(result["detail"]["eligible"]) == ["A-1", "B-1"]
    code, result = _run(project, "--task", "A-1")
    assert code == 0 and result["task_id"] == "A-1"
    other = Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("B-1")
    assert other["stage"] == "PLAN"  # the unselected task was never advanced


def test_refuses_task_without_contract(tmp_path: Path) -> None:
    code, result = _run(_project(tmp_path, ["T-1"], contracts=[]))
    assert code == 2 and result["stop_reason"] == "REFUSED:missing_contract"


def test_recovers_provably_dead_stale_claim_then_completes(tmp_path: Path) -> None:
    project, store = _setup(tmp_path, FAKE_CONTRACT)
    proc = subprocess.Popen(SLEEPER)
    execution_id = _running_execution(store, proc)
    proc.kill()
    proc.wait()
    store.close()

    code, result = _run(project, "--task", TASK)

    assert code == 0, result
    assert [r["execution_id"] for r in result["recovered"]] == [execution_id]
    assert result["stop_reason"] == "DONE"
    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert reopened.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "FAILED"


def test_does_not_recover_live_or_unknown_executions(tmp_path: Path) -> None:
    for live in (True, False):
        base = tmp_path / ("live" if live else "unknown")
        base.mkdir()
        project, store = _setup(base, FAKE_CONTRACT)
        proc = subprocess.Popen(SLEEPER) if live else None
        try:
            execution_id = _running_execution(store, proc)
            store.close()
            code, result = _run(project, "--task", TASK)
            assert code == 2 and result["stop_reason"] == "REFUSED:active_execution", result
            assert result["recovered"] == []
            reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
            assert reopened.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "RUNNING"
            assert reopened.get_task(TASK)["status"] == "CLAIMED"
            reopened.close()
        finally:
            if proc is not None:
                proc.kill()
                proc.wait()


def test_stops_at_max_steps_with_clear_reason(tmp_path: Path) -> None:
    code, result = _run(_project(tmp_path, ["T-1"]), "--max-steps", "2")
    assert code == 1
    assert result["stop_reason"] == "MAX_STEPS" and result["steps_run"] == 2
    assert result["final"]["stage"] != "DONE" and "2 steps" in result["message"]


def test_refuses_when_health_has_current_problems(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    other = store.upsert_task("blocked one", source="local", source_id="X-1")
    store.block_task(other)
    store.close()
    code, result = _run(project, "--task", "T-1")
    assert code == 2 and result["stop_reason"] == "REFUSED:current_problems"
    assert "blocked_tasks" in result["detail"]["problems"]


def _continue(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", *argv])
    return code, json.loads(out.getvalue())


def test_continue_defaults_to_supervised_run_to_completion(tmp_path: Path) -> None:
    code, result = _continue(_project(tmp_path, ["T-1"]))
    assert code == 0 and result["stop_reason"] == "DONE" and result["final"]["stage"] == "DONE"
    assert result["steps_run"] >= 5


def test_continue_task_supervises_only_that_task_to_done(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A-1", "B-1"])
    code, result = _continue(project, "--task", "A-1")
    assert code == 0 and result["task_id"] == "A-1" and result["stop_reason"] == "DONE"
    assert Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("B-1") is None  # targeted sync skips it


def test_continue_default_refuses_ambiguous_selection(tmp_path: Path) -> None:
    code, result = _continue(_project(tmp_path, ["A-1", "B-1"]))
    assert code == 2 and result["stop_reason"] == "REFUSED:multiple_eligible_tasks"


def test_continue_once_keeps_single_tick_behavior(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    code, result = _continue(project, "--once", "--task", "T-1")
    assert code == 0
    assert "stop_reason" not in result and result["progressed"] == 1  # legacy summary shape: exactly one tick
    assert result["targeted_task_id"] == "T-1"
    assert Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("T-1")["stage"] == "IMPLEMENT"
