from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_bounded_execution import SLEEPER, TASK, _running_execution, _setup

import stagemesh.cli as cli_module
from stagemesh.coordinator import TargetSelection
from stagemesh.diagnosis import DiagnosisPolicy
from stagemesh.domain import ExecutionKind, Stage
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.process_identity import popen_identity
from stagemesh.run_ready import run_ready

FAKE_CONTRACT = {
    "objective": "fake task",
    "allowed_files": ["stagemesh-task-*.txt"],
    "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
}


def _project(
    tmp_path: Path,
    task_ids: list[str],
    contracts: list[str] | None = None,
    labels: dict[str, list[str]] | None = None,
    config: dict | None = None,
    dependencies: dict[str, list[str]] | None = None,
) -> Path:
    project = tmp_path / "repo"
    project.mkdir(parents=True)
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    (project / "README.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    backlog = {
        "objective": "o",
        "tasks": [
            {
                "id": t,
                "title": f"task {t}",
                "eligible": True,
                "state": "OPEN",
                "labels": (labels or {}).get(t, []),
                "dependencies": (dependencies or {}).get(t, []),
            }
            for t in task_ids
        ],
    }
    if config is not None:
        (runtime / "config.json").write_text(json.dumps(config), encoding="utf-8")
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
    assert any(step["new"]["latest_agent"] == "fake" for step in steps)


def test_formatted_step_reports_selected_coding_agent(tmp_path: Path) -> None:
    from stagemesh.run_ready import format_step

    project = _project(tmp_path, ["T-1"])
    code, result = _run(project)
    assert code == 0, result
    lines = [format_step(step) for step in result["steps"]]
    assert any("actor: fake" in line for line in lines)


def test_continue_human_output_is_operator_timeline(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--task", "T-1"])

    text = out.getvalue()
    assert code == 0
    assert text.count("StageMesh continue: task T-1") == 1
    assert "Implementation #2" in text and "status: running" in text
    assert "actor: fake" in text
    assert "candidate:" in text
    assert text.count("actor: fake") == 1
    assert "Validation #" in text and "actor: contract" in text
    assert "Review #" in text and "actor: builtin-deterministic-fallback" in text
    assert "Integration #" in text and "actor: builtin" in text
    assert "Run stopped: done" in text


def test_auto_continue_walks_to_next_task_after_provider_pool_exhaustion(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, ["A-1", "B-1"])
    calls = []

    def fake_run_ready(*args, **kwargs):
        calls.append(kwargs.get("task_id"))
        if len(calls) == 1:
            return cli_module.RunSummary(
                True,
                "NO_PROGRESS",
                task_id="A-1",
                detail={
                    "failure": {
                        "event": "task.implementation_unsuccessful",
                        "reason": "all_implementation_providers_no_progress: claude: no_implementation_change",
                        "pool_exhausted": True,
                        "provider_sequence": ["claude"],
                    }
                },
            )
        return cli_module.RunSummary(True, "DONE", task_id="B-1", final={"stage": "DONE"})

    monkeypatch.setattr(cli_module, "run_ready", fake_run_ready)
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0
    assert len(calls) == 2
    assert data["task_id"] == "B-1" and data["stop_reason"] == "DONE"
    assert data["detail"]["continued_after_provider_exhaustion"][0]["task_id"] == "A-1"

def test_json_continue_keeps_human_timeline_off_stdout(tmp_path: Path) -> None:
    out = io.StringIO()

    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(_project(tmp_path, ["T-1"])), "continue", "--dry-run", "--json"])

    data = json.loads(out.getvalue())
    assert code == 0 and data["stop_reason"] == "DONE"
    assert "StageMesh continue" not in out.getvalue()
    assert "Run stopped" not in out.getvalue()


def test_timeline_notices_only_show_fallback_skip_or_refusal() -> None:
    from stagemesh.run_ready import format_step

    step = {
        "step": 2,
        "task_id": "T-1",
        "progressed": 1,
        "previous": {"stage": "IMPLEMENT", "status": "OPEN"},
        "new": {
            "stage": "VALIDATE",
            "status": "OPEN",
            "latest_candidate": "abc123def456",
            "latest_candidate_provider": "claude",
            "latest_agent": "claude",
            "latest_evidence": {},
        },
    }
    text = format_step(
        step,
        notices=[
            "stage IMPLEMENT starting for task T-1",
            "  selected implementation provider codex",
            "  fallback: codex failed (provider_timeout) -> trying claude",
            "  final implementation provider: claude; candidate abc123def456; result SUCCEEDED",
            "  skipped grok: cli_not_installed: 'grok' is not callable on PATH",
        ],
    )

    assert "actor: claude" in text
    assert "selected implementation provider" not in text
    assert "final implementation provider" not in text
    assert "note: fallback: codex failed (provider_timeout) -> trying claude" in text
    assert "note: skipped grok:" in text


def test_timeline_shows_independent_review_refusal_only_as_notice() -> None:
    from stagemesh.run_ready import format_step

    step = {
        "step": 4,
        "task_id": "T-1",
        "progressed": 0,
        "previous": {"stage": "REVIEW", "status": "OPEN"},
        "new": {
            "stage": "REVIEW",
            "status": "OPEN",
            "latest_candidate": "abc123def456",
            "latest_candidate_provider": "codex",
            "latest_agent": "codex",
            "latest_evidence": {
                "review": {
                    "status": "CAPACITY",
                    "payload": {
                        "review_provider": "dynamic-pool:",
                        "independent_review_required": True,
                    },
                },
            },
        },
    }

    text = format_step(
        step,
        notices=[
            "  provider pool considered: codex",
            "  REFUSED: independent review cannot be satisfied; providers considered: codex: not_independent",
        ],
    )

    assert "status: capacity" in text
    assert "actor: dynamic-pool:" in text
    assert "provider pool considered" not in text
    assert "note: REFUSED: independent review cannot be satisfied" in text


def test_timeline_uses_stage_actor_not_stale_implementation_provider() -> None:
    from stagemesh.run_ready import format_step

    base = {
        "task_id": "T-1",
        "progressed": 1,
        "previous": {"status": "OPEN"},
        "new": {
            "status": "OPEN",
            "latest_candidate": "abc123def456",
            "latest_candidate_provider": "codex",
            "latest_agent": "codex",
            "latest_evidence": {
                "validation": {"status": "FAILED", "payload": {"validator": "contract", "validation_checks": {}}},
                "review": {
                    "status": "FAILED",
                    "payload": {
                        "review_provider": "claude",
                        "review_execution_provider": "claude",
                        "independent_review_required": True,
                        "independent_reviewer": True,
                    },
                },
                "integration": {
                    "status": "FAILED",
                    "payload": {"integrator": "builtin", "integration_ref": "refs/heads/integration"},
                },
            },
        },
    }

    validation = format_step({**base, "step": 3, "previous": {**base["previous"], "stage": "VALIDATE"}, "new": {**base["new"], "stage": "IMPLEMENT"}})
    review = format_step({**base, "step": 4, "previous": {**base["previous"], "stage": "REVIEW"}, "new": {**base["new"], "stage": "IMPLEMENT"}})
    integration = format_step({**base, "step": 5, "previous": {**base["previous"], "stage": "INTEGRATE"}, "new": {**base["new"], "stage": "IMPLEMENT"}})

    assert "actor: contract" in validation and "actor: codex" not in validation
    assert "actor: claude" in review and "actor: codex" not in review
    assert "actor: builtin" in integration and "actor: codex" not in integration


def test_no_progress_stop_reason_is_human_readable() -> None:
    from stagemesh.run_ready import RunSummary, format_stop

    summary = RunSummary(
        started=True,
        stop_reason="NO_PROGRESS",
        task_id="T-1",
        message="task.implementation_unsuccessful: provider_timeout",
        final={"stage": "IMPLEMENT", "status": "OPEN"},
    )

    text = format_stop(summary)
    assert "Run stopped: no progress" in text
    assert "reason: task.implementation_unsuccessful: provider_timeout" in text


def test_refuses_when_no_eligible_task(tmp_path: Path) -> None:
    code, result = _run(_project(tmp_path, []))
    assert code == 2 and result["stop_reason"] == "REFUSED:no_eligible_task" and result["started"] is False


def test_refuses_multiple_eligible_tasks_without_task_flag(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A-1", "B-1"], config={"task_selection": {"auto_select": False}})
    code, result = _run(project)
    assert code == 2 and result["stop_reason"] == "REFUSED:multiple_eligible_tasks"
    assert sorted(result["detail"]["eligible"]) == ["A-1", "B-1"]
    code, result = _run(project, "--task", "A-1")
    assert code == 0 and result["task_id"] == "A-1"
    other = Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("B-1")
    assert other["stage"] == "PLAN"  # the unselected task was never advanced


def test_refuses_task_without_contract(tmp_path: Path) -> None:
    code, result = _run(_project(tmp_path, ["T-1"], contracts=[]), "--no-auto-plan")
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


def _block_other(project: Path, source_id: str = "X-1") -> str:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    other = store.upsert_task("blocked one", source="local", source_id=source_id)
    store.block_task(other)
    store.close()
    return other


def _status(project: Path, task_id: str) -> str:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    try:
        return str(store.get_task(task_id)["status"])
    finally:
        store.close()


def test_health_still_reports_blocked_tasks(tmp_path: Path) -> None:
    from stagemesh.observability import health

    project = _project(tmp_path, ["T-1"])
    _block_other(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    report = health(store)
    store.close()
    assert report.blocked_task_count > 0 and "blocked_tasks" in report.current_problems and not report.ok


def test_unrelated_blocked_task_does_not_stop_explicit_run(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    blocked = _block_other(project)
    code, result = _run(project, "--task", "T-1")
    assert code == 0 and result["stop_reason"] == "DONE" and result["task_id"] == "T-1", result
    assert _status(project, blocked) == "BLOCKED"  # never auto-mutated


def test_unrelated_blocked_task_does_not_stop_auto_run(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    blocked = _block_other(project)
    code, result = _run(project)
    assert code == 0 and result["stop_reason"] == "DONE" and result["task_id"] == "T-1", result
    assert result["selection"]["task_id"] == "T-1" and result["selection"]["mode"] != "explicit"
    assert _status(project, blocked) == "BLOCKED"


def test_explicit_blocked_task_is_refused_without_execution(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    blocked = _block_other(project)
    code, result = _run(project, "--task", blocked)
    assert code == 2 and result["stop_reason"] == "REFUSED:task_blocked" and result["started"] is False, result
    assert "retry-task" in result["message"] and result["steps_run"] == 0
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    store.close()
    assert _status(project, blocked) == "BLOCKED"


def test_current_failed_executions_remain_nonfatal_but_global_hazards_fatal(tmp_path: Path) -> None:
    from stagemesh.observability import health
    from stagemesh.run_ready import current_problems

    _project, store = _setup(tmp_path, FAKE_CONTRACT)
    store.conn.execute("UPDATE tasks SET status='BLOCKED' WHERE id=?", (TASK,))
    store.conn.commit()
    assert "blocked_tasks" in health(store).current_problems
    assert current_problems(store) == ()
    # unknown execution: fatal
    claim = store.acquire_claim(TASK, "w", lease_seconds=-1)
    from stagemesh.domain import ExecutionKind

    execution_id = store.start_execution(task_id=TASK, claim_id=claim, kind=ExecutionKind.IMPLEMENTATION, pid=None)
    store.conn.execute("UPDATE executions SET status='UNKNOWN' WHERE id=?", (execution_id,))
    store.conn.commit()
    assert "unknown_executions" in current_problems(store)
    # failed execution alone: nonfatal
    store.conn.execute("UPDATE executions SET status='FAILED' WHERE id=?", (execution_id,))
    store.conn.commit()
    assert "unknown_executions" not in current_problems(store)
    assert "current_failed_executions" not in current_problems(store)
    store.close()


def test_stale_running_execution_remains_fatal_for_current_problems(tmp_path: Path) -> None:
    from stagemesh.run_ready import current_problems

    _project, store = _setup(tmp_path, FAKE_CONTRACT)
    proc = subprocess.Popen(SLEEPER)
    _running_execution(store, proc)
    proc.kill()
    proc.wait()
    store.conn.execute("UPDATE tasks SET status='BLOCKED' WHERE id=?", (TASK,))
    store.conn.commit()
    assert current_problems(store) == ("stale_running_executions",)
    store.close()


def test_continue_recovers_orphaned_builtin_validation_execution(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    config = cli_module.load_config(project)
    cli_module._sync_all_sources(store, project, config, None)
    execution_id = store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.VALIDATION, actor="stagemesh-validator")
    store.close()

    code, result = _continue(project, "--max-steps", "1")

    assert code == 1, result
    assert result["stop_reason"] == "MAX_STEPS"
    assert any(item["execution_id"] == execution_id and item["reason"] == "ORPHANED_BUILTIN_STAGE_EXECUTION" for item in result["recovered"])


def test_continue_recovers_orphaned_builtin_review_execution(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    config = cli_module.load_config(project)
    cli_module._sync_all_sources(store, project, config, None)
    execution_id = store.start_execution(
        task_id="T-1",
        claim_id=None,
        kind=ExecutionKind.REVIEW,
        actor="builtin-deterministic-fallback",
    )
    store.close()

    code, result = _continue(project, "--max-steps", "1")

    assert code == 1, result
    assert result["stop_reason"] == "MAX_STEPS"
    assert any(item["execution_id"] == execution_id and item["reason"] == "ORPHANED_BUILTIN_STAGE_EXECUTION" for item in result["recovered"])


def test_run_ready_recovers_dead_execution_created_during_tick_before_health_stop(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A-1", "B-1"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("task A", source="local-backlog", source_id="A-1")
    store.upsert_task("task B", source="local-backlog", source_id="B-1")

    class CreatesDeadExecution:
        diagnosis_policy = DiagnosisPolicy()

        def __init__(self, target: TargetSelection) -> None:
            self.target = target

        def validate_target(self) -> None:
            assert self.target.task_id == "A-1"

        def tick(self) -> int:
            proc = subprocess.Popen(SLEEPER)
            identity = popen_identity(proc)
            execution_id = store.start_execution(
                task_id="B-1",
                claim_id=None,
                kind=ExecutionKind.IMPLEMENTATION,
                pid=identity.pid,
                process_create_time=identity.create_time,
                boot_id=identity.boot_id,
                executable=identity.executable,
            )
            proc.kill()
            proc.wait()
            store.advance_task("A-1", Stage.IMPLEMENT)
            assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "RUNNING"
            return 1

    summary = run_ready(store, project, lambda target: CreatesDeadExecution(target), task_id="A-1", max_steps=1)

    assert summary.stop_reason == "MAX_STEPS", summary.to_dict()
    assert any(
        item["task_id"] == "B-1"
        and item["kind"] == ExecutionKind.IMPLEMENTATION
        and item["action"] == "RELEASED"
        and item["process_state"] == "DEAD"
        for item in summary.recovered
    )
    assert not any(item == "stale_running_executions" for item in summary.detail.get("problems", []))


def test_unrelated_unknown_execution_still_refuses_run(tmp_path: Path) -> None:
    from stagemesh.domain import ExecutionKind

    project = _project(tmp_path, ["T-1"])
    other = _block_other(project)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    claim = store.acquire_claim(other, "w", lease_seconds=-1)
    execution_id = store.start_execution(task_id=other, claim_id=claim, kind=ExecutionKind.IMPLEMENTATION, pid=None)
    store.conn.execute("UPDATE executions SET status='UNKNOWN' WHERE id=?", (execution_id,))
    store.conn.commit()
    store.close()
    code, result = _run(project, "--task", "T-1")
    assert code == 2 and result["stop_reason"] == "REFUSED:current_problems", result
    assert "unknown_executions" in result["detail"]["problems"] and "blocked_tasks" not in result["detail"]["problems"]


def _continue(project: Path, *argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(project), "continue", "--dry-run", "--json", *argv])
    return code, json.loads(out.getvalue())


def test_continue_defaults_to_supervised_run_to_completion(tmp_path: Path) -> None:
    code, result = _continue(_project(tmp_path, ["T-1"]))
    assert code == 0 and result["stop_reason"] == "DONE" and result["final"]["stage"] == "DONE"
    assert result["steps_run"] >= 5


@pytest.mark.parametrize(
    ("max_steps", "checkpoint_stage"),
    [
        (1, "IMPLEMENT"),
        (2, "VALIDATE"),
        (3, "REVIEW"),
        (4, "INTEGRATE"),
    ],
)
def test_continue_runtime_resume_matrix_reaches_done_from_each_stage(
    tmp_path: Path, max_steps: int, checkpoint_stage: str
) -> None:
    project = _project(tmp_path, ["T-1"])

    code, checkpoint = _continue(project, "--task", "T-1", "--max-steps", str(max_steps))

    assert code == 1, checkpoint
    assert checkpoint["stop_reason"] == "MAX_STEPS"
    assert checkpoint["final"]["stage"] == checkpoint_stage

    code, final = _continue(project, "--task", "T-1")

    assert code == 0, final
    assert final["stop_reason"] == "DONE"
    assert final["final"]["stage"] == "DONE"
    assert final["final"]["latest_validation"] == "PASSED"
    assert final["final"]["latest_review"] == "PASSED"


def test_continue_task_supervises_only_that_task_to_done(tmp_path: Path) -> None:
    project = _project(tmp_path, ["A-1", "B-1"])
    code, result = _continue(project, "--task", "A-1")
    assert code == 0 and result["task_id"] == "A-1" and result["stop_reason"] == "DONE"
    assert Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("B-1") is None  # targeted sync skips it


def test_continue_default_refuses_ambiguous_selection(tmp_path: Path) -> None:
    code, result = _continue(_project(tmp_path, ["A-1", "B-1"], config={"task_selection": {"auto_select": False}}))
    assert code == 2 and result["stop_reason"] == "REFUSED:multiple_eligible_tasks"


def test_continue_once_keeps_single_tick_behavior(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    code, result = _continue(project, "--once", "--task", "T-1")
    assert code == 0
    assert "stop_reason" not in result and result["progressed"] == 1  # legacy summary shape: exactly one tick
    assert result["targeted_task_id"] == "T-1"
    assert Store(project / ".stagemesh" / "stagemesh.sqlite3").get_task("T-1")["stage"] == "IMPLEMENT"


def test_refuses_contract_larger_than_the_store_limit(tmp_path: Path) -> None:
    project = _project(tmp_path, ["T-1"])
    contract = dict(FAKE_CONTRACT, objective="x" * 11000)
    (project / ".stagemesh" / "contracts" / "T-1.json").write_text(json.dumps(contract), encoding="utf-8")
    code, result = _run(project)
    assert code == 2 and result["stop_reason"] == "REFUSED:invalid_contract"
    assert result["detail"]["limit"] == 10000 and result["detail"]["size"] > 10000
    assert Store(project / ".stagemesh" / "stagemesh.sqlite3").latest_candidate("T-1") is None


def test_no_progress_reports_the_concrete_provider_failure(tmp_path: Path) -> None:
    from stagemesh.coordinator import Coordinator
    from stagemesh.execution import Executor
    from stagemesh.run_ready import run_ready

    class Exploding(Executor):
        name = "codex"

        def run(self, store, task_id, claim_id, project):
            raise RuntimeError("provider binary exploded")

    project = _project(tmp_path, ["T-1"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("t", source="local-backlog", source_id="T-1")

    summary = run_ready(store, project, lambda target: Coordinator(store, project, executor=Exploding(), target=target), task_id="T-1")

    assert summary.stop_reason == "NO_PROGRESS" and summary.final["stage"] == "IMPLEMENT"
    assert "RuntimeError: provider binary exploded" in summary.message
    assert summary.detail["failure"]["event"] == "task.implementation_unsuccessful"
    assert summary.detail["failure"]["executor"] == "codex"
