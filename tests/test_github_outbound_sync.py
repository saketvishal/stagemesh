"""Comprehensive deterministic test suite for GitHub outbound lifecycle synchronization.

Verifies:
1. normal `stagemesh continue` passes GitHub task_source into BuildRunner.
2. GitHub imported task reaches DONE -> outbound sync occurs.
3. correct issue number/source identity is used.
4. completion evidence is posted.
5. done label applied.
6. issue closed.
7. subprocess/API failure is detected and recorded.
8. StageMesh task remains DONE if GitHub synchronization temporarily fails.
9. retry later succeeds.
10. repeated reconciliation is idempotent.
11. restart with local DONE + GitHub open backfills correctly.
12. non-GitHub task does not mutate GitHub.
13. objective task DONE does NOT prematurely close objective issue.
14. completed objective DOES close/synchronize its objective issue.
15. existing GitHub task-source discovery continues to work.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import record_event
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskEvent,
)
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.orchestrator import BuildRunner
from build_coordinator.task_source.github import GitHubTaskSource
from build_coordinator.types import EventInput


class MockGitHubClient:
    def __init__(self, issues: list[dict] | None = None, fail_on: str | None = None):
        self.issues = list(issues or [])
        self.comments: list[dict] = []
        self.closed: list[str] = []
        self.labels: list[dict] = []
        self.fail_on = fail_on

    def list_issues(self, repo: str, labels: tuple[str, ...]):
        if self.fail_on == "list":
            raise RuntimeError("GitHub API connection timeout")
        return self.issues

    def add_comment(self, repo: str, number: str, body: str):
        if self.fail_on == "comment":
            raise RuntimeError("API rate limit exceeded")
        self.comments.append({"repo": repo, "number": str(number), "body": body})

    def add_label(self, repo: str, number: str, label: str):
        if self.fail_on == "label":
            raise RuntimeError("Failed to modify labels")
        self.labels.append({"repo": repo, "number": str(number), "label": label})

    def close_issue(self, repo: str, number: str):
        if self.fail_on == "close":
            raise RuntimeError("Failed to close issue")
        self.closed.append(str(number))


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def test_1_continue_passes_task_source_to_runner(monkeypatch, tmp_path, registry):
    """1. Wire the active GitHub task source into BuildRunner in normal stagemesh continue."""
    import build_coordinator.project.commands as commands

    captured_kwargs = {}

    class DummyRunner:
        def __init__(self, session_factory, config, **kwargs):
            captured_kwargs.update(kwargs)
            self.kwargs = kwargs

        def reload_config(self, *args, **kwargs):
            pass

        def run_once(self):
            return SimpleNamespace(
                mode="RUNNING",
                launched=[],
                observed=[],
                recovered=[],
                escalations=[],
                objectives_reconciled=[],
                objective_follow_ups_created=[],
                objective_unrelated_tasks_created=[],
                objective_gates_raised=[],
                objectives_completed=[],
                outbound_synced=[],
            )

    proj_dir = tmp_path / "test_proj"
    subprocess.run(["git", "init", "-b", "main", str(proj_dir)], check=True, capture_output=True)
    stagemesh_dir = proj_dir / ".stagemesh"
    stagemesh_dir.mkdir(parents=True)
    (stagemesh_dir / "project.yaml").write_text(
        """schema_version: 1
id: test-proj
name: Test Project
execution:
  concurrency: 1
  default_review_policy: NONE
workers:
  builder:
    adapter: subprocess
    command: ["echo"]
""",
        encoding="utf-8",
    )

    mock_client = MockGitHubClient([])
    dummy_source = GitHubTaskSource(repo="test-owner/test-repo", client=mock_client)

    monkeypatch.setattr(commands, "BuildRunner", DummyRunner)
    monkeypatch.setattr(
        commands,
        "_optional_task_source",
        lambda *args, **kwargs: (dummy_source, []),
    )
    monkeypatch.setattr(
        commands,
        "build_runner_config",
        lambda *args, **kwargs: RunnerConfig(
            poll_seconds=1.0,
            workers=(WorkerConfig(worker_id="b1", role="BUILDER", adapter="subprocess"),),
        ),
    )

    args = SimpleNamespace(
        target=[],
        project_dir=str(proj_dir),
        no_sync=True,
        github=True,
        dry_run=False,
        task_id=None,
        max_cycles=1,
        timeout=None,
        once=True,
        all_projects=False,
    )

    old_env = dict(os.environ)
    try:
        commands.handle_continue(args)
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    assert "task_source" in captured_kwargs
    assert captured_kwargs["task_source"] is dummy_source
    assert isinstance(captured_kwargs["task_source"], GitHubTaskSource)


def test_2_to_6_github_task_done_outbound_sync_e2e():
    """2. GitHub imported task reaches DONE -> outbound sync occurs.
    3. correct issue number/source identity is used.
    4. completion evidence is posted.
    5. done label applied.
    6. issue closed.
    """
    client = MockGitHubClient(
        [
            {
                "number": 101,
                "title": "Build distributed storage adapter",
                "body": "Add streaming support.\n\n### Acceptance Criteria\n- S3 multi-part streaming",
                "labels": [{"name": "review:independent"}],
                "url": "https://github.com/example/repo/issues/101",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        # Import task
        results = source.discover_tasks(session)
        session.commit()
    assert len(results) == 1
    task_id = results[0].task_id
    assert task_id == "GH-101"

    # Simulate execution reaching authoritative DONE with evidence
    with SessionLocal() as session:
        task = session.get(BuildTask, task_id)
        task.state = "DONE"
        # Add checkpoint evidence
        session.add(
            BuildTaskCheckpoint(
                task_id=task_id,
                worker_id="builder-alpha",
                claim_id="claim-001",
                current_step="Integrated feature branch",
                current_head_sha="fa38290bcde",
                completed_work=["Implemented streaming reader", "Validated chunk boundaries"],
            )
        )
        # Add execution review evidence
        session.add(
            BuildRunnerExecution(
                task_id=task_id,
                role="REVIEWER",
                worker_id="reviewer-1",
                provider="scripted",
                adapter="subprocess",
                status="SUCCEEDED",
                result_data={"verdict": "APPROVED", "summary": "Clean chunking implementation"},
            )
        )
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle_result = runner.run_once()

    assert task_id in cycle_result.outbound_synced

    # Verify GitHub mutations:
    # 3. Correct issue number used
    assert len(client.comments) == 1
    assert client.comments[0]["number"] == "101"
    assert client.comments[0]["repo"] == "example/repo"

    # 4. Completion evidence posted
    body = client.comments[0]["body"]
    assert "### StageMesh Lifecycle Update: `DONE`" in body
    assert "builder-alpha" in body
    assert "fa38290bcde" in body
    assert "APPROVED" in body
    assert "Implemented streaming reader" in body

    # 5. Done label applied
    assert len(client.labels) == 1
    assert client.labels[0]["number"] == "101"
    assert client.labels[0]["label"] == "stagemesh:done"

    # 6. Issue closed
    assert client.closed == ["101"]


def test_7_8_failure_detected_and_task_remains_done():
    """7. Subprocess/API failure is detected and recorded.
    8. StageMesh task remains DONE if GitHub synchronization temporarily fails.
    """
    client = MockGitHubClient(
        [
            {
                "number": 105,
                "title": "Payment gateway integration",
                "body": "Add stripe webhooks.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/105",
            }
        ],
        fail_on="comment",  # Force API failure during comment
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-105")
        task.state = "DONE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle_result = runner.run_once()

    # Outbound sync should have failed
    assert "GH-105" not in cycle_result.outbound_synced
    assert client.closed == []

    # 7. Check failure recorded durably in BuildTaskEvent
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-105",
                BuildTaskEvent.event_type == "task.outbound_sync_failed",
            )
        ).all()
        assert len(events) == 1
        assert "API rate limit exceeded" in events[0].event_data.get("error", "")
        assert events[0].event_data.get("issue_number") == 105

        # 8. Crucial safety: Task state MUST remain DONE
        task = session.get(BuildTask, "GH-105")
        assert task.state == "DONE"


def test_9_retry_after_failure_succeeds():
    """9. Retry later succeeds when GitHub becomes available."""
    client = MockGitHubClient(
        [
            {
                "number": 105,
                "title": "Payment gateway integration",
                "body": "Add stripe webhooks.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/105",
            }
        ],
        fail_on="comment",
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-105")
        task.state = "DONE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    # First cycle: fails
    runner.run_once()
    assert client.closed == []

    # Clear error condition
    client.fail_on = None

    # Second cycle: retry
    cycle_result2 = runner.run_once()
    assert "GH-105" in cycle_result2.outbound_synced
    assert "105" in client.closed
    assert len(client.comments) == 1

    # Verify success event was recorded
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-105",
                BuildTaskEvent.event_type == "task.outbound_synced",
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_data.get("state") == "DONE"


def test_10_repeated_reconciliation_is_idempotent():
    """10. Repeated reconciliation is idempotent: no duplicate comments or closes."""
    client = MockGitHubClient(
        [
            {
                "number": 106,
                "title": "Fix memory leak in buffer pool",
                "body": "Fix pool leak.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/106",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-106")
        task.state = "DONE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)

    # First cycle: syncs
    runner.run_once()
    assert len(client.comments) == 1
    assert len(client.closed) == 1
    assert len(client.labels) == 1

    # Second cycle: should be a no-op
    runner.run_once()
    assert len(client.comments) == 1
    assert len(client.closed) == 1
    assert len(client.labels) == 1

    # Third cycle: still a no-op
    runner.run_once()
    assert len(client.comments) == 1
    assert len(client.closed) == 1
    assert len(client.labels) == 1


def test_11_restart_with_local_done_backfills_correctly():
    """11. Restart with local DONE + GitHub open backfills correctly (Caventra scenario)."""
    # Issue exists on GitHub and is still open
    client = MockGitHubClient(
        [
            {
                "number": 10,
                "title": "Legacy pre-existing task",
                "body": "Task completed before bugfix.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/10",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    # Local database already has GH-10 in state DONE without outbound_synced record
    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-10")
        task.state = "DONE"
        # Simulate that task completed in an earlier run without syncing
        session.commit()

    assert client.closed == []
    assert client.comments == []

    # New runner instance starts up (simulating stagemesh continue restart)
    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-10" in cycle.outbound_synced
    assert "10" in client.closed
    assert len(client.comments) == 1
    assert "GH-10" in client.comments[0]["body"]
    assert client.labels[0]["label"] == "stagemesh:done"


def test_12_non_github_tasks_do_not_mutate_github():
    """12. Non-GitHub task does not mutate GitHub merely because ID ends in digits."""
    client = MockGitHubClient([])
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        # Create non-GitHub tasks
        t1 = BuildTask(
            task_id="SM-001",
            title="Local maintenance script",
            description="Internal chore",
            acceptance_criteria=["Done"],
            state="DONE",
        )
        t2 = BuildTask(
            task_id="CAV-10",
            title="Caventra internal task",
            description="Internal Caventra chore",
            acceptance_criteria=["Done"],
            state="DONE",
        )
        t3 = BuildTask(
            task_id="O-1",
            title="Local objective task",
            description="Internal",
            acceptance_criteria=["Done"],
            state="DONE",
        )
        session.add_all([t1, t2, t3])
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    runner.run_once()

    # None of these should have touched GitHub!
    assert client.comments == []
    assert client.closed == []
    assert client.labels == []


def test_13_objective_task_done_does_not_prematurely_close_objective_issue():
    """13. Objective task DONE does NOT prematurely close objective issue."""
    client = MockGitHubClient(
        [
            {
                "number": 42,
                "title": "Migrate authentication to WebAuthn",
                "body": (
                    "## Objective\n"
                    "Migrate all user authentication to WebAuthn passkeys.\n\n"
                    "### Acceptance Criteria\n"
                    "- Passkey registration endpoint\n"
                    "- Passkey authentication endpoint\n"
                ),
                "labels": [{"name": "objective"}],
                "url": "https://github.com/example/repo/issues/42",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    # Both BuildObjective and BuildTask exist for GH-42
    with SessionLocal() as session:
        obj = session.get(BuildObjective, "GH-42")
        task = session.get(BuildTask, "GH-42")
        assert obj is not None
        assert obj.state == "PLANNING"
        assert task is not None

        # Simulate planner / root task reaching DONE, but objective is still ACTIVE
        task.state = "DONE"
        obj.state = "ACTIVE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    runner.run_once()

    # CRITICAL OBJECTIVE SAFETY: Issue #42 MUST REMAIN OPEN!
    assert "42" not in client.closed
    assert not any(l.get("label") == "stagemesh:done" for l in client.labels)
    assert not any(c.get("number") == "42" for c in client.comments)


def test_14_completed_objective_does_close_objective_issue():
    """14. Completed objective DOES close and synchronize its objective issue."""
    client = MockGitHubClient(
        [
            {
                "number": 42,
                "title": "Migrate authentication to WebAuthn",
                "body": "## Objective\nMigrate all user authentication to WebAuthn passkeys.",
                "labels": [{"name": "objective"}],
                "url": "https://github.com/example/repo/issues/42",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        obj = session.get(BuildObjective, "GH-42")
        obj.state = "COMPLETED"
        obj.completion_criteria = ["Passkey registration endpoint", "Passkey authentication endpoint"]
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-42" in cycle.outbound_synced
    assert "42" in client.closed
    assert len(client.comments) == 1
    body = client.comments[0]["body"]
    assert "### StageMesh Objective Completed: `COMPLETED`" in body
    assert "Passkey registration endpoint" in body
    assert any(l.get("label") == "stagemesh:done" for l in client.labels)

    # Verify objective event recorded
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.objective_id == "GH-42",
                BuildObjectiveEvent.event_type == "objective.outbound_synced",
            )
        ).all()
        assert len(events) == 1


def test_15_subprocess_gh_cli_error_handling_and_returncode_inspection(monkeypatch):
    """Verifies that subprocess gh CLI checks return codes and records failures."""
    source = GitHubTaskSource(repo="example/repo")  # No client -> uses subprocess.run

    with SessionLocal() as session:
        task = BuildTask(
            task_id="GH-77",
            title="Real subprocess test",
            description="Testing gh CLI invocation",
            acceptance_criteria=["Done"],
            state="DONE",
        )
        session.add(task)
        session.commit()

    # Mock subprocess.run where 'gh issue comment' returns non-zero exit code
    mock_run = MagicMock()
    mock_run.return_value = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="GraphQL: Could not resolve to an issue (404)",
    )
    monkeypatch.setattr(subprocess, "run", mock_run)

    with SessionLocal() as session:
        ok = source.sync_outbound(session, "GH-77", "DONE")
        assert ok is False
        session.commit()

    # Verify command was called with exact args
    assert mock_run.called
    call_args = mock_run.call_args[0][0]
    assert call_args[:4] == ["gh", "issue", "comment", "77"]

    # Verify durable failure was recorded
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-77",
                BuildTaskEvent.event_type == "task.outbound_sync_failed",
            )
        ).all()
        assert len(events) == 1
        assert "404" in events[0].event_data.get("error", "")
        # Task remains in DONE
        task = session.get(BuildTask, "GH-77")
        assert task.state == "DONE"


def test_16_subprocess_gh_cli_success_path(monkeypatch):
    """Verifies successful subprocess gh CLI sequence: comment, label, close."""
    source = GitHubTaskSource(repo="example/repo")

    with SessionLocal() as session:
        task = BuildTask(
            task_id="GH-88",
            title="Real subprocess success test",
            description="Testing successful gh CLI",
            acceptance_criteria=["Done"],
            state="DONE",
        )
        session.add(task)
        session.commit()

    called_cmds = []

    def fake_subprocess_run(cmd, **kwargs):
        called_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="success", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with SessionLocal() as session:
        ok = source.sync_outbound(session, "GH-88", "DONE", evidence={"summary": "all done"})
        assert ok is True
        session.commit()

    # Must execute: comment, edit --add-label, close
    assert len(called_cmds) == 3
    assert called_cmds[0][:4] == ["gh", "issue", "comment", "88"]
    assert "all done" in called_cmds[0][7]
    assert called_cmds[1] == ["gh", "issue", "edit", "88", "--repo", "example/repo", "--add-label", "stagemesh:done"]
    assert called_cmds[2] == ["gh", "issue", "close", "88", "--repo", "example/repo"]

    # Check success event
    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-88",
                BuildTaskEvent.event_type == "task.outbound_synced",
            )
        ).all()
        assert len(events) == 1
