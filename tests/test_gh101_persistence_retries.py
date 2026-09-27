"""Focused GH-101 persistence retry regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
