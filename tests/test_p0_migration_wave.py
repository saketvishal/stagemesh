"""Deterministic tests for StageMesh priority:P0 migration wave bootstrap.

Proves:
- P0 outranks normal backlog;
- multiple P0 tasks can execute according to configured concurrency;
- lower-priority work does not jump ahead while executable P0 work exists;
- dependencies still override priority when necessary;
- #62-style audit/follow-up work can be gated behind a migration wave;
- restart/resume preserves correct scheduling;
- label priority and range syntax parsing.
"""

from __future__ import annotations

import os
import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import EventInput, record_event
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.project.backlog import SYNC_EVENT, task_priorities
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.service import (
    TaskSpec,
    list_available_tasks,
    task_is_claimable,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.task_source.github import GitHubTaskSource


class FakeGitHubClient:
    def __init__(self, issues: list[dict]):
        self.issues = list(issues)
        self.comments: list[dict] = []
        self.closed: list[str] = []

    def list_issues(self, repo: str, labels: tuple[str, ...]):
        return self.issues

    def add_comment(self, repo: str, number: str, body: str):
        self.comments.append({"repo": repo, "number": number, "body": body})

    def close_issue(self, repo: str, number: str):
        self.closed.append(number)


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))
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


def _make_runner(workers: list[WorkerConfig]):
    config = RunnerConfig(
        workers=tuple(workers),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    return BuildRunner(SessionLocal, config, git=FakeGit())


# ---------------------------------------------------------------------------
# 1. Parsing tests: labels, priorities, and dependency ranges
# ---------------------------------------------------------------------------

def test_parse_priority_labels():
    source = GitHubTaskSource(repo="test/repo")
    assert source._parse_priority(["priority:P0"], "") == 0
    assert source._parse_priority(["priority:p0"], "") == 0
    assert source._parse_priority(["P0"], "") == 0
    assert source._parse_priority(["priority:P1"], "") == 20
    assert source._parse_priority(["priority:P2"], "") == 50
    assert source._parse_priority(["priority:P3"], "") == 100
    assert source._parse_priority(["bug", "backend"], "") == 100
    # From body
    assert source._parse_priority([], "<!-- priority: 0 -->") == 0
    assert source._parse_priority([], "Priority: P0") == 0


def test_parse_dependencies_ranges_and_en_dashes():
    source = GitHubTaskSource(repo="test/repo")

    # Unicode en-dash range
    body_en_dash = "## Execution gate\n\n**Run after issues #55\u2013#61.** Issues #55\u2013#61 are P0 wave."
    deps = source._parse_dependencies(body_en_dash)
    assert deps == ["GH-55", "GH-56", "GH-57", "GH-58", "GH-59", "GH-60", "GH-61"]

    # Hyphen range
    body_hyphen = "**Run after issues #55-#61.**"
    assert source._parse_dependencies(body_hyphen) == [
        "GH-55", "GH-56", "GH-57", "GH-58", "GH-59", "GH-60", "GH-61"
    ]

    # Word range "to"
    body_to = "Run after issues #10 to #13"
    assert source._parse_dependencies(body_to) == ["GH-10", "GH-11", "GH-12", "GH-13"]

    # Individual list
    body_list = "Blocked by: GH-100, GH-101"
    assert source._parse_dependencies(body_list) == ["GH-100", "GH-101"]


# ---------------------------------------------------------------------------
# 2. Scheduling: P0 outranks normal backlog
# ---------------------------------------------------------------------------

def test_p0_outranks_normal_backlog():
    client = FakeGitHubClient([
        {
            "number": 55,
            "title": "Migrate persistent watcher",
            "body": "Port generic watcher",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        },
        {
            "number": 1,
            "title": "Roadmap item alpha acceptance",
            "body": "Reconcile alpha evidence",
            "labels": [],
            "url": "https://github.com/test/repo/issues/1",
        },
    ])
    source = GitHubTaskSource(repo="test/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        # Also seed a local project backlog task with default priority 100
        upsert_task(
            session,
            TaskSpec(
                task_id="SM-001",
                title="Normal backlog task",
                description="Normal backlog",
                acceptance_criteria=["done"],
            ),
        )
        record_event(
            session,
            EventInput(
                task_id="SM-001",
                event_type=SYNC_EVENT,
                actor="test",
                event_data={"priority": 100},
            ),
        )
        session.commit()

    # Verify priorities in DB
    with SessionLocal() as session:
        priorities = task_priorities(session, ["GH-55", "GH-1", "SM-001"])
        assert priorities["GH-55"] == 0
        assert priorities.get("GH-1", 100) == 100
        assert priorities["SM-001"] == 100

    # Single-worker runner (concurrency 1)
    runner = _make_runner([WorkerConfig("builder-1", "BUILDER", adapter="fake")])
    result = runner.run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        launched = session.get(BuildRunnerExecution, result.launched[0])
        # P0 task GH-55 must be the one launched, not GH-1 and not SM-001
        assert launched.task_id == "GH-55"


# ---------------------------------------------------------------------------
# 3. Concurrency: multiple P0 tasks execute in parallel
# ---------------------------------------------------------------------------

def test_multiple_p0_tasks_execute_in_parallel():
    client = FakeGitHubClient([
        {
            "number": 55,
            "title": "P0 Task 55",
            "body": "Task 55",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        },
        {
            "number": 56,
            "title": "P0 Task 56",
            "body": "Task 56",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/56",
        },
        {
            "number": 57,
            "title": "P0 Task 57",
            "body": "Task 57",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/57",
        },
    ])
    source = GitHubTaskSource(repo="test/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    # Runner with 2 workers (concurrency 2)
    runner = _make_runner([
        WorkerConfig("builder-a", "BUILDER", adapter="fake"),
        WorkerConfig("builder-b", "BUILDER", adapter="fake"),
    ])
    result = runner.run_once()

    # Exactly 2 P0 tasks should be launched in parallel
    assert len(result.launched) == 2
    with SessionLocal() as session:
        launched_tasks = {
            session.get(BuildRunnerExecution, eid).task_id
            for eid in result.launched
        }
        assert launched_tasks == {"GH-55", "GH-56"}
        # Both tasks should be in CLAIMED state
        assert session.get(BuildTask, "GH-55").state == "CLAIMED"
        assert session.get(BuildTask, "GH-56").state == "CLAIMED"
        # The 3rd task remains claimable/ready for the next slot
        assert session.get(BuildTask, "GH-57").state == "READY"


# ---------------------------------------------------------------------------
# 4. Non-preemption: lower-priority work does not jump ahead of executable P0
# ---------------------------------------------------------------------------

def test_lower_priority_does_not_jump_ahead_while_executable_p0_exists():
    client = FakeGitHubClient([
        {
            "number": 55,
            "title": "P0 Task 55",
            "body": "Task 55",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        },
        {
            "number": 56,
            "title": "P0 Task 56",
            "body": "Task 56",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/56",
        },
        {
            "number": 10,
            "title": "Normal task 10",
            "body": "Normal",
            "labels": [],
            "url": "https://github.com/test/repo/issues/10",
        },
    ])
    source = GitHubTaskSource(repo="test/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    # Only 1 builder worker available
    runner = _make_runner([WorkerConfig("builder-1", "BUILDER", adapter="fake")])

    result = runner.run_once()
    assert len(result.launched) == 1
    with SessionLocal() as session:
        # GH-55 launched into the only slot
        assert session.get(BuildRunnerExecution, result.launched[0]).task_id == "GH-55"
        # GH-56 is still READY (executable P0)
        assert session.get(BuildTask, "GH-56").state == "READY"
        # GH-10 (lower priority) must NOT have launched
        assert session.get(BuildTask, "GH-10").state == "READY"

    # In a second cycle while GH-55 is still running on builder-1,
    # capacity is full and GH-10 still cannot launch
    result2 = runner.run_once()
    assert len(result2.launched) == 0
    with SessionLocal() as session:
        assert session.get(BuildTask, "GH-10").state == "READY"


# ---------------------------------------------------------------------------
# 5. Dependencies override priority: #62 execution gate
# ---------------------------------------------------------------------------

def test_dependencies_override_priority_and_gate_audit_62():
    # Setup issues 55..61 as P0, and 62 as execution gate
    issues = [
        {
            "number": n,
            "title": f"Migration task {n}",
            "body": f"Description {n}",
            "labels": [{"name": "priority:P0"}],
            "url": f"https://github.com/test/repo/issues/{n}",
        }
        for n in range(55, 62)
    ]
    issues.append({
        "number": 62,
        "title": "Migration audit: reconcile every caventra-orchestrator capability",
        "body": "## Execution gate\n\n**Run after issues #55\u2013#61.** Issues #55\u2013#61 are P0 wave.",
        "labels": [],
        "url": "https://github.com/test/repo/issues/62",
    })

    client = FakeGitHubClient(issues)
    source = GitHubTaskSource(repo="test/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        now = utcnow()
        task_62 = session.get(BuildTask, "GH-62")
        assert task_62 is not None
        assert task_62.dependencies == ["GH-55", "GH-56", "GH-57", "GH-58", "GH-59", "GH-60", "GH-61"]

        # GH-62 is NOT claimable because its dependencies are not DONE
        assert task_is_claimable(session, task_62, now) is False
        available_ids = {t.task_id for t in list_available_tasks(session)}
        assert "GH-62" not in available_ids
        for n in range(55, 62):
            assert f"GH-{n}" in available_ids

        # Transition 55..60 to DONE
        for n in range(55, 61):
            transition_task(session, f"GH-{n}", "CLAIMED", actor="test")
            transition_task(session, f"GH-{n}", "IN_PROGRESS", actor="test")
            transition_task(session, f"GH-{n}", "VALIDATING", actor="test")
            transition_task(session, f"GH-{n}", "DONE", actor="test", reason="done")
        session.commit()

    # GH-61 is still READY (not DONE), so GH-62 must STILL not be claimable
    with SessionLocal() as session:
        now = utcnow()
        task_62 = session.get(BuildTask, "GH-62")
        assert task_is_claimable(session, task_62, now) is False
        assert "GH-62" not in {t.task_id for t in list_available_tasks(session)}

        # Now complete the final task GH-61
        transition_task(session, "GH-61", "CLAIMED", actor="test")
        transition_task(session, "GH-61", "IN_PROGRESS", actor="test")
        transition_task(session, "GH-61", "VALIDATING", actor="test")
        transition_task(session, "GH-61", "DONE", actor="test", reason="done")
        session.commit()

    # All dependencies 55..61 are DONE: GH-62 is now unblocked and claimable!
    with SessionLocal() as session:
        now = utcnow()
        task_62 = session.get(BuildTask, "GH-62")
        assert task_is_claimable(session, task_62, now) is True
        assert "GH-62" in {t.task_id for t in list_available_tasks(session)}


# ---------------------------------------------------------------------------
# 6. Durability: restart / resume preserves scheduling and priority
# ---------------------------------------------------------------------------

def test_restart_preserves_durable_scheduling_and_priority():
    client = FakeGitHubClient([
        {
            "number": 55,
            "title": "P0 watcher",
            "body": "Watcher",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        },
        {
            "number": 2,
            "title": "Normal task 2",
            "body": "Normal",
            "labels": [],
            "url": "https://github.com/test/repo/issues/2",
        },
    ])
    source = GitHubTaskSource(repo="test/repo", client=client)

    # Initial sync and commit
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    # Simulate fresh session / process restart: no re-sync from GitHub
    with SessionLocal() as session:
        priorities = task_priorities(session, ["GH-55", "GH-2"])
        assert priorities["GH-55"] == 0
        assert priorities.get("GH-2", 100) == 100

        available = list_available_tasks(session)
        available.sort(key=lambda t: (priorities.get(t.task_id, 100), t.task_id))
        assert available[0].task_id == "GH-55"
        assert available[1].task_id == "GH-2"

    # Fresh runner instance against the persisted DB
    runner = _make_runner([WorkerConfig("builder-1", "BUILDER", adapter="fake")])
    result = runner.run_once()
    assert len(result.launched) == 1
    with SessionLocal() as session:
        launched = session.get(BuildRunnerExecution, result.launched[0])
        assert launched.task_id == "GH-55"


# ---------------------------------------------------------------------------
# 7. Runtime state separation: GitHub sync does not overwrite active task state
# ---------------------------------------------------------------------------

def test_github_sync_preserves_runtime_state():
    client = FakeGitHubClient([
        {
            "number": 55,
            "title": "P0 watcher",
            "body": "Watcher initial",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        },
    ])
    source = GitHubTaskSource(repo="test/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        # Advance task to IN_PROGRESS
        transition_task(session, "GH-55", "CLAIMED", actor="test")
        transition_task(session, "GH-55", "IN_PROGRESS", actor="test")
        session.commit()

    # Issue updated on GitHub (new title/body)
    client.issues = [
        {
            "number": 55,
            "title": "P0 watcher updated",
            "body": "Watcher updated body",
            "labels": [{"name": "priority:P0"}],
            "url": "https://github.com/test/repo/issues/55",
        }
    ]

    # Re-sync
    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()
        assert len(results) == 1
        assert results[0].action == "UPDATED"

        task = session.get(BuildTask, "GH-55")
        # Runtime state MUST remain IN_PROGRESS, not reset to READY
        assert task.state == "IN_PROGRESS"
        # Spec was updated
        assert task.title == "P0 watcher updated"
