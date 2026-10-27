from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stagemesh.activity import (
    MAX_LIMIT,
    ActivityQuery,
    ActivityQueryError,
    query_activity,
)
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage
from stagemesh.persistence import Store

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _at(minutes: int) -> float:
    return (BASE + timedelta(minutes=minutes)).timestamp()


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "s.sqlite3")
    s.migrate()
    yield s
    s.close()


def _task(store: Store, name: str, minute: int, stage: Stage = Stage.PLAN, done: bool = False) -> str:
    task_id = store.upsert_task(name, source_id=name)
    status = "DONE" if done else "OPEN"
    store.conn.execute(
        "UPDATE tasks SET stage=?, status=?, updated_at=? WHERE id=?", (stage.value, status, _at(minute), task_id)
    )
    store.conn.commit()
    return task_id


def _execution(store: Store, task_id: str, minute: int, actor: str | None, finish: ExecutionStatus | None, **kw) -> str:
    execution_id = store.start_execution(
        task_id=task_id, claim_id=None, kind=ExecutionKind.REVIEW, actor=actor, **kw
    )
    if finish is not None:
        store.finish_execution(execution_id, finish, result=kw.pop("result", None))
    store.conn.execute("UPDATE executions SET started_at=? WHERE id=?", (_at(minute), execution_id))
    store.conn.commit()
    return execution_id


def _ids(page) -> list[str]:
    return [e.id for e in page.entries]


def test_unfiltered_orders_newest_first_with_all_kinds(store: Store) -> None:
    t = _task(store, "one", 10)
    e = _execution(store, t, 20, "codex", ExecutionStatus.SUCCEEDED)
    a = store.add_audit_event("task.note", {"task_id": t})
    store.conn.execute("UPDATE audit_events SET created_at=? WHERE id=?", (_at(30), a))
    store.conn.commit()
    page = query_activity(store)
    assert _ids(page) == [a, e, t]
    assert [x.kind for x in page.entries] == ["audit", "execution", "task"]
    assert all(x.at.tzinfo is not None for x in page.entries)
    assert page.entries[0].task_id == t and page.total == 3 and page.next_offset is None


def test_state_filter_open_and_closed(store: Store) -> None:
    open_task = _task(store, "open", 10)
    done_task = _task(store, "done", 11, Stage.DONE, done=True)
    running = _execution(store, open_task, 12, "codex", None)
    finished = _execution(store, open_task, 13, "codex", ExecutionStatus.FAILED)
    assert set(_ids(query_activity(store, ActivityQuery(state="open")))) == {open_task, running}
    assert set(_ids(query_activity(store, ActivityQuery(state="closed")))) == {done_task, finished}


def test_task_stage_and_provider_filters(store: Store) -> None:
    plan = _task(store, "p", 10, Stage.PLAN)
    review = _task(store, "r", 11, Stage.REVIEW)
    e1 = _execution(store, plan, 12, "codex", ExecutionStatus.SUCCEEDED)
    e2 = _execution(store, review, 13, "claude", ExecutionStatus.SUCCEEDED)
    assert _ids(query_activity(store, ActivityQuery(stage="REVIEW"))) == [e2, review]
    assert _ids(query_activity(store, ActivityQuery(provider="codex"))) == [e1]
    assert _ids(query_activity(store, ActivityQuery(task_id=plan))) == [e1, plan]
    both = query_activity(store, ActivityQuery(provider="claude", stage="REVIEW"))
    assert _ids(both) == [e2]


def test_task_id_filter_applies_to_audit_payload(store: Store) -> None:
    t1, t2 = _task(store, "a", 1), _task(store, "b", 2)
    mine = store.add_audit_event("x.y", {"task_id": t1})
    store.add_audit_event("x.y", {"task_id": t2})
    store.add_audit_event("x.y", {"note": "no task"})
    page = query_activity(store, ActivityQuery(task_id=t1))
    assert mine in _ids(page) and len(page.entries) == 2


def test_time_range_is_half_open_and_timezone_aware(store: Store) -> None:
    t = _task(store, "t", 0)
    e1 = _execution(store, t, 10, "codex", ExecutionStatus.SUCCEEDED)
    e2 = _execution(store, t, 20, "codex", ExecutionStatus.SUCCEEDED)
    plus_two = timezone(timedelta(hours=2))
    since = (BASE + timedelta(minutes=10)).astimezone(plus_two)
    until = (BASE + timedelta(minutes=20)).astimezone(plus_two)
    page = query_activity(store, ActivityQuery(since=since, until=until))
    assert _ids(page) == [e1]
    assert e2 not in _ids(page)


def test_pagination_is_stable_and_complete(store: Store) -> None:
    t = _task(store, "t", 0)
    ids = [_execution(store, t, 5, "codex", ExecutionStatus.SUCCEEDED) for _ in range(5)]  # identical timestamps
    seen: list[str] = []
    offset = 0
    while offset is not None:
        page = query_activity(store, ActivityQuery(provider="codex", limit=2, offset=offset))
        seen.extend(_ids(page))
        offset = page.next_offset
    assert sorted(seen) == sorted(ids) and len(set(seen)) == 5
    assert seen == _ids(query_activity(store, ActivityQuery(provider="codex", limit=10)))


def test_free_text_and_secrets_are_not_exposed(store: Store) -> None:
    t = store.upsert_task("deploy with token=abc123 https://user:pw@host/x", source_id="s")
    e = _execution(store, t, 1, "codex", ExecutionStatus.FAILED, candidate_sha="abcdef1")
    store.conn.execute("UPDATE executions SET result=? WHERE id=?", ("provider said: api_key=SECRET\nstack", e))
    a = store.add_audit_event("note.free text with spaces", {"task_id": t, "password": "hunter2"})
    store.conn.commit()
    page = query_activity(store)
    dump = repr(page)
    for leaked in ("abc123", "pw@", "SECRET", "hunter2", "free text"):
        assert leaked not in dump
    by_id = {x.id: x for x in page.entries}
    assert by_id[e].result_code is None and by_id[e].candidate_sha == "abcdef1"
    assert by_id[a].event_type is None and by_id[a].task_id == t
    assert "REDACTED" in by_id[t].title


def test_secret_looking_actor_is_dropped(store: Store) -> None:
    t = _task(store, "t", 0)
    e = _execution(store, t, 1, "api_key-xyz", ExecutionStatus.SUCCEEDED)
    assert next(x for x in query_activity(store).entries if x.id == e).provider is None


def test_query_does_not_mutate_store(store: Store) -> None:
    t = _task(store, "t", 0)
    _execution(store, t, 1, "codex", None)
    before = store.conn.total_changes
    query_activity(store, ActivityQuery(state="open"))
    assert store.conn.total_changes == before


@pytest.mark.parametrize(
    "query",
    [
        ActivityQuery(since=datetime(2026, 1, 1)),  # noqa: DTZ001
        ActivityQuery(until=datetime(2026, 1, 1)),  # noqa: DTZ001
        ActivityQuery(since=BASE, until=BASE),
        ActivityQuery(since=BASE + timedelta(hours=1), until=BASE),
        ActivityQuery(state="bogus"),  # type: ignore[arg-type]
        ActivityQuery(stage="plan"),
        ActivityQuery(provider="  "),
        ActivityQuery(task_id="x" * 201),
        ActivityQuery(limit=0),
        ActivityQuery(limit=MAX_LIMIT + 1),
        ActivityQuery(limit=True),  # type: ignore[arg-type]
        ActivityQuery(offset=-1),
        ActivityQuery(offset=1000, limit=1),
        ActivityQuery(since="2026-01-01"),  # type: ignore[arg-type]
    ],
)
def test_invalid_queries_are_rejected(store: Store, query: ActivityQuery) -> None:
    with pytest.raises(ActivityQueryError):
        query_activity(store, query)


@pytest.mark.parametrize("credential", [
    "password: hunter2", "password = hunter2", 'password: "hunter2 extra"',
    "authorization: Bearer hunter2", "api_key= hunter2", "token hunter2",
])
def test_title_redacts_associated_credential_values(store: Store, credential: str) -> None:
    store.upsert_task("deploy " + credential, source_id="title-secret")
    page = query_activity(store)
    assert "hunter2" not in repr(page)
    assert "extra" not in repr(page)
    assert page.entries[0].title.startswith("deploy ")
    assert "REDACTED" in page.entries[0].title


@pytest.mark.parametrize("result, expected", [
    ("hunter2", None), ("sk-abc123", None), ("ghp_abc123", None),
    ("Bearer:abc123", None), ("provider_timeout-extra", None),
    ("provider_timeout", "provider_timeout"), ("operator_stop", "operator_stop"),
    ("findings", "findings"), (None, None),
])
def test_result_codes_use_known_outcomes(store: Store, result, expected) -> None:
    t = _task(store, "ordinary title", 0)
    e = _execution(store, t, 1, "custom-provider", ExecutionStatus.FAILED)
    store.conn.execute("UPDATE executions SET result=? WHERE id=?", (result, e))
    store.conn.commit()
    page = query_activity(store)
    entry = next(x for x in page.entries if x.id == e)
    assert entry.result_code == expected
    assert entry.provider == "custom-provider"
    assert next(x for x in page.entries if x.id == t).title == "ordinary title"


@pytest.mark.parametrize("limit", [200, 199])
@pytest.mark.parametrize("task_count, overflow", [(998, False), (1000, True)])
def test_combined_pagination_cap(store: Store, limit: int, task_count: int, overflow: bool) -> None:
    t = _task(store, "seed", 0)
    store.conn.executemany(
        "INSERT INTO tasks (id, title, source_id, source, stage, status, created_at, updated_at) "
        "SELECT ?, title, ?, source, stage, status, created_at, updated_at FROM tasks WHERE id=?",
        [(f"task-{i:04d}", f"source-{i}", t) for i in range(task_count - 1)],
    )
    store.conn.commit()
    e = _execution(store, t, 1, "codex", ExecutionStatus.SUCCEEDED)
    a = store.add_audit_event("task.note", {"task_id": t})
    before = store.conn.total_changes
    seen = []
    offset = 0
    while offset is not None:
        page = query_activity(store, ActivityQuery(limit=limit, offset=offset))
        assert page.total == 1000
        assert page.truncated is overflow
        assert len(page.entries) <= limit
        seen.extend(_ids(page))
        offset = page.next_offset
    assert len(seen) == len(set(seen)) == 1000
    assert seen[:2] == [a, e]
    assert store.conn.total_changes == before
    assert len(page.entries) == (200 if limit == 200 else 5)
