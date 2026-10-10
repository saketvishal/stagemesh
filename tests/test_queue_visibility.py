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
    """Start a RUNNING execution that classifies as LIVE, DEAD, or UNKNOWN."""
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
    assert report["state"] == "running"
    assert report["last_event"] is None and report["stop_reason"] is None
    assert report["execution_ids"] == report["task_ids"] == report["pids"] == []
    assert report["stale_execution_ids"] == report["unknown_execution_ids"] == []
    assert format_queue_control(report) == [
        "queue admission: running (active executions: 0)",
        "  last event: none",
    ]


def test_running_queue_with_zero_active_executions_is_not_a_phantom_blocker(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)
    record_audit(
        store,
        QUEUE_CONTROL_EVENT,
        {
            "at": "2099-01-01T00:00:00.000+00:00",
            "state": "running",
            "reason": "previous queue run started",
            "terminate_running": False,
            "source": "runner",
        },
    )
    report = queue_control_report(store)
    store.close()

    assert report["state"] == "running"
    assert report["active_executions"] == []

    code, out, _ = run_stagemesh_cli(project, "continue", "--dry-run", "--json", "--task", "T-1")
    result = json.loads(out)
    assert code == 0 and result["started"] is True


def test_status_shows_compact_queue_counts_in_text_and_json(tmp_path: Path) -> None:
    project, _ = _project_with_executions(tmp_path)
    store = _store(project)
    store.block_task("T-2")
    record_audit(store, "task.blocked", {"task_id": "T-2", "reason": "validation_gate", "stage": "VALIDATE"})
    store.close()

    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    data = json.loads(out)
    assert code == 0
    assert data["task_count"] == 2
    assert data["open_task_count"] == 1
    assert data["eligible_open_task_count"] == 1
    assert data["blocked_task_count"] == 1
    assert data["blocked_reason_buckets"] == {"validation_gate": 1}
    assert data["running_count"] == 3
    assert data["stale_execution_count"] == 1
    assert data["source_ready_count"] == 2

    code, out, _ = run_stagemesh_cli(project, "status")
    assert code == 0
    assert "counts: tracked=2 open=1 eligible_open=1 blocked=1 running=3 stale=1 source_ready=2" in out
    assert "blocked_bucket validation_gate: 1" in out

    code, out, _ = run_stagemesh_cli(project, "operator", "--json")
    assert code == 0
    operator = json.loads(out)
    buckets = next(section for section in operator["sections"] if section["name"] == "Blocked Buckets")
    assert buckets["rows"] == [{"count": 1, "reason": "validation_gate"}]

    dashboard = project / "dashboard.html"
    code, out, _ = run_stagemesh_cli(project, "dashboard", "--output", str(dashboard), "--json")
    assert code == 0
    assert "Blocked Buckets" in json.loads(out)["sections"]
    assert "validation_gate" in dashboard.read_text(encoding="utf-8")


def test_json_surfaces_expose_state_event_reason_and_process(tmp_path: Path) -> None:
    project, ids = _project_with_executions(tmp_path)
    code, _, _ = run_stagemesh_cli(
        project,
        "queue-control",
        "stop",
        "--reason",
        "deploy freeze",
        "--terminate-running",
        "--json",
    )
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
        assert event["state"] == "stopping" and event["reason"] == "deploy freeze", name
        assert event["terminate_running"] is True, name
        assert event["source"] == "operator" and event["at"], name
        assert sorted(control["execution_ids"]) == sorted(ids.values()), name
        assert control["task_ids"] == ["T-1", "T-2"], name
        assert control["pids"] == [os.getpid()], name
        assert control["stale_execution_ids"] == [ids["DEAD"]], name
        assert control["unknown_execution_ids"] == [ids["UNKNOWN"]], name
        by_id = {e["id"]: e for e in control["active_executions"]}
        assert {k: by_id[v]["process_state"] for k, v in ids.items()} == {
            "LIVE": "LIVE",
            "DEAD": "DEAD",
            "UNKNOWN": "UNKNOWN",
        }, name
        live = by_id[ids["LIVE"]]
        assert live["task_id"] == "T-1" and live["pid"] == os.getpid(), name

    code, out, _ = run_stagemesh_cli(project, "operator", "--json")
    assert code == 0
    operator = json.loads(out)
    active_section = next(
        section for section in operator["sections"] if section["name"] == "Active Executions"
    )
    active_rows = {row["id"]: row for row in active_section["rows"]}
    assert active_rows[ids["LIVE"]]["provider"] == "unknown"
    assert active_rows[ids["LIVE"]]["pid"] == os.getpid()
    assert active_rows[ids["LIVE"]]["pid_label"] == str(os.getpid())
    assert active_rows[ids["DEAD"]]["stale"] is True
    assert active_rows[ids["DEAD"]]["process_state"] == "DEAD"
    assert active_rows[ids["UNKNOWN"]]["stage"] == "VALIDATION"

    dashboard = project / "dashboard.html"
    code, out, _ = run_stagemesh_cli(project, "dashboard", "--output", str(dashboard), "--json")
    assert code == 0
    dashboard_data = json.loads(out)
    dashboard_rows = {row["id"]: row for row in dashboard_data["active_executions"]}
    assert dashboard_rows[ids["LIVE"]]["pid"] == os.getpid()
    assert dashboard_rows[ids["DEAD"]]["stale"] is True
    dashboard_text = dashboard.read_text(encoding="utf-8")
    assert "<h2>Active Executions</h2>" in dashboard_text
    assert str(os.getpid()) in dashboard_text and "DEAD" in dashboard_text


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

    code, out, _ = run_stagemesh_cli(project, "operator")
    assert code == 0
    assert (
        f"active_execution task=T-1 stage=REVIEW provider=unknown pid={pid} process=LIVE stale=False"
        in out
    )
    assert (
        f"active_execution task=T-2 stage=IMPLEMENTATION provider=unknown pid={pid} process=DEAD stale=True"
        in out
    )

    code, out, _ = run_stagemesh_cli(project, "queue-control", "status")
    assert code == 0
    lines = [line.strip() for line in out.splitlines()]
    assert lines[:2] == ["queue admission: stopping", "active executions: 3"], out
    for fragment in expected[1:]:
        assert fragment in out, (fragment, out)

    code, out, _ = run_stagemesh_cli(project, "health")
    assert code == 0
    assert "queue_admission: stopping" in out and "queue_active_executions: 3" in out
    for fragment in expected[1:]:
        assert fragment in out, (fragment, out)


def test_stopped_event_keeps_operator_reason_until_resume(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    run_stagemesh_cli(project, "queue-control", "stop", "--reason", "deploy freeze")
    store = _store(project)
    record_audit(
        store,
        QUEUE_CONTROL_EVENT,
        {
            "at": "2099-01-01T00:00:00.000+00:00",
            "state": "stopped",
            "reason": "previous stop completed",
            "terminate_running": False,
            "source": "runner",
        },
    )
    report = queue_control_report(store)
    store.close()
    assert report["state"] == "stopped"
    assert report["last_event"]["reason"] == "previous stop completed"
    assert report["last_event"]["source"] == "runner"
    assert report["stop_reason"] == "deploy freeze"

    code, out, _ = run_stagemesh_cli(project, "status")
    assert code == 0 and "stop reason: deploy freeze" in out and "last event: stopped" in out

    run_stagemesh_cli(project, "queue-control", "resume", "--reason", "all clear")
    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    control = json.loads(out)["queue_control"]
    assert control["state"] == "running" and control["stop_reason"] is None
    assert control["last_event"]["state"] == "resumed"
    assert control["last_event"]["reason"] == "all clear"
    code, out, _ = run_stagemesh_cli(project, "status")
    assert "stop reason" not in out and "last event: resumed" in out


def test_display_commands_do_not_change_queue_control_state(tmp_path: Path) -> None:
    project, _ = _project_with_executions(tmp_path)
    run_stagemesh_cli(project, "queue-control", "pause", "--reason", "maintenance")

    def snapshot() -> tuple[int, list[tuple[str, str]]]:
        store = _store(project)
        try:
            events = store.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type=?", (QUEUE_CONTROL_EVENT,)
            ).fetchone()[0]
            executions = [(r["id"], r["status"]) for r in store.running_executions()]
            return events, sorted(executions)
        finally:
            store.close()

    before = snapshot()
    commands = (
        ("status",),
        ("health", "--json"),
        ("task-doctor", "--task", "T-1"),
        ("queue-control", "status"),
    )
    for argv in commands:
        run_stagemesh_cli(project, *argv)
    assert snapshot() == before


def _event(store: Store, state: str, reason: str, source: str) -> None:
    record_audit(
        store,
        QUEUE_CONTROL_EVENT,
        {
            "at": "2099-01-01T00:00:00.000+00:00",
            "state": state,
            "reason": reason,
            "terminate_running": False,
            "source": source,
        },
    )


def test_resume_and_pause_clear_a_prior_cycle_stop_reason(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)
    _event(store, "stopping", "deploy freeze", "operator")
    _event(store, "stopped", "operator stop completed", "runner")
    assert queue_control_report(store)["stop_reason"] == "deploy freeze"
    _event(store, "resumed", "all clear", "operator")
    assert queue_control_report(store)["stop_reason"] is None
    _event(store, "stopped", "keyboard interrupt stop completed", "runner")
    report = queue_control_report(store)
    assert report["state"] == "stopped"
    assert report["stop_reason"] == "keyboard interrupt stop completed"
    _event(store, "paused", "maintenance", "operator")
    assert queue_control_report(store)["stop_reason"] is None
    _event(store, "stopping", "second freeze", "operator")
    assert queue_control_report(store)["stop_reason"] == "second freeze"
    store.close()


def test_execution_without_a_pid_is_unknown_not_stale_and_prints_cleanly(tmp_path: Path) -> None:
    project = _smoke_project(tmp_path)
    store = _store(project)
    cli_module._sync_all_sources(store, project, load_config(project), None)
    execution_id = store.start_execution(
        task_id="T-1", claim_id=None, kind=ExecutionKind.REVIEW, actor="claude"
    )
    store.close()

    code, out, _ = run_stagemesh_cli(project, "status", "--json")
    control = json.loads(out)["queue_control"]
    assert code == 0
    assert control["execution_ids"] == [execution_id] and control["task_ids"] == ["T-1"]
    assert control["pids"] == [] and control["stale_execution_ids"] == []
    assert control["unknown_execution_ids"] == [execution_id]
    execution = control["active_executions"][0]
    assert execution["pid"] is None and execution["process_state"] == "UNKNOWN"

    surfaces = (
        ("status",),
        ("task-doctor", "--task", "T-1"),
        ("queue-control", "status"),
        ("health",),
    )
    marker = f"execution {execution_id}: task T-1 REVIEW pid unknown process UNKNOWN"
    for argv in surfaces:
        code, out, _ = run_stagemesh_cli(project, *argv)
        assert code == 0, argv
        assert marker in out, (argv, out)
        assert "tasks: T-1; pids: none known" in out, (argv, out)
        assert f"unknown process identity: {execution_id}" in out, (argv, out)
        assert "stale (process gone)" not in out, (argv, out)

    code, out, _ = run_stagemesh_cli(project, "operator")
    assert code == 0
    assert (
        "stage=REVIEW provider=claude pid=no pid recorded process=UNKNOWN stale=False"
        in out
    )

    code, out, _ = run_stagemesh_cli(project, "operator", "--json")
    assert code == 0
    data = json.loads(out)
    row = next(
        row
        for section in data["sections"]
        if section["name"] == "Active Executions"
        for row in section["rows"]
    )
    assert row["pid"] is None
    assert row["pid_label"] == "no pid recorded"
    assert row["provider"] == "claude"
    assert row["stage"] == "REVIEW"
    assert row["process_state"] == "UNKNOWN"
    assert row["stale"] is False


def _history(store: Store, *events: tuple[str, str, str]) -> dict:
    """Record (state, reason, source) queue-control events in order and return the resulting report."""
    for state, reason, source in events:
        record_audit(
            store,
            QUEUE_CONTROL_EVENT,
            {"at": "2099-01-01T00:00:00.000+00:00", "state": state, "reason": reason, "terminate_running": False, "source": source},
        )
    return queue_control_report(store)


_FIRST_STOP = (("stopping", "deploy freeze", "operator"), ("stopped", "operator stop completed", "runner"))
_NEW_RUN = ("running", "queue run started", "runner")


def test_scenario_a_new_run_then_ctrl_c_reports_the_ctrl_c_reason(tmp_path: Path) -> None:
    store = _store(_smoke_project(tmp_path))
    report = _history(
        store, *_FIRST_STOP, _NEW_RUN,
        ("stopping", "keyboard interrupt", "runner"), ("stopped", "keyboard interrupt stop completed", "runner"),
    )
    store.close()
    assert report["state"] == "stopped" and report["stop_reason"] == "keyboard interrupt"


def test_scenario_b_stop_reason_never_reaches_back_past_a_new_run(tmp_path: Path) -> None:
    store = _store(_smoke_project(tmp_path))
    report = _history(store, *_FIRST_STOP, _NEW_RUN, ("stopped", "new run completed stop", "runner"))
    store.close()
    assert report["state"] == "stopped"
    assert report["stop_reason"] == "new run completed stop"  # not the earlier run's "deploy freeze"
    assert report["last_event"]["reason"] == "new run completed stop"


def test_scenario_c_pause_resume_stop_across_runs_uses_only_the_current_cycle(tmp_path: Path) -> None:
    store = _store(_smoke_project(tmp_path))
    report = _history(
        store, *_FIRST_STOP, _NEW_RUN,
        ("paused", "maintenance", "operator"), ("resumed", "all clear", "operator"),
        ("stopping", "second freeze", "operator"),
    )
    assert report["state"] == "stopping" and report["stop_reason"] == "second freeze"
    report = _history(store, ("stopped", "operator stop completed", "runner"))
    assert report["state"] == "stopped" and report["stop_reason"] == "second freeze"
    report = _history(store, _NEW_RUN, ("paused", "again", "operator"), ("resumed", "again done", "operator"),
                      ("stopped", "closed without a request", "runner"))
    store.close()
    assert report["stop_reason"] == "closed without a request"  # nothing from the earlier cycles


def test_scenario_d_ctrl_c_after_a_new_run_never_reports_a_previous_cycles_reason(tmp_path: Path) -> None:
    store = _store(_smoke_project(tmp_path))
    first = _history(store, *_FIRST_STOP, _NEW_RUN)
    assert first["state"] == "running" and first["stop_reason"] is None  # a live run shows no stop reason at all
    report = _history(
        store, ("stopping", "keyboard interrupt", "runner"), ("stopped", "keyboard interrupt stop completed", "runner")
    )
    store.close()
    assert report["stop_reason"] == "keyboard interrupt"


def test_a_stop_honored_at_startup_keeps_its_own_reason_without_a_running_event(tmp_path: Path) -> None:
    """A stopping request stored before a run starts is the same cycle: no `running` separates it from its `stopped`."""
    store = _store(_smoke_project(tmp_path))
    report = _history(store, ("stopping", "deploy freeze", "operator"), ("stopped", "queue admission stop completed", "runner"))
    store.close()
    assert report["stop_reason"] == "deploy freeze"
