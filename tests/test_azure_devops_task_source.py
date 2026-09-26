"""Tests for the optional Azure DevOps task-source adapter."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from build_coordinator.claims import task_is_claimable
from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.project.commands import _optional_task_source
from build_coordinator.project.definition import ProjectDefinition
from build_coordinator.service import utcnow
from build_coordinator.task_source import get_task_source
from build_coordinator.task_source.azure_devops import AzureDevOpsTaskSource


class FakeAzureDevOpsClient:
    def __init__(self, work_items: list[dict] | None = None) -> None:
        self.work_items = list(work_items or [])
        self.updates: list[dict] = []

    def list_work_items(self, *, organization: str, project: str, query: str | None):
        return self.work_items

    def update_work_item(
        self,
        *,
        organization: str,
        project: str,
        work_item_id: str,
        state: str,
        evidence: dict,
    ):
        self.updates.append(
            {
                "organization": organization,
                "project": project,
                "work_item_id": work_item_id,
                "state": state,
                "evidence": evidence,
            }
        )


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


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


def test_azure_devops_source_is_disabled_by_default():
    assert get_task_source(None) is None

    with pytest.raises(ValueError, match="missing Azure DevOps organization"):
        get_task_source({"type": "azure_devops"})


def test_configured_azure_devops_source_creates_adapter():
    source = get_task_source(
        {
            "type": "azure_devops",
            "options": {
                "organization": "https://dev.azure.com/acme",
                "project": "mesh",
                "query": "Select [System.Id] From WorkItems",
            },
        }
    )
    assert isinstance(source, AzureDevOpsTaskSource)
    assert source.organization == "https://dev.azure.com/acme"
    assert source.project == "mesh"


def test_project_azure_devops_config_loads_and_activates():
    proj = _make_project(
        {
            "azure_devops": {
                "enabled": True,
                "organization": "https://dev.azure.com/acme",
                "project": "mesh",
                "query": "Select [System.Id] From WorkItems",
            }
        }
    )

    adapter, diags = _optional_task_source(proj, force=False, dry_run=True)

    assert isinstance(adapter, AzureDevOpsTaskSource)
    assert adapter.organization == "https://dev.azure.com/acme"
    assert adapter.project == "mesh"
    assert adapter.query == "Select [System.Id] From WorkItems"
    assert adapter.dry_run is True
    assert diags == []



def test_azure_devops_import_records_source_identity_without_becoming_lifecycle_authority():
    client = FakeAzureDevOpsClient(
        [
            {
                "id": 123,
                "fields": {
                    "System.Title": "Wire Azure DevOps import",
                    "System.Description": "Import work item descriptions only.",
                    "Microsoft.VSTS.Common.AcceptanceCriteria": "- Create adapter\n- Add tests",
                },
                "url": "https://dev.azure.com/acme/mesh/_workitems/edit/123",
            }
        ]
    )
    source = AzureDevOpsTaskSource(
        organization="https://dev.azure.com/acme",
        project="mesh",
        client=client,
    )

    with SessionLocal() as session:
        results = source.discover_tasks(session)
        task = session.get(BuildTask, "ADO-123")
        task.state = "BLOCKED"
        session.commit()

    assert results[0].task_id == "ADO-123"
    assert results[0].action == "CREATED"

    with SessionLocal() as session:
        source.discover_tasks(session)
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "ADO-123")
        assert task is not None
        assert task.state == "BLOCKED"
        assert task.definition_metadata["task_source"] == "azure_devops"
        assert task.definition_metadata["source_type"] == "azure_devops"
        assert task.definition_metadata["source_owner"] == "https://dev.azure.com/acme/mesh"
        assert task.definition_metadata["source_ref"] == "123"
        assert task.definition_metadata["source_work_item_id"] == "123"
        event = session.query(BuildTaskEvent).filter_by(
            task_id="ADO-123",
            actor="azure-devops-sync",
            event_type="task.synced_from_source",
        ).one()
        assert event.event_data["work_item_id"] == "123"


def test_closed_source_suppression_is_source_neutral_not_lifecycle_authority():
    with SessionLocal() as session:
        session.add(
            BuildTask(
                task_id="LOCAL-1",
                title="Locally owned task with closed source context",
                description="Do work",
                acceptance_criteria=["Works"],
                definition_metadata={
                    "task_source": "local",
                    "source_type": "local",
                    "source_ref": "backlog/LOCAL-1",
                    "source_state": "CLOSED",
                },
                state="READY",
            )
        )
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "LOCAL-1")
        assert task is not None
        assert task.state == "READY"
        assert not task_is_claimable(session, task, utcnow())


def test_azure_devops_outbound_sends_lifecycle_state_and_evidence_only():
    client = FakeAzureDevOpsClient([{"id": 123, "fields": {"System.Title": "Sync outbound"}}])
    source = AzureDevOpsTaskSource(
        organization="https://dev.azure.com/acme",
        project="mesh",
        client=client,
    )

    with SessionLocal() as session:
        source.discover_tasks(session)
        ok = source.sync_outbound(
            session,
            "ADO-123",
            "DONE",
            evidence={
                "worker_id": "builder-1",
                "feature_sha": "abc123",
                "summary": "completed cleanly",
                "title": "must not propagate",
                "acceptance_criteria": ["must not propagate"],
            },
        )
        session.commit()

    assert ok is True
    assert client.updates == [
        {
            "organization": "https://dev.azure.com/acme",
            "project": "mesh",
            "work_item_id": "123",
            "state": "DONE",
            "evidence": {
                "worker_id": "builder-1",
                "feature_sha": "abc123",
                "summary": "completed cleanly",
            },
        }
    ]

    with SessionLocal() as session:
        source.sync_outbound(session, "ADO-123", "DONE", evidence={"summary": "duplicate"})
        source.sync_outbound(session, "SM-123", "DONE", evidence={"summary": "local task"})
        session.commit()

    assert len(client.updates) == 1


def test_azure_devops_cli_outbound_updates_lifecycle_state_and_discussion(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("build_coordinator.task_source.azure_devops.subprocess.run", fake_run)
    source = AzureDevOpsTaskSource(
        organization="https://dev.azure.com/acme",
        project="mesh",
    )

    source._update_work_item(
        "123",
        {
            "state": "DONE",
            "evidence": {
                "worker_id": "builder-1",
                "summary": "completed cleanly",
            },
        },
    )

    assert len(commands) == 1
    cmd = commands[0]["cmd"]
    assert cmd[:4] == ["az", "boards", "work-item", "update"]
    assert "--fields" in cmd
    assert cmd[cmd.index("--fields") + 1] == "System.State=DONE"
    assert "--discussion" in cmd
    discussion = cmd[cmd.index("--discussion") + 1]
    assert "StageMesh lifecycle update: DONE" in discussion
    assert "worker_id: builder-1" in discussion
    assert "summary: completed cleanly" in discussion
