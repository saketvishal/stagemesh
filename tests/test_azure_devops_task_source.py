from __future__ import annotations

import json
from pathlib import Path
import pytest

from stagemesh.azure_devops import AzureDevOpsClientShim, AzureDevOpsTaskSource
from stagemesh.domain import Stage, TaskStatus
from stagemesh.persistence import Store


class FakeAzureDevOpsClient(AzureDevOpsClientShim):
    def __init__(self, work_items: list[dict]):
        self.work_items = {wi["id"]: dict(wi) for wi in work_items}
        self.updated_items = []
        self.should_fail_auth = False
        self.should_fail_rate_limit = False

    def query_work_items(self, organization: str, project: str, query: str) -> list[dict]:
        if self.should_fail_auth:
            raise RuntimeError("401 Unauthorized: Invalid PAT token")
        if self.should_fail_rate_limit:
            raise RuntimeError("429 Too Many Requests: Rate limit exceeded")
        return list(self.work_items.values())

    def update_work_item(self, organization: str, project: str, work_item_id: int, fields: dict) -> dict:
        if work_item_id in self.work_items:
            self.work_items[work_item_id].setdefault("fields", {}).update(fields)
        self.updated_items.append((work_item_id, fields))
        return self.work_items.get(work_item_id, {"id": work_item_id, "fields": fields})


def test_azure_devops_task_discovery_and_mapping(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()

    mock_items = [
        {
            "id": 101,
            "fields": {
                "System.Title": "Fix Azure DevOps Pipeline",
                "System.Description": "Pipeline fails on step 3",
                "Microsoft.VSTS.Common.AcceptanceCriteria": "Step 3 passes",
                "System.State": "To Do",
            },
        },
        {
            "id": 102,
            "fields": {
                "System.Title": "Add telemetry logging",
                "System.Description": "Log telemetry events",
                "System.State": "Done",
            },
        },
    ]
    client = FakeAzureDevOpsClient(mock_items)
    source = AzureDevOpsTaskSource(
        organization="my-org",
        project="my-proj",
        query="SELECT [System.Id] FROM WorkItems",
        client=client,
    )

    discovered = source.discover_tasks(store)
    assert len(discovered) == 2

    # Check mapping into stable StageMesh identity
    t1 = store.get_task_by_source("azure-devops", "101")
    assert t1 is not None
    assert t1["id"] == "ADO-101"
    assert t1["title"] == "Fix Azure DevOps Pipeline"

    # Closed work item handled correctly
    t2 = store.get_task_by_source("azure-devops", "102")
    assert t2 is not None
    assert t2["status"] == TaskStatus.DONE.value


def test_azure_devops_duplicate_safe_sync(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()

    mock_items = [
        {
            "id": 201,
            "fields": {"System.Title": "Unique Task", "System.State": "New"},
        }
    ]
    client = FakeAzureDevOpsClient(mock_items)
    source = AzureDevOpsTaskSource(organization="org", project="proj", query="q", client=client)

    # First sync creates task
    source.discover_tasks(store)
    assert len(store.tasks()) == 1

    # Second sync skips duplicate insertion
    source.discover_tasks(store)
    assert len(store.tasks()) == 1


def test_azure_devops_source_state_reconciliation(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()

    mock_items = [{"id": 301, "fields": {"System.Title": "Sync State Task", "System.State": "In Progress"}}]
    client = FakeAzureDevOpsClient(mock_items)
    source = AzureDevOpsTaskSource(organization="org", project="proj", query="q", client=client)

    task_id = store.upsert_task("Sync State Task", source="azure-devops", source_id="301")
    store.advance_task(task_id, Stage.DONE)

    # Remote update projection
    source.reconcile_task_state(store, task_id)
    assert len(client.updated_items) == 1
    assert client.updated_items[0][0] == 301
    assert client.updated_items[0][1].get("System.State") in ("Done", "Closed")


def test_azure_devops_failure_classification(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()

    client = FakeAzureDevOpsClient([])
    client.should_fail_auth = True
    source = AzureDevOpsTaskSource(organization="org", project="proj", query="q", client=client)

    with pytest.raises(RuntimeError, match="401 Unauthorized"):
        source.discover_tasks(store)
