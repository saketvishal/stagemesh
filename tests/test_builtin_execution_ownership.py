"""Built-in stage executions (validation, review, integration) record their owner, so concurrent runners do not fail each other's.

Incident: three `continue --task N` processes ran at once on one project. Each one's recovery sweep failed the other two's RUNNING
validation/review rows as ORPHANED_BUILTIN_STAGE_EXECUTION, because those rows had no owner identity and were all treated as stale.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_bounded_execution import SLEEPER, TASK
from test_operator_workflow import _commit, _with_baseline
from test_run_ready import _continue, _project

import stagemesh.cli as cli_module
from stagemesh.domain import ExecutionKind, Stage
from stagemesh.persistence import Store
from stagemesh.process_identity import owner_execution_fields, popen_identity, process_identity
from stagemesh.review import Reviewer
from stagemesh.run_ready import _recover_all_dead, _recover_dead
from stagemesh.validation import Validator

BUILTIN_KINDS = [ExecutionKind.VALIDATION, ExecutionKind.REVIEW]


def _owned_by(proc: subprocess.Popen[str]) -> dict[str, object]:
    identity = popen_identity(proc)
    return {"pid": identity.pid, "process_create_time": identity.create_time, "boot_id": identity.boot_id, "executable": identity.executable}


def _status(store: Store, execution_id: str) -> tuple[str, str | None]:
    row = store.conn.execute("SELECT status, result FROM executions WHERE id=?", (execution_id,)).fetchone()
    return str(row["status"]), row["result"]


def _orphan_events(store: Store) -> list[dict]:
    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='recovery.orphan_execution_failed'").fetchall()
    return [json.loads(r[0]) for r in rows]


@pytest.fixture
def sibling():
    """A live process standing in for another runner."""
    proc = subprocess.Popen(SLEEPER)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("kind", BUILTIN_KINDS)
def test_a_live_sibling_runners_builtin_stage_is_left_alone(tmp_path: Path, sibling, kind) -> None:
    _project_dir, store, _base = _with_baseline(tmp_path)
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=kind, actor="sibling", **_owned_by(sibling))

    assert _recover_all_dead(store) == []  # another runner's startup / per-step sweep
    assert _recover_dead(store, TASK) == []  # even the sweep of the very task it belongs to: the owner is alive
    assert _status(store, execution_id) == ("RUNNING", None) and _orphan_events(store) == []


@pytest.mark.parametrize("kind", BUILTIN_KINDS)
def test_a_dead_owners_builtin_stage_is_marked_orphaned(tmp_path: Path, kind) -> None:
    _project_dir, store, _base = _with_baseline(tmp_path)
    proc = subprocess.Popen(SLEEPER)
    owner = _owned_by(proc)
    proc.kill()
    proc.wait()
    assert process_identity(proc.pid) is None
    execution_id = store.start_execution(task_id=TASK, claim_id=None, kind=kind, actor="crashed-runner", **owner)

    recovered = _recover_all_dead(store)  # provable death is recovered by any sweep, not only the owner's task

    assert [item["execution_id"] for item in recovered] == [execution_id] and recovered[0]["action"] == "RELEASED"
    assert _status(store, execution_id) == ("FAILED", "orphaned")
    (event,) = _orphan_events(store)
    assert event["execution_id"] == execution_id and event["execution_kind"] == str(kind)


@pytest.mark.parametrize("kind", BUILTIN_KINDS)
def test_unknown_owner_identity_fails_closed_for_sweeps_and_only_the_runners_own_task_fences_it(tmp_path: Path, kind) -> None:
    _project_dir, store, _base = _with_baseline(tmp_path)
    anonymous = store.start_execution(task_id=TASK, claim_id=None, kind=kind, actor="legacy")  # no owner at all
    half_known = store.start_execution(  # a pid without a create time proves nothing either
        task_id=TASK, claim_id=None, kind=ExecutionKind.VALIDATION if kind == ExecutionKind.REVIEW else ExecutionKind.REVIEW, pid=os.getpid()
    )

    assert _recover_all_dead(store) == []  # a sweep over other tasks cannot tell it from a live sibling's
    assert _status(store, anonymous)[0] == "RUNNING" and _status(store, half_known)[0] == "RUNNING"

    recovered = _recover_dead(store, TASK)  # the runner about to drive this task: existing single-runner crash recovery

    assert [item["execution_id"] for item in recovered] == [anonymous]
    assert recovered[0]["reason"] == "ORPHANED_BUILTIN_STAGE_EXECUTION"
    assert _status(store, anonymous) == ("FAILED", "orphaned")
    assert _status(store, half_known)[0] == "RUNNING"  # still unprovable: never failed automatically


def test_builtin_stages_record_the_running_process_as_owner(tmp_path: Path) -> None:
    project, store, _base = _with_baseline(tmp_path)
    sha = _commit(project, {"docs/a.md": "candidate\n"})
    store.add_candidate(TASK, sha, "codex", durable_handoff=True)
    store.advance_task(TASK, Stage.VALIDATE)

    Validator().validate(store, TASK, sha, project)
    Reviewer().review(store, TASK, sha, project)

    fields = owner_execution_fields()
    assert fields["pid"] == os.getpid() and fields["process_create_time"] is not None and fields["boot_id"]
    rows = store.conn.execute(
        "SELECT kind, pid, process_create_time, boot_id FROM executions WHERE task_id=? AND kind IN ('VALIDATION','REVIEW')", (TASK,)
    ).fetchall()
    assert {row["kind"] for row in rows} == {"VALIDATION", "REVIEW"}
    for row in rows:
        assert (row["pid"], row["process_create_time"], row["boot_id"]) == (fields["pid"], fields["process_create_time"], fields["boot_id"])


def test_a_runner_never_fails_the_builtin_stages_of_a_concurrent_runner_on_another_task(tmp_path: Path, sibling) -> None:
    project = _project(tmp_path, ["T-1", "T-2", "T-3"])
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, cli_module.load_config(project), None)
    # T-2 and T-3 are being validated / reviewed by a live sibling runner right now; T-1 is the task this runner drives
    validating = store.start_execution(task_id="T-2", claim_id=None, kind=ExecutionKind.VALIDATION, actor="sibling", **_owned_by(sibling))
    reviewing = store.start_execution(task_id="T-3", claim_id=None, kind=ExecutionKind.REVIEW, actor="sibling", **_owned_by(sibling))
    legacy = store.start_execution(task_id="T-3", claim_id=None, kind=ExecutionKind.VALIDATION, actor="legacy")  # no owner recorded
    store.close()

    code, result = _continue(project, "--task", "T-1")

    assert code == 0 and result["stop_reason"] == "DONE", result
    assert not [item for item in result["recovered"] if item["task_id"] != "T-1"]
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    assert _status(store, validating)[0] == "RUNNING" and _status(store, reviewing)[0] == "RUNNING"
    assert _status(store, legacy)[0] == "RUNNING"  # unknown owner on another task: left for an explicit recover-stale
    assert [e for e in _orphan_events(store) if e["task_id"] != "T-1"] == []
    store.close()
