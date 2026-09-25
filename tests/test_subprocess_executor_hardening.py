from __future__ import annotations

import json
import sys
from pathlib import Path

from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.results import parse_executor_result
from build_coordinator.execution.subprocess_executor import SubprocessExecutor


def _launch(tmp_path: Path, *, result_path: Path | None = None) -> ExecutionLaunch:
    return ExecutionLaunch(
        task_id="GH-61",
        role="BUILDER",
        worker_id="builder-a",
        provider="local",
        worktree_path=str(tmp_path),
        branch_name="task/GH-61",
        prompt="do the thing",
        execution_id="exec-gh-61",
        result_path=str(result_path or tmp_path / "result.json"),
    )


def test_subprocess_executor_records_sanitized_evidence_when_provider_exits_without_result(tmp_path: Path):
    log_dir = tmp_path / "logs"
    result_path = tmp_path / "result.json"
    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('token=super-secret', file=sys.stderr); sys.exit(2)",
        ],
        log_dir=log_dir,
    )

    handle = executor.launch(_launch(tmp_path, result_path=result_path))
    observation = executor.poll(handle.execution_id)

    assert observation.status == "FAILED"
    assert observation.exit_code == 2
    assert observation.result_data["return_code"] == 2
    assert observation.result_data["provider_failure"] == "EXECUTION_FAILURE"
    assert observation.result_data["failure_kind"] == "EXECUTOR_RESULT_INVALID_OR_MISSING"
    assert "hello" in observation.result_data["stdout_tail"]
    assert "super-secret" not in observation.result_data["stderr_tail"]
    assert "token=<redacted>" in observation.result_data["stderr_tail"]


def test_subprocess_executor_classifies_transient_no_result_failures_for_bounded_retry(tmp_path: Path):
    executor = SubprocessExecutor(
        [sys.executable, "-c", "import sys; print('429 Too Many Requests', file=sys.stderr); sys.exit(2)"],
        log_dir=tmp_path / "logs",
    )

    handle = executor.launch(_launch(tmp_path))
    observation = executor.poll(handle.execution_id)

    assert observation.status == "FAILED"
    assert observation.result_data["provider_failure"] == "RATE_LIMITED"
    assert observation.result_data["return_code"] == 2


def test_subprocess_executor_preflight_missing_workdir_is_structured_human_action(tmp_path: Path):
    missing = tmp_path / "missing"
    executor = SubprocessExecutor([sys.executable, "-c", "raise SystemExit(0)"])
    launch = _launch(missing, result_path=tmp_path / "result.json")

    handle = executor.launch(launch)
    observation = executor.poll(handle.execution_id)

    assert observation.status == "HUMAN_ACTION_REQUIRED"
    assert observation.human_escalation_type == "COORDINATOR_INVARIANT_FAILURE"
    assert observation.result_data["failure_kind"] == "WORKDIR_MISSING"
    assert observation.result_data["diagnostics"]["worktree_path"] == str(missing)


def test_subprocess_executor_late_temp_failure_is_structured_human_action(tmp_path: Path, monkeypatch):
    executor = SubprocessExecutor([sys.executable, "-c", "raise SystemExit(0)"])
    calls = {"count": 0}
    original = executor._prepare_temp_dir

    def fail_after_preflight(worker_id: str, execution_id: str):
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("managed temp unwritable")
        return original(worker_id, execution_id)

    monkeypatch.setattr(executor, "_prepare_temp_dir", fail_after_preflight)

    handle = executor.launch(_launch(tmp_path))
    observation = executor.poll(handle.execution_id)

    assert observation.status == "HUMAN_ACTION_REQUIRED"
    assert observation.human_escalation_type == "COORDINATOR_INVARIANT_FAILURE"
    assert observation.result_data["failure_kind"] == "SANDBOX_TEMP_NOT_WRITABLE"
    assert "managed temp unwritable" in observation.result_data["detail"]


def test_subprocess_executor_late_log_failure_is_structured_human_action(tmp_path: Path, monkeypatch):
    executor = SubprocessExecutor(
        [sys.executable, "-c", "raise SystemExit(0)"],
        log_dir=tmp_path / "logs",
    )

    def fail_open_logs(execution_id: str):
        raise PermissionError("log denied")

    monkeypatch.setattr(executor, "_open_logs", fail_open_logs)

    handle = executor.launch(_launch(tmp_path))
    observation = executor.poll(handle.execution_id)

    assert observation.status == "HUMAN_ACTION_REQUIRED"
    assert observation.human_escalation_type == "COORDINATOR_INVARIANT_FAILURE"
    assert observation.result_data["failure_kind"] == "LOG_PATH_NOT_WRITABLE"
    assert "log denied" in observation.result_data["detail"]


def test_generic_failure_evidence_survives_result_validation():
    parsed = parse_executor_result(
        {
            "schema_version": 1,
            "execution_id": "exec-gh-61",
            "task_id": "GH-61",
            "role": "BUILDER",
            "status": "HUMAN_ACTION_REQUIRED",
            "human_escalation_type": "COORDINATOR_INVARIANT_FAILURE",
            "failure_kind": "RESULT_PATH_NOT_WRITABLE",
            "detail": "operator must grant write access",
            "diagnostics": {"result_path": "C:/tmp/result.json", "api_token": "drop-me"},
            "stdout_tail": "ok",
            "stderr_tail": "permission denied",
            "return_code": 13,
        },
        execution_id="exec-gh-61",
        task_id="GH-61",
        role="BUILDER",
    )

    assert parsed.persisted["failure_kind"] == "RESULT_PATH_NOT_WRITABLE"
    assert parsed.persisted["detail"] == "operator must grant write access"
    assert parsed.persisted["diagnostics"] == {"result_path": "C:/tmp/result.json"}
    assert parsed.persisted["stderr_tail"] == "permission denied"
    assert parsed.persisted["return_code"] == 13
    json.dumps(parsed.persisted)
