from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
import stagemesh.persistence as persistence_module
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceStatus, ExecutionKind, ExecutionStatus, Stage
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.timing import (
    execution_timings,
    format_duration,
    format_execution,
    format_task_summary,
    task_timing,
)
from stagemesh.validation import Validator

TASK = "TASK-1"


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: float = 1000.0) -> tuple[Store, Clock]:
    clock = Clock(start)
    monkeypatch.setattr(persistence_module.time, "time", clock)
    store = Store(tmp_path / "s.sqlite3")
    store.migrate()
    store.upsert_task("timing", source_id=TASK)
    return store, clock


def _run(store: Store, clock: Clock, kind: ExecutionKind, seconds: float, status: ExecutionStatus, **kw: str) -> str:
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=kind, actor=kw.get("actor"))
    clock.now += seconds
    store.finish_execution(execution_id, status, kw.get("sha"), result=kw.get("result"))
    return execution_id


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (0.4, "0s"), (4, "4s"), (59.6, "1m 00s"), (68, "1m 08s"), (146, "2m 26s"), (494, "8m 14s"), (3789, "1h 03m"), (3600, "1h 00m"), (None, "-")],
)
def test_format_duration(seconds: float | None, text: str) -> None:
    assert format_duration(seconds) == text


def test_successful_lifecycle_timing_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(tmp_path, monkeypatch)
    _run(store, clock, ExecutionKind.IMPLEMENTATION, 494, ExecutionStatus.SUCCEEDED, actor="codex", sha="a" * 40)
    clock.now += 3600  # a human sits on the task: not execution time
    _run(store, clock, ExecutionKind.VALIDATION, 68, ExecutionStatus.SUCCEEDED, actor="stagemesh-validator", sha="a" * 40)
    _run(store, clock, ExecutionKind.REVIEW, 146, ExecutionStatus.SUCCEEDED, actor="claude", sha="a" * 40)
    _run(store, clock, ExecutionKind.INTEGRATION, 4, ExecutionStatus.SUCCEEDED, actor="stagemesh-integrator", sha="a" * 40)

    timing = task_timing(store, TASK)

    review = timing["executions"][2]
    assert (review["stage"], review["attempt"], review["actor"], review["result"]) == ("REVIEW", 1, "claude", "passed")
    assert review["candidate_sha"] == "a" * 40
    assert review["finished_at"] - review["started_at"] == pytest.approx(146)
    assert review["duration_seconds"] == 146
    assert timing["summary"]["total_execution_seconds"] == 494 + 68 + 146 + 4
    assert timing["summary"]["retries_seconds"] == 0
    assert format_task_summary(timing).splitlines() == [
        f"Task {TASK} timing",
        "  IMPLEMENT: 8m 14s",
        "  VALIDATE: 1m 08s",
        "  REVIEW: 2m 26s",
        "  INTEGRATE: 4s",
        "  retries/remediation: 0s",
        "  total execution time: 11m 52s",
    ]
    assert format_execution(review).splitlines() == ["Review #1", "  result: passed", "  actor: claude", "  duration: 2m 26s"]
    verbose = format_execution(review, verbose=True)
    assert f"task: {TASK}" in verbose and "started: " in verbose and "finished: " in verbose and "candidate: " in verbose


def test_failed_executions_are_timed_and_retries_are_separated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(tmp_path, monkeypatch)
    _run(store, clock, ExecutionKind.IMPLEMENTATION, 100, ExecutionStatus.SUCCEEDED, actor="codex", sha="b" * 40)
    _run(store, clock, ExecutionKind.VALIDATION, 30, ExecutionStatus.FAILED, actor="stagemesh-validator", sha="b" * 40)
    _run(store, clock, ExecutionKind.IMPLEMENTATION, 200, ExecutionStatus.SUCCEEDED, actor="codex", sha="c" * 40)
    _run(store, clock, ExecutionKind.VALIDATION, 20, ExecutionStatus.SUCCEEDED, actor="stagemesh-validator", sha="c" * 40)
    _run(store, clock, ExecutionKind.REVIEW, 60, ExecutionStatus.FAILED, actor="claude", sha="c" * 40, result="findings")
    _run(store, clock, ExecutionKind.INTEGRATION, 5, ExecutionStatus.FAILED, actor="stagemesh-integrator", sha="c" * 40)

    records = execution_timings(store, TASK)

    assert [(r["stage"], r["attempt"], r["result"], r["duration_seconds"]) for r in records] == [
        ("IMPLEMENT", 1, "passed", 100),
        ("VALIDATE", 1, "failed", 30),
        ("IMPLEMENT", 2, "passed", 200),
        ("VALIDATE", 2, "passed", 20),
        ("REVIEW", 1, "failed", 60),
        ("INTEGRATE", 1, "failed", 5),
    ]
    assert records[4]["reason"] == "findings"
    summary = task_timing(store, TASK)["summary"]
    assert summary["stage_seconds"] == {"IMPLEMENT": 100, "VALIDATE": 30, "REVIEW": 60, "INTEGRATE": 5}
    assert summary["retries_seconds"] == 220
    assert summary["total_execution_seconds"] == 415


def test_running_execution_has_no_finish_and_is_not_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(tmp_path, monkeypatch)
    store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.REVIEW, actor="claude")
    clock.now += 50

    timing = task_timing(store, TASK)

    rec = timing["executions"][0]
    assert (rec["finished_at"], rec["duration_seconds"], rec["result"]) == (None, None, "running")
    assert timing["summary"]["total_execution_seconds"] == 0 and timing["summary"]["running"] == 1
    assert "duration: 50s so far" in format_execution(rec, now=clock.now)


def test_recovered_orphan_execution_is_timed_and_labelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, clock = _store(tmp_path, monkeypatch)
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="codex")
    clock.now += 90

    assert store.mark_orphan_running_execution_failed(execution_id, "test")

    rec = execution_timings(store, TASK)[0]
    assert (rec["result"], rec["reason"], rec["duration_seconds"]) == ("failed", "orphaned", 90)


def test_migration_adds_columns_to_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    store = Store(db)
    store.migrate()
    store.conn.executescript("ALTER TABLE executions RENAME TO executions_new; DELETE FROM schema_migrations WHERE version=4;")
    store.conn.executescript(
        "CREATE TABLE executions AS SELECT id, task_id, claim_id, kind, status, pid, process_create_time, boot_id, executable, "
        "candidate_sha, started_at, updated_at FROM executions_new; DROP TABLE executions_new;"
    )
    store.close()

    reopened = Store(db)
    reopened.migrate()

    columns = {row["name"] for row in reopened.conn.execute("PRAGMA table_info(executions)")}
    assert {"actor", "result"} <= columns


def test_task_timing_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "proj"
    (project / ".stagemesh").mkdir(parents=True)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("timing", source_id=TASK)
    clock = Clock(1791240130.25)
    monkeypatch.setattr(persistence_module.time, "time", clock)
    _run(store, clock, ExecutionKind.REVIEW, 138.15, ExecutionStatus.SUCCEEDED, actor="claude", sha="d" * 40)
    store.close()

    args = argparse.Namespace(project=str(project), task=TASK, json=True, verbose=False)
    assert cli_module.command_task_timing(args) == 0

    rec = json.loads(capsys.readouterr().out)["executions"][0]
    assert rec["started_at"] == pytest.approx(1791240130.25)
    assert rec["finished_at"] == pytest.approx(1791240268.40)
    assert rec["duration_seconds"] == pytest.approx(138.15)
    assert (rec["task_id"], rec["actor"], rec["candidate_sha"]) == (TASK, "claude", "d" * 40)

    args.json = False
    assert cli_module.command_task_timing(args) == 0
    out = capsys.readouterr().out
    assert "Review #1" in out and "duration: 2m 18s" in out and "total execution time: 2m 18s" in out


def _repo(tmp_path: Path) -> tuple[Path, Store]:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / "docs").mkdir()
    (project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    workspace.commit_all("base")
    contracts = project / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    contract = {"objective": "docs only", "allowed_files": ["docs/**"], "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}]}
    (contracts / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("timing", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    return project, store


def test_provider_timeout_is_timed_with_actor_and_reason(tmp_path: Path) -> None:
    project, store = _repo(tmp_path)
    executor = SubprocessExecutor([sys.executable, "-c", "import time; time.sleep(120)"], name="codex", timeout_seconds=2)

    Coordinator(store, project, executor=executor).tick()

    rec = execution_timings(store, TASK)[0]
    assert (rec["stage"], rec["actor"], rec["result"], rec["reason"]) == ("IMPLEMENT", "codex", "failed", "provider_timeout")
    assert 1.5 <= rec["duration_seconds"] < 60


def test_real_implementation_and_validation_failure_are_timed(tmp_path: Path) -> None:
    project, store = _repo(tmp_path)
    script = tmp_path / "provider.py"
    script.write_text("import pathlib\npathlib.Path('src_out.py').write_text('x')\n", encoding="utf-8")  # outside docs/**
    executor = SubprocessExecutor([sys.executable, str(script)], name="codex")
    Coordinator(store, project, executor=executor).tick()
    candidate = store.latest_candidate(TASK)
    assert candidate is not None

    status = Validator().validate(store, TASK, str(candidate["sha"]), project)

    assert status is EvidenceStatus.FAILED
    impl, validation = execution_timings(store, TASK)[:2]
    assert (impl["stage"], impl["actor"], impl["result"]) == ("IMPLEMENT", "codex", "passed")
    assert (validation["stage"], validation["actor"], validation["result"]) == ("VALIDATE", "stagemesh-validator", "failed")
    assert validation["candidate_sha"] == str(candidate["sha"])
    assert validation["duration_seconds"] >= 0


def test_continue_step_output_and_json_show_execution_duration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from test_run_ready import _project

    project = _project(tmp_path, ["T-1"])
    assert cli_module.main(["--project", str(project), "continue", "--dry-run", "--task", "T-1"]) == 0
    human = capsys.readouterr().out
    assert any(line.startswith("  duration: ") and line.endswith("s") for line in human.splitlines())

    project = _project(tmp_path / "json", ["T-1"])
    assert cli_module.main(["--project", str(project), "run-ready", "--dry-run", "--json"]) == 0
    steps = json.loads(capsys.readouterr().out)["steps"]
    timed = [step for step in steps if step["executions"]]
    assert timed
    for step in timed:
        assert isinstance(step["duration_seconds"], float) and step["duration_seconds"] >= 0
        for rec in step["executions"]:
            assert rec["finished_at"] >= rec["started_at"] > 0
            assert rec["duration_seconds"] == pytest.approx(rec["finished_at"] - rec["started_at"], abs=0.001)
            assert rec["actor"] and rec["result"] == "passed"
