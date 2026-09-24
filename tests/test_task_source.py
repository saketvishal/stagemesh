"""Tests for StageMesh task source discovery and synchronization."""

from __future__ import annotations

import pytest
from pathlib import Path

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildObjective, BuildTask
from build_coordinator.task_source.base import TaskSourceConfig
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
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def test_github_task_source_syncs_standard_task():
    client = FakeGitHubClient(
        [
            {
                "number": 101,
                "title": "Implement S3 adapter streaming",
                "body": (
                    "Stream large blobs directly to S3 storage.\n\n"
                    "### Acceptance Criteria\n"
                    "- Verify chunked transfer\n"
                    "- Add unit test\n\n"
                    "Blocked by: GH-100"
                ),
                "labels": [{"name": "review:independent"}, {"name": "risk:high"}],
                "url": "https://github.com/example/repo/issues/101",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)
    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert len(results) == 1
    assert results[0].task_id == "GH-101"
    assert results[0].action == "CREATED"

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-101")
        assert task is not None
        assert task.title == "Implement S3 adapter streaming"
        assert task.dependencies == ["GH-100"]
        assert task.review_policy == "INDEPENDENT"
        assert task.risk_level == "HIGH"
        assert "Verify chunked transfer" in task.acceptance_criteria


def test_github_task_source_syncs_objective():
    client = FakeGitHubClient(
        [
            {
                "number": 201,
                "title": "High-level Migration Objective",
                "body": "## Objective\nMigrate database engine to distributed PostgreSQL.",
                "labels": [{"name": "objective"}],
                "url": "https://github.com/example/repo/issues/201",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)
    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert len(results) == 1
    assert results[0].task_id == "GH-201"

    with SessionLocal() as session:
        obj = session.get(BuildObjective, "GH-201")
        assert obj is not None
        assert "High-level Migration Objective" in obj.goal


def test_github_task_source_sync_outbound():
    client = FakeGitHubClient([])
    source = GitHubTaskSource(repo="example/repo", client=client)
    with SessionLocal() as session:
        ok = source.sync_outbound(
            session,
            "GH-101",
            "DONE",
            evidence={
                "worker_id": "builder-1",
                "claim_id": "claim-xyz",
                "review_verdict": "GREEN",
            },
        )
    assert ok is True
    assert len(client.comments) == 1
    assert client.comments[0]["number"] == "101"
    assert "GREEN" in client.comments[0]["body"]
    assert "101" in client.closed
