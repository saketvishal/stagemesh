from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import stagemesh.providers as providers_module
from stagemesh.diagnosis import (
    TOOL_FAILURE_EVENT,
    TOOL_RECOVERY_EVENT,
    plan_tool_recovery,
    tool_failure_evidence,
    tool_failure_signature,
)
from stagemesh.execution import SubprocessExecutor
from stagemesh.git import GitWorkspace
from stagemesh.persistence import Store
from stagemesh.providers import RuntimeCommandAdapter
from stagemesh.remediation import ToolRecoveryRefused, execute_tool_recovery, tool_recovery_plan


def _ev(stderr: str, cwd: str = "/w") -> dict:
    ev = tool_failure_evidence(["tool", "run"], cwd, 1, "", stderr)
    assert ev is not None
    return ev


def _runtime_project(tmp_path: Path) -> tuple[Path, Store, str]:
    project = tmp_path / "repo"
    project.mkdir()
    git = GitWorkspace(project)
    git.init_if_needed()
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "StageMesh Test")
    (project / "docs").mkdir()
    (project / "docs" / "base.md").write_text("base\n", encoding="utf-8")
    git.commit_all("base")
    runtime = project / ".stagemesh"
    (runtime / "contracts").mkdir(parents=True)
    store = Store(runtime / "state.sqlite3")
    store.migrate()
    task_id = store.upsert_task("recover tool failure", source_id="RECOVERY-TASK")
    (runtime / "contracts" / f"{task_id}.json").write_text(
        json.dumps({"objective": "recover", "allowed_files": ["docs/**"]}), encoding="utf-8"
    )
    return project, store, task_id


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add_audit_event(self, event_type: str, payload: dict) -> str:
        self.rows.append({"event_type": event_type, "payload": json.dumps(payload)})
        return "id"

    def task_audit_events(self, event_type: str, task_id: str) -> list[dict]:
        return [row for row in self.rows if row["event_type"] == event_type and json.loads(row["payload"]).get("task_id") == task_id]


def test_transient_failure_gets_one_retry_then_stops_with_operator_action() -> None:
    assert plan_tool_recovery(_ev("ETIMEDOUT"))["action"] == "retry_once"
    stopped = plan_tool_recovery(_ev("ETIMEDOUT"), ["retry"])
    assert stopped["action"] == "stop" and stopped["attempted"] == ["retry"] and stopped["operator_action"]


@pytest.mark.parametrize(
    "stderr", ["EACCES: permission denied", "command not found", "No module named 'x'", "AssertionError: assert 1 == 2"]
)
def test_non_transient_failures_never_retry_or_install(stderr: str) -> None:
    plan = plan_tool_recovery(_ev(stderr))
    assert plan["action"] == "stop" and "setup_command" not in plan and plan["operator_action"]


def test_only_explicitly_configured_setup_runs_once() -> None:
    ev = _ev("No module named 'x'")
    configured = {"missing_dependency": ["make", "setup"]}
    plan = plan_tool_recovery(ev, setup_actions=configured)
    assert plan["action"] == "setup" and plan["setup_command"] == ["make", "setup"]
    assert plan_tool_recovery(ev, ["setup"], configured)["action"] == "stop"


def test_permission_failures_ignore_configured_setup() -> None:
    plan = plan_tool_recovery(_ev("EACCES: permission denied"), setup_actions={"permission": ["chmod", "-R", "777", "."]})
    assert plan["action"] == "stop" and "setup_command" not in plan


@pytest.mark.parametrize("configured", [[""], ["tool", 7], "tool --setup"])
def test_malformed_setup_configuration_fails_closed(configured) -> None:
    plan = plan_tool_recovery(_ev("No module named 'x'"), setup_actions={"missing_dependency": configured})
    assert plan["action"] == "stop" and "setup_command" not in plan


def test_signature_ignores_excerpt_but_not_cwd() -> None:
    assert tool_failure_signature(_ev("ETIMEDOUT a")) == tool_failure_signature(_ev("ETIMEDOUT b"))
    assert tool_failure_signature(_ev("ETIMEDOUT", "/a")) != tool_failure_signature(_ev("ETIMEDOUT", "/b"))


def test_executor_stops_on_repeated_signature_and_isolates_tasks() -> None:
    store = _Store()
    ex = SubprocessExecutor(["tool"])
    for execution in ("e1", "e2"):
        ex._record_tool_evidence(store, "t1", execution, "/w", 1, "", "request failed: ETIMEDOUT", "tool_execution_failure", False)
    first, second = (json.loads(r["payload"])["evidence"]["recovery"] for r in store.rows)
    assert first["action"] == "retry_once"
    assert second["action"] == "stop" and second["attempted"] == []
    assert second["previous_recommendations"] == ["retry_once"]
    ex._record_tool_evidence(store, "t2", "e3", "/w", 1, "", "request failed: ETIMEDOUT", "tool_execution_failure", False)
    assert json.loads(store.rows[-1]["payload"])["evidence"]["recovery"]["action"] == "retry_once"
    assert all(r["event_type"] == TOOL_FAILURE_EVENT for r in store.rows)


def test_plan_reads_only_this_tasks_history() -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "other", "evidence": {**ev, "recovery": {"action": "retry_once"}}})
    assert tool_recovery_plan(store, "t1", ev)["action"] == "retry_once"


def test_unexecuted_retry_does_not_spend_an_attempt_or_unlock_setup() -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    configured = {"transient": ["configured-setup", "--local"]}
    first = tool_recovery_plan(store, "t1", ev, configured)
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})

    stopped = tool_recovery_plan(store, "t1", ev, configured)

    assert stopped["action"] == "stop"
    assert stopped["attempted"] == []
    assert stopped["previous_recommendations"] == ["retry_once"]
    assert stopped["execution_status"] == "NOT_CONFIRMED"
    assert "execution" in stopped["operator_action"]
    assert "setup_command" not in stopped


def test_unexecuted_setup_is_not_reported_as_an_attempt() -> None:
    store = _Store()
    ev = _ev("No module named 'x'")
    configured = {"missing_dependency": ["configured-setup"]}
    first = tool_recovery_plan(store, "t1", ev, configured)
    assert first["action"] == "setup"
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})

    stopped = tool_recovery_plan(store, "t1", ev, configured)

    assert stopped["action"] == "stop" and stopped["attempted"] == []
    assert stopped["previous_recommendations"] == ["setup"]


@pytest.mark.parametrize("prior_recovery", [None, {}, {"action": "stop"}, {"action": "future_action"}])
def test_previous_failure_without_confirmed_execution_still_stops(prior_recovery) -> None:
    store = _Store()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": prior_recovery}})

    stopped = tool_recovery_plan(store, "t1", ev)

    assert stopped["action"] == "stop" and stopped["attempted"] == []
    assert stopped["execution_status"] == "NOT_CONFIRMED"


def test_recommendation_history_survives_store_reopen_and_stays_stopped(tmp_path) -> None:
    from stagemesh.persistence import Store

    path = tmp_path / "recovery.sqlite3"
    ev = _ev("ETIMEDOUT")
    store = Store(path)
    store.migrate()
    first = tool_recovery_plan(store, "t1", ev)
    store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": first}})
    store.close()
    store = Store(path)
    try:
        second = tool_recovery_plan(store, "t1", ev)
        store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": second}})
        third = tool_recovery_plan(store, "t1", ev)
        assert second["action"] == third["action"] == "stop"
        assert second["attempted"] == third["attempted"] == []
        assert third["previous_recommendations"] == ["retry_once", "stop"]
        assert tool_recovery_plan(store, "other", ev)["action"] == "retry_once"
        assert tool_recovery_plan(store, "t1", _ev("ETIMEDOUT", "/other"))["action"] == "retry_once"
    finally:
        store.close()


@pytest.mark.parametrize("noise", ["unrelated_events", "same_task_other_signatures"])
def test_other_activity_cannot_evict_recovery_stop_history(tmp_path, noise) -> None:
    from stagemesh.persistence import Store

    path = tmp_path / "history.sqlite3"
    store = Store(path)
    store.migrate()
    ev = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_FAILURE_EVENT, {
        "task_id": "t1", "evidence": {**ev, "recovery": tool_recovery_plan(store, "t1", ev)},
    })
    for index in range(201):
        if noise == "unrelated_events":
            store.add_audit_event("unrelated.event", {"task_id": "other", "index": index})
        else:
            store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": _ev("ETIMEDOUT", f"/noise/{index}")})
    store.close()
    store = Store(path)
    try:
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["retry_once"]
    finally:
        store.close()


def test_recovery_summary_is_bounded_without_losing_history_count(tmp_path) -> None:
    from stagemesh.persistence import Store

    store = Store(tmp_path / "summary.sqlite3")
    store.migrate()
    ev = _ev("ETIMEDOUT")
    try:
        for action in ["retry_once", *(["stop"] * 24)]:
            store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": {"action": action}}})
        # Force timestamp ties so insertion order must still be deterministic.
        store.conn.execute("UPDATE audit_events SET created_at=42")
        store.conn.commit()
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["stop"] * 20
        assert stopped["previous_recommendation_count"] == 25
    finally:
        store.close()


def test_corrupt_or_non_object_audit_payload_does_not_hide_known_failure(tmp_path) -> None:
    from stagemesh.persistence import Store

    store = Store(tmp_path / "corrupt.sqlite3")
    store.migrate()
    ev = _ev("ETIMEDOUT")
    try:
        store.add_audit_event(TOOL_FAILURE_EVENT, {"task_id": "t1", "evidence": {**ev, "recovery": {"action": "retry_once"}}})
        for raw in ["{", "[]", "null", '{"task_id":"other"}']:
            event_id = store.add_audit_event(TOOL_FAILURE_EVENT, {})
            # Inject stored corruption rather than weakening the normal audit writer.
            store.conn.execute("UPDATE audit_events SET payload=? WHERE id=?", (raw, event_id))
        store.conn.commit()
        stopped = tool_recovery_plan(store, "t1", ev)
        assert stopped["action"] == "stop" and stopped["attempted"] == []
        assert stopped["previous_recommendations"] == ["retry_once"]
        assert stopped["previous_recommendation_count"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("stderr,kind", [("ETIMEDOUT", "transient"), ("No module named 'x'", "missing_dependency")])
@pytest.mark.parametrize("attempted", [["retry"], ["setup"], ["unknown"], ("retry", "setup")])
def test_any_prior_action_stops_signature_even_with_configured_setup(
    stderr: str, kind: str, attempted: list[str] | tuple[str, ...]
) -> None:
    evidence = _ev(stderr)
    plan = plan_tool_recovery(evidence, attempted, {kind: ["configured-setup", "--local"]})
    assert plan["action"] == "stop"
    assert plan["attempted"] == list(attempted)
    assert plan["signature"] == tool_failure_signature(evidence)
    assert plan["operator_action"] == evidence["next_action"]
    assert "setup_command" not in plan


def test_recovery_executor_invokes_transient_retry_once_and_records_receipt() -> None:
    store = _Store()
    evidence = _ev("ETIMEDOUT")
    calls: list[str] = []
    plan = tool_recovery_plan(store, "t1", evidence)

    receipt = execute_tool_recovery(
        store, "t1", "e1", evidence, plan, retry=lambda: not calls.append("retry")
    )

    assert calls == ["retry"]
    assert receipt["attempted"] == ["retry"] and receipt["execution_status"] == "SUCCEEDED"
    assert [row["event_type"] for row in store.rows] == [TOOL_RECOVERY_EVENT, TOOL_RECOVERY_EVENT]
    stopped = execute_tool_recovery(
        store, "t1", "e2", evidence, plan, retry=lambda: not calls.append("second")
    )
    assert calls == ["retry"]
    assert stopped["action"] == "stop" and stopped["attempted"] == ["retry"]


def test_recovery_executor_runs_only_the_configured_setup_argv() -> None:
    store = _Store()
    evidence = _ev("No module named 'x'")
    configured = {"missing_dependency": ["project-setup", "--local"]}
    observed: list[tuple[str, ...]] = []
    plan = tool_recovery_plan(store, "t1", evidence, configured)

    receipt = execute_tool_recovery(
        store,
        "t1",
        "e1",
        evidence,
        plan,
        run_setup=lambda argv: not observed.append(argv),
        setup_actions=configured,
    )

    assert observed == [("project-setup", "--local")]
    assert receipt["attempted"] == ["setup"] and receipt["execution_status"] == "SUCCEEDED"


def test_recovery_executor_never_invokes_for_permission_or_unrelated_task_receipts() -> None:
    store = _Store()
    permission = _ev("EACCES: permission denied")
    calls: list[str] = []
    refused = execute_tool_recovery(
        store,
        "t1",
        "e1",
        permission,
        tool_recovery_plan(store, "t1", permission, {"permission": ["unsafe"]}),
        run_setup=lambda _argv: not calls.append("unsafe"),
        setup_actions={"permission": ["unsafe"]},
    )
    assert refused["action"] == "stop" and not calls

    other = _ev("ETIMEDOUT")
    store.add_audit_event(TOOL_RECOVERY_EVENT, {
        "task_id": "other", "execution_id": "other-e", "signature": tool_failure_signature(other),
        "action": "retry", "status": "STARTED",
    })
    own = execute_tool_recovery(
        store, "t1", "e2", other, tool_recovery_plan(store, "t1", other),
        retry=lambda: not calls.append("own"),
    )
    assert calls == ["own"] and own["execution_status"] == "SUCCEEDED"


def test_started_or_failed_recovery_consumes_the_single_action_budget() -> None:
    store = _Store()
    evidence = _ev("ETIMEDOUT")
    plan = tool_recovery_plan(store, "t1", evidence)
    with pytest.raises(RuntimeError, match="boom"):
        execute_tool_recovery(
            store, "t1", "e1", evidence, plan, retry=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    stopped = tool_recovery_plan(store, "t1", evidence)
    assert stopped["action"] == "stop" and stopped["attempted"] == ["retry"]


def test_prelaunch_refusal_is_durable_but_does_not_consume_recovery_allowance() -> None:
    store = _Store()
    evidence = _ev("ETIMEDOUT")
    plan = tool_recovery_plan(store, "t1", evidence)
    store.add_audit_event(TOOL_FAILURE_EVENT, {
        "task_id": "t1", "execution_id": "e1", "evidence": {**evidence, "recovery": plan},
    })

    result = execute_tool_recovery(
        store,
        "t1",
        "e1",
        evidence,
        plan,
        retry=lambda: (_ for _ in ()).throw(ToolRecoveryRefused("budget_exhausted")),
    )

    assert result["execution_status"] == "REFUSED"
    assert result["attempted"] == [] and result["failure"] == "budget_exhausted"
    assert tool_recovery_plan(store, "t1", evidence)["action"] == "retry_once"
    receipts = [json.loads(row["payload"]) for row in store.rows if row["event_type"] == TOOL_RECOVERY_EVENT]
    assert [receipt["status"] for receipt in receipts] == ["STARTED", "REFUSED"]


@pytest.mark.parametrize(
    "evidence,forged",
    [
        (_ev("EACCES: permission denied"), {"action": "retry_once"}),
        (_ev("No module named 'x'"), {"action": "setup", "setup_command": ["not-configured"]}),
    ],
)
def test_recovery_executor_rejects_forged_policy_plans(evidence, forged) -> None:
    store = _Store()
    calls: list[str] = []
    forged = {**forged, "signature": tool_failure_signature(evidence)}
    result = execute_tool_recovery(
        store,
        "t1",
        "e1",
        evidence,
        forged,
        retry=lambda: not calls.append("retry"),
        run_setup=lambda _argv: not calls.append("setup"),
    )
    assert result["action"] == "stop" and not calls


def test_subprocess_executor_performs_one_transient_retry_and_persists_candidate(tmp_path: Path) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    counter = tmp_path / "attempts.txt"
    script = tmp_path / "transient_provider.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(attempt))\n"
        "sys.stdin.read()\n"
        "if attempt == 1:\n"
        "    print('request failed: ETIMEDOUT', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "pathlib.Path('docs/recovered.md').write_text('ok\\n')\n",
        encoding="utf-8",
    )
    try:
        result = SubprocessExecutor([sys.executable, str(script)], name="test-provider").run(
            store, task_id, None, project
        )
        assert result.status.value == "SUCCEEDED"
        assert counter.read_text() == "2"
        rows = store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)
        receipts = [json.loads(row["payload"]) for row in rows]
        assert [receipt["status"] for receipt in receipts] == ["STARTED", "SUCCEEDED"]
        assert {receipt["action"] for receipt in receipts} == {"retry"}
        assert [row["status"] for row in store.conn.execute(
            "SELECT status FROM executions WHERE task_id=? ORDER BY rowid", (task_id,)
        )] == ["FAILED", "SUCCEEDED"]
    finally:
        store.close()


def test_subprocess_executor_runs_configured_setup_once_then_verifies_original_command(tmp_path: Path) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    marker = tmp_path / "setup-ready.txt"
    counter = tmp_path / "setup-attempts.txt"
    script = tmp_path / "setup_provider.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))\n"
        "sys.stdin.read()\n"
        f"ready = pathlib.Path({str(marker)!r})\n"
        "if not ready.exists():\n"
        "    print(\"No module named 'x'\", file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "pathlib.Path('docs/recovered.md').write_text('setup ok\\n')\n",
        encoding="utf-8",
    )
    setup = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ready')"]
    executor = SubprocessExecutor(
        [sys.executable, str(script)],
        name="test-provider",
        recovery_setup_actions={"missing_dependency": setup},
    )
    try:
        result = executor.run(store, task_id, None, project)
        assert result.status.value == "SUCCEEDED"
        assert counter.read_text() == "2" and marker.read_text() == "ready"
        receipts = [json.loads(row["payload"]) for row in store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)]
        assert [receipt["status"] for receipt in receipts] == ["STARTED", "SUCCEEDED"]
        assert {receipt["action"] for receipt in receipts} == {"setup"}
        setup_rows = list(store.conn.execute(
            "SELECT actor, status FROM executions WHERE task_id=? AND actor LIKE '%:tool-recovery-setup'", (task_id,)
        ))
        assert [(row["actor"], row["status"]) for row in setup_rows] == [
            ("test-provider:tool-recovery-setup", "SUCCEEDED")
        ]
    finally:
        store.close()


def test_runtime_command_adapter_performs_one_transient_retry(tmp_path: Path) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    counter = tmp_path / "runtime-attempts.txt"
    script = tmp_path / "runtime_provider.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(attempt))\n"
        "sys.stdin.read()\n"
        "if attempt == 1:\n"
        "    print('request failed: ETIMEDOUT', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "pathlib.Path('docs/runtime-recovered.md').write_text('ok\\n')\n",
        encoding="utf-8",
    )
    try:
        result = RuntimeCommandAdapter("test-provider", (sys.executable, str(script))).execute(
            store, task_id, None, project
        )
        assert result.status.value == "SUCCEEDED" and counter.read_text() == "2"
        receipts = [json.loads(row["payload"]) for row in store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)]
        assert [receipt["status"] for receipt in receipts] == ["STARTED", "SUCCEEDED"]
        assert {receipt["action"] for receipt in receipts} == {"retry"}
    finally:
        store.close()


def test_runtime_command_adapter_runs_configured_setup_then_verifies_original_command(tmp_path: Path) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    marker = tmp_path / "runtime-setup-ready.txt"
    counter = tmp_path / "runtime-setup-attempts.txt"
    script = tmp_path / "runtime_setup_provider.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))\n"
        "sys.stdin.read()\n"
        f"ready = pathlib.Path({str(marker)!r})\n"
        "if not ready.exists():\n"
        "    print(\"No module named 'x'\", file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "pathlib.Path('docs/runtime-setup-recovered.md').write_text('setup ok\\n')\n",
        encoding="utf-8",
    )
    setup = (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ready')")
    try:
        result = RuntimeCommandAdapter(
            "test-provider",
            (sys.executable, str(script)),
            recovery_setup_actions=(("missing_dependency", setup),),
        ).execute(store, task_id, None, project)
        assert result.status.value == "SUCCEEDED"
        assert counter.read_text() == "2" and marker.read_text() == "ready"
        receipts = [json.loads(row["payload"]) for row in store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)]
        assert [receipt["status"] for receipt in receipts] == ["STARTED", "SUCCEEDED"]
        assert {receipt["action"] for receipt in receipts} == {"setup"}
        setup_rows = list(store.conn.execute(
            "SELECT actor, status FROM executions WHERE task_id=? AND actor LIKE '%:tool-recovery-setup'", (task_id,)
        ))
        assert [(row["actor"], row["status"]) for row in setup_rows] == [
            ("test-provider:tool-recovery-setup", "SUCCEEDED")
        ]
    finally:
        store.close()


def test_runtime_command_adapter_closes_exact_execution_when_communication_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    script = tmp_path / "runtime_transport_failure.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")

    def raise_after_process_exit(proc, input_text: str, _timeout: int):  # type: ignore[no-untyped-def]
        proc.communicate(input_text, timeout=10)
        raise RuntimeError("transport failed after launch")

    monkeypatch.setattr(providers_module, "communicate_bounded", raise_after_process_exit)
    try:
        with pytest.raises(RuntimeError, match="transport failed"):
            RuntimeCommandAdapter("test-provider", (sys.executable, str(script))).execute(
                store, task_id, None, project
            )
        rows = list(store.conn.execute(
            "SELECT status, result FROM executions WHERE task_id=? ORDER BY rowid", (task_id,)
        ))
        assert rows and all(row["status"] == "FAILED" for row in rows)
        assert "transport failed" in str(rows[-1]["result"])
    finally:
        store.close()


def test_runtime_setup_exception_closes_setup_execution_and_recovery_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    script = tmp_path / "runtime_setup_transport_failure.py"
    script.write_text(
        "import sys\nsys.stdin.read()\nprint(\"No module named 'x'\", file=sys.stderr)\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    setup = (sys.executable, "-c", "pass")
    real_communicate = providers_module.communicate_bounded
    calls = 0

    def fail_during_setup(proc, input_text: str, timeout: int):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        result = real_communicate(proc, input_text, timeout)
        if calls == 2:
            raise RuntimeError("setup transport failed")
        return result

    monkeypatch.setattr(providers_module, "communicate_bounded", fail_during_setup)
    try:
        with pytest.raises(RuntimeError, match="setup transport failed"):
            RuntimeCommandAdapter(
                "test-provider",
                (sys.executable, str(script)),
                recovery_setup_actions=(("missing_dependency", setup),),
            ).execute(store, task_id, None, project)
        rows = list(store.conn.execute(
            "SELECT actor, status, result FROM executions WHERE task_id=? ORDER BY rowid", (task_id,)
        ))
        assert rows and all(row["status"] == "FAILED" for row in rows)
        assert rows[-1]["actor"] == "test-provider:tool-recovery-setup"
        assert "setup transport failed" in str(rows[-1]["result"])
        receipts = [json.loads(row["payload"]) for row in store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)]
        assert [receipt["status"] for receipt in receipts] == ["STARTED", "FAILED"]
    finally:
        store.close()


def test_recovery_refuses_second_provider_call_at_task_budget_boundary(tmp_path: Path) -> None:
    project, store, task_id = _runtime_project(tmp_path)
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"budgets": {"max_provider_calls_per_task": 1}}), encoding="utf-8"
    )
    counter = tmp_path / "budget-attempts.txt"
    script = tmp_path / "budget_provider.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))\n"
        "sys.stdin.read()\n"
        "print('request failed: ETIMEDOUT', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    try:
        result = RuntimeCommandAdapter("test-provider", (sys.executable, str(script))).execute(
            store, task_id, None, project
        )
        assert result.status.value == "FAILED" and counter.read_text() == "1"
        assert len(list(store.conn.execute(
            "SELECT 1 FROM audit_events WHERE event_type='task.budget_exhausted'"
        ))) == 1
        assert list(store.task_audit_events(TOOL_RECOVERY_EVENT, task_id)) == []
    finally:
        store.close()
