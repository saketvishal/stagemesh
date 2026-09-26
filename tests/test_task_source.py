"""Tests for StageMesh task source discovery and synchronization."""

from __future__ import annotations

import pytest
from pathlib import Path

from build_coordinator.claims import task_is_claimable
from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildObjective, BuildTask
from build_coordinator.planner import planner_task_id
from build_coordinator.service import utcnow
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
        assert session.get(BuildTask, "GH-201") is None
        planner = session.get(BuildTask, planner_task_id("GH-201"))
        assert planner is not None
        assert planner.objective_id == "GH-201"
        assert "Migrate database engine to distributed PostgreSQL" in planner.description
        assert "Do not implement the work" in planner.description


def test_github_objective_reimport_reconciles_legacy_root_and_creates_planner():
    client = FakeGitHubClient(
        [
            {
                "number": 71,
                "title": "Architecture and validation program",
                "body": "## Objective\nDecompose broad architecture work.\n\nDepends on: #75",
                "labels": [{"name": "objective"}],
                "url": "https://github.com/example/repo/issues/71",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        session.add(
            BuildObjective(
                objective_id="GH-71",
                goal="legacy objective",
                constraints=[],
                allowed_scope=[],
                prohibited_scope=[],
                completion_criteria=[],
                dependencies=[],
                human_gate_policy={},
                parallelism=1,
                main_push_policy="NEVER",
                state="PLANNING",
                max_auto_created_tasks=20,
                max_child_depth=1,
            )
        )
        session.add(
            BuildTask(
                task_id="GH-71",
                title="Legacy synthetic objective task",
                description="Old imports created this as an executable root task.",
                acceptance_criteria=[],
                dependencies=["GH-75"],
                state="READY",
            )
        )
        session.commit()

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert len(results) == 1
    assert results[0].task_id == "GH-71"

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "GH-71")
        root = session.get(BuildTask, "GH-71")
        planner = session.get(BuildTask, planner_task_id("GH-71"))

        assert objective is not None
        assert objective.dependencies == ["GH-75"]

        assert root is not None
        assert root.reason_created == "OBJECTIVE_ROOT_COMPAT"
        assert root.objective_id == "GH-71"
        assert root.dependencies == ["GH-75"]
        assert root.state == "STALE"
        assert not task_is_claimable(session, root, utcnow())

        assert planner is not None
        assert planner.objective_id == "GH-71"
        assert planner.reason_created == "OBJECTIVE_PLANNER"
        assert planner.dependencies == ["GH-75"]
        assert "Architecture and validation program" in planner.description


def test_github_objective_resync_refreshes_planner_task_description():
    client = FakeGitHubClient(
        [
            {
                "number": 111,
                "title": "Original objective",
                "body": "## Objective\nDraft the original plan.",
                "labels": [{"name": "objective"}],
                "url": "https://github.com/example/repo/issues/111",
            }
        ]
    )
    source = GitHubTaskSource(repo="example/repo", client=client)
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    client.issues = [
        {
            "number": 111,
            "title": "Updated objective",
            "body": "## Objective\nDraft the updated rollout plan.",
            "labels": [{"name": "objective"}],
            "url": "https://github.com/example/repo/issues/111",
        }
    ]
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        planner = session.get(BuildTask, planner_task_id("GH-111"))
        assert planner is not None
        assert "Updated objective" in planner.description
        assert "updated rollout plan" in planner.description
        assert "original plan" not in planner.description


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
