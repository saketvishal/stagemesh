from __future__ import annotations

import json
import os
from pathlib import Path

from test_queue_run import _smoke_project, run_stagemesh_cli

import stagemesh.cli as cli_module
from stagemesh.audit import record_audit
from stagemesh.config import load_config
from stagemesh.domain import ExecutionKind
from stagemesh.parallel import QUEUE_CONTROL_EVENT
from stagemesh.persistence import Store
from stagemesh.process_identity import process_identity
from stagemesh.queue_visibility import format_queue_control, queue_control_report


def _store(project: Path) -> Store:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return store


def _start(store: Store, task_id: str, kind: ExecutionKind, process: str) -> str:
    """Start a RUNNING execution whose saved process identity classifies as LIVE, DEAD or UNKNOWN."""
    if process == "UNKNOWN":  # a pid with no recorded create time/boot id cannot be identified
        return store.start_execution(task_id=task_id, claim_id=None, kind=kind, pid=os.getpid())
    observed = process_identity(os.getpid())
    assert observed is not None and observed.is_known
    create_time = observed.create_time if process == "LIVE" else observed.create_time + 12345.0
    return store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=kind,
        pid=os.getpid(),
        process_create_time=create_time,
        boot_id=observed.boot_id,
        executable=observed.executable,
    )


def _project_with_executions(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = _smoke_project(tmp_path)
    store = _store(project)
    cli_module._sync_all_sources(store, project, load_config(project), None)
    ids = {
        "LIVE": _start(store, "T-1", ExecutionKind.REVIEW, "LIVE"),
        "DEAD": _start(store, "T-2", ExecutionKind.IMPLEMENTATION, "DEAD"),
        "UNKNOWN": _start(store, "T-2", ExecutionKind.VALIDATION, "UNKNOWN"),
    }
    store.close()
    return project, ids


def test_idle_queue_reports_no_event_stop_reason_or_executions(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)
    report = queue_control_report(store)
    store.close()
    assert report["state"] == "running" and report["last_event"] is None and report["stop_reason"] is None
    assert report["execution_ids"] == report["task_ids"] == report["pids"] == []
    assert report["stale_execution_ids"] == report["unknown_execution_ids"] == []
    assert format_queue_control(report) == ["queue admission: running (active executions: 0)", "  last event: none"]


def test_json_surfaces_state_last_event_stop_reason_ids_and_process_identity(tmp_path: Path) -> None:
    project, ids = _project_with_executions(tmp_path)
    code, _, _ = run_stagemesh_cli(project, "queue-control", "stop", "--reason", "deploy freeze", "--terminate-running", "--json")
    assert code == 0

    views = {
        "status": run_stagemesh_cli(project, "status", "--json"),
        "health": run_stagemesh_cli(project, "health", "--json"),
        "task-doctor": run_stagemesh_cli(project, "task-doctor", "--task", "T-1", "--json"),
        "queue-control": run_stagemesh_cli(project, "queue-control", "status", "--json"),
    }
    for name, (code, out, _) in views.items():
        assert code == 0, name
        control = json.loads(out)["queue_control"] if name != "queue-control" else json.loads(out)
        assert control["state"] == "stopping" and control["stopping"] is True, name
        assert control["stop_reason"] == "deploy freeze", name
        event = control["last_event"]
        assert event["state"] == "stopping" and event["reason"] == "deploy freeze" and event["terminate_running"] is True, name
        assert event["source"] == "operator" and event["at"], name
        assert sorted(control["execution_ids"]) == sorted(ids.values()), name
        assert control["task_ids"] == ["T-1", "T-2"], name
        assert control["pids"] == [os.getpid()], name
        assert control["stale_execution_ids"] == [ids["DEAD"]], name
        assert control["unknown_execution_ids"] == [ids["UNKNOWN"]], name
        by_id = {e["id"]: e for e in control["active_executions"]}
        assert {k: by_id[v]["process_state"] for k, v in ids.items()} == {"LIVE": "LIVE", "DEAD": "DEAD", "UNKNOWN": "UNKNOWN"}, name
        assert by_id[ids["LIVE"]]["task_id"] == "T-1" and by_id[ids["LIVE"]]["pid"] == os.getpid(), name


def test_text_status_health_and_task_doctor_show_operator_fields(tmp_path: Path) -> None:
    project, ids = _project_with_executions(tmp_path)
    run_stagemesh_cli(project, "queue-control", "stop", "--reason", "deploy freeze")

    pid = os.getpid()
    expected = [
        "queue admission: stopping (active executions: 3)",
        "last event: stopping at ",
        "by operator: deploy freeze",
        "stop reason: deploy freeze",
        f"execution {ids['LIVE']}: task T-1 REVIEW pid {pid} process LIVE",
        f"execution {ids['DEAD']}: task T-2 IMPLEMENTATION pid {pid} process DEAD",
        f"execution {ids['UNKNOWN']}: task T-2 VALIDATION pid {pid} process UNKNOWN",
        f"tasks: T-1, T-2; pids: {pid}",
        f"stale (process gone): {ids['DEAD']}",
        f"unknown process identity: {ids['UNKNOWN']}",
    ]
    for argv in (("status",), ("task-doctor", "--task", "T-1")):
        code, out, _ = run_stagemesh_cli(project, *argv)
        assert code == 0, argv
        for fragment in expected:
            assert fragment in out, (argv, fragment, out)

    code, out, _ = run_stagemesh_cli(project, "queue-control", "status")
    assert code == 0
    lines = [line.strip() for line in out.splitlines()]
    assert lines[:2] == ["queue admission: stopping", "active executions: 3"], out  # legacy shape, details follow
    for fragment in expected[1:]:
        assert fragment in out, (fragment, out)

    code, out, _ = run_stagemesh_cli(project, "health")
    assert code == 0
    assert "queue_admission: stopping" in out and "queue_active_executions: 3" in out
    for fragment in expected[1:]:
        assert fragment in out, (fragment, out)


def test_runner_stopped_event_keeps_operator_stop_reason_and_resume_clears_it(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    run_stagemesh_cli(project, "queue-control", "stop", "--reason", "deploy freeze")
    store = _store(project)
    record_audit(
        store,
        QUEUE_CONTROL_EVENT,
        {"at": "2099-01-01T00:00:00.000+00:00", "state": "stopped", "reason": "previous stop completed", "terminate_running": False, "source": "runner"},
    )
    report = queue_control_report(store)
    store.close()
    assert report["state"] == "stopped"
    assert report["last_event"]["reason"] == "previous stop completed" and report["last_event"]["source"] == "runner"
    assert report["stop_reason"] == "deploy freeze"

    code, out, _ = run_stagemesh_cli(project, "status")
    assert code == 0 and "stop reason: deploy freeze" in out and "last event: stopped" in out

    run_stagemesh_cli(project, "queue-control", "resume", "--reason", "all clear")
    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    control = json.loads(out)["queue_control"]
    assert control["state"] == "running" and control["stop_reason"] is None
    assert control["last_event"]["state"] == "resumed" and control["last_event"]["reason"] == "all clear"
    code, out, _ = run_stagemesh_cli(project, "status")
    assert "stop reason" not in out and "last event: resumed" in out


def test_display_commands_do_not_change_queue_control_state(tmp_path: Path) -> None:
    project, _ = _project_with_executions(tmp_path)
    run_stagemesh_cli(project, "queue-control", "pause", "--reason", "maintenance")

    def snapshot() -> tuple[int, list[tuple[str, str]]]:
        store = _store(project)
        try:
            events = store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?", (QUEUE_CONTROL_EVENT,)).fetchone()[0]
            executions = [(r["id"], r["status"]) for r in store.running_executions()]
            return events, sorted(executions)
        finally:
            store.close()

    before = snapshot()
    for argv in (("status",), ("health", "--json"), ("task-doctor", "--task", "T-1"), ("queue-control", "status")):
        run_stagemesh_cli(project, *argv)
    assert snapshot() == before


def test_ctrl_c_stop_after_resume_does_not_show_an_earlier_cycles_stop_reason(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)

    def event(state: str, reason: str, source: str) -> None:
        record_audit(store, QUEUE_CONTROL_EVENT, {"at": "2099-01-01T00:00:00.000+00:00", "state": state, "reason": reason, "terminate_running": False, "source": source})

    event("stopping", "deploy freeze", "operator")
    event("stopped", "operator stop completed", "runner")
    assert queue_control_report(store)["stop_reason"] == "deploy freeze"
    event("resumed", "all clear", "operator")
    assert queue_control_report(store)["stop_reason"] is None
    event("stopped", "keyboard interrupt stop completed", "runner")  # Ctrl+C closes a stop nobody requested this cycle
    report = queue_control_report(store)
    assert report["state"] == "stopped" and report["stop_reason"] == "keyboard interrupt stop completed"
    event("paused", "maintenance", "operator")
    assert queue_control_report(store)["stop_reason"] is None
    event("stopping", "second freeze", "operator")
    assert queue_control_report(store)["stop_reason"] == "second freeze"
    store.close()


def test_execution_without_a_pid_is_unknown_not_stale_and_prints_cleanly(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)
    cli_module._sync_all_sources(store, project, load_config(project), None)
    execution_id = store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.IMPLEMENTATION)  # in-process executor: no pid
    store.close()

    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    control = json.loads(out)["queue_control"]
    assert code == 0 and control["execution_ids"] == [execution_id] and control["task_ids"] == ["T-1"]
    assert control["pids"] == [] and control["stale_execution_ids"] == []
    assert control["unknown_execution_ids"] == [execution_id]
    execution = control["active_executions"][0]
    assert execution["pid"] is None and execution["process_state"] == "UNKNOWN"

    for argv in (("status",), ("task-doctor", "--task", "T-1"), ("queue-control", "status"), ("health",)):
        code, out, _ = run_stagemesh_cli(project, *argv)
        assert code == 0, argv
        assert f"execution {execution_id}: task T-1 IMPLEMENTATION pid unknown process UNKNOWN" in out, (argv, out)
        assert "tasks: T-1; pids: none known" in out, (argv, out)
        assert f"unknown process identity: {execution_id}" in out and "stale (process gone)" not in out, (argv, out)
