from __future__ import annotations

import json
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


def _transition(store: Store, task: str, minute: int, before: str = "INTEGRATE", after: str = "DONE") -> str:
    event = store.add_audit_event("stage.transition", {"task_id": task, "from": before, "to": after})
    store.conn.execute("UPDATE audit_events SET created_at=? WHERE id=?", (_at(minute), event))
    store.conn.commit()
    return event


def test_time_basis_distinguishes_opened_updated_and_recorded_completion(store: Store):
    task = _task(store, "cross-window", 80, Stage.DONE, done=True)
    store.conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (_at(5), task))
    execution = _execution(store, task, 10, "codex", ExecutionStatus.SUCCEEDED)
    store.conn.execute("UPDATE executions SET updated_at=? WHERE id=?", (_at(25), execution))
    store.conn.commit()
    event = _transition(store, task, 30)
    start, end = BASE + timedelta(minutes=20), BASE + timedelta(minutes=40)
    assert not query_activity(store, ActivityQuery(time_basis="opened", since=start, until=end)).entries
    updated = query_activity(store, ActivityQuery(time_basis="updated", since=start, until=end))
    completed = query_activity(store, ActivityQuery(time_basis="completed", since=start, until=end))
    assert _ids(updated) == _ids(completed) == [event, execution]
    assert [e.at for e in completed.entries] == [BASE + timedelta(minutes=30), BASE + timedelta(minutes=25)]
    assert completed.entries[0].stage == "DONE" and completed.entries[0].status == "DONE"
    opened = query_activity(store, ActivityQuery(time_basis="opened", until=BASE + timedelta(minutes=11)))
    assert _ids(opened) == [execution, task]
    # A later task title/source update is not a second completion.
    assert not query_activity(store, ActivityQuery(time_basis="completed", since=BASE + timedelta(minutes=70))).entries


def test_completed_does_not_invent_legacy_done_or_unknown_execution_completion(store: Store):
    task = _task(store, "legacy-done", 20, Stage.DONE, done=True)
    unknown = _execution(store, task, 10, "codex", ExecutionStatus.UNKNOWN)
    running = _execution(store, task, 10, "codex", None)
    failed = _execution(store, task, 10, "codex", ExecutionStatus.FAILED)
    page = query_activity(store, ActivityQuery(time_basis="completed"))
    assert _ids(page) == [failed]
    assert unknown not in _ids(page) and running not in _ids(page) and task not in _ids(page)
    assert page.status_counts["execution"]["UNKNOWN"] == 0
    assert query_activity(store).status_counts["execution"]["UNKNOWN"] == 1


def test_reopened_and_recompleted_history_survives_current_state_and_restart(store: Store):
    task = _task(store, "reopened", 50)
    first = _transition(store, task, 10)
    _transition(store, task, 20, "DONE", "IMPLEMENT")
    second = _transition(store, task, 30)
    _transition(store, task, 40, "DONE", "IMPLEMENT")
    path = Path(store.conn.execute("PRAGMA database_list").fetchone()[2])
    reopened = Store(path)
    try:
        query = ActivityQuery(time_basis="completed", task_id=task, stage="DONE", state="closed")
        page = query_activity(reopened, query)
        assert _ids(page) == [second, first] and page.status_counts["audit"]["DONE"] == 2
        assert page.status_counts["task"]["DONE"] == 0  # events are not fabricated task rows
        assert reopened.get_task(task)["status"] == "OPEN"
        assert _ids(query_activity(reopened, ActivityQuery(time_basis="completed", until=BASE + timedelta(minutes=20)))) == [first]
        assert not query_activity(reopened, ActivityQuery(time_basis="completed", state="open")).entries
        assert not query_activity(reopened, ActivityQuery(time_basis="completed", stage="IMPLEMENT")).entries
    finally:
        reopened.close()


def test_project_filter_uses_exact_stored_attribution_and_preserves_unattributed_rows(store: Store):
    alpha = store.upsert_task("alpha", source_id="alpha", project="team/alpha")
    beta = store.upsert_task("beta", source_id="beta", project="team/beta")
    unknown = store.upsert_task("unknown", source_id="unknown")
    execution = _execution(store, alpha, 5, "codex", None)
    event = store.add_audit_event("task.note", {"task_id": alpha})
    global_event = store.add_audit_event("global.note", {"project": "team/alpha"})
    page = query_activity(store, ActivityQuery(project="team/alpha"))
    assert set(_ids(page)) == {alpha, execution, event}
    assert all(e.project == "team/alpha" for e in page.entries)
    assert not query_activity(store, ActivityQuery(project="TEAM/alpha")).entries
    unfiltered = {e.id: e for e in query_activity(store).entries}
    assert unfiltered[beta].project == "team/beta"
    assert unfiltered[unknown].project is None and unfiltered[global_event].project is None


def test_project_and_completion_predicates_precede_audit_retention_cap(store: Store):
    task = store.upsert_task("target", source_id="target", project="alpha")
    wanted = _transition(store, task, 1)
    store.conn.executemany("INSERT INTO audit_events VALUES (?, ?, ?, ?)",
                           [(f"noise-{i}", "stage.transition", json.dumps({"task_id": "other", "from": "INTEGRATE", "to": "DONE"}), _at(i + 2)) for i in range(1005)])
    store.conn.commit()
    for query in (ActivityQuery(time_basis="completed", project="alpha"),
                  ActivityQuery(time_basis="completed", task_id=task),
                  ActivityQuery(task_id=task, until=BASE + timedelta(days=1))):
        page = query_activity(store, query)
        assert _ids(page) == [wanted] and not page.truncated


@pytest.mark.parametrize("payload", ["not json", "null", "[]", '{"task_id":[]}',
    '{"task_id":"t","from":"DONE","to":"DONE"}', '{"task_id":"t","from":"unknown","to":"DONE"}',
    '{"task_id":"t","from":"REVIEW","to":"IMPLEMENT"}', '{"task_id":"","from":"PLAN","to":"DONE"}'])
def test_completed_rejects_malformed_or_noncompletion_audits(store: Store, payload):
    event = store.add_audit_event("stage.transition", {})
    store.conn.execute("UPDATE audit_events SET payload=? WHERE id=?", (payload, event))
    store.conn.commit()
    assert not query_activity(store, ActivityQuery(time_basis="completed")).entries
    query_activity(store, ActivityQuery(project="any"))  # malformed JSON also stays safe at the project join


def test_counts_are_partitioned_before_pagination_and_counts_are_bounded(store: Store, monkeypatch):
    import stagemesh.activity as module
    task = _task(store, "blocked", 1)
    store.conn.execute("UPDATE tasks SET status='BLOCKED' WHERE id=?", (task,))
    _execution(store, task, 2, "codex", None)
    _execution(store, task, 3, "codex", ExecutionStatus.FAILED)
    _transition(store, task, 4)
    first = query_activity(store, ActivityQuery(limit=1))
    second = query_activity(store, ActivityQuery(limit=1, offset=1))
    assert first.status_counts == second.status_counts
    assert first.status_counts["task"]["BLOCKED"] == 1
    assert first.status_counts["execution"]["RUNNING"] == first.status_counts["execution"]["FAILED"] == 1
    assert first.status_counts["audit"] == {"DONE": 0, "EVENT": 1}
    assert sum(sum(group.values()) for group in first.status_counts.values()) == first.total == 4
    monkeypatch.setattr(module, "MAX_SCAN", 2)
    capped = query_activity(store, ActivityQuery(limit=1))
    assert capped.truncated and capped.total == 2
    assert sum(sum(group.values()) for group in capped.status_counts.values()) == 2
    assert capped.status_counts["task"]["BLOCKED"] == 0


def test_selected_time_basis_keeps_half_open_offset_boundaries(store: Store):
    task = _task(store, "boundaries", 0)
    at_start, at_end = _transition(store, task, 60), _transition(store, task, 120)
    zone = timezone(timedelta(hours=-5))
    page = query_activity(store, ActivityQuery(time_basis="completed",
        since=(BASE + timedelta(hours=1)).astimezone(zone), until=(BASE + timedelta(hours=2)).astimezone(zone)))
    assert _ids(page) == [at_start] and at_end not in _ids(page)


@pytest.mark.parametrize("query", [ActivityQuery(project=""), ActivityQuery(project="x" * 201),
    ActivityQuery(project=1), ActivityQuery(time_basis="finished"), ActivityQuery(time_basis=None)])
def test_new_filter_values_fail_closed(store: Store, query):
    with pytest.raises(ActivityQueryError):
        query_activity(store, query)


def test_project_display_redacts_credentials_without_mutation(store: Store):
    project = "https://user:password@host/repo"
    store.upsert_task("one", source_id="one", project=project)
    before = store.conn.total_changes
    page = query_activity(store, ActivityQuery(project=project))
    assert page.total == 1 and "password" not in repr(page)
    assert store.conn.total_changes == before
