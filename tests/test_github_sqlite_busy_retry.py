"""GH-101: GitHub reconciliation persistence paths must retry transient
SQLite lock contention with bounded backoff instead of surfacing an
untyped/unretried failure (controller.py, gates.py, ingestion.py, sync.py)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.github.gates import (
    poll_and_ingest_gate_approvals,
    publish_open_gates,
)
from build_coordinator.github.client import GitHubComment
from build_coordinator.github.ingestion import ingest_github_issue
from build_coordinator.github.client import GitHubIssue
from build_coordinator.github.sync import sync_objective_status_to_github
from build_coordinator.models import BuildObjectiveEvent, BuildObjectiveGate
from build_coordinator.objectives import create_objective
from build_coordinator.types import ObjectiveSpec
from sqlalchemy import select


def _open_gate(session, *, objective_id: str, gate_type: str, reason: str) -> BuildObjectiveGate:
    gate = BuildObjectiveGate(
        objective_id=objective_id,
        source_task_id=None,
        gate_type=gate_type,
        reason=reason,
    )
    session.add(gate)
    session.flush()
    return gate


class _FakeDBAPIError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def _lock_error() -> OperationalError:
    return OperationalError("database is locked", None, _FakeDBAPIError("database is locked"))


class _FlakyCommitSession:
    """Wraps a real Session, injecting N transient lock failures on commit()
    before delegating to the real commit -- exercising the retry path
    without needing real concurrent SQLite writers."""

    def __init__(self, session, fail_times: int):
        self._session = session
        self._fail_times = fail_times

    def commit(self):
        if self._fail_times > 0:
            self._fail_times -= 1
            raise _lock_error()
        self._session.commit()

    def __getattr__(self, name):
        return getattr(self._session, name)


class StubGitHubClient:
    def __init__(self):
        self.comments: list[dict] = []
        self.labels: list[dict] = []
        self.issue_comments: list[GitHubComment] = []

    def add_issue_comment(self, repo, issue_number, body):
        self.comments.append({"repo": repo, "issue_number": issue_number, "body": body})

    def set_issue_status_label(self, repo, issue_number, status):
        self.labels.append({"repo": repo, "issue_number": issue_number, "status": status})

    def get_issue_comments(self, repo, issue_number):
        return self.issue_comments


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def test_publish_open_gates_retries_transient_lock_then_succeeds():
    client = StubGitHubClient()
    with SessionLocal() as session:
        create_objective(session, ObjectiveSpec(objective_id="GH-200", goal="do the thing"))
        session.commit()

    with SessionLocal() as session:
        _open_gate(session, objective_id="GH-200", gate_type="SCOPE_EXPANSION", reason="needs approval")
        session.commit()

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=2)
        published = publish_open_gates(flaky, client, "example/repo", 200, "GH-200")

    assert len(published) == 1
    assert len(client.comments) == 1

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.objective_id == "GH-200",
                BuildObjectiveEvent.event_type == "github.gate_published",
            )
        ).all()
        # Exactly one durable event -- the two failed attempts must not have
        # left partial/duplicate rows behind.
        assert len(events) == 1


def test_poll_and_ingest_gate_approvals_retries_transient_lock_then_succeeds():
    client = StubGitHubClient()
    with SessionLocal() as session:
        create_objective(session, ObjectiveSpec(objective_id="GH-201", goal="do the thing"))
        session.commit()

    with SessionLocal() as session:
        gate = _open_gate(session, objective_id="GH-201", gate_type="SCOPE_EXPANSION", reason="needs approval")
        session.commit()
        gate_id = gate.gate_id

    client.issue_comments = [
        GitHubComment(id=1, author="saketvishal", body=f"/approve {gate_id}", created_at="2026-01-01T00:00:00Z")
    ]

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=2)
        resolved = poll_and_ingest_gate_approvals(flaky, client, "example/repo", 201, "GH-201")
        assert len(resolved) == 1
        assert resolved[0].status == "RESOLVED"

    assert len(client.comments) == 1


def test_ingest_github_issue_new_objective_retries_transient_lock():
    client = StubGitHubClient()
    issue = GitHubIssue(
        number=202,
        title="Build a widget",
        body="### Acceptance Criteria\n- it works",
        labels=("build:objective",),
        author="saketvishal",
        state="open",
        html_url="https://github.com/example/repo/issues/202",
    )

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=2)
        objective, is_new = ingest_github_issue(flaky, client, "saketvishal/stagemesh", issue)
        objective_id = objective.objective_id

    assert is_new is True

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.objective_id == objective_id,
                BuildObjectiveEvent.event_type == "github.issue_ingested",
            )
        ).all()
        assert len(events) == 1


def test_ingest_github_issue_existing_objective_retries_transient_lock():
    client = StubGitHubClient()
    issue = GitHubIssue(
        number=203,
        title="Build another widget",
        body="### Acceptance Criteria\n- it works",
        labels=("build:objective",),
        author="saketvishal",
        state="open",
        html_url="https://github.com/example/repo/issues/203",
    )

    with SessionLocal() as session:
        objective, is_new = ingest_github_issue(session, client, "saketvishal/stagemesh", issue)
        objective_id = objective.objective_id
        session.commit()
    assert is_new is True

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=2)
        existing, is_new_again = ingest_github_issue(flaky, client, "saketvishal/stagemesh", issue)
        assert is_new_again is False
        assert existing.objective_id == objective_id


def test_sync_objective_status_to_github_retries_transient_lock():
    client = StubGitHubClient()
    with SessionLocal() as session:
        objective = create_objective(session, ObjectiveSpec(objective_id="GH-204", goal="do the thing"))
        session.commit()
        obj_id = objective.objective_id

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=2)
        from build_coordinator.models import BuildObjective

        objective = real_session.get(BuildObjective, obj_id)
        status = sync_objective_status_to_github(flaky, client, "example/repo", 204, objective)

    assert status is not None
    assert len(client.comments) == 1

    with SessionLocal() as session:
        events = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.objective_id == obj_id,
                BuildObjectiveEvent.event_type == "github.status_synced",
            )
        ).all()
        assert len(events) == 1


def test_publish_open_gates_exhausted_retry_raises_typed_busy_error():
    from build_coordinator.db import DatabaseBusyError

    client = StubGitHubClient()
    with SessionLocal() as session:
        create_objective(session, ObjectiveSpec(objective_id="GH-205", goal="do the thing"))
        session.commit()

    with SessionLocal() as session:
        _open_gate(session, objective_id="GH-205", gate_type="SCOPE_EXPANSION", reason="needs approval")
        session.commit()

    with SessionLocal() as real_session:
        flaky = _FlakyCommitSession(real_session, fail_times=10)
        with pytest.raises(DatabaseBusyError):
            publish_open_gates(flaky, client, "example/repo", 205, "GH-205")
