"""Regression coverage for stagemesh:* GitHub label auto-provisioning (SM-008).

Verifies:
1. A brand-new repo with zero StageMesh labels gets `stagemesh:done` created
   on first sync, with no operator setup required.
2. Label provisioning is idempotent: it is not re-attempted once confirmed.
3. Label provisioning only runs when the GitHub adapter is enabled (repo set).
4. Permission/network/API failures during provisioning are recorded as
   durable outbound-sync evidence and never raised -- local task state is
   unaffected.
5. DONE synchronization is not permanently stuck merely because
   stagemesh:done was initially absent: once provisioned, the label is
   applied and the issue is closed with no manual intervention.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.events import record_event
from build_coordinator.models import TASK_STATES, BuildTask, BuildTaskEvent
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.runner.orchestrator import BuildRunner
from build_coordinator.task_source.github import GitHubTaskSource
from build_coordinator.types import EventInput


EXPECTED_LIFECYCLE_LABELS = [f"stagemesh:{state.lower()}" for state in TASK_STATES]


class LabelAwareMockClient:
    """Mock GitHub client that models real label-existence semantics."""

    def __init__(self, issues: list[dict] | None = None, existing_labels: list[str] | None = None, fail_on: str | None = None):
        self.issues = list(issues or [])
        self.known_labels: set[str] = set(existing_labels or [])
        self.created_labels: list[str] = []
        self.comments: list[dict] = []
        self.closed: list[str] = []
        self.labels: list[dict] = []
        self.fail_on = fail_on

    def list_issues(self, repo: str, labels: tuple[str, ...]):
        return self.issues

    def list_labels(self, repo: str):
        if self.fail_on == "list_labels":
            raise RuntimeError("permission denied: cannot list labels")
        return sorted(self.known_labels)

    def create_label(self, repo: str, name: str):
        if self.fail_on == "create_label":
            raise RuntimeError("permission denied: cannot create labels")
        self.created_labels.append(name)
        self.known_labels.add(name)

    def add_comment(self, repo: str, number: str, body: str):
        self.comments.append({"repo": repo, "number": str(number), "body": body})

    def add_label(self, repo: str, number: str, label: str):
        # Mirrors real GitHub: applying a label that doesn't exist fails.
        if label not in self.known_labels:
            raise RuntimeError(f"label '{label}' not found in repository")
        self.labels.append({"repo": repo, "number": str(number), "label": label})

    def close_issue(self, repo: str, number: str):
        self.closed.append(str(number))


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def test_new_repo_with_zero_labels_gets_stagemesh_done_provisioned():
    """1. Brand-new repo, no StageMesh labels -> provisioned automatically on first sync."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/new-repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert set(EXPECTED_LIFECYCLE_LABELS).issubset(client.known_labels)


def test_provisioning_is_idempotent_across_cycles():
    """2. Label creation only happens once even across repeated discover cycles."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/new-repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        source.discover_tasks(session)
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS


def test_provisioning_skips_labels_that_already_exist():
    """Idempotent w.r.t. pre-existing repo state: no duplicate creation attempted."""
    client = LabelAwareMockClient(issues=[], existing_labels=EXPECTED_LIFECYCLE_LABELS)
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == []


def test_provisioning_never_runs_when_adapter_disabled():
    """3. No repo configured -> adapter disabled -> no provisioning attempted."""
    client = LabelAwareMockClient(issues=[])
    source = GitHubTaskSource(repo=None, client=client)

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert results == []
    assert client.created_labels == []


def test_provisioning_failure_recorded_durably_and_does_not_raise():
    """4. Permission/API failure during provisioning is recorded, not raised."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="list_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        # Must not raise.
        source.discover_tasks(session)
        session.commit()

    assert client.created_labels == []

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.event_type == "task_source.label_provisioning_failed",
            )
        ).all()
        assert len(events) == 1
        assert "permission denied" in events[0].event_data.get("error", "")


def test_provisioning_retries_on_next_cycle_after_failure():
    """Failed provisioning is retried on a later sync, not abandoned forever."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="list_labels")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
    assert client.created_labels == []

    client.fail_on = None
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()
    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS


def test_done_sync_not_permanently_stuck_when_label_initially_absent():
    """5. Regression: DONE task, stagemesh:done absent -> provisioned, applied, issue closed.

    No manual intervention required: the same runner cycle that discovers the
    issue also provisions the missing label before applying it during
    outbound sync.
    """
    client = LabelAwareMockClient(
        issues=[
            {
                "number": 200,
                "title": "Fix flaky retry logic",
                "body": "Stabilize the retry backoff.",
                "labels": [],
                "url": "https://github.com/example/repo/issues/200",
            }
        ],
        existing_labels=[],  # stagemesh:done does not exist yet
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        source.discover_tasks(session)
        task = session.get(BuildTask, "GH-200")
        task.state = "DONE"
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "stagemesh:done" in client.created_labels
    assert "GH-200" in cycle.outbound_synced
    assert client.labels and client.labels[0]["label"] == "stagemesh:done"
    assert client.closed == ["200"]

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-200")
        assert task.state == "DONE"


def test_done_sync_provisions_label_without_fresh_discovery_pass():
    """A restart with local DONE work can provision labels during outbound sync."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-201",
                title="Already completed imported task",
                description="Completed before this process started.",
                acceptance_criteria=["Done"],
                state="DONE",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-201",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/201"},
            ),
        )
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert "GH-201" in cycle.outbound_synced
    assert client.labels == [{"repo": "example/repo", "number": "201", "label": "stagemesh:done"}]
    assert client.closed == ["201"]


def test_non_done_lifecycle_sync_provisions_label_before_applying():
    """Any stagemesh:* task lifecycle label the adapter emits is provisioned first."""
    client = LabelAwareMockClient(issues=[], existing_labels=[])
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-203",
                title="Review ready imported task",
                description="Ready for review.",
                acceptance_criteria=["Reviewed"],
                state="REVIEW_READY",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-203",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/203"},
            ),
        )
        ok = source.sync_outbound(session, "GH-203", "REVIEW_READY")
        session.commit()

    assert ok is True
    assert client.created_labels == EXPECTED_LIFECYCLE_LABELS
    assert client.labels == [{"repo": "example/repo", "number": "203", "label": "stagemesh:review_ready"}]
    assert client.closed == []


def test_outbound_records_task_specific_failure_when_label_provisioning_fails():
    """Provisioning failures leave DONE intact and create retryable outbound evidence."""
    client = LabelAwareMockClient(issues=[], existing_labels=[], fail_on="create_label")
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="GH-202",
                title="Completed task awaiting GitHub sync",
                description="Done locally.",
                acceptance_criteria=["Done"],
                state="DONE",
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-202",
                event_type="task.synced_from_source",
                actor="github-sync",
                event_data={"source": "https://github.com/example/repo/issues/202"},
            ),
        )
        session.commit()

    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-202" not in cycle.outbound_synced
    assert client.comments == []
    assert client.labels == []
    assert client.closed == []

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-202")
        assert task.state == "DONE"
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == "GH-202",
                BuildTaskEvent.event_type == "task.outbound_sync_failed",
            )
        ).all()
        assert len(events) == 1
        assert events[0].event_data.get("action") == "label_provisioning"
