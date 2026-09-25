"""Deterministic tests for GitHub task source activation, diagnostics, objective ingestion, and dependency gating."""

from __future__ import annotations

import pytest
from pathlib import Path

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildObjective, BuildObjectiveEvent, BuildTask
from build_coordinator.project.commands import _optional_task_source
from build_coordinator.project.definition import ProjectDefinition
from build_coordinator.claims import task_is_claimable, utcnow
from build_coordinator.planner import planner_task_id
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


def test_project_github_config_loads_and_activates():
    proj = _make_project({"github": {"enabled": True, "repo": "stagemesh/stagemesh"}})
    adapter, diags = _optional_task_source(proj, force=False, dry_run=False)
    assert adapter is not None
    assert isinstance(adapter, GitHubTaskSource)
    assert adapter.repo == "stagemesh/stagemesh"
    assert len(diags) == 0


def test_github_sync_ingests_objective_issues_and_preserves_dependencies():
    issues = [
        {
            "number": 4,
            "title": "StageMesh V1 internal alpha: end-to-end engineering flow by 2026-09-30",
            "body": "## Objective\nDeliver a testable end-to-end StageMesh V1 internal alpha by September 30, 2026.",
            "labels": [{"name": "stagemesh:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/4",
        },
        {
            "number": 40,
            "title": "Objective: durable planner and event foundation",
            "body": "## Objective\nTemporal foundation.\n\n## Dependency\nRun after issue #4 is complete.",
            "labels": [{"name": "objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/40",
        },
        {
            "number": 41,
            "title": "Objective: evaluation corpus and benchmark program",
            "body": "## Objective\nEvaluation program.\n\n## Dependency\nRun after issue #4 is complete.",
            "labels": [{"name": "stagemesh:objective"}, {"name": "status:QUEUED"}],
            "url": "https://github.com/example/repo/issues/41",
        },
        {
            "number": 42,
            "title": "Objective: prove or reject the controller reliability hypothesis",
            "body": "## Objective\nCompetitive moat experiment.\n\n## Dependencies\nRun after issues #40 and #41 are complete.",
            "labels": [{"name": "stagemesh:objective"}, {"name": "status:QUEUED"}],
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
        assert session.get(BuildTask, "GH-4") is None
        assert session.get(BuildTask, "GH-40") is None
        assert session.get(BuildTask, "GH-41") is None
        assert session.get(BuildTask, "GH-42") is None

        obj4 = session.get(BuildObjective, "GH-4")
        obj40 = session.get(BuildObjective, "GH-40")
        obj41 = session.get(BuildObjective, "GH-41")
        obj42 = session.get(BuildObjective, "GH-42")
        assert obj4 is not None
        assert obj40 is not None
        assert obj41 is not None
        assert obj42 is not None

        # Dependency relationships
        assert obj4.dependencies == []
        assert obj40.dependencies == ["GH-4"]
        assert obj41.dependencies == ["GH-4"]
        assert obj42.dependencies == ["GH-40", "GH-41"]

        # Claimability: only the root objective planner is claimable, never the root issue.
        now = utcnow()
        p4 = session.get(BuildTask, planner_task_id("GH-4"))
        p40 = session.get(BuildTask, planner_task_id("GH-40"))
        p41 = session.get(BuildTask, planner_task_id("GH-41"))
        p42 = session.get(BuildTask, planner_task_id("GH-42"))
        assert p4 is not None
        assert p40 is not None
        assert p41 is not None
        assert p42 is not None
        assert task_is_claimable(session, p4, now) is True
        assert task_is_claimable(session, p40, now) is False
        assert task_is_claimable(session, p41, now) is False
        assert task_is_claimable(session, p42, now) is False

        available_ids = {t.task_id for t in list_available_tasks(session)}
        assert "GH-4" not in available_ids
        assert planner_task_id("GH-4") in available_ids
        assert planner_task_id("GH-40") not in available_ids
        assert planner_task_id("GH-41") not in available_ids
        assert planner_task_id("GH-42") not in available_ids

        # Completing GH-4 authoritatively unlocks GH-40/GH-41 planning, not via a root task.
        obj4.state = "COMPLETED"
        session.commit()

    with SessionLocal() as session:
        p40 = session.get(BuildTask, planner_task_id("GH-40"))
        p41 = session.get(BuildTask, planner_task_id("GH-41"))
        p42 = session.get(BuildTask, planner_task_id("GH-42"))
        now = utcnow()

        assert task_is_claimable(session, p40, now) is True
        assert task_is_claimable(session, p41, now) is True
        assert task_is_claimable(session, p42, now) is False

        # Complete GH-40 and GH-41 objectives -> GH-42 becomes eligible.
        session.get(BuildObjective, "GH-40").state = "COMPLETED"
        session.get(BuildObjective, "GH-41").state = "COMPLETED"
        session.commit()

    with SessionLocal() as session:
        p42 = session.get(BuildTask, planner_task_id("GH-42"))
        now = utcnow()
        assert task_is_claimable(session, p42, now) is True


def test_repeated_sync_is_idempotent_and_preserves_local_state():
    # Pre-existing local task in BLOCKED state
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="SM-122-01",
                title="SM-122: Progressive Guidance frontend route and page",
                description="Local task",
                acceptance_criteria=["Pass validation"],
                dependencies=[],
            ),
        )
        transition_task(session, "SM-122-01", "CLAIMED", actor="builder")
        transition_task(session, "SM-122-01", "BLOCKED", actor="builder", reason="EXECUTION_RETRY_LIMIT_REACHED")
        session.commit()

    issues = [
        {
            "number": 4,
            "title": "StageMesh V1 internal alpha",
            "body": "## Objective\nAlpha objective.",
            "labels": [{"name": "stagemesh:objective"}, {"name": "status:QUEUED"}],
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

    # Transition the planner task to IN_PROGRESS
    with SessionLocal() as session:
        planner_id = planner_task_id("GH-4")
        transition_task(session, planner_id, "CLAIMED", actor="planner")
        transition_task(session, planner_id, "IN_PROGRESS", actor="planner")
        session.commit()

    # Second sync (must be idempotent)
    with SessionLocal() as session:
        second_results = source.discover_tasks(session)
        session.commit()
    assert second_results[0].action == "SKIPPED"
    assert "authoritative objective" in second_results[0].details

    # Verify no root task was created and planner state was preserved.
    with SessionLocal() as session:
        assert session.get(BuildTask, "GH-4") is None
        planner = session.get(BuildTask, planner_task_id("GH-4"))
        assert planner.state == "IN_PROGRESS"

        # Verify unrelated local work was completely untouched
        local = session.get(BuildTask, "SM-122-01")
        assert local is not None
        assert local.state == "BLOCKED"
        assert local.title == "SM-122: Progressive Guidance frontend route and page"


def test_objective_direct_execution_requires_explicit_opt_in():
    issue = {
        "number": 71,
        "title": "Broad architecture objective",
        "body": "## Objective\nDo broad architecture work.\n\nDepends on: GH-75",
        "labels": [{"name": "objective"}],
        "url": "https://github.com/example/repo/issues/71",
    }
    source = GitHubTaskSource(repo="example/repo", client=FakeGitHubClient([issue]))

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildObjective, "GH-71") is not None
        assert session.get(BuildTask, "GH-71") is None

    issue["labels"].append({"name": "stagemesh:direct-execution"})
    source = GitHubTaskSource(repo="example/repo", client=FakeGitHubClient([issue]))
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "GH-71")
        assert task is not None
        assert task.objective_id == "GH-71"
        assert task.dependencies == ["GH-75"]


def test_historical_objective_root_task_is_reconciled_without_losing_dependencies():
    with SessionLocal() as session:
        obj = BuildObjective(
            objective_id="GH-71",
            goal="historical objective",
            completion_criteria=[],
            state="PLANNING",
        )
        session.add(obj)
        upsert_task(
            session,
            TaskSpec(
                task_id="GH-71",
                title="Historical synthetic root",
                description="old behavior",
                acceptance_criteria=["ok"],
                dependencies=["GH-75"],
            ),
        )
        session.commit()

    source = GitHubTaskSource(
        repo="example/repo",
        client=FakeGitHubClient(
            [
                {
                    "number": 71,
                    "title": "Broad architecture objective",
                    "body": "## Objective\nNo dependency text in this rewritten body.",
                    "labels": [{"name": "objective"}],
                    "url": "https://github.com/example/repo/issues/71",
                }
            ]
        ),
    )
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        obj = session.get(BuildObjective, "GH-71")
        task = session.get(BuildTask, "GH-71")
        assert obj.dependencies == ["GH-75"]
        assert task.state == "STALE"
        assert task.reason_created == "OBJECTIVE_ROOT_COMPAT"
        assert task_is_claimable(session, task, utcnow()) is False
        assert "GH-71" not in {t.task_id for t in list_available_tasks(session)}
        event = session.query(BuildObjectiveEvent).filter_by(
            objective_id="GH-71",
            event_type="objective.historical_root_task_reconciled",
        ).one()
        assert event.event_data["dependencies"] == ["GH-75"]


def test_direct_execution_opt_in_revives_historical_objective_root_task():
    with SessionLocal() as session:
        session.add(
            BuildObjective(
                objective_id="GH-71",
                goal="historical objective",
                completion_criteria=[],
                dependencies=["GH-75"],
                state="PLANNING",
            )
        )
        upsert_task(
            session,
            TaskSpec(
                task_id="GH-75",
                title="Prerequisite",
                description="already complete",
                acceptance_criteria=["done"],
            ),
        ).state = "DONE"
        historical = upsert_task(
            session,
            TaskSpec(
                task_id="GH-71",
                title="Historical synthetic root",
                description="old behavior",
                acceptance_criteria=["ok"],
                dependencies=["GH-75"],
            ),
        )
        historical.reason_created = "OBJECTIVE_ROOT_COMPAT"
        historical.objective_id = "GH-71"
        historical.state = "STALE"
        session.commit()

    source = GitHubTaskSource(
        repo="example/repo",
        client=FakeGitHubClient(
            [
                {
                    "number": 71,
                    "title": "Broad architecture objective",
                    "body": "## Objective\nNo dependency text in this rewritten body.",
                    "labels": [{"name": "objective"}, {"name": "stagemesh:direct-execution"}],
                    "url": "https://github.com/example/repo/issues/71",
                }
            ]
        ),
    )
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        obj = session.get(BuildObjective, "GH-71")
        task = session.get(BuildTask, "GH-71")
        assert obj.dependencies == ["GH-75"]
        assert task.objective_id == "GH-71"
        assert task.dependencies == ["GH-75"]
        assert task.reason_created == "GITHUB_SOURCE"
        assert task.state == "READY"
        assert task_is_claimable(session, task, utcnow()) is True


def test_gh_71_objective_waits_for_gh_75_authoritative_completion_after_reimport():
    issues = [
        {
            "number": 75,
            "title": "Do not dispatch objectives as tasks",
            "body": "Ordinary implementation issue.",
            "labels": [],
            "url": "https://github.com/example/repo/issues/75",
        },
        {
            "number": 71,
            "title": "Architecture/modularity validation program",
            "body": "## Objective\nBroad program.\n\nDepends on: GH-75",
            "labels": [{"name": "objective"}],
            "url": "https://github.com/example/repo/issues/71",
        },
    ]
    source = GitHubTaskSource(repo="example/repo", client=FakeGitHubClient(issues))

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "GH-71")
        planner = session.get(BuildTask, planner_task_id("GH-71"))
        assert objective.dependencies == ["GH-75"]
        assert session.get(BuildTask, "GH-71") is None
        assert task_is_claimable(session, planner, utcnow()) is False
        transition_task(session, "GH-75", "CLAIMED", actor="builder")
        transition_task(session, "GH-75", "IN_PROGRESS", actor="builder")
        transition_task(session, "GH-75", "VALIDATING", actor="builder")
        transition_task(session, "GH-75", "DONE", actor="integration", reason="completed")
        session.commit()

    source = GitHubTaskSource(repo="example/repo", client=FakeGitHubClient(issues))
    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildObjective, "GH-71").dependencies == ["GH-75"]
        assert task_is_claimable(session, session.get(BuildTask, planner_task_id("GH-71")), utcnow()) is True
