"""Scenario 2: a repeated setup-class timeout yields a bounded diagnosis and stops blind retries."""

from __future__ import annotations

from pathlib import Path

from stagemesh.diagnosis import VALIDATION_GATE, DiagnosisPolicy, diagnose
from stagemesh.domain import TaskStatus

from test_bounded_execution import TASK, _setup
from test_diagnosis import DOCS_CONTRACT, PY, Attempts, drive, events


def _hanging_gate(printed: str) -> dict:
    script = f"import sys,time; print({printed!r}); sys.stdout.flush(); time.sleep(30)" if printed else "import time; time.sleep(30)"
    return {"name": "pure-unit", "command": [PY, "-c", script], "timeout_seconds": 2}


def _run(tmp_path: Path, printed: str, ticks: int = 8) -> tuple[Attempts, object]:
    project, store = _setup(tmp_path, dict(DOCS_CONTRACT, required_tests=[_hanging_gate(printed)]))
    executor = Attempts()
    drive(project, store, executor, DiagnosisPolicy(), ticks=ticks)
    return executor, store


def test_session_setup_timeout_is_diagnosed_once_and_never_retried_blindly(tmp_path: Path) -> None:
    executor, store = _run(tmp_path, "collected 41 items\nsession fixture db_engine: setting up")

    assert executor.calls == 1  # no second implementation attempt was spent
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    assert not events(store, "task.remediation_queued")
    stop, = events(store, "task.diagnosis_stop")
    assert stop["category"] == VALIDATION_GATE and stop["diagnosis_required"] and not stop["environment_unavailable"]
    block = events(store, "task.block_classified")[-1]
    assert block["kind"] == "BLOCKED_RECOVERABLE_OPERATIONAL"
    assert "gate_timeout_diagnosis_required" in block["reason"]
    diagnosis = diagnose(store, TASK)
    assert "fixture or session setup" in diagnosis.summary
    assert "StageMesh never skips fixtures" in diagnosis.recommendation
    assert diagnosis.failing_evidence[-1]["timeout_notes"]


def test_unreachable_service_in_the_timeout_output_is_infrastructure(tmp_path: Path) -> None:
    executor, store = _run(tmp_path, "psycopg2.OperationalError: connection refused")

    assert executor.calls == 1
    stop, = events(store, "task.diagnosis_stop")
    assert stop["environment_unavailable"] is True
    assert "validation_environment_unavailable" in events(store, "task.block_classified")[-1]["reason"]


def test_empty_timeout_log_is_not_blamed_on_anything_and_a_repeat_stops_after_one_retry(tmp_path: Path) -> None:
    executor, store = _run(tmp_path, "")

    assert executor.calls == 2  # one bounded remediation attempt, then the equivalent failure stops the task
    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    diagnosis = diagnose(store, TASK)
    assert diagnosis.repeated and diagnosis.category == VALIDATION_GATE
    assert "not established" in diagnosis.summary
    assert "not evidence of" in diagnosis.summary  # the disclaimer, not a guessed root cause
    assert diagnosis.failing_evidence[-1]["timeout_notes"][0].startswith("pure-unit:")
    assert len(events(store, "task.remediation_queued")) == 1


def test_a_timeout_while_tests_are_running_keeps_ordinary_remediation(tmp_path: Path) -> None:
    executor, store = _run(tmp_path, "tests/test_a.py::test_hangs PASSED [ 5%]", ticks=3)

    assert executor.calls == 2
    assert store.get_task(TASK)["status"] == TaskStatus.OPEN
    assert not events(store, "task.diagnosis_stop")
