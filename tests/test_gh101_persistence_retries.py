"""Focused GH-101 persistence retry regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

from build_coordinator.db import DatabaseBusyError


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Lifecycle:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def session(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def test_project_sync_retries_from_fresh_session_after_typed_busy_commit(monkeypatch):
    from build_coordinator.project import commands

    lifecycle = _Lifecycle()
    commits = {"count": 0}
    sync_sessions: list[_Session] = []

    class _Report:
        def as_dict(self):
            return {"ok": True}

    def flaky_commit(_session):
        commits["count"] += 1
        if commits["count"] == 1:
            raise DatabaseBusyError("SQLite write contention on commit")

    def sync_backlog(session, *_args, **_kwargs):
        sync_sessions.append(session)
        return _Report()

    monkeypatch.setattr(commands, "_project_from_args", lambda _args: SimpleNamespace())
    monkeypatch.setattr(commands, "_open", lambda _project: lifecycle)
    monkeypatch.setattr(commands, "load_backlog", lambda _project: [])
    monkeypatch.setattr(commands, "sync_backlog", sync_backlog)
    monkeypatch.setattr(commands, "commit_or_busy", flaky_commit)
    printed: list[dict] = []
    monkeypatch.setattr(commands, "_print", printed.append)

    commands.handle_project(
        SimpleNamespace(
            project_command="sync",
            name=[],
            project_dir=None,
            dry_run=False,
        )
    )

    assert commits["count"] == 2
    assert len(lifecycle.sessions) == 2
    assert sync_sessions == lifecycle.sessions
    assert printed == [{"ok": True}]


def test_watcher_lock_acquisition_retries_from_fresh_session_after_typed_busy_commit(monkeypatch):
    from build_coordinator.watcher import loop

    lifecycle = _Lifecycle()
    commits = {"count": 0}
    acquisitions: list[_Session] = []

    def flaky_commit(_session):
        commits["count"] += 1
        if commits["count"] == 1:
            raise DatabaseBusyError("SQLite write contention on commit")

    def acquire_lock(session, **_kwargs):
        acquisitions.append(session)
        return SimpleNamespace(recovered_stale=False, record=SimpleNamespace())

    class _Controller:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_once(self):
            runner_result = SimpleNamespace(
                mode="RUNNING",
                recovered=[],
                launched=[],
                observed=[],
                escalations=[],
            )
            return SimpleNamespace(
                runner_result=runner_result,
                issues_ingested=[],
                gates_approved=[],
                gates_published=[],
                prs_created=[],
                statuses_synced={},
            )

    monkeypatch.setattr(loop, "authorize", lambda _slug: SimpleNamespace(control_repo_root=Path("."), slug="owner/repo", labels=[]))
    monkeypatch.setattr(loop.watcher_lock, "acquire_lock", acquire_lock)
    monkeypatch.setattr(loop, "commit_or_busy", flaky_commit)
    monkeypatch.setattr(loop, "GitHubAutonomousController", _Controller)
    monkeypatch.setattr(loop, "acquire_coordinator_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "release_coordinator_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop.watcher_lock, "heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop.watcher_lock, "record_cycle", lambda *_args, **_kwargs: None)

    outcome = loop.run_foreground_cycle(
        lifecycle.session,
        repository_slug="owner/repo",
        logger=SimpleNamespace(log=lambda *_args, **_kwargs: None),
        runner_config=SimpleNamespace(),
    )

    assert outcome.ok is True
    assert commits["count"] > 2
    assert len(acquisitions) == 2
    assert acquisitions[0] in lifecycle.sessions
    assert acquisitions[1] in lifecycle.sessions
    assert acquisitions[0] is not acquisitions[1]


def test_runner_command_lock_acquisition_retries_on_transient_sqlite_busy(monkeypatch):
    """GH-101: `stagemesh run`'s coordinator lock acquisition (cli._runner)
    must be retried under `with_sqlite_retry`, mirroring `stagemesh continue`,
    instead of surfacing a raw OperationalError from a short-lived writer
    lock during startup."""
    from build_coordinator import cli

    attempts = {"acquire": 0, "commit": 0}

    def flaky_acquire_coordinator_lock(_session, *, instance_id):
        attempts["acquire"] += 1
        if attempts["acquire"] == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return SimpleNamespace(recovered_stale=False, record=SimpleNamespace())

    def counting_commit_or_busy(_session):
        attempts["commit"] += 1

    run_once_calls = {"count": 0}

    class _Runner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run_once(self):
            run_once_calls["count"] += 1
            return SimpleNamespace(
                mode="RUNNING",
                recovered=[],
                launched=[],
                observed=[],
                escalations=[],
                capacity_full=False,
                objectives_reconciled=0,
                objective_follow_ups_created=0,
                objective_unrelated_tasks_created=0,
                objective_gates_raised=0,
                objectives_completed=0,
            )

    monkeypatch.setattr(cli, "acquire_coordinator_lock", flaky_acquire_coordinator_lock)
    monkeypatch.setattr(cli, "heartbeat_coordinator_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "release_coordinator_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "commit_or_busy", counting_commit_or_busy)
    monkeypatch.setattr(cli, "BuildRunner", _Runner)
    printed: list[dict] = []
    monkeypatch.setattr(cli, "_print", printed.append)

    cli._runner(SimpleNamespace(once=True, dry_run=False), session=SimpleNamespace())

    assert attempts["acquire"] == 2
    assert run_once_calls["count"] == 1
    assert printed and printed[0]["mode"] == "RUNNING"


def test_github_persistence_modules_do_not_commit_raw_sessions():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "build_coordinator/github/controller.py",
        "build_coordinator/github/gates.py",
        "build_coordinator/github/ingestion.py",
        "build_coordinator/github/sync.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "session.commit(" not in source, relative
