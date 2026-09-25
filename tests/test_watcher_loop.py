"""Integration test: the watcher foreground cycle delegates to

`BuildRunner.run_once()` and does not duplicate claims/executions across a

simulated restart (SDD-001 section 9.8)."""



from __future__ import annotations



import json

import subprocess

from datetime import UTC, datetime

from unittest.mock import MagicMock



import pytest

from sqlalchemy import delete, select



from build_coordinator.db import Base, SessionLocal, engine, initialize_schema

from build_coordinator.github.client import GitHubClient, GitHubIssue

from build_coordinator.models import (

    BuildObjective,

    BuildRunnerExecution,

    BuildTask,

    BuildWatcherRecord,

)

from build_coordinator.runner.git_safety import FakeGit

from build_coordinator.runner.models import RunnerConfig, WorkerConfig

from build_coordinator.service import upsert_task

from build_coordinator.types import TaskSpec

from build_coordinator.watcher import authorization as auth

from build_coordinator.watcher.loop import run_foreground_cycle

from build_coordinator.watcher.safe_logging import WatcherLogger





@pytest.fixture(autouse=True)

def clean_state():

    Base.metadata.drop_all(bind=engine)

    initialize_schema()

    with SessionLocal() as session:

        for model in (BuildRunnerExecution, BuildTask, BuildWatcherRecord):

            session.execute(delete(model))

        session.commit()

    yield





def _init_repo(path) -> None:

    path.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)

    subprocess.run(

        ["git", "-C", str(path), "remote", "add", "origin", "https://github.com/saketvishal/stagemesh-orchestrator.git"],

        check=True,

        capture_output=True,

    )





@pytest.fixture

def authorized_repo(tmp_path, monkeypatch):

    repo_root = tmp_path / "orchestrator"

    _init_repo(repo_root)

    config_path = tmp_path / "build-coordinator.json"

    config_path.write_text(

        json.dumps(

            {

                "authorized_repositories": [

                    {

                        "slug": "stagemesh-orchestrator",

                        "control_repo_root": str(repo_root),

                        "labels": False,

                    }

                ]

            }

        ),

        encoding="utf-8",

    )

    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(config_path))

    return auth.authorize("stagemesh-orchestrator")





def _runner_config() -> RunnerConfig:

    return RunnerConfig(

        workers=(WorkerConfig("builder-a", "BUILDER", adapter="fake"),),

    )





def test_run_foreground_cycle_delegates_to_build_runner(authorized_repo):

    with SessionLocal() as session:

        upsert_task(

            session,

            TaskSpec(

                task_id="WATCH-1",

                title="Watcher-dispatched task",

                description="fake builder task",

                acceptance_criteria=["passes"],

                review_policy="NONE",

            ),

        )

        session.commit()



    logger = _logger()

    outcome = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=_runner_config(),

        git=FakeGit(),

        github_client=_github_no_issues(),

    )

    assert outcome.ok



    with SessionLocal() as session:

        task = session.get(BuildTask, "WATCH-1")

        assert task.state == "CLAIMED"

        executions = session.scalars(select(BuildRunnerExecution)).all()

        assert len(executions) == 1





def test_run_foreground_cycle_does_not_duplicate_after_simulated_restart(authorized_repo):

    with SessionLocal() as session:

        upsert_task(

            session,

            TaskSpec(

                task_id="WATCH-2",

                title="Watcher-dispatched task",

                description="fake builder task",

                acceptance_criteria=["passes"],

                review_policy="NONE",

            ),

        )

        session.commit()



    logger = _logger()

    config = _runner_config()



    first = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        git=FakeGit(),

        github_client=_github_no_issues(),

        instance_id="watcher-instance",

    )

    assert first.ok



    # Simulate the watcher process crashing and a replacement process

    # starting up: within a single pytest process we can't literally exit

    # and relaunch, so stand in for "old process died" by pointing the

    # durable lock record at a PID that is certainly not alive -- exactly

    # what `watcher.lock.acquire_lock`'s stale-recovery path is for.

    with SessionLocal() as session:

        record = session.get(BuildWatcherRecord, first.task_name)

        record.process_id = 999_999

        session.commit()



    # A brand-new run_foreground_cycle call (fresh BuildRunner, fresh

    # executors dict) against the same durable coordinator state must

    # recover the stale lock and reconcile the already-launched execution,

    # not launch a second one for the same task.

    second = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        git=FakeGit(),

        github_client=_github_no_issues(),

        instance_id="watcher-instance-2",

    )

    assert second.ok



    with SessionLocal() as session:

        executions = session.scalars(

            select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "WATCH-2")

        ).all()

        assert len(executions) == 1



    # The watcher lock itself must also not duplicate: still exactly one

    # BuildWatcherRecord row for this repository's task name.

    with SessionLocal() as session:

        records = session.scalars(select(BuildWatcherRecord)).all()

        assert len(records) == 1





def test_repeated_watcher_cycles_ingest_github_issue_without_self_locking(authorized_repo, tmp_path):

    mock_gh = MagicMock(spec=GitHubClient)

    mock_gh.get_authorized_issues.return_value = [

        GitHubIssue(

            number=6,

            title="Watcher unattended ingestion smoke test",

            body="Acceptance test issue",

            labels=("build:objective",),

            author="saketvishal",

            state="OPEN",

            html_url="https://github.com/saketvishal/stagemesh-orchestrator/issues/6",

        )

    ]

    mock_gh.get_issue_comments.return_value = []



    logger = _logger()

    config = RunnerConfig(workers=(), result_dir=str(tmp_path / "results"))



    first = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        github_client=mock_gh,

        instance_id="watcher-instance",

    )

    assert first.ok



    second = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        github_client=mock_gh,

        instance_id="watcher-instance",

    )

    assert second.ok



    with SessionLocal() as session:

        objective = session.get(BuildObjective, "GH-stagemesh-orchestrator-6")

        assert objective is not None

        assert len(session.query(BuildObjective).filter_by(objective_id=objective.objective_id).all()) == 1



        record = session.get(BuildWatcherRecord, first.task_name)

        assert record.last_cycle_summary["github_issues_ingested"] == 0

        assert record.last_error_type is None





def test_stale_lock_recovery_runs_github_ingestion(authorized_repo, tmp_path, monkeypatch):

    monkeypatch.setattr("build_coordinator.watcher.lock.is_pid_alive", lambda pid: True)

    mock_gh = MagicMock(spec=GitHubClient)

    mock_gh.get_authorized_issues.return_value = [

        GitHubIssue(

            number=6,

            title="Watcher unattended ingestion smoke test",

            body="Acceptance test issue",

            labels=("build:objective",),

            author="saketvishal",

            state="OPEN",

            html_url="https://github.com/saketvishal/stagemesh-orchestrator/issues/6",

        )

    ]

    mock_gh.get_issue_comments.return_value = []



    logger = _logger()

    config = RunnerConfig(workers=(), result_dir=str(tmp_path / "results"))



    first = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        github_client=_github_no_issues(),

        instance_id="old-owner",

    )

    assert first.ok



    with SessionLocal() as session:

        record = session.get(BuildWatcherRecord, first.task_name)

        record.process_id = 50784

        record.watcher_id = "ffdc04c2-b381-4f15-bb73-a22151552513"

        record.heartbeat_at = datetime(2026, 9, 13, 23, 17, 58, 884875, tzinfo=UTC)

        session.commit()



    recovered = run_foreground_cycle(

        SessionLocal,

        repository_slug="stagemesh-orchestrator",

        logger=logger,

        runner_config=config,

        github_client=mock_gh,

        instance_id="new-owner",

    )

    assert recovered.ok



    with SessionLocal() as session:

        objective = session.get(BuildObjective, "GH-stagemesh-orchestrator-6")

        record = session.get(BuildWatcherRecord, first.task_name)

        assert objective is not None

        assert record.watcher_id == "new-owner"

        assert record.last_cycle_summary["github_issues_ingested"] == 1





def _logger():

    import tempfile

    from pathlib import Path



    return WatcherLogger(Path(tempfile.mkdtemp(prefix="watcher-log-test-")))





def _github_no_issues():

    mock_gh = MagicMock(spec=GitHubClient)

    mock_gh.get_authorized_issues.return_value = []

    mock_gh.get_issue_comments.return_value = []

    return mock_gh

