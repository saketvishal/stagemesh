from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.activity_periods import (
    ActivityPeriodError,
    ActivityPeriodQuery,
    query_activity_periods,
)
from stagemesh.domain import Stage
from stagemesh.persistence import Store

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    value = Store(tmp_path / "periods.sqlite3")
    value.migrate()
    yield value
    value.close()


def _created(store: Store, task_id: str, at: datetime) -> None:
    store.conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id=?", (at.timestamp(), at.timestamp(), task_id))
    store.conn.execute(
        "UPDATE audit_events SET created_at=? WHERE event_type='task.created' "
        "AND json_extract(payload, '$.task_id')=?",
        (at.timestamp(), task_id),
    )
    store.conn.commit()


def _latest_transition(store: Store, task_id: str, at: datetime) -> None:
    row = store.conn.execute(
        "SELECT rowid FROM audit_events WHERE event_type='task.status_changed' "
        "AND json_extract(payload, '$.task_id')=? ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    store.conn.execute("UPDATE audit_events SET created_at=? WHERE rowid=?", (at.timestamp(), row["rowid"]))
    store.conn.commit()


def test_active_intervals_and_series_use_complete_recorded_reopen_cycles(store: Store) -> None:
    task = store.upsert_task("cycle", source_id="cycle", project="team/alpha")
    _created(store, task, BASE)
    store.advance_task(task, Stage.DONE)
    _latest_transition(store, task, BASE + timedelta(days=2))
    store.advance_task_baseline(task, "b" * 40, {"task_id": task, "reason": "fixture reopen"})
    _latest_transition(store, task, BASE + timedelta(days=4))
    store.advance_task(task, Stage.DONE)
    _latest_transition(store, task, BASE + timedelta(days=5))

    report = query_activity_periods(
        store,
        ActivityPeriodQuery(
            since=BASE + timedelta(days=1),
            until=BASE + timedelta(days=6),
            timezone="UTC",
            project="team/alpha",
        ),
    )

    assert [(item.active_from, item.active_until) for item in report.active_intervals] == [
        (BASE + timedelta(days=4), BASE + timedelta(days=5)),
        (BASE, BASE + timedelta(days=2)),
    ]
    assert all(item.current_status == "DONE" for item in report.active_intervals)
    assert sum(bucket.opened_occurrences for bucket in report.series) == 1
    assert sum(bucket.completed_occurrences for bucket in report.series) == 2
    assert sum(bucket.opened_tasks for bucket in report.series) == 1
    assert sum(bucket.completed_tasks for bucket in report.series) == 2
    assert report.unavailable_tasks == 0 and not report.truncated


def test_same_task_recompletion_counts_occurrences_not_duplicate_tasks(store: Store) -> None:
    task = store.upsert_task("recomplete", source_id="recomplete")
    _created(store, task, BASE)
    store.advance_task(task, Stage.DONE)
    _latest_transition(store, task, BASE + timedelta(hours=1))
    store.advance_task_baseline(task, "c" * 40, {"task_id": task, "reason": "fixture reopen"})
    _latest_transition(store, task, BASE + timedelta(hours=2))
    store.advance_task(task, Stage.DONE)
    _latest_transition(store, task, BASE + timedelta(hours=3))

    report = query_activity_periods(
        store, ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1))
    )

    assert report.series[0].completed_occurrences == 2
    assert report.series[0].completed_tasks == 1
    assert report.series[0].opened_occurrences == 2
    assert report.series[0].opened_tasks == 1


def test_active_interval_pages_expose_total_and_next_offset(store: Store) -> None:
    first = store.upsert_task("first", source_id="first")
    second = store.upsert_task("second", source_id="second")
    _created(store, first, BASE)
    _created(store, second, BASE + timedelta(hours=1))

    page_one = query_activity_periods(
        store,
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), limit=1),
    )
    page_two = query_activity_periods(
        store,
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), limit=1, offset=1),
    )

    assert [item.task_id for item in page_one.active_intervals] == [second]
    assert page_one.total_intervals == 2 and page_one.next_offset == 1
    assert [item.task_id for item in page_two.active_intervals] == [first]
    assert page_two.total_intervals == 2 and page_two.next_offset is None
    assert page_one.series == page_two.series


def test_legacy_or_discontinuous_history_is_unavailable_not_reconstructed(store: Store) -> None:
    legacy = store.upsert_task("legacy", source_id="legacy")
    store.upsert_task("broken", source_id="broken")
    store.conn.execute(
        "DELETE FROM audit_events WHERE event_type='task.created' AND json_extract(payload, '$.task_id')=?",
        (legacy,),
    )
    store.conn.execute(
        "INSERT INTO audit_events VALUES ('bad-chain','task.status_changed',?,?)",
        ('{"task_id":"broken","previous_status":"BLOCKED","current_status":"OPEN","stage":"PLAN","cause":"fixture"}', BASE.timestamp()),
    )
    store.conn.commit()

    before = store.db_path.read_bytes()
    report = query_activity_periods(store, ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1)))

    assert report.unavailable_tasks == 2
    assert not report.active_intervals
    assert all(bucket.opened_occurrences == bucket.completed_occurrences == 0 for bucket in report.series)
    assert store.db_path.read_bytes() == before


def test_timezone_buckets_follow_dst_boundaries(store: Store) -> None:
    task = store.upsert_task("dst", source_id="dst")
    _created(store, task, datetime(2026, 3, 8, 7, tzinfo=UTC))
    report = query_activity_periods(
        store,
        ActivityPeriodQuery(
            since=datetime(2026, 3, 7, 6, tzinfo=UTC),
            until=datetime(2026, 3, 10, 5, tzinfo=UTC),
            timezone="America/Chicago",
        ),
    )

    lengths = [(bucket.end - bucket.start).total_seconds() / 3600 for bucket in report.series]
    assert lengths == [24, 23, 24]
    assert [bucket.opened_occurrences for bucket in report.series] == [0, 1, 0]


def test_task_created_anchor_is_idempotent_for_source_upsert(store: Store) -> None:
    first = store.upsert_task("first", source="github", source_id="77")
    second = store.upsert_task("renamed", source="github", source_id="77")
    assert first == second
    count = store.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type='task.created' "
        "AND json_extract(payload, '$.task_id')=?",
        (first,),
    ).fetchone()[0]
    assert count == 1


def test_activity_periods_cli_json_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = cli_module.db_path(project)
    store = Store(database)
    store.migrate()
    task = store.upsert_task("cli period", source_id="cli-period")
    _created(store, task, BASE)
    store.close()
    before = database.read_bytes()

    code = cli_module.main([
        "--project", str(project), "activity-periods",
        "--since", "2026-01-01T00:00:00Z",
        "--until", "2026-01-03T00:00:00Z",
        "--timezone", "UTC", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0 and payload["active_intervals"][0]["task_id"] == task
    assert payload["series"][0]["opened_occurrences"] == 1
    assert database.read_bytes() == before


def test_built_wheel_declares_windows_iana_timezone_data(tmp_path: Path) -> None:
    import build_backend

    name = build_backend.build_wheel(str(tmp_path))
    with zipfile.ZipFile(tmp_path / name) as wheel:
        metadata_name = next(item for item in wheel.namelist() if item.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata_name).decode("utf-8")
    assert 'Requires-Dist: tzdata>=2025.2; platform_system == "Windows"' in metadata


@pytest.mark.parametrize(
    "query",
    [
        ActivityPeriodQuery(since=BASE, until=BASE),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=3661)),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), bucket="month"),  # type: ignore[arg-type]
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), timezone="Not/AZone"),
        ActivityPeriodQuery(since=BASE.replace(tzinfo=None), until=BASE + timedelta(days=1)),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), limit=0),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), limit=201),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), offset=-1),
        ActivityPeriodQuery(since=BASE, until=BASE + timedelta(days=1), offset=1000),
    ],
)
def test_invalid_period_queries_fail_closed(store: Store, query: ActivityPeriodQuery) -> None:
    with pytest.raises(ActivityPeriodError):
        query_activity_periods(store, query)
