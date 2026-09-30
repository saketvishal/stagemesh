from __future__ import annotations

import json
import subprocess
from typing import Any
from pathlib import Path

from .domain import Stage, TaskStatus
from .persistence import Store


class AzureDevOpsClientShim:
    """Client interface for Azure DevOps work items."""

    def query_work_items(self, organization: str, project: str, query: str) -> list[dict]:
        cmd = [
            "az",
            "boards",
            "query",
            "--org",
            organization,
            "--project",
            project,
            "--wiql",
            query,
            "--output",
            "json",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            err = res.stderr.strip()
            if "401" in err or "Unauthorized" in err:
                raise RuntimeError(f"401 Unauthorized: {err}")
            if "429" in err or "Rate limit" in err:
                raise RuntimeError(f"429 Too Many Requests: {err}")
            raise RuntimeError(f"Azure DevOps query failed: {err}")
        return json.loads(res.stdout)

    def update_work_item(self, organization: str, project: str, work_item_id: int, fields: dict) -> dict:
        field_args = [f"{k}={v}" for k, v in fields.items()]
        cmd = [
            "az",
            "boards",
            "work-item",
            "update",
            "--id",
            str(work_item_id),
            "--fields",
            *field_args,
            "--output",
            "json",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Azure DevOps update failed: {res.stderr}")
        return json.loads(res.stdout)


class AzureDevOpsTaskSource:
    """Azure DevOps task source adapter for StageMesh vNext."""

    def __init__(
        self,
        organization: str,
        project: str,
        query: str,
        client: AzureDevOpsClientShim | None = None,
    ):
        self.organization = organization
        self.project = project
        self.query = query
        self.client = client or AzureDevOpsClientShim()

    def discover_tasks(self, store: Store) -> list[dict]:
        raw_items = self.client.query_work_items(self.organization, self.project, self.query)
        discovered = []

        for item in raw_items:
            wi_id = item.get("id")
            if not wi_id:
                continue
            fields = item.get("fields", {})
            title = fields.get("System.Title", f"Azure DevOps #{wi_id}")
            state = fields.get("System.State", "New")

            is_closed = state.lower() in ("done", "closed", "removed")
            status = TaskStatus.DONE if is_closed else TaskStatus.OPEN

            task_id = store.upsert_task(
                title=title,
                source="azure-devops",
                source_id=str(wi_id),
                project=self.project,
            )
            if is_closed:
                store.advance_task(task_id, Stage.DONE)
                store.conn.execute("UPDATE tasks SET status=? WHERE id=?", (TaskStatus.DONE.value, task_id))
                store.conn.commit()

            task = store.get_task(task_id)
            if task:
                discovered.append(dict(task))

        return discovered

    def reconcile_task_state(self, store: Store, task_id: str) -> None:
        task = store.get_task(task_id)
        if not task or task["source"] != "azure-devops":
            return
        source_id = task["source_id"]
        if not source_id or not str(source_id).isdigit():
            return

        wi_id = int(source_id)
        if task["stage"] == Stage.DONE.value or task["status"] == TaskStatus.DONE.value:
            self.client.update_work_item(
                self.organization,
                self.project,
                wi_id,
                {"System.State": "Done"},
            )
