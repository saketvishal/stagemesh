"""Azure DevOps adapter for optional StageMesh task import/export.

The coordinator remains the authoritative backlog and lifecycle owner. Azure
DevOps is only an optional source of task descriptions and an optional sink for
StageMesh lifecycle/evidence updates.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

from sqlalchemy import select

from build_coordinator.events import record_event
from build_coordinator.models import BuildTask, BuildTaskEvent
from build_coordinator.service import upsert_task
from build_coordinator.task_source.base import SyncResult, TaskSource, source_identity_metadata
from build_coordinator.types import EventInput, TaskSpec

logger = logging.getLogger(__name__)


class AzureDevOpsTaskSource(TaskSource):
    """Discovers Azure DevOps work items and syncs StageMesh lifecycle evidence."""

    def __init__(
        self,
        *,
        organization: str | None = None,
        project: str | None = None,
        query: str | None = None,
        dry_run: bool = False,
        client: Any = None,
    ) -> None:
        self.organization = organization or os.getenv("BUILD_COORDINATOR_AZDO_ORG")
        self.project = project or os.getenv("BUILD_COORDINATOR_AZDO_PROJECT")
        self.query = query or os.getenv("BUILD_COORDINATOR_AZDO_QUERY")
        self.dry_run = dry_run
        self._client = client

    def discover_tasks(self, session) -> list[SyncResult]:
        if not self.organization or not self.project:
            return []
        results: list[SyncResult] = []
        for item in self._fetch_work_items():
            result = self._sync_work_item(session, item)
            if result is not None:
                results.append(result)
        return results

    def _fetch_work_items(self) -> list[dict[str, Any]]:
        if self._client is not None:
            return self._client.list_work_items(
                organization=self.organization,
                project=self.project,
                query=self.query,
            )

        if not self.query:
            return []
        cmd = [
            "az",
            "boards",
            "query",
            "--org",
            self.organization,
            "--project",
            self.project,
            "--wiql",
            self.query,
            "--output",
            "json",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
            payload = json.loads(res.stdout)
        except FileNotFoundError:
            raise RuntimeError("Azure CLI ('az') is not installed or not found on PATH")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"failed to fetch Azure DevOps work items: {err}")
        except Exception as exc:
            raise RuntimeError(f"failed to fetch Azure DevOps work items: {exc}")

        if isinstance(payload, list):
            return payload
        return payload.get("workItems") or payload.get("value") or []

    def _sync_work_item(self, session, item: dict[str, Any]) -> SyncResult | None:
        work_item_id = str(item.get("id") or item.get("workItemId") or "").strip()
        if not work_item_id:
            return None
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        title = str(item.get("title") or fields.get("System.Title") or f"Azure DevOps Work Item {work_item_id}")
        description = str(
            item.get("description")
            or fields.get("System.Description")
            or fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
            or ""
        )
        task_id = str(item.get("task_id") or item.get("taskId") or f"ADO-{work_item_id}")
        url = str(item.get("url") or item.get("_links", {}).get("html", {}).get("href") or self._work_item_url(work_item_id))

        criteria = self._acceptance_criteria(item, fields)
        existing = session.get(BuildTask, task_id)
        if existing is not None:
            return SyncResult(
                task_id=existing.task_id,
                title=existing.title,
                action="SKIPPED",
                source_ref=url,
                details="existing local task left authoritative",
            )

        task = upsert_task(
            session,
            TaskSpec(
                task_id=task_id,
                title=title,
                description=description[:2000],
                acceptance_criteria=criteria,
                dependencies=list(item.get("dependencies") or []),
                definition_metadata=source_identity_metadata(
                    source_type="azure_devops",
                    source_owner=f"{self.organization}/{self.project}",
                    source_ref=work_item_id,
                    source_url=url,
                    legacy={"source_work_item_id": work_item_id},
                ),
            ),
        )
        session.flush()
        self._record_sync_event(session, task.task_id, work_item_id, url, "CREATED")
        return SyncResult(task_id=task.task_id, title=task.title, action="CREATED", source_ref=url)

    @staticmethod
    def _acceptance_criteria(item: dict[str, Any], fields: dict[str, Any]) -> list[str]:
        raw = item.get("acceptance_criteria") or fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")
        if isinstance(raw, list):
            return [str(value) for value in raw if str(value).strip()]
        if isinstance(raw, str) and raw.strip():
            lines = [line.strip(" -*\t") for line in raw.splitlines()]
            return [line for line in lines if line]
        return ["Satisfy all requirements stated in Azure DevOps work item."]

    def _work_item_url(self, work_item_id: str) -> str:
        return f"{self.organization}/{self.project}/_workitems/edit/{work_item_id}"

    def _record_sync_event(self, session, task_id: str, work_item_id: str, url: str, action: str) -> None:
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="task.synced_from_source",
                actor="azure-devops-sync",
                event_data={
                    "source": url,
                    "work_item_id": work_item_id,
                    "action": action,
                },
            ),
        )

    def _resolve_work_item_id(self, session, task_id: str) -> str | None:
        match = re.match(r"^ADO-(\d+)$", task_id)
        if match:
            return match.group(1)
        events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.actor == "azure-devops-sync")
            .order_by(BuildTaskEvent.created_at.asc())
        ).all()
        for event in events:
            data = event.event_data or {}
            if data.get("work_item_id"):
                return str(data["work_item_id"])
        return None

    def _is_outbound_synced(self, session, task_id: str, state: str) -> bool:
        events = session.scalars(
            select(BuildTaskEvent).where(
                BuildTaskEvent.task_id == task_id,
                BuildTaskEvent.event_type == "task.outbound_synced",
            )
        ).all()
        return any(
            (event.event_data or {}).get("adapter") == "azure_devops"
            and (event.event_data or {}).get("state") == state
            for event in events
        )

    def sync_outbound(
        self,
        session,
        task_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        evidence = evidence or {}
        work_item_id = self._resolve_work_item_id(session, task_id)
        if work_item_id is None:
            return True
        if self._is_outbound_synced(session, task_id, state):
            return True

        payload = {
            "state": state,
            "evidence": self._allowed_evidence(evidence),
        }
        if not self.dry_run:
            try:
                self._update_work_item(work_item_id, payload)
            except Exception as exc:
                err = str(exc)
                logger.warning("Failed to sync outbound to Azure DevOps work item %s: %s", work_item_id, err)
                self._record_outbound_failed(session, task_id, work_item_id, err)
                return False
        self._record_outbound_synced(session, task_id, work_item_id, state, payload)
        return True

    @staticmethod
    def _allowed_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        allowed = {"worker_id", "claim_id", "feature_sha", "review_verdict", "summary", "completed_work"}
        return {key: evidence[key] for key in allowed if key in evidence}

    def _update_work_item(self, work_item_id: str, payload: dict[str, Any]) -> None:
        if self._client is not None:
            if hasattr(self._client, "update_work_item"):
                self._client.update_work_item(
                    organization=self.organization,
                    project=self.project,
                    work_item_id=work_item_id,
                    state=payload["state"],
                    evidence=payload["evidence"],
                )
            return
        comment = self._format_history_comment(payload["state"], payload["evidence"])
        subprocess.run(
            [
                "az",
                "boards",
                "work-item",
                "update",
                "--org",
                self.organization,
                "--project",
                self.project,
                "--id",
                work_item_id,
                "--fields",
                f"System.State={payload['state']}",
                "--discussion",
                comment,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    @staticmethod
    def _format_history_comment(state: str, evidence: dict[str, Any]) -> str:
        lines = [f"StageMesh lifecycle update: {state}"]
        for key, value in evidence.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)

    def _record_outbound_synced(
        self,
        session,
        task_id: str,
        work_item_id: str,
        state: str,
        payload: dict[str, Any],
    ) -> None:
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="task.outbound_synced",
                actor="azure-devops-sync",
                event_data={
                    "adapter": "azure_devops",
                    "work_item_id": work_item_id,
                    "state": state,
                    "payload": payload,
                },
            ),
        )
        session.flush()

    def _record_outbound_failed(self, session, task_id: str, work_item_id: str, error: str) -> None:
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="task.outbound_sync_failed",
                actor="azure-devops-sync",
                event_data={
                    "adapter": "azure_devops",
                    "work_item_id": work_item_id,
                    "error": error,
                    "action": "update_work_item",
                },
            ),
        )
        session.flush()
