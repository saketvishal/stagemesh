"""SM-016: `execution.setup` commands run once per prepared task workspace,
before the agent starts, with durable evidence gating the launch."""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.project.definition import ProjectError, load_project
from build_coordinator.project.doctor import FAIL, check_project
from build_coordinator.runner import BuildRunner
from build_coordinator.execution.fake import FakeExecutor
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import TaskSpec, upsert_task


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _project_yaml(tmp_path, setup=None, bootstrap=None):
    root = tmp_path / "repo"
    (root / ".stagemesh" / "tasks").mkdir(parents=True)
    execution = {"concurrency": 1, "reviewers": 1}
    if setup is not None:
        execution["setup"] = setup
    if bootstrap is not None:
        execution["bootstrap"] = bootstrap
    definition = {
        "schema_version": 1,
        "id": "fixture",
        "name": "Fixture",
        "execution": execution,
    }
    (root / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    return root


# ---------------------------------------------------------------- definition


def test_execution_setup_parses_into_project_definition(tmp_path):
    root = _project_yaml(tmp_path, setup=["echo one", "echo two"])
    project = load_project(root)
    assert project.setup_commands == ("echo one", "echo two")
    assert [command.command for command in project.bootstrap_commands] == ["echo one", "echo two"]


def test_execution_bootstrap_parses_ordered_commands_and_tool_requirements(tmp_path):
    root = _project_yaml(
        tmp_path,
        bootstrap={
            "timeout_seconds": 12,
            "required_tools": ["python"],
            "commands": [
                "python -m pip install -r requirements.txt",
                {"command": "python scripts/generate.py", "timeout_seconds": 3, "required_tools": ["git"]},
            ],
        },
    )
    project = load_project(root)
    assert project.setup_commands == ()
    assert [command.command for command in project.bootstrap_commands] == [
        "python -m pip install -r requirements.txt",
        "python scripts/generate.py",
    ]
    assert [command.timeout_seconds for command in project.bootstrap_commands] == [12.0, 3.0]
    assert project.bootstrap_commands[1].required_tools == ("python", "git")


def test_project_without_execution_setup_has_no_setup_commands(tmp_path):
    root = _project_yaml(tmp_path)
    project = load_project(root)
    assert project.setup_commands == ()


def test_execution_setup_must_be_a_list_of_command_strings(tmp_path):
    root = _project_yaml(tmp_path, setup={"not": "a list"})
    with pytest.raises(ProjectError):
        load_project(root)


def test_execution_setup_rejects_blank_commands(tmp_path):
    root = _project_yaml(tmp_path, setup=["echo ok", "   "])
    with pytest.raises(ProjectError):
        load_project(root)


def test_execution_bootstrap_rejects_invalid_command_shapes(tmp_path):
    root = _project_yaml(tmp_path, bootstrap={"commands": [{"timeout_seconds": 1}]})
    with pytest.raises(ProjectError):
        load_project(root)


def test_doctor_reports_missing_bootstrap_tools(tmp_path):
    root = _project_yaml(
        tmp_path,
        bootstrap={"commands": [{"command": "definitely-missing-stagemesh-tool --version"}]},
    )
    subprocess.run(["git", "init"], cwd=root, capture_output=True, text=True, check=False)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=root, capture_output=True, text=True, check=False)
    project = load_project(root)
    checks = check_project(project)
    missing = [check for check in checks if check.name == "bootstrap tool definitely-missing-stagemesh-tool"]
    assert missing
    assert missing[0].status == FAIL


# ---------------------------------------------------------------- runner


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="setup test",
        acceptance_criteria=["passes"],
        review_policy="NONE",
    )


def _runner(setup_commands):
    config = RunnerConfig(setup_commands=tuple(setup_commands))
    return BuildRunner(SessionLocal, config)


def _bootstrap_runner(commands):
    config = RunnerConfig(bootstrap_commands=tuple(commands))
    return BuildRunner(SessionLocal, config)


def _worker(tmp_path):
    return WorkerConfig("builder-1", "BUILDER", worktree_path=str(tmp_path))


def test_projects_without_setup_commands_are_unaffected(tmp_path):
    runner = _runner([])
    worker = _worker(tmp_path)
    with SessionLocal() as session:
        upsert_task(session, _task("SM-100"))
        task = session.get(BuildTask, "SM-100")
        assert runner._run_task_setup(session, task, worker) is True
        session.commit()
        assert session.scalars(select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")).all() == []


def test_successful_setup_runs_once_and_records_evidence(tmp_path):
    marker = tmp_path / "ran.txt"
    command = f"{sys.executable} -c \"open(r'{marker}', 'a').write('x')\""
    runner = _runner([command])
    worker = _worker(tmp_path)
    with SessionLocal() as session:
        upsert_task(session, _task("SM-101"))
        task = session.get(BuildTask, "SM-101")
        assert runner._run_task_setup(session, task, worker) is True
        session.commit()

    assert marker.read_text() == "x"
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")
        ).all()
        assert len(events) == 1
        assert events[0].event_data["passed"] is True
        assert events[0].event_data["phase"] == "bootstrap"

        # Re-running against the same prepared workspace must not re-execute.
        task = session.get(BuildTask, "SM-101")
        assert runner._run_task_setup(session, task, worker) is True
        session.commit()

    assert marker.read_text() == "x"  # unchanged: the command did not run again
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")
        ).all()
        assert len(events) == 1


def test_failing_setup_command_blocks_task_with_typed_reason(tmp_path):
    runner = _runner([f"{sys.executable} -c \"import sys; sys.exit(1)\""])
    worker = _worker(tmp_path)
    with SessionLocal() as session:
        upsert_task(session, _task("SM-102"))
        task = session.get(BuildTask, "SM-102")
        assert runner._run_task_setup(session, task, worker) is False
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "SM-102")
        assert task.state == "BLOCKED"
        assert task.waiting_input["failure_evidence"]["underlying_invariant"] == "SETUP_FAILED"
        assert task.waiting_input["failure_evidence"]["failure_type"] == "EXIT_CODE"
        assert task.waiting_input["failure_evidence"]["phase"] == "bootstrap"
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")
        ).all()
        assert len(events) == 1
        assert events[0].event_data["passed"] is False


def test_bootstrap_timeout_blocks_task_with_timeout_classification(tmp_path):
    runner = _bootstrap_runner(
        [{"command": f"{sys.executable} -c \"import time; time.sleep(2)\"", "timeout_seconds": 0.1}]
    )
    worker = _worker(tmp_path)
    with SessionLocal() as session:
        upsert_task(session, _task("SM-104"))
        task = session.get(BuildTask, "SM-104")
        assert runner._run_task_setup(session, task, worker) is False
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "SM-104")
        assert task.waiting_input["failure_evidence"]["failure_type"] == "TIMEOUT"
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")
        ).all()
        assert events[0].event_data["results"][0]["failure_type"] == "TIMEOUT"


def test_failing_setup_blocks_dispatch_before_agent_launch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, capture_output=True, text=True, check=False)
    worker = WorkerConfig("builder-1", "BUILDER", worktree_path=str(workspace))
    executor = FakeExecutor()
    runner = BuildRunner(
        SessionLocal,
        RunnerConfig(
            workers=(worker,),
            setup_commands=(f"{sys.executable} -c \"import sys; sys.exit(3)\"",),
        ),
        executors={"builder-1": executor},
    )

    with SessionLocal() as session:
        upsert_task(session, _task("SM-103"))
        result = runner._run_once(session)
        session.commit()

    assert result.launched == []
    assert executor.launches == []
    assert result.escalations == ["SM-103:SETUP_FAILED"]
    with SessionLocal() as session:
        task = session.get(BuildTask, "SM-103")
        assert task.state == "BLOCKED"
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.setup")
        ).all()
        assert len(events) == 1
        assert events[0].event_data["results"][0]["exit_code"] == 3
