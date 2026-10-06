from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.concurrency import IntegrationLock
from stagemesh.config import load_config
from stagemesh.contracts import parse_contract
from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.execution import ExecutionResult, communicate_bounded
from stagemesh.git import GitWorkspace
from stagemesh.parallel import queue_control_state, recover_orphaned_claims, worker_id_for
from stagemesh.queue_run import QueueRunner, dirty_in_scope, dirty_paths, preflight, write_scope_overlap
from stagemesh.review import Reviewer
from stagemesh.serialized_integration import SerializedIntegrator
from stagemesh.workspaces import prepare_task_workspace, task_workspace

from test_parallel import Rig, ScriptedExecutor, contract_for
from test_run_ready import _project

PY = sys.executable


def queue(rig: Rig, executor, concurrency: int = 2, **kwargs):
    return rig.runner(executor, concurrency, runner_class=QueueRunner, **kwargs)


def write_contract(rig: Rig, task_id: str, **overrides) -> None:
    body = contract_for(rig.files[task_id][0])
    body.update(overrides)
    (rig.project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(json.dumps(body), encoding="utf-8")


def outcomes(summary) -> dict[str, str]:
    return {t.task_id: t.summary.stop_reason for t in summary.tasks}


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_two_non_conflicting_tasks_run_concurrently(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    summary = queue(rig, ScriptedExecutor(rig.files, barrier=threading.Barrier(2))).run()  # the barrier only opens if both run at once
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"} and summary.succeeded
    assert {"out/A.txt", "out/B.txt"} <= rig.tree()


def test_conflicting_tasks_are_never_run_together(tmp_path: Path) -> None:
    files = {"A": ("shared/a.txt", "a\n"), "B": ("shared/b.txt", "b\n"), "C": ("other/c.txt", "c\n")}
    rig = Rig(tmp_path, ["A", "B", "C"], files=files)
    for task_id in ("A", "B"):
        write_contract(rig, task_id, allowed_files=["shared/**"])  # overlapping write scope
    executor = ScriptedExecutor(files)
    summary = queue(rig, executor, concurrency=3).run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE", "C": "DONE"}
    marks = {(kind, task): at for kind, task, at in executor.log}
    assert marks[("end", "A")] <= marks[("start", "B")] or marks[("end", "B")] <= marks[("start", "A")]
    assert any(d["reason"].startswith("allowed_paths") for d in summary.deferred)  # the overlap was reported, not hidden


def test_missing_contract_is_refused_when_auto_planning_is_disabled(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    (rig.project / ".stagemesh" / "contracts" / "B.json").unlink()
    summary = queue(rig, ScriptedExecutor(rig.files), auto_plan=False).run()
    assert outcomes(summary) == {"A": "DONE", "B": "REFUSED:missing_contract"}
    assert summary.stop_reason == "PARTIAL" and "out/B.txt" not in rig.tree()
    assert not (rig.project / ".stagemesh" / "contracts" / "B.json").exists()  # nothing was auto-planned


def test_missing_contract_is_refused_when_no_safe_contract_can_be_derived(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])  # the rig project has no test tooling to build validation gates from
    (rig.project / ".stagemesh" / "contracts" / "B.json").unlink()
    summary = queue(rig, ScriptedExecutor(rig.files)).run()
    assert outcomes(summary) == {"A": "DONE", "B": "REFUSED:auto_plan_failed"}
    assert "cannot auto-plan" in next(t for t in summary.tasks if t.task_id == "B").summary.message
    assert not (rig.project / ".stagemesh" / "contracts" / "B.json").exists() and "out/B.txt" not in rig.tree()


def test_queue_run_auto_plans_a_missing_contract_and_runs_the_task_without_continue(tmp_path: Path, monkeypatch) -> None:
    import stagemesh.auto_plan as auto_plan_module

    gate = {"name": "project-acceptance-fake", "command": [PY, "-c", "pass"], "timeout_seconds": 60}
    monkeypatch.setattr(auto_plan_module, "detect_gates", lambda project: [dict(gate)])
    said: list[tuple[str, str]] = []
    rig = Rig(tmp_path, ["A", "B"], labels={"B": ["beta"]})
    (rig.project / ".stagemesh" / "contracts" / "B.json").unlink()
    scope = {"schema_version": 1, "areas": [{"id": "beta", "labels": ["beta"], "allowed_files": ["out/B.txt"]}]}
    (rig.project / "stagemesh.scope.json").write_text(json.dumps(scope), encoding="utf-8")
    runner = queue(rig, ScriptedExecutor(rig.files), emit=lambda task_id, text: said.append((task_id, text)))
    summary = runner.run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"} and summary.succeeded
    assert {"out/A.txt", "out/B.txt"} <= rig.tree()
    generated = [json.loads(r["payload"]) for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='contract.auto_generated'")]
    assert [g["task_id"] for g in generated] == ["B"] and generated[0]["gates"] == ["project-acceptance-fake"]
    assert any(task == "B" and text.startswith("task B: auto-planned contract ") for task, text in said)
    bound = rig.store.contract_binding("B", str(rig.store.latest_candidate("B")["sha"]))
    assert bound is not None  # the auto-planned contract is what the candidate was validated against


def test_dirty_working_tree_in_task_scope_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    (rig.project / "out").mkdir()
    (rig.project / "out" / "A.txt").write_text("uncommitted work\n", encoding="utf-8")
    summary = queue(rig, ScriptedExecutor(rig.files)).run()
    assert outcomes(summary) == {"A": "REFUSED:dirty_working_tree", "B": "DONE"}
    refused = next(t for t in summary.tasks if t.task_id == "A").summary
    assert refused.detail["paths"] == ["out/A.txt"] and "uncommitted" in refused.message
    assert (rig.project / "out" / "A.txt").read_text(encoding="utf-8") == "uncommitted work\n"  # untouched


def test_dirty_paths_outside_every_scope_do_not_block(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    (rig.project / "README.md").write_text("edited elsewhere\n", encoding="utf-8")
    assert "README.md" in dirty_paths(rig.project) and not any(p.startswith(".stagemesh") for p in dirty_paths(rig.project))
    assert outcomes(queue(rig, ScriptedExecutor(rig.files)).run()) == {"A": "DONE"}


def test_integration_is_serialized(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    state = {"inside": 0, "max": 0}
    guard = threading.Lock()
    real_hold = rig.lock.hold

    @contextlib.contextmanager
    def watched(owner):
        with real_hold(owner):
            with guard:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
            time.sleep(0.15)
            try:
                yield
            finally:
                with guard:
                    state["inside"] -= 1

    rig.lock.hold = watched  # type: ignore[method-assign]
    summary = queue(rig, ScriptedExecutor(rig.files, barrier=threading.Barrier(2))).run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"} and state["max"] == 1
    assert {"out/A.txt", "out/B.txt"} <= rig.tree()


def test_a_blocked_task_does_not_disturb_an_independent_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    bad = dict(rig.files, A=("outside/A.txt", "out of scope\n"))  # A writes outside its contract, so it ends BLOCKED
    summary = queue(rig, ScriptedExecutor(bad), max_steps=80).run()
    result = outcomes(summary)
    assert result["B"] == "DONE" and result["A"] == "BLOCKED", result
    assert "out/B.txt" in rig.tree() and "outside/A.txt" not in rig.tree()
    assert rig.store.get_task("A")["status"] == "BLOCKED" and rig.store.get_task("B")["status"] == "DONE"
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert GitWorkspace(rig.project).run("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""


def test_stale_dead_claim_is_recovered_per_task_and_live_ones_are_left(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B", "C"])
    gone = subprocess.Popen([PY, "-c", "pass"])
    gone.wait()
    dead = rig.store.acquire_claim("A", f"parallel-{gone.pid}-A")
    live = rig.store.acquire_claim("B", f"parallel-{os.getpid()}-B")  # owned by a live process: must not be touched
    assert dead and live
    summary = queue(rig, ScriptedExecutor(rig.files)).run()
    assert [r["task_id"] for r in summary.recovered if r.get("action") == "RELEASED"] == ["A"]
    assert outcomes(summary) == {"A": "DONE", "C": "DONE"}
    assert rig.store.get_task("B")["status"] == "CLAIMED"
    assert rig.store.conn.execute("SELECT active FROM claims WHERE id=?", (live,)).fetchone()["active"] == 1
    assert "out/B.txt" not in rig.tree()
    rig.store.release_claim(live)
    assert recover_orphaned_claims(rig.store) == []


def test_global_safety_failure_halts_every_task(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    rig.lock.timeout_seconds = 0.5
    release = threading.Event()
    held = threading.Event()

    def outsider() -> None:
        with IntegrationLock(rig.lock.path, timeout_seconds=30).hold("another process"):
            held.set()
            release.wait(20)

    thread = threading.Thread(target=outsider)
    thread.start()
    held.wait(5)
    try:
        summary = queue(rig, ScriptedExecutor(rig.files, barrier=threading.Barrier(2)), max_steps=20).run()
    finally:
        release.set()
        thread.join()
    assert summary.stop_reason == "GLOBAL_SAFETY_FAILURE" and "integration lock" in summary.message
    assert "out/A.txt" not in rig.tree() and "out/B.txt" not in rig.tree()
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()


def test_pause_prevents_new_queue_admissions_until_resume(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    holder: dict[str, QueueRunner] = {}
    errors: list[str] = []

    class SlowFirst(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            if task_id == "A":
                holder["runner"].pause_admission("maintenance")

                def resume_later() -> None:
                    time.sleep(0.4)
                    if any(kind == "start" and task == "B" for kind, task, _ in executor.log):
                        errors.append("B started while admission was paused")
                    holder["runner"].resume_admission("maintenance complete")

                threading.Thread(target=resume_later).start()
                time.sleep(0.6)
            return super().run(store, task_id, claim_id, project)

    executor = SlowFirst(rig.files)
    runner = queue(rig, executor, concurrency=1)
    holder["runner"] = runner
    summary = runner.run()
    assert not errors
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"}
    assert summary.control["state"] == "resumed"
    events = [
        json.loads(r["payload"])
        for r in rig.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='queue.admission_control'"
        )
    ]
    assert [e["state"] for e in events] == ["paused", "resumed"]


def test_pause_without_running_tasks_exits_cleanly(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    runner = queue(rig, ScriptedExecutor(rig.files), concurrency=1)
    runner.pause_admission("maintenance")
    summary = runner.run()
    assert summary.stop_reason == "PAUSED"
    assert outcomes(summary) == {}
    assert rig.store.get_task("A")["status"] == "OPEN"


def test_stop_during_implementation_releases_claims_and_next_run_resumes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    holder: dict[str, QueueRunner] = {}

    class HangingImplementation(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
            run_path = prepare_task_workspace(project, task_id)
            (run_path / "half.txt").write_text("partial\n", encoding="utf-8")
            holder["runner"].stop_admission("operator stop", terminate_running=True)
            deadline = time.monotonic() + 10
            while not holder["runner"].stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="stopped")

    runner = queue(rig, HangingImplementation(rig.files), concurrency=1, interrupt_grace_seconds=5)
    holder["runner"] = runner
    summary = runner.run()
    assert summary.stop_reason == "STOPPED"
    assert outcomes(summary) == {"A": "STOPPED"}
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert not list(rig.store.running_executions())
    assert rig.store.get_task("A")["status"] == "OPEN"
    assert task_workspace(rig.project, "A").exists() and not (task_workspace(rig.project, "A") / "half.txt").exists()
    assert rig.store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='queue.stop'").fetchone()
    assert queue_control_state(rig.store)["state"] == "stopped"
    assert outcomes(queue(rig, ScriptedExecutor(rig.files), concurrency=1).run()) == {"A": "DONE"}


def test_mid_run_stop_preserves_unknown_process_identity_until_recovery_override(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    holder: dict[str, QueueRunner] = {}
    seen: dict[str, str] = {}
    release_worker = threading.Event()

    class UnknownIdentityImplementation(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            seen["claim"] = claim_id
            seen["execution"] = store.start_execution(  # a pid with no recorded create time: identity can never be proven
                task_id=task_id,
                claim_id=claim_id,
                kind=ExecutionKind.IMPLEMENTATION,
                pid=os.getpid(),
            )
            holder["runner"].stop_admission("operator stop", terminate_running=True)
            release_worker.wait(20)  # stay alive through _halt/_abandon, like a provider that outlives the grace period
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="stopped")

    runner = queue(rig, UnknownIdentityImplementation(rig.files), concurrency=1, interrupt_grace_seconds=0.2)
    holder["runner"] = runner
    try:
        summary = runner.run()
        assert summary.stop_reason == "STOPPED"
        row = rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (seen["execution"],)).fetchone()
        assert row["status"] == "RUNNING"
        assert rig.store.conn.execute("SELECT active FROM claims WHERE id=?", (seen["claim"],)).fetchone()["active"] == 1
        skipped = [
            json.loads(r["payload"])
            for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.abandon_skipped_unknown_execution'")
        ]
        assert [e["execution_id"] for e in skipped] == [seen["execution"]]
        for control in surfaced_queue_control(rig.project).values():
            assert control["state"] == "stopped"
            assert [e["id"] for e in control["active_executions"]] == [seen["execution"]]

        # the next run must neither touch nor re-admit the protected task
        again = queue(rig, ScriptedExecutor(rig.files), concurrency=1).run()
        assert "DONE" not in outcomes(again).values()
        row = rig.store.conn.execute("SELECT status FROM executions WHERE id=?", (seen["execution"],)).fetchone()
        assert row["status"] == "RUNNING"

        code, out, _ = run_stagemesh_cli(
            rig.project,
            "recover-stale",
            "--task",
            "A",
            "--release-unknown",
            "--execution",
            seen["execution"],
            "--reason",
            "test verified the runner stopped before the unknown execution could be identified",
        )
        assert code == 0 and "RELEASED_BY_OPERATOR" in out
        assert not list(rig.store.running_executions())
        assert rig.store.conn.execute("SELECT active FROM claims WHERE id=?", (seen["claim"],)).fetchone()["active"] == 0
    finally:
        release_worker.set()
        for thread in runner._threads.values():
            thread.join(timeout=10)
    assert outcomes(queue(rig, ScriptedExecutor(rig.files), concurrency=1).run()) == {"A": "DONE"}


def test_stop_during_review_kills_tracked_provider_and_fails_execution(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A"])
    holder: dict[str, QueueRunner] = {}

    class HangingReviewAdapter:
        name = "review-stop-provider"

        def review_candidate(self, prompt, project, sha):
            proc = subprocess.Popen(
                [PY, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            holder["runner"].stop_admission("operator stop", terminate_running=True)
            communicate_bounded(proc, "prompt", 120)
            return json.dumps({"decision": "INFRASTRUCTURE_FAILURE", "reason": "stopped"})

    integrator = SerializedIntegrator(rig.ref, False, rig.lock)

    def make(target, store, task_id):
        return Coordinator(
            store,
            rig.project,
            executor=ScriptedExecutor(rig.files),
            reviewer=Reviewer(adapter=HangingReviewAdapter()),
            integrator=integrator,
            target=target,
            worker_id=worker_id_for(task_id),
        )

    runner = QueueRunner(rig.store, rig.project, make, concurrency=1, poll_seconds=0.05, interrupt_grace_seconds=5)
    holder["runner"] = runner
    summary = runner.run()
    assert summary.stop_reason == "STOPPED"
    assert outcomes(summary) == {"A": "STOPPED"}
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert not list(rig.store.running_executions())
    failed = rig.store.conn.execute("SELECT kind FROM executions WHERE task_id='A' AND status='FAILED'")
    assert any(e["kind"] == "REVIEW" for e in failed)
    assert queue_control_state(rig.store)["state"] == "stopped"
    assert outcomes(queue(rig, ScriptedExecutor(rig.files), concurrency=1).run()) == {"A": "DONE"}


def surfaced_queue_control(project: Path) -> dict[str, dict]:
    """queue_control exactly as the operator sees it through `status --json` and `health --json`."""
    surfaced = {}
    for command in ("status", "health"):
        code, out, _ = run_stagemesh_cli(project, command, "--json")
        assert code == 0
        surfaced[command] = json.loads(out)["queue_control"]
    return surfaced


def control_events(rig: Rig, state: str) -> list[dict]:
    events = [
        json.loads(r["payload"])
        for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='queue.admission_control' ORDER BY rowid")
    ]
    return [e for e in events if e["state"] == state]


@pytest.mark.parametrize("mode", ["graceful", "terminate", "ctrl-c"])
def test_every_stop_path_ends_stopped_on_status_and_health(tmp_path: Path, mode: str) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    holder: dict[str, QueueRunner] = {}
    started = threading.Event()

    class StoppingRunner(QueueRunner):
        def _apply_control_requests(self) -> None:
            if mode == "ctrl-c" and started.is_set():
                raise KeyboardInterrupt  # Ctrl+C lands in the dispatcher while A is running
            super()._apply_control_requests()

    class RunningFirst(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            if task_id != "A":
                return super().run(store, task_id, claim_id, project)
            if mode == "graceful":
                holder["runner"].stop_admission("operator stop")  # A finishes; B is never admitted
                return super().run(store, task_id, claim_id, project)
            execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
            if mode == "terminate":
                holder["runner"].stop_admission("operator stop", terminate_running=True)
            else:
                started.set()
            assert wait_until(holder["runner"].stop.is_set, 10)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(ExecutionStatus.FAILED, failure_reason="stopped")

    runner = rig.runner(RunningFirst(rig.files), 1, runner_class=StoppingRunner, interrupt_grace_seconds=5)
    holder["runner"] = runner
    summary = runner.run()

    assert summary.stop_reason == ("INTERRUPTED" if mode == "ctrl-c" else "STOPPED")
    assert outcomes(summary) == {"A": {"graceful": "DONE", "terminate": "STOPPED", "ctrl-c": "INTERRUPTED"}[mode]}
    for control in surfaced_queue_control(rig.project).values():
        assert control["state"] == "stopped"
        assert not control["paused"] and not control["stopping"]
        assert control["active_executions"] == []
    assert len(control_events(rig, "stopped")) == 1
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert rig.store.get_task("B")["status"] == "OPEN"
    assert set(outcomes(queue(rig, ScriptedExecutor(rig.files), concurrency=2).run()).values()) == {"DONE"}


def test_scope_overlap_rules() -> None:
    def c(**kw):
        return parse_contract({"objective": "x", **kw})

    assert write_scope_overlap(c(allowed_files=["src/**"]), c(allowed_files=["src/a.py"])).kind == "allowed_paths"
    assert write_scope_overlap(c(allowed_files=["src/**"]), c(allowed_files=["docs/**"])) is None
    assert write_scope_overlap(c(allowed_files=["src/**"]), c(allowed_files=["src/a.py"], forbidden_files=["src/**"])) is None
    assert write_scope_overlap(c(allowed_files=["**"], forbidden_files=["src/**"]), c(allowed_files=["src/x.py"])) is None
    assert write_scope_overlap(c(), c(allowed_files=["docs/a.md"])).kind == "allowed_paths"  # default scope is everything
    contract = c(allowed_files=["src/**"], forbidden_files=["src/secret/**"])
    assert dirty_in_scope(contract, ["src/a.py", "src/secret/k.txt", "docs/x.md"]) == ["src/a.py"]


def _smoke_project(tmp_path: Path) -> Path:
    from test_project_smoke import profile

    project = _project(tmp_path, ["T-1", "T-2"], contracts=[])
    for task_id in ("T-1", "T-2"):
        (project / ".stagemesh" / "contracts" / f"{task_id}.json").write_text(
            json.dumps(contract_for(f"stagemesh-task-{task_id}.txt")), encoding="utf-8"
        )
    (project / ".stagemesh" / "profile.json").write_text(json.dumps(profile()), encoding="utf-8")
    return project


def run_cli(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), "queue-run", *argv])
    return code, out.getvalue(), err.getvalue()


def run_stagemesh_cli(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue(), err.getvalue()


def test_cli_json_reports_per_task_progress_and_final_states(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    code, out, _ = run_cli(project, "--concurrency", "2", "--dry-run", "--json")
    data = json.loads(out)
    assert code == 0 and data["mode"] == "queue" and data["concurrency"] == 2 and data["succeeded"] is True
    assert data["preflight"]["ok"] and data["preflight"]["smoke"]["ran"] and data["refused"] == []
    assert data["task_outcomes"] == {"T-1": "DONE", "T-2": "DONE"}
    for task in data["tasks"]:
        events = [e["event"] for e in task["lifecycle"]]
        assert events[0] == "selected" and events[-1] == "finished" and "started" in events
        assert [e["stage"] for e in task["lifecycle"] if e["event"] == "stage"][:2] == ["PLAN", "IMPLEMENT"]
        assert task["final"]["stage"] == "DONE" and all(step["task_id"] == task["task_id"] for step in task["steps"])


def test_cli_queue_control_commands_and_status_surfaces(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    code, out, _ = run_stagemesh_cli(project, "queue-control", "pause", "--reason", "maintenance", "--json")
    control = json.loads(out)
    assert code == 0 and control["state"] == "paused"

    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    status = json.loads(out)
    assert code == 0 and status["queue_control"]["state"] == "paused"

    from stagemesh.persistence import Store

    wrapped = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    cli_module._sync_all_sources(wrapped, project, load_config(project), None)
    execution_id = wrapped.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.REVIEW, pid=os.getpid())
    wrapped.close()

    code, out, _ = run_stagemesh_cli(project, "health", "--json")
    health = json.loads(out)
    assert code == 0 and health["queue_control"]["state"] == "paused"
    assert [e["id"] for e in health["queue_control"]["active_executions"]] == [execution_id]

    code, out, _ = run_stagemesh_cli(project, "task-doctor", "--task", "T-1", "--json")
    doctor = json.loads(out)
    assert code == 0 and doctor["queue_control"]["state"] == "paused"
    assert [e["id"] for e in doctor["queue_control"]["active_executions"]] == [execution_id]

    code, out, _ = run_stagemesh_cli(project, "queue-control", "resume", "--json")
    assert code == 0 and json.loads(out)["state"] == "running"


def test_cli_pause_and_resume_control_a_live_runner(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    holder: dict[str, QueueRunner] = {}
    errors: list[str] = []

    class BlockingFirst(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            if task_id == "A":
                code, out, _ = run_stagemesh_cli(rig.project, "queue-control", "pause", "--reason", "maintenance", "--json")
                if code != 0 or json.loads(out)["state"] != "paused":
                    errors.append("pause command failed")
                if not wait_until(lambda: holder["runner"].summary.control["state"] == "paused"):
                    errors.append("runner did not observe pause")
                if any(kind == "start" and task == "B" for kind, task, _ in executor.log):
                    errors.append("B started while admission was paused")
                code, out, _ = run_stagemesh_cli(rig.project, "queue-control", "resume", "--reason", "done", "--json")
                if code != 0 or json.loads(out)["state"] != "running":
                    errors.append("resume command failed")
            return super().run(store, task_id, claim_id, project)

    executor = BlockingFirst(rig.files)
    runner = queue(rig, executor, concurrency=1)
    holder["runner"] = runner
    summary = runner.run()
    assert not errors
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"}


@pytest.mark.parametrize("terminate", [False, True])
def test_cli_stop_request_stops_a_live_runner(tmp_path: Path, terminate: bool) -> None:
    rig = Rig(tmp_path, ["A", "B"])
    holder: dict[str, QueueRunner] = {}
    errors: list[str] = []

    class StopFromOperator(ScriptedExecutor):
        def run(self, store, task_id, claim_id, project):
            if task_id != "A":
                return super().run(store, task_id, claim_id, project)
            argv = ["queue-control", "stop", "--reason", "operator stop", "--json"] + (["--terminate-running"] if terminate else [])
            code, out, _ = run_stagemesh_cli(rig.project, *argv)  # only the DB request: nothing calls the runner directly
            if code != 0 or json.loads(out)["state"] != "stopping":
                errors.append("stop command failed")
            observed = holder["runner"].stop.is_set if terminate else (lambda: holder["runner"].admission_stopped)
            if not wait_until(observed, 10):
                errors.append("runner did not pick up the stop request")
            if terminate:
                execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
                store.finish_execution(execution_id, ExecutionStatus.FAILED)
                return ExecutionResult(ExecutionStatus.FAILED, failure_reason="stopped")
            return super().run(store, task_id, claim_id, project)

    executor = StopFromOperator(rig.files)
    runner = queue(rig, executor, concurrency=1, interrupt_grace_seconds=5)
    holder["runner"] = runner
    summary = runner.run()
    assert not errors
    assert summary.stop_reason == "STOPPED"
    assert outcomes(summary) == {"A": "STOPPED" if terminate else "DONE"}
    assert not any(kind == "start" and task == "B" for kind, task, _ in executor.log)
    for control in surfaced_queue_control(rig.project).values():
        assert control["state"] == "stopped" and control["active_executions"] == []
    assert len(control_events(rig, "stopped")) == 1
    assert not rig.store.conn.execute("SELECT 1 FROM claims WHERE active=1").fetchall()
    assert set(outcomes(queue(rig, ScriptedExecutor(rig.files), concurrency=2).run()).values()) == {"DONE"}


def test_cli_reports_refusals_in_json(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    (project / ".stagemesh" / "contracts" / "T-2.json").unlink()
    code, out, _ = run_cli(project, "--concurrency", "2", "--dry-run", "--json", "--no-auto-plan")
    data = json.loads(out)
    assert code == 1 and data["stop_reason"] == "PARTIAL" and data["refused"] == ["T-2"]
    assert data["task_outcomes"] == {"T-1": "DONE", "T-2": "REFUSED:missing_contract"}


def test_cli_queue_run_auto_plans_by_default(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    (project / ".stagemesh" / "contracts" / "T-2.json").unlink()
    code, out, _ = run_cli(project, "--concurrency", "2", "--dry-run", "--json")
    data = json.loads(out)
    assert code == 0 and data["task_outcomes"] == {"T-1": "DONE", "T-2": "DONE"} and data["refused"] == []
    assert (project / ".stagemesh" / "contracts" / "T-2.json").exists()


def test_failing_project_smoke_refuses_the_whole_run(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    raw = json.loads((project / ".stagemesh" / "profile.json").read_text(encoding="utf-8"))
    raw["forbidden_files"] = []  # smoke: no forbidden-file patterns
    (project / ".stagemesh" / "profile.json").write_text(json.dumps(raw), encoding="utf-8")
    code, out, _ = run_cli(project, "--concurrency", "2", "--dry-run", "--json")
    data = json.loads(out)
    assert code == 2 and data["stop_reason"] == "REFUSED:preflight_failed" and data["tasks"] == []
    assert any(c["name"] == "forbidden-file patterns exist" and c["status"] == "fail" for c in data["preflight"]["smoke"]["checks"])
    code, _, err = run_cli(project, "--concurrency", "2", "--dry-run")
    assert code == 2 and "forbidden-file patterns exist" in err
    db = sqlite3.connect(project / ".stagemesh" / "stagemesh.sqlite3")
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0  # nothing was started
    db.close()


def test_unusable_profile_refuses_and_no_profile_falls_back_to_contracts(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path / "a")
    (project / ".stagemesh" / "profile.json").write_text("{not json", encoding="utf-8")
    assert run_cli(project, "--concurrency", "2", "--dry-run", "--json")[0] == 2
    plain = _project(tmp_path / "b", ["T-1"], contracts=[])
    (plain / ".stagemesh" / "contracts" / "T-1.json").write_text(json.dumps(contract_for("stagemesh-task-T-1.txt")), encoding="utf-8")
    code, out, _ = run_cli(plain, "--concurrency", "1", "--dry-run", "--json")
    data = json.loads(out)
    assert code == 0 and data["preflight"]["smoke"]["ran"] is False and data["task_outcomes"] == {"T-1": "DONE"}
    assert preflight(plain, load_config(plain), require_ref=True)["ok"]


def test_cli_rejects_bad_concurrency(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    assert run_cli(project, "--concurrency", "0", "--dry-run")[0] == 2


def test_dispatch_loop_reselects_when_a_deferred_blockers_finished_before_the_liveness_check(tmp_path: Path) -> None:
    """Deterministic form of the race: a round defers a task and nothing is running any more -> select again, never exit."""
    rig = Rig(tmp_path, ["A"])
    runner = queue(rig, ScriptedExecutor(rig.files))
    rounds: list[int] = []

    def scripted_select(summary, free, running, attempted):
        rounds.append(len(rounds))
        runner._deferred_this_round = len(rounds) == 1  # round 1 defers a task whose blocker already finished
        return []

    runner._select_batch = scripted_select  # type: ignore[method-assign]
    runner._dispatch_loop(runner.summary)
    assert rounds == [0, 1]


def test_a_deferred_task_is_not_admission_checked_until_its_blocker_has_finished(tmp_path: Path) -> None:
    """The blocker's own in-flight writes dirty the checkout; checking the deferred task then would wrongly refuse it for good."""
    files = {"A": ("shared/a.txt", "a\n"), "B": ("shared/b.txt", "b\n")}
    rig = Rig(tmp_path, ["A", "B"], files=files)
    for task_id in ("A", "B"):
        write_contract(rig, task_id, allowed_files=["shared/**"])
    executor = ScriptedExecutor(files)
    runner = queue(rig, executor, concurrency=2)
    admitted: list[tuple[str, float]] = []
    original = runner._admit

    def recording_admit(task_id, contract):
        admitted.append((task_id, time.monotonic()))
        return original(task_id, contract)

    runner._admit = recording_admit  # type: ignore[method-assign]
    summary = runner.run()
    assert outcomes(summary) == {"A": "DONE", "B": "DONE"}
    ended = {task: at for kind, task, at in executor.log if kind == "end"}
    b_checks = [at for task, at in admitted if task == "B"]
    assert b_checks and all(at >= ended["A"] for at in b_checks)
