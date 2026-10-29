from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from stagemesh import cli as cli_module
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage
from stagemesh.persistence import Store

BASE = datetime(2026, 1, 15, 18, tzinfo=UTC)


def _project(tmp_path: Path) -> tuple[Path, str, str]:
    project = tmp_path / "repo"
    project.mkdir()
    store = Store(cli_module.db_path(project))
    store.migrate()
    task_id = store.upsert_task("review activity", source_id="ACTIVITY-1")
    store.conn.execute(
        "UPDATE tasks SET stage=?, updated_at=? WHERE id=?",
        (Stage.REVIEW.value, BASE.timestamp(), task_id),
    )
    execution_id = store.start_execution(
        task_id=task_id,
        claim_id=None,
        kind=ExecutionKind.REVIEW,
        actor="codex",
    )
    store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, result="no_implementation_change")
    store.conn.execute(
        "UPDATE executions SET started_at=? WHERE id=?",
        ((BASE + timedelta(minutes=1)).timestamp(), execution_id),
    )
    store.conn.commit()
    store.close()
    return project, task_id, execution_id


def test_activity_json_filters_without_mutating_the_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, task_id, execution_id = _project(tmp_path)
    database = cli_module.db_path(project)
    before = database.read_bytes()

    code = cli_module.main([
        "--project", str(project), "activity", "--provider", "codex", "--task", task_id, "--json"
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in payload["entries"]] == [execution_id]
    assert payload["entries"][0]["provider"] == "codex"
    assert payload["entries"][0]["at"].endswith("Z")
    assert payload["time_basis"] == "activity"
    assert payload["status_counts"]["execution"]["SUCCEEDED"] == 1
    assert database.read_bytes() == before


def test_activity_text_is_bounded_and_shows_pagination_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, task_id, execution_id = _project(tmp_path)

    assert cli_module.main(["--project", str(project), "activity", "--limit", "1"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("activity: showing 1 of 2")
    assert f"id={execution_id}" in lines[1] and f"task={task_id}" in lines[1]
    assert lines[-1] == "next: rerun with --offset 1"


def test_activity_text_escapes_terminal_control_characters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, task_id, _execution_id = _project(tmp_path)
    store = Store(cli_module.db_path(project))
    store.conn.execute("UPDATE tasks SET title=? WHERE id=?", ("notice\x1b[2J\x1b[HFORGED", task_id))
    store.conn.commit()
    store.close()

    assert cli_module.main(["--project", str(project), "activity", "--state", "open"]) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "notice\\u001b[2J\\u001b[HFORGED" in output


def test_activity_text_escapes_controls_from_audit_task_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _task_id, _execution_id = _project(tmp_path)
    store = Store(cli_module.db_path(project))
    store.add_audit_event("controlled.fixture", {"task_id": "audit\x1b[2J\x1b[HFORGED"})
    store.close()

    assert cli_module.main(["--project", str(project), "activity", "--state", "closed"]) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "task=audit\\u001b[2J\\u001b[HFORGED" in output


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("today", ("2026-01-15T06:00:00+00:00", "2026-01-16T06:00:00+00:00")),
        ("yesterday", ("2026-01-14T06:00:00+00:00", "2026-01-15T06:00:00+00:00")),
        ("week", ("2026-01-12T06:00:00+00:00", "2026-01-19T06:00:00+00:00")),
        ("month", ("2026-01-01T06:00:00+00:00", "2026-02-01T06:00:00+00:00")),
    ],
)
def test_activity_periods_use_local_calendar_boundaries(period: str, expected: tuple[str, str]) -> None:
    local_now = datetime(2026, 1, 15, 12, tzinfo=timezone(timedelta(hours=-6)))
    start, end = cli_module._activity_period_bounds(period, now=local_now)
    assert (start.isoformat(), end.isoformat()) == expected


class _SpringForwardZone(tzinfo):
    def utcoffset(self, value: datetime | None) -> timedelta:
        return timedelta(hours=-5 if value is not None and value.date() >= date(2026, 3, 8) else -6)

    def dst(self, value: datetime | None) -> timedelta:
        return self.utcoffset(value) - timedelta(hours=-6)

    def tzname(self, value: datetime | None) -> str:
        return "fixture-local"


def test_activity_week_preserves_a_local_offset_transition() -> None:
    start, end = cli_module._activity_period_bounds(
        "week", now=datetime(2026, 3, 8, 12, tzinfo=_SpringForwardZone())
    )
    assert start == datetime(2026, 3, 2, 6, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 5, tzinfo=UTC)
    assert end - start == timedelta(hours=167)


@pytest.mark.parametrize(
    "options",
    [
        ["--last", "0d"],
        ["--last", "seven-days"],
        ["--last", "9" * 4500 + "d"],
        ["--since", "2026-01-01T00:00:00"],
        ["--since", "0001-01-01T00:00:00+14:00"],
        ["--until", "9999-12-31T23:59:59-14:00"],
        ["--period", "today", "--until", "2026-01-02T00:00:00Z"],
        ["--since", "2026-01-02T00:00:00Z", "--until", "2026-01-01T00:00:00Z"],
        ["--limit", "0"],
        ["--offset", "-1"],
    ],
)
def test_activity_rejects_invalid_ranges_and_pagination(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    project, _task_id, _execution_id = _project(tmp_path)
    assert cli_module.main(["--project", str(project), "activity", *options]) == 2
    assert capsys.readouterr().err.startswith("activity error:")


def test_activity_range_accepts_offset_instants() -> None:
    args = Namespace(since="2026-01-01T00:00:00-06:00", until="2026-01-02T00:00:00Z", last=None, period=None)
    since, until = cli_module._activity_range(args)
    assert since == datetime(2026, 1, 1, 6, tzinfo=UTC)
    assert until == datetime(2026, 1, 2, tzinfo=UTC)
