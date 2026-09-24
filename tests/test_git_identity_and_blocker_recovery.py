from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from build_coordinator.db import Base
from build_coordinator.events import record_event
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskEvent
from build_coordinator.runner.git_safety import (
    _is_disallowed_identity,
    resolve_git_identity,
    resolve_git_identity_args,
)
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.runner.orchestrator import BuildRunner, RunnerCycleResult
from build_coordinator.service import ensure_state, transition_task
from build_coordinator.types import EventInput


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def test_is_disallowed_identity():
    assert _is_disallowed_identity("stagemesh")
    assert _is_disallowed_identity("StageMesh")
    assert _is_disallowed_identity("stagemesh@localhost")
    assert _is_disallowed_identity("claude")
    assert _is_disallowed_identity("Anthropic-Claude")
    assert _is_disallowed_identity("codex")
    assert _is_disallowed_identity("grok")
    assert _is_disallowed_identity("openai")
    assert not _is_disallowed_identity("Vishal Singh")
    assert not _is_disallowed_identity("operator@company.org")


def test_resolve_git_identity_from_repo_config(tmp_path: Path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Alice Developer")
    _git(repo, "config", "user.email", "alice@example.com")

    name, email = resolve_git_identity(repo)
    assert name == "Alice Developer"
    assert email == "alice@example.com"

    args = resolve_git_identity_args(repo)
    assert args == ("-c", "user.name=Alice Developer", "-c", "user.email=alice@example.com")


def test_resolve_git_identity_rejects_disallowed_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "test_repo_disallowed"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "StageMesh")
    _git(repo, "config", "user.email", "stagemesh@localhost")

    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    monkeypatch.setenv("USERNAME", "TestOperator")

    name, email = resolve_git_identity(repo)
    assert name != "StageMesh"
    assert "stagemesh" not in email.lower()
    assert name == "TestOperator"
    assert email == "testoperator@localhost"


def test_resolve_git_identity_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Operator CI")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ci@domain.org")

    name, email = resolve_git_identity(tmp_path)
    assert name == "Operator CI"
    assert email == "ci@domain.org"


def _setup_runner_test_env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init")
    _git(repo_root, "config", "user.name", "Test Operator")
    _git(repo_root, "config", "user.email", "operator@example.com")
    (repo_root / "tracked.txt").write_text("initial", encoding="utf-8")
    _git(repo_root, "add", "tracked.txt")
    _git(repo_root, "commit", "-m", "init")

    config = RunnerConfig(
        workers=[],
        auto_push_allowed=False,
    )
    runner = BuildRunner(session_factory, config)
    import dataclasses
    runner._settings = dataclasses.replace(runner._settings, repo_root=repo_root, data_dir=tmp_path)

    return session_factory, runner, repo_root


def test_adaptive_blocker_recovery_working_checkout_dirty_when_clean(tmp_path: Path):
    session_factory, runner, repo_root = _setup_runner_test_env(tmp_path)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-100", title="Test Task", description="test desc", state="BLOCKED")
        session.add(task)
        # Record review succeeded
        exec_row = BuildRunnerExecution(
            execution_id="exec-rev-1",
            task_id="GH-100",
            role="REVIEWER",
            worker_id="reviewer-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            completed_at=datetime.now(UTC),
            result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
        )
        session.add(exec_row)
        session.flush()

        record_event(
            session,
            EventInput(
                task_id="GH-100",
                event_type="task.transitioned",
                actor="runner",
                to_state="BLOCKED",
                event_data={"reason": "WORKING_CHECKOUT_DIRTY"},
            ),
        )
        session.commit()

    # Working tree is clean
    res = runner.run_once()
    assert "GH-100" in res.recovered

    with session_factory() as session:
        task = session.get(BuildTask, "GH-100")
        assert task.state == "REVIEWING"


def test_adaptive_blocker_recovery_working_checkout_dirty_preserves_human_work(tmp_path: Path):
    session_factory, runner, repo_root = _setup_runner_test_env(tmp_path)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-101", title="Test Task Dirty", description="test desc", state="BLOCKED")
        session.add(task)
        session.flush()
        record_event(
            session,
            EventInput(
                task_id="GH-101",
                event_type="task.transitioned",
                actor="runner",
                to_state="BLOCKED",
                event_data={"reason": "WORKING_CHECKOUT_DIRTY"},
            ),
        )
        session.commit()

    # Create uncommitted human change in repo_root
    (repo_root / "uncommitted_human_file.txt").write_text("precious work", encoding="utf-8")

    res = runner.run_once()
    assert "GH-101" not in res.recovered

    with session_factory() as session:
        task = session.get(BuildTask, "GH-101")
        assert task.state == "BLOCKED"

    # Ensure human work was not deleted
    assert (repo_root / "uncommitted_human_file.txt").read_text(encoding="utf-8") == "precious work"


def test_adaptive_blocker_recovery_review_environment_blocked(tmp_path: Path):
    session_factory, runner, repo_root = _setup_runner_test_env(tmp_path)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-102", title="Test Task Env Blocked", description="test desc", state="BLOCKED")
        session.add(task)
        session.flush()
        record_event(
            session,
            EventInput(
                task_id="GH-102",
                event_type="task.transitioned",
                actor="runner",
                to_state="BLOCKED",
                event_data={"reason": "REVIEW_ENVIRONMENT_BLOCKED"},
            ),
        )
        session.commit()

    res = runner.run_once()
    assert "GH-102" in res.recovered

    with session_factory() as session:
        task = session.get(BuildTask, "GH-102")
        assert task.state == "REVIEW_READY"


def test_adaptive_blocker_recovery_upstream_push_failed(tmp_path: Path):
    session_factory, runner, repo_root = _setup_runner_test_env(tmp_path)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-103", title="Test Task Push Failed", description="test desc", state="BLOCKED")
        session.add(task)
        session.flush()
        record_event(
            session,
            EventInput(
                task_id="GH-103",
                event_type="task.transitioned",
                actor="runner",
                to_state="BLOCKED",
                event_data={"reason": "UPSTREAM_PUSH_FAILED"},
            ),
        )
        session.commit()

    res = runner.run_once()
    assert "GH-103" in res.recovered

    with session_factory() as session:
        task = session.get(BuildTask, "GH-103")
        assert task.state == "DONE"
