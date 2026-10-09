from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, Stage, TaskStatus
from stagemesh.operator_actions import OperatorActionError, recover_stale, release_unknown_execution
from stagemesh.persistence import Store
from stagemesh.run_ready import run_ready

from test_bounded_execution import SLEEPER, TASK, _running_execution
from test_operator_workflow import _cli, _commit, _with_baseline

REASON = "inspected: no review process exists; the run that started it was killed"


def _review_state(tmp_path: Path):
    """A task-54-like state: candidate exists, task is REVIEW/OPEN, a REVIEW execution is RUNNING with no process identity."""
    project, store, _ = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/a.md": "candidate\n"})
    store.add_candidate(TASK, sha, "codex", durable_handoff=True)
    store.advance_task(TASK, Stage.REVIEW)
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.REVIEW, candidate_sha=sha)
    return project, store, execution_id


def _try_run(store: Store, project: Path):
    return run_ready(store, project, lambda target: Coordinator(store, project, target=target), task_id=TASK, max_steps=1)


def _status(store: Store, execution_id: str) -> str:
    return store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0]


def _audit(store: Store) -> list[dict]:
    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='recovery.operator_release_unknown'").fetchall()
    return [json.loads(r[0]) for r in rows]


def test_normal_recovery_skips_unknown_review_but_continue_fences_builtin_stage_execution(tmp_path: Path) -> None:
    project, store, execution_id = _review_state(tmp_path)
    (action,) = recover_stale(store, TASK)
    assert action.kind == "REVIEW" and action.process_state == "UNKNOWN" and action.action == "SKIPPED_UNKNOWN"
    recovered = _try_run(store, project)
    assert recovered.started and recovered.stop_reason == "BLOCKED"
    assert recovered.recovered[0]["action"] == "RELEASED"
    assert recovered.recovered[0]["reason"] == "ORPHANED_BUILTIN_STAGE_EXECUTION"
    assert _status(store, execution_id) == "FAILED" and _audit(store) == []
    store.close()
    code, out = _cli(project, "recover-stale", "--task", TASK, "--json")
    assert code == 0 and json.loads(out)["actions"] == []


def test_explicit_release_terminalizes_the_unknown_review_execution_and_audits_it(tmp_path: Path) -> None:
    project, store, execution_id = _review_state(tmp_path)
    action = release_unknown_execution(store, TASK, execution_id, REASON)
    assert action.action == "RELEASED_BY_OPERATOR" and action.process_state == "UNKNOWN"
    assert _status(store, execution_id) == "FAILED"  # terminal, but the row (history) stays
    task = store.get_task(TASK)
    assert (task["stage"], task["status"]) == (Stage.REVIEW, TaskStatus.OPEN)  # the task itself is untouched
    (event,) = _audit(store)
    assert event["task_id"] == TASK and event["execution_id"] == execution_id
    assert event["previous_status"] == "RUNNING" and event["execution_kind"] == "REVIEW" and event["task_stage"] == "REVIEW"
    assert event["reason"] == REASON and event["operator_action"] == "RELEASE_UNKNOWN_EXECUTION" and event["operator"]
    assert event["process_state"] == "UNKNOWN" and event["outcome"] == "RELEASED_BY_OPERATOR"
    assert store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='recovery.orphan_execution_failed'").fetchone()[0] == 1


def test_task_becomes_runnable_after_explicit_recovery_via_the_cli(tmp_path: Path) -> None:
    project, store, execution_id = _review_state(tmp_path)
    store.close()
    code, out = _cli(project, "recover-stale", "--task", TASK, "--release-unknown", "--execution", execution_id, "--reason", REASON, "--json")
    payload = json.loads(out)
    assert code == 0 and payload["released"] == 0 and payload["actions"][0]["action"] == "RELEASED_BY_OPERATOR"
    assert (payload["stage"], payload["status"]) == ("REVIEW", "OPEN")
    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    summary = _try_run(reopened, project)
    assert summary.started and not summary.stop_reason.startswith("REFUSED:"), summary.to_dict()
    assert len(_audit(reopened)) == 1


def test_live_executions_are_never_released(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    try:
        execution_id = _running_execution(store, proc)
        with pytest.raises(OperatorActionError, match="LIVE"):
            release_unknown_execution(store, TASK, execution_id, REASON)
        assert _status(store, execution_id) == "RUNNING" and _audit(store) == []
        assert store.get_task(TASK)["status"] == TaskStatus.CLAIMED
        store.close()
        code, _ = _cli(project, "recover-stale", "--task", TASK, "--release-unknown", "--execution", execution_id, "--reason", REASON)
        assert code == 2
    finally:
        proc.kill()
        proc.wait()


def test_provably_dead_executions_are_pointed_at_the_plain_command(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    execution_id = _running_execution(store, proc)
    proc.kill()
    proc.wait()
    with pytest.raises(OperatorActionError, match="plain `recover-stale"):
        release_unknown_execution(store, TASK, execution_id, REASON)
    assert _status(store, execution_id) == "RUNNING"


def test_the_override_needs_an_execution_a_reason_and_a_matching_running_row(tmp_path: Path) -> None:
    project, store, execution_id = _review_state(tmp_path)
    for reason in ("", "   "):
        with pytest.raises(OperatorActionError, match="--reason"):
            release_unknown_execution(store, TASK, execution_id, reason)
    with pytest.raises(OperatorActionError, match="does not belong"):
        release_unknown_execution(store, TASK, "no-such-execution", REASON)
    with pytest.raises(OperatorActionError, match="task does not exist"):
        release_unknown_execution(store, "nope", execution_id, REASON)
    assert _status(store, execution_id) == "RUNNING" and _audit(store) == []
    store.close()
    assert _cli(project, "recover-stale", "--task", TASK, "--release-unknown", "--reason", REASON)[0] == 2  # no --execution: no sweep
    assert _cli(project, "recover-stale", "--task", TASK, "--release-unknown", "--execution", execution_id)[0] == 2  # no reason
    assert _cli(project, "recover-stale", "--task", TASK, "--reason", REASON)[0] == 2  # flags without the override
    reopened = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    assert _status(reopened, execution_id) == "RUNNING"
    release_unknown_execution(reopened, TASK, execution_id, REASON)
    with pytest.raises(OperatorActionError, match="not RUNNING"):
        release_unknown_execution(reopened, TASK, execution_id, REASON)  # a second release is refused, not repeated
    assert len(_audit(reopened)) == 1


def test_unknown_implementation_execution_releases_its_claim(tmp_path: Path) -> None:
    project, store, _ = _with_baseline(tmp_path)
    execution_id = _running_execution(store, None)  # claimed implementation with no recorded process
    action = release_unknown_execution(store, TASK, execution_id, REASON)
    assert action.action == "RELEASED_BY_OPERATOR" and _status(store, execution_id) == "FAILED"
    assert store.conn.execute("SELECT COUNT(*) FROM claims WHERE active=1").fetchone()[0] == 0
    assert (store.get_task(TASK)["stage"], store.get_task(TASK)["status"]) == (Stage.IMPLEMENT, TaskStatus.OPEN)
    assert _audit(store)[0]["execution_kind"] == "IMPLEMENTATION" and _audit(store)[0]["claim_id"]
