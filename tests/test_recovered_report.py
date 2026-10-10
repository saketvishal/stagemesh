"""`continue` reports recovered work from several producers; a missing `execution_id` must never crash the CLI."""
from __future__ import annotations

import pytest

from stagemesh.cli import _format_recovered, _report_run_ready
from stagemesh.run_ready import RunSummary

STALE_EXECUTION = {"task_id": "143", "execution_id": "ex-1", "kind": "IMPLEMENTATION", "pid": 4242, "action": "RELEASED"}
DETACHED_BUILTIN = {"task_id": "143", "execution_id": "ex-2", "kind": "REVIEW", "pid": None, "process_state": "DETACHED_BUILTIN"}
AUTO_RECOVERY = {"task_id": "143", "candidate_sha": "0123456789abcdef", "auto_recovery": "blocked_validation_recheck"}
NO_EXECUTION_ID = {"task_id": "143", "pid": 4242, "action": "RELEASED"}  # the shape that raised KeyError: 'execution_id'


def test_continue_report_survives_recovered_entries_without_an_execution_id(capsys: pytest.CaptureFixture[str]) -> None:
    summary = RunSummary(
        False, "NO_PROGRESS", task_id="143", message="grok implementation_failure",
        recovered=[STALE_EXECUTION, DETACHED_BUILTIN, AUTO_RECOVERY, NO_EXECUTION_ID, {}],
    )

    code = _report_run_ready(summary, {}, as_json=False)

    out = capsys.readouterr().out
    assert code == 1  # the stop is still reported and still fails the command
    assert "recovered stale execution ex-1 (pid 4242 dead) (task 143)" in out
    assert "recovered stale execution ex-2 (DETACHED_BUILTIN) (task 143)" in out  # no "pid None dead"
    assert "automatic recovery blocked_validation_recheck (task 143) candidate 0123456789" in out
    assert "recovered (task 143): RELEASED" in out
    assert "stopped:" in out and "grok implementation_failure" in out


@pytest.mark.parametrize("item", [NO_EXECUTION_ID, {}, {"execution_id": None, "pid": None}, "unexpected", None, 7, {"execution_id": "x"}])
def test_format_recovered_never_raises_for_any_payload_shape(item: object) -> None:
    assert isinstance(_format_recovered(item), str)


def test_json_report_is_unchanged_by_recovered_entry_shape(capsys: pytest.CaptureFixture[str]) -> None:
    summary = RunSummary(True, "DONE", task_id="143", recovered=[NO_EXECUTION_ID])

    assert _report_run_ready(summary, {}, as_json=True) == 0

    assert '"recovered"' in capsys.readouterr().out
