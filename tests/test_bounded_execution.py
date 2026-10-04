from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.diagnosis import DiagnosisPolicy
from stagemesh.coordinator import Coordinator, TargetSelection, TargetSelectionError
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process, popen_identity, process_identity
from stagemesh.remediation import RemediationPolicy
from stagemesh.scheduling import Scheduler

TASK = "TASK-1"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def _setup(tmp_path: Path, contract: dict[str, object] | None = None) -> tuple[Path, Store]:
    project = tmp_path / "repo"
    project.mkdir()
    workspace = GitWorkspace(project)
    workspace.init_if_needed()
    workspace.run("config", "user.email", "test@example.invalid")
    workspace.run("config", "user.name", "StageMesh Test")
    (project / "src").mkdir()
    (project / "docs").mkdir()
    (project / "src" / "app.py").write_text("X = 0\n", encoding="utf-8")
    (project / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    workspace.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    contract = contract or {
        "objective": "docs only",
        "allowed_files": ["docs/**"],
        "required_tests": [{"name": "smoke", "command": [sys.executable, "-c", "pass"]}],
    }
    (runtime / "contracts" / f"{TASK}.json").write_text(json.dumps(contract), encoding="utf-8")
    store = Store(runtime / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("bounded", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    return project, store


def _provider(tmp_path: Path, body: str) -> SubprocessExecutor:
    script = tmp_path / "provider.py"
    script.write_text(body, encoding="utf-8")
    return SubprocessExecutor([sys.executable, str(script)], name="codex")


def _out_of_scope_provider(tmp_path: Path, prompts: Path | None = None) -> SubprocessExecutor:
    counter = tmp_path / "counter.txt"
    capture = ""
    if prompts is not None:
        capture = (
            "import sys\n"
            f"d = pathlib.Path({str(prompts)!r}); d.mkdir(exist_ok=True)\n"
            "(d / ('prompt-%d.txt' % len(list(d.iterdir())))).write_text(sys.stdin.read(), encoding='utf-8')\n"
        )
    return _provider(
        tmp_path,
        "import pathlib\n"
        f"{capture}"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) + 1 if c.exists() else 1\n"
        "c.write_text(str(n))\n"
        "pathlib.Path('src/app.py').write_text('X = %d' % n)\n",
    )


def _events(store: Store, event_type: str) -> list[dict[str, object]]:
    return [json.loads(row["payload"]) for row in store.audit_events() if row["event_type"] == event_type]


def _drive_to_blocked(coordinator: Coordinator, store: Store) -> None:
    for _ in range(14):
        coordinator.tick()
        if store.get_task(TASK)["status"] == TaskStatus.BLOCKED:
            return
    raise AssertionError("task never blocked")


def test_remediation_budget_survives_new_candidate_shas_and_blocks(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    coordinator = Coordinator(
        store,
        project,
        executor=_out_of_scope_provider(tmp_path),
        remediation_policy=RemediationPolicy(max_attempts=3),
        diagnosis_policy=DiagnosisPolicy(stop_on_repeat=False),  # this test is about the budget itself, not early diagnosis
    )

    _drive_to_blocked(coordinator, store)

    shas = {row["sha"] for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (TASK,))}
    assert len(shas) == 4  # every candidate was a fresh SHA, yet the budget was not reset
    assert store.task_remediation_count(TASK, "VALIDATE") == 3
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert len(_events(store, "task.remediation_exhausted")) == 1


def test_blocked_task_is_unschedulable_and_receives_no_execution(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    marker = tmp_path / "invoked.marker"
    executor = _provider(tmp_path, f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n")
    coordinator = Coordinator(store, project, executor=executor)
    store.block_task(TASK)
    before = len(store.audit_events())

    decision = Scheduler(store).decision(TASK)

    assert decision.eligible is False and decision.reason == "blocked"
    for _ in range(3):
        assert coordinator.tick() == 0
    assert not marker.exists()
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    assert store.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert len(store.audit_events()) == before
    with pytest.raises(TargetSelectionError):
        Coordinator(store, project, executor=executor, target=TargetSelection(TASK)).tick()


def test_remediation_prompt_carries_persisted_findings_only(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    prompts = tmp_path / "prompts"
    store.upsert_finding("old", TASK, "deadbeef", "error", "OLD-UNRELATED-FINDING")
    coordinator = Coordinator(store, project, executor=_out_of_scope_provider(tmp_path, prompts))

    for _ in range(3):  # implement, validate (fails, queues remediation), implement again
        coordinator.tick()

    first = (prompts / "prompt-0.txt").read_text(encoding="utf-8")
    second = (prompts / "prompt-1.txt").read_text(encoding="utf-8")
    failed_sha = store.latest_task_remediation(TASK)["candidate_sha"]
    assert "Previous candidate" not in first and "Required remediation" not in first
    assert "Objective: docs only" in first
    assert f"Previous candidate {failed_sha} failed VALIDATE" in second
    assert "src/app.py is outside the allowed file scope" in second
    assert "Objective: docs only" in second
    assert "OLD-UNRELATED-FINDING" not in second


def test_retry_task_unblocks_with_fresh_budget_and_keeps_history(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    coordinator = Coordinator(
        store, project, executor=_out_of_scope_provider(tmp_path), remediation_policy=RemediationPolicy(max_attempts=1)
    )
    _drive_to_blocked(coordinator, store)
    findings = store.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    evidence = store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    args = argparse.Namespace(project=str(project), task=TASK, json=True)

    assert cli_module.command_retry_task(args) == 0

    task = store.get_task(TASK)
    assert task["status"] == TaskStatus.OPEN
    assert _events(store, "task.unblocked") == [{"task_id": TASK, "remediation_attempts_cleared": 1}]
    assert store.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == findings
    assert store.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == evidence
    assert store.conn.execute("SELECT COUNT(*) FROM task_remediations").fetchone()[0] == 1  # retained, cleared
    assert store.task_remediation_count(TASK, "VALIDATE") == 0
    assert coordinator.tick() == 1  # scheduling resumes
    assert cli_module.command_retry_task(args) == 2  # no longer blocked


def test_provider_timeout_terminates_process_and_releases_claim(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    executor = SubprocessExecutor(SLEEPER, name="codex", timeout_seconds=2)
    started = time.monotonic()

    assert Coordinator(store, project, executor=executor).tick() == 0

    assert time.monotonic() - started < 60
    execution = store.conn.execute("SELECT * FROM executions WHERE kind=?", (ExecutionKind.IMPLEMENTATION,)).fetchone()
    assert execution["status"] == ExecutionStatus.FAILED
    saved = store.execution_process_identity(execution["id"])
    assert saved.is_known
    assert classify_process(saved, process_identity(saved.pid)) == "DEAD"
    assert _events(store, "task.implementation_unsuccessful")[0]["reason"] == "provider_timeout"
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.IMPLEMENT, TaskStatus.OPEN)
    assert store.conn.execute("SELECT COUNT(*) FROM claims WHERE active=1").fetchone()[0] == 0
    assert store.latest_candidate(TASK) is None


def _running_execution(store: Store, proc: subprocess.Popen[str] | None) -> str:
    claim_id = store.acquire_claim(TASK, "worker", lease_seconds=-1)  # lease already expired
    assert claim_id is not None
    identity = popen_identity(proc) if proc is not None else None
    return store.start_execution(
        task_id=TASK,
        claim_id=claim_id,
        kind=ExecutionKind.IMPLEMENTATION,
        pid=identity.pid if identity else None,
        process_create_time=identity.create_time if identity else None,
        boot_id=identity.boot_id if identity else None,
        executable=identity.executable if identity else None,
    )


def _launcher_marker(tmp_path: Path) -> tuple[SubprocessExecutor, Path]:
    marker = tmp_path / "second-launch.marker"
    body = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\npathlib.Path('docs/a.md').write_text('new')\n"
    return _provider(tmp_path, body), marker


def test_live_provider_prevents_second_implementation(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    executor, marker = _launcher_marker(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    try:
        _running_execution(store, proc)

        assert Coordinator(store, project, executor=executor).tick() == 0

        assert not marker.exists()
        assert store.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
    finally:
        proc.kill()
        proc.wait()


def test_unknown_identity_fails_safe(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    executor, marker = _launcher_marker(tmp_path)
    execution_id = _running_execution(store, None)

    assert Coordinator(store, project, executor=executor).tick() == 0

    assert not marker.exists()
    assert store.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
    assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "RUNNING"


def test_dead_provider_is_recovered_and_task_retried(tmp_path: Path) -> None:
    project, store = _setup(tmp_path)
    executor, marker = _launcher_marker(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    execution_id = _running_execution(store, proc)
    proc.kill()
    proc.wait()

    assert Coordinator(store, project, executor=executor).tick() == 1

    assert store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0] == "FAILED"
    assert _events(store, "recovery.stale_claim_released")
    assert marker.exists()  # a fresh implementation ran after recovery
    assert store.latest_candidate(TASK) is not None


def test_popen_identity_is_comparable_with_later_observation(tmp_path: Path) -> None:
    proc = subprocess.Popen(SLEEPER)
    try:
        saved = popen_identity(proc)
        assert saved.is_known and saved.executable
        assert classify_process(saved, process_identity(proc.pid)) == "LIVE"
        reused = type(saved)(saved.pid, (saved.create_time or 0) + 1e7, saved.boot_id, saved.executable)
        assert classify_process(reused, process_identity(proc.pid)) == "DEAD"  # same PID, different process
    finally:
        proc.kill()
        proc.wait()
    assert classify_process(saved, process_identity(saved.pid)) == "DEAD"
