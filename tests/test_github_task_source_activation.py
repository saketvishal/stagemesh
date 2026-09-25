"""Deterministic tests for GitHub task source activation, diagnostics, objective ingestion, and dependency gating."""

from __future__ import annotations

import pytest
from pathlib import Path

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildObjective, BuildTask
from build_coordinator.project.commands import _optional_task_source
from build_coordinator.project.definition import ProjectDefinition
from build_coordinator.claims import task_is_claimable, utcnow
from build_coordinator.service import upsert_task, transition_task, list_available_tasks
from build_coordinator.task_source import get_task_source
from build_coordinator.task_source.github import GitHubTaskSource
from build_coordinator.types import TaskSpec


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
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield
    engine.dispose()


def _make_project(task_sources: dict) -> ProjectDefinition:
    return ProjectDefinition(
        root=Path("."),
        project_id="test-proj",
        name="Test Project",
        aliases=[],
        concurrency=2,
        reviewers=1,
        default_review_policy="INDEPENDENT",
        main_ref="main",
        remote_name="origin",
        state_dir=Path(".build-coordinator"),
        task_sources=task_sources,
    )


def test_configured_github_source_creates_adapter():
    source = get_task_source({"type": "github", "repo": "owner/repo"})
    assert isinstance(source, GitHubTaskSource)
    assert source.repo == "owner/repo"

    proj = _make_project({"github": {"enabled": True, "repo": "owner/repo"}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is not None
    assert isinstance(adapter, GitHubTaskSource)
    assert len(diags) == 0


def test_missing_required_configuration_fails_clearly():
    with pytest.raises(ValueError, match="missing repository identity"):
        get_task_source({"type": "github", "repo": ""})

    proj = _make_project({"github": {"enabled": True}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is None
    assert len(diags) == 1
    assert diags[0]["action"] == "ERROR"
    assert "missing repo identity" in diags[0]["details"]


def test_unsupported_task_source_fails_clearly():
    with pytest.raises(ValueError, match="unsupported task source type"):
        get_task_source({"type": "unsupported_backend"})

    proj = _make_project({"jira": {"enabled": True}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is None
    assert len(diags) == 1
    assert diags[0]["action"] == "ERROR"
    assert "unsupported task source: 'jira'" in diags[0]["details"]


def test_disabled_task_source_reports_disabled_diagnostic():
    proj = _make_project({"github": {"enabled": False, "repo": "owner/repo"}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is None
    assert len(diags) == 1
    assert diags[0]["action"] == "DISABLED"
    assert "enabled: false" in diags[0]["details"]


def test_zero_adapter_silent_failure_is_prevented():
    proj = _make_project({"github": {"enabled": False, "repo": "owner/repo"}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is None
    # Must NOT be empty when task_sources contains github
    assert len(diags) > 0


def test_caventra_style_project_config_loads_and_activates():
    proj = _make_project({"github": {"enabled": True, "repo": "saketvishal/Caventra"}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is not None
    assert isinstance(adapter, GitHubTaskSource)
    assert adapter.repo == "saketvishal/Caventra"
    assert len(diags) == 0


def test_github_sync_ingests_objective_issues_and_preserves_dependencies():
    issues = [
        {
            "number": 4,
            "title": "Caventra V1 internal alpha: end-to-end Matter Intelligence flow by 2026-09-30",
            "body": "## Objective\nDeliver a testable end-to-end Caventra V1 internal alpha by September 30, 2026.",
            "labels": [{"name": "caventra:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/4",
        },
        {
            "number": 40,
            "title": "Objective: temporal Matter Intelligence and Decision Event foundation",
            "body": "## Objective\nTemporal foundation.\n\n## Dependency\nRun after issue #4 is complete.",
            "labels": [{"name": "caventra:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/40",
        },
        {
            "number": 41,
            "title": "Objective: legal data, temporal evaluation corpus, and model benchmark program",
            "body": "## Objective\nEvaluation program.\n\n## Dependency\nRun after issue #4 is complete.",
            "labels": [{"name": "caventra:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/41",
        },
        {
            "number": 42,
            "title": "Objective: prove or reject Caventra's longitudinal intelligence moat",
            "body": "## Objective\nCompetitive moat experiment.\n\n## Dependencies\nRun after issues #40 and #41 are complete.",
            "labels": [{"name": "caventra:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/42",
        },
    ]

    client = FakeGitHubClient(issues)
    source = GitHubTaskSource(repo="example/repo", client=client)

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        session.commit()

    assert len(results) == 4
    synced_ids = {r.task_id for r in results}
    assert synced_ids == {"GH-4", "GH-40", "GH-41", "GH-42"}

    with SessionLocal() as session:
        t4 = session.get(BuildTask, "GH-4")
        t40 = session.get(BuildTask, "GH-40")
        t41 = session.get(BuildTask, "GH-41")
        t42 = session.get(BuildTask, "GH-42")

        assert t4 is not None
        assert t40 is not None
        assert t41 is not None
        assert t42 is not None

        # BuildObjective records also exist
        assert session.get(BuildObjective, "GH-4") is not None
        assert session.get(BuildObjective, "GH-40") is not None
        assert session.get(BuildObjective, "GH-41") is not None
        assert session.get(BuildObjective, "GH-42") is not None

        # Dependency relationships
        assert t4.dependencies == []
        assert t40.dependencies == ["GH-4"]
        assert t41.dependencies == ["GH-4"]
        assert t42.dependencies == ["GH-40", "GH-41"]

        # Claimability: only GH-4 is claimable
        now = utcnow()
        assert task_is_claimable(session, t4, now) is True
        assert task_is_claimable(session, t40, now) is False
        assert task_is_claimable(session, t41, now) is False
        assert task_is_claimable(session, t42, now) is False

        available_ids = {t.task_id for t in list_available_tasks(session)}
        assert "GH-4" in available_ids
        assert "GH-40" not in available_ids
        assert "GH-41" not in available_ids
        assert "GH-42" not in available_ids

        # Transition GH-4 to DONE -> GH-40 and GH-41 become claimable, GH-42 still not claimable
        transition_task(session, "GH-4", "CLAIMED", actor="builder")
        transition_task(session, "GH-4", "IN_PROGRESS", actor="builder")
        transition_task(session, "GH-4", "VALIDATING", actor="builder")
        transition_task(session, "GH-4", "DONE", actor="integration", reason="completed")
        session.commit()

    with SessionLocal() as session:
        t40 = session.get(BuildTask, "GH-40")
        t41 = session.get(BuildTask, "GH-41")
        t42 = session.get(BuildTask, "GH-42")
        now = utcnow()

        assert task_is_claimable(session, t40, now) is True
        assert task_is_claimable(session, t41, now) is True
        assert task_is_claimable(session, t42, now) is False

        # Complete GH-40 and GH-41 -> GH-42 becomes claimable
        for tid in ("GH-40", "GH-41"):
            transition_task(session, tid, "CLAIMED", actor="builder")
            transition_task(session, tid, "IN_PROGRESS", actor="builder")
            transition_task(session, tid, "VALIDATING", actor="builder")
            transition_task(session, tid, "DONE", actor="integration", reason="completed")
        session.commit()

    with SessionLocal() as session:
        t42 = session.get(BuildTask, "GH-42")
        now = utcnow()
        assert task_is_claimable(session, t42, now) is True


def test_repeated_sync_is_idempotent_and_preserves_local_state():
    # Pre-existing local task (e.g. CAV-122) in BLOCKED state
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="CAV-122-01",
                title="SDD-122: Progressive Guidance frontend route and page",
                description="Local task",
                acceptance_criteria=["Pass validation"],
                dependencies=[],
            ),
        )
        transition_task(session, "CAV-122-01", "CLAIMED", actor="builder")
        transition_task(session, "CAV-122-01", "BLOCKED", actor="builder", reason="EXECUTION_RETRY_LIMIT_REACHED")
        session.commit()

    issues = [
        {
            "number": 4,
            "title": "Caventra V1 internal alpha",
            "body": "## Objective\nAlpha objective.",
            "labels": [{"name": "caventra:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/4",
        }
    ]

    client = FakeGitHubClient(issues)
    source = GitHubTaskSource(repo="example/repo", client=client)

    # First sync
    with SessionLocal() as session:
        first_results = source.discover_tasks(session)
        session.commit()
    assert first_results[0].action == "CREATED"

    # Transition GH-4 to IN_PROGRESS
    with SessionLocal() as session:
        transition_task(session, "GH-4", "CLAIMED", actor="builder")
        transition_task(session, "GH-4", "IN_PROGRESS", actor="builder")
        session.commit()

    # Second sync (must be idempotent)
    with SessionLocal() as session:
        second_results = source.discover_tasks(session)
        session.commit()
    assert second_results[0].action == "SKIPPED"
    assert "in sync (IN_PROGRESS)" in second_results[0].details

    # Verify GH-4 state was preserved and NOT reset to READY
    with SessionLocal() as session:
        t4 = session.get(BuildTask, "GH-4")
        assert t4.state == "IN_PROGRESS"

        # Verify CAV-122-01 was completely untouched
        cav = session.get(BuildTask, "CAV-122-01")
        assert cav is not None
        assert cav.state == "BLOCKED"
        assert cav.title == "SDD-122: Progressive Guidance frontend route and page"
