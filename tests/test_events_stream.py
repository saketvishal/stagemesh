"""Tests for SM-005: structured event streaming.

Covers build_coordinator.events.stream_events / decode_cursor (in-order,
resumable-from-cursor emission of durable coordinator events) and the
`events stream` CLI surface.
"""

from __future__ import annotations

import json

import pytest

from build_coordinator.cli import _build_parser, _events_stream
from build_coordinator.db import DatabaseLifecycle
from build_coordinator.events import decode_cursor, record_event, stream_events
from build_coordinator.types import EventInput


@pytest.fixture
def session():
    lifecycle = DatabaseLifecycle("sqlite:///:memory:")
    lifecycle.initialize_schema()
    with lifecycle.session() as db_session:
        yield db_session


def _record(session, event_type: str, task_id: str = "task-1"):
    row = record_event(session, EventInput(task_id=task_id, event_type=event_type))
    session.commit()
    return row


def test_stream_events_returns_events_in_emission_order(session):
    _record(session, "task.claimed")
    _record(session, "task.transitioned")
    _record(session, "task.checkpointed")

    records = stream_events(session)

    assert [r.event_type for r in records] == [
        "task.claimed",
        "task.transitioned",
        "task.checkpointed",
    ]


def test_stream_events_assigns_stable_unique_identifiers(session):
    _record(session, "task.claimed")
    _record(session, "task.transitioned")

    records = stream_events(session)

    event_ids = [r.event_id for r in records]
    assert len(event_ids) == len(set(event_ids))
    assert all(event_ids)


def test_stream_events_is_resumable_from_cursor(session):
    _record(session, "task.claimed")
    _record(session, "task.transitioned")
    _record(session, "task.checkpointed")

    first_page = stream_events(session, limit=2)
    assert [r.event_type for r in first_page] == ["task.claimed", "task.transitioned"]

    resumed = stream_events(session, after_cursor=first_page[-1].cursor)
    assert [r.event_type for r in resumed] == ["task.checkpointed"]


def test_stream_events_after_last_cursor_returns_empty(session):
    _record(session, "task.claimed")

    records = stream_events(session)
    resumed = stream_events(session, after_cursor=records[-1].cursor)

    assert resumed == []


def test_stream_events_filters_by_task_id(session):
    _record(session, "task.claimed", task_id="task-1")
    _record(session, "task.claimed", task_id="task-2")

    records = stream_events(session, task_id="task-2")

    assert len(records) == 1
    assert records[0].task_id == "task-2"


def test_decode_cursor_rejects_malformed_input():
    with pytest.raises(ValueError):
        decode_cursor("not-a-real-cursor")

    with pytest.raises(ValueError):
        decode_cursor("1-2")


def test_stream_events_rejects_malformed_cursor(session):
    with pytest.raises(ValueError):
        stream_events(session, after_cursor="not-a-real-cursor")


def test_events_stream_command_parses():
    parser = _build_parser()
    args = parser.parse_args(["events", "stream"])
    assert args.command == "events"
    assert args.events_command == "stream"
    assert args.after_cursor is None
    assert args.limit is None


def test_events_stream_command_accepts_options():
    parser = _build_parser()
    args = parser.parse_args(
        ["events", "stream", "--after-cursor", "abc", "--task-id", "task-1", "--limit", "5"]
    )
    assert args.after_cursor == "abc"
    assert args.task_id == "task-1"
    assert args.limit == 5


def test_events_stream_cli_emits_jsonl_structured_records(session, capsys):
    _record(session, "task.claimed", task_id="task-1")
    parser = _build_parser()
    args = parser.parse_args(["events", "stream", "--task-id", "task-1"])

    _events_stream(args, session)

    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(payloads) == 1
    assert payloads[0]["cursor"] == payloads[0]["event_id"]
    assert payloads[0]["task_id"] == "task-1"
    assert payloads[0]["event_type"] == "task.claimed"
    assert payloads[0]["event_data"] == {}
    assert payloads[0]["created_at"]


def test_events_stream_cli_rejects_malformed_cursor(session):
    parser = _build_parser()
    args = parser.parse_args(["events", "stream", "--after-cursor", "not-a-real-cursor"])

    with pytest.raises(SystemExit, match="invalid event cursor"):
        _events_stream(args, session)
