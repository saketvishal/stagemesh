from __future__ import annotations

import json
import sys
from dataclasses import replace

import pytest
from test_provider_pool import TASK, Rig

from stagemesh.diagnosis import TOOL_FAILURE_EVENT
from stagemesh.domain import ExecutionStatus
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.queue_control import OperatorStopRequested
from stagemesh.workspaces import task_workspace


def _runtime(tmp_path, monkeypatch, message, *, timeout=False):
    rig = Rig(tmp_path, {"controlled": "ok"})
    rig.tick()
    script = tmp_path / "controlled.py"
    marker = tmp_path / "calls.txt"
    script.write_text(
        "import pathlib,sys,time\n"
        f"p=pathlib.Path({str(marker)!r});p.write_text((p.read_text() if p.exists() else '')+'call\\n')\n"
        f"sys.stderr.write({message!r});sys.stderr.flush()\n"
        + ("time.sleep(30)\n" if timeout else "raise SystemExit(1)\n"),
        encoding="utf-8",
    )
    monkeypatch.setattr("stagemesh.providers._build_task_prompt", lambda *_a, **_kw: "PROMPT_CONTENT_CANARY")
    # Model a prompt-bearing runtime argv while keeping the configured identity distinct.
    monkeypatch.setattr("stagemesh.providers._command_and_input", lambda command, prompt: ([*command, prompt], ""))
    adapter = RuntimeCommandAdapter("controlled", (sys.executable, str(script), "{prompt}"),
                                    timeout_seconds=0.5 if timeout else 10)
    return rig, adapter, marker


def _events(store):
    return [json.loads(row["payload"]) for row in store.task_audit_events(TOOL_FAILURE_EVENT, TASK)]


@pytest.mark.parametrize(("message", "kind", "timeout"), [
    ("ModuleNotFoundError: No module named 'fixture' token=ghp_FIXTURESECRET", "missing_dependency", False),
    ("AssertionError: expected fixture success", "assertion_failure", False),
    ("EACCES: permission denied", "permission", False),
    ("ETIMEDOUT temporary failure", "transient", False),
    ("controlled hang", "timeout", True),
])
def test_runtime_persists_failure_after_confirmed_process_end(tmp_path, monkeypatch, message, kind, timeout):
    rig, adapter, marker = _runtime(tmp_path, monkeypatch, message, timeout=timeout)
    try:
        result = adapter.run(rig.store, TASK, None, rig.project)
        assert result.status == ExecutionStatus.FAILED
        events = _events(rig.store)
        assert len(events) == 1 and events[0]["evidence"]["kind"] == kind
        event = events[0]
        execution = rig.store.conn.execute("SELECT * FROM executions WHERE id=?", (event["execution_id"],)).fetchone()
        assert execution["status"] == "FAILED" and execution["actor"] == "controlled"
        evidence = event["evidence"]
        assert evidence["cwd"] == str(task_workspace(rig.project, TASK))
        assert evidence["command"]["argc"] == 3
        assert "PROMPT_CONTENT_CANARY" not in json.dumps(event) and "ghp_FIXTURESECRET" not in json.dumps(event)
        assert len(evidence["excerpt"]) <= 600 and evidence["next_action"]
        assert evidence["recovery"]["attempted"] == []
        assert marker.read_text() == "call\n"
        assert rig.store.latest_candidate(TASK) is None
        path = rig.store.db_path
        rig.store.close()
        rig.store = Store(path)
        assert _events(rig.store) == events
    finally:
        rig.store.close()


@pytest.mark.parametrize(("message", "reason"), [
    ("authentication_error: not logged in", "authentication_failure"),
    ("rate limit exceeded 429", "quota_rate_limit"),
    ("provider executable not found", "provider_unavailable"),
    ("503 service unavailable", "transient_provider_failure"),
    ("unrecognised implementation error", "implementation_failure"),
])
def test_runtime_keeps_provider_and_untyped_failure_semantics(tmp_path, monkeypatch, message, reason):
    rig, adapter, marker = _runtime(tmp_path, monkeypatch, message)
    try:
        result = adapter.run(rig.store, TASK, None, rig.project)
        assert result.failure_reason == reason and _events(rig.store) == []
        assert result.capacity_failure == (reason != "implementation_failure")
        assert marker.read_text() == "call\n"
    finally:
        rig.store.close()


def test_runtime_unknown_cleanup_cannot_publish_tool_failure(tmp_path, monkeypatch):
    from stagemesh import providers
    rig, adapter, marker = _runtime(tmp_path, monkeypatch, "AssertionError: controlled")
    communicate = providers.communicate_bounded
    def unknown(proc, prompt, timeout):
        communicate(proc, prompt, timeout)
        raise OperatorStopRequested("controlled unconfirmed cleanup", cleanup_complete=False)
    monkeypatch.setattr(providers, "communicate_bounded", unknown)
    try:
        with pytest.raises(OperatorStopRequested):
            adapter.run(rig.store, TASK, None, rig.project)
        assert _events(rig.store) == []
        assert rig.store.conn.execute("SELECT COUNT(*) FROM executions WHERE status='RUNNING'").fetchone()[0] == 1
        assert marker.read_text() == "call\n"
    finally:
        rig.store.close()


def test_runtime_repeated_signature_after_reopen_does_not_claim_recovery_ran(tmp_path, monkeypatch):
    rig, adapter, marker = _runtime(tmp_path, monkeypatch, "ETIMEDOUT temporary failure")
    # Verify the stage-specific configured command, not a default or expanded prompt.
    adapter = replace(adapter, command=("never-launch-this-default",), implement_command=adapter.command)
    try:
        adapter.run(rig.store, TASK, None, rig.project)
        first = _events(rig.store)[0]["evidence"]
        assert first["recovery"]["action"] == "retry_once"
        for index in range(60):
            rig.store.add_audit_event("controlled.noise", {"sequence": index})
        path = rig.store.db_path
        rig.store.close()
        rig.store = Store(path)
        adapter.run(rig.store, TASK, None, rig.project)  # explicit second caller, no automatic replay
        events = _events(rig.store)
        assert len(events) == 2
        repeated = events[-1]["evidence"]
        assert repeated["recovery"]["action"] == "stop"
        assert repeated["recovery"]["execution_status"] == "NOT_CONFIRMED"
        assert repeated["recovery"]["attempted"] == []
        assert repeated["recovery"]["previous_recommendation_count"] == 1
        assert repeated["command"] == first["command"] and "never-launch-this-default" not in json.dumps(repeated)
        assert marker.read_text() == "call\ncall\n"
        assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 2
        assert rig.store.latest_candidate(TASK) is None
    finally:
        rig.store.close()


@pytest.mark.parametrize("secret", ["opaque credential with spaces", "opaque=credential with spaces", "opaque-base64="])
def test_runtime_split_credentials_are_absent_from_persisted_event(tmp_path, monkeypatch, secret):
    rig, adapter, marker = _runtime(tmp_path, monkeypatch, f"AssertionError: echoed {secret}")
    adapter = replace(adapter, command=(*adapter.command, "--password", secret))
    try:
        adapter.run(rig.store, TASK, None, rig.project)
        events = _events(rig.store)
        assert len(events) == 1
        blob = json.dumps(events[0])
        assert secret not in blob and "opaque credential" not in blob
        assert "REDACTED" in blob and events[0]["evidence"]["command"]["argc"] == 5
        assert "PROMPT_CONTENT_CANARY" not in blob and marker.read_text() == "call\n"
    finally:
        rig.store.close()
