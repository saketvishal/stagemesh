import subprocess

import pytest

from stagemesh.audit import record_audit
from stagemesh.contracts import GateCommand, run_gate
from stagemesh.diagnosis import _classify, _no_progress, IMPLEMENTATION_DEFECT, VALIDATION_GATE
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.persistence import Store
from stagemesh.recovery import _auto_revalidation_already_tried, auto_reintegrate_blocked_runtime_failures


@pytest.mark.parametrize("output", ["test_timeout failed", "AssertionError: account not found", "expected no such file"])
def test_test_failure_words_do_not_become_environment_failures(output):
    assert _classify(EvidenceKind.VALIDATION, {"gate_failed"}, [{"returncode": 1, "stdout": output}]) == IMPLEMENTATION_DEFECT


def test_gate_launch_permission_failure_is_structured(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(subprocess, "run", denied)
    result = run_gate(tmp_path, GateCommand("unit", ("tool",)))
    assert result.failure_kind == "environment" and result.returncode is None
    assert _classify(EvidenceKind.VALIDATION, {"gate_failed"}, [{"failure_kind": result.failure_kind, "returncode": None}]) == VALIDATION_GATE


def test_failed_revalidation_does_not_renew_its_budget(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("task", "local", "T")
    payload = {"contract_hash": "bound-contract"}
    store.add_evidence("T", "candidate", EvidenceKind.VALIDATION, EvidenceStatus.FAILED, payload)
    record_audit(store, "task.auto_revalidated_blocked_validation", {"task_id": "T", "candidate_sha": "candidate", **payload})
    # A failed recheck creates a fresh evidence id. It must not become a fresh retry budget.
    store.add_evidence("T", "candidate", EvidenceKind.VALIDATION, EvidenceStatus.FAILED, payload)
    evidence = store.conn.execute("SELECT id, created_at FROM evidence ORDER BY rowid DESC LIMIT 1").fetchone()
    assert _auto_revalidation_already_tried(store, "T", "candidate", evidence["id"], evidence["created_at"])
    store.add_evidence("T", "candidate", EvidenceKind.VALIDATION, EvidenceStatus.FAILED, {"contract_hash": "repaired-contract"})
    evidence = store.conn.execute("SELECT id, created_at FROM evidence ORDER BY rowid DESC LIMIT 1").fetchone()
    assert not _auto_revalidation_already_tried(store, "T", "candidate", evidence["id"], evidence["created_at"])
    store.close()


def test_capacity_polling_does_not_exhaust_implementation_budget(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("task", "local", "T")
    for _ in range(5):
        record_audit(store, "task.capacity_failure", {"task_id": "T", "reason": "RATE_LIMITED"})
    capacity = _no_progress(store, "T", 0, None, 2)
    assert capacity["capacity_wait"] and not capacity["repeated"] and capacity["attempts"] == 0
    for _ in range(2):
        record_audit(store, "task.implementation_unsuccessful", {"task_id": "T", "reason": "no_implementation_change"})
    assert _no_progress(store, "T", 0, None, 2)["repeated"]
    store.close()


@pytest.mark.parametrize("code,recover", [("integration_rebase_unavailable", True), ("integration_rebase_conflict", False)])
def test_integration_runtime_retry_is_bounded_and_preserves_candidate(tmp_path, code, recover):
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    store.upsert_task("task", "local", "T")
    store.add_candidate("T", "candidate", "provider", durable_handoff=True)
    store.advance_task("T", Stage.INTEGRATE)
    store.block_task("T")
    payload = {"contract_hash": "bound", "findings": [{"code": code}]}
    store.add_evidence("T", "candidate", EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, payload)
    actions = auto_reintegrate_blocked_runtime_failures(store, tmp_path)
    assert bool(actions) is recover
    assert store.latest_candidate("T")["sha"] == "candidate"
    if recover:
        assert store.get_task("T")["status"] == TaskStatus.OPEN
        assert store.get_task("T")["stage"] == Stage.INTEGRATE
        store.block_task("T")
        store.add_evidence("T", "candidate", EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, payload)
        assert not auto_reintegrate_blocked_runtime_failures(store, tmp_path)
    store.close()


def test_five_tasks_integrate_with_three_concurrent_workers(tmp_path):
    import threading
    from test_parallel import Rig, ScriptedExecutor

    tasks = ["A", "B", "C", "D", "E"]
    rig = Rig(tmp_path, tasks)
    executor = ScriptedExecutor(rig.files, barrier=threading.Barrier(3), barrier_tasks={"A", "B", "C"})
    summary = rig.runner(executor, concurrency=3).run()
    assert rig.outcomes(summary) == {task: "DONE" for task in tasks}
    assert {f"out/{task}.txt" for task in tasks} <= rig.tree()
    assert len(set(executor.worktrees.values())) == 5
    rig.store.close()


def test_rebase_runtime_error_is_not_a_content_conflict(tmp_path, monkeypatch):
    import json
    from test_parallel import Rig, ScriptedExecutor
    from test_single_task_stale_rebase import _single_task_coordinator, _advance_until, _land_on_main
    from stagemesh.git import GitWorkspace

    rig = Rig(tmp_path, ["A"])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, Stage.INTEGRATE)
    candidate = rig.store.latest_candidate("A")["sha"]
    tip = _land_on_main(rig, "other.txt", "another task\n")
    original = GitWorkspace.run

    def unavailable(self, *args, **kwargs):
        if args and args[0] == "rebase" and "--abort" not in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: Filename too long")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GitWorkspace, "run", unavailable)
    coord.tick()
    row = rig.store.conn.execute("SELECT payload FROM evidence WHERE kind=? ORDER BY rowid DESC LIMIT 1", (EvidenceKind.INTEGRATION,)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["findings"][0]["code"] == "integration_rebase_unavailable"
    assert "Filename too long" in payload["findings"][0]["message"]
    assert rig.store.latest_candidate("A")["sha"] == candidate
    assert GitWorkspace(rig.project).head() == tip
    assert rig.store.get_task("A")["status"] == TaskStatus.BLOCKED
    rig.store.close()


def test_review_clone_failure_preserves_the_underlying_error(tmp_path, monkeypatch):
    import json
    from stagemesh.providers import RuntimeCommandAdapter
    from stagemesh.capacity import CapacityKind

    monkeypatch.setattr(RuntimeCommandAdapter, "check_capacity", lambda self: CapacityKind.AVAILABLE)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 128, "", "MSYS shell failed: 0xC0000022"))
    adapter = RuntimeCommandAdapter("reviewer", ("tool",))
    result = json.loads(adapter.review_candidate("review", tmp_path, "candidate"))
    assert result["decision"] == "INFRASTRUCTURE_FAILURE"
    assert "0xC0000022" in result["reason"]
