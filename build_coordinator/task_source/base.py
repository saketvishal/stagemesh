"""Provider-neutral task source interface for StageMesh.

A TaskSource allows StageMesh to discover engineering work from external systems
(e.g., GitHub Issues, local task files, ticketing backends) and maintain that
work in its durable task queue, without delegating lifecycle state to the external system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SyncResult:
    task_id: str
    title: str
    action: str  # "CREATED", "UPDATED", "SKIPPED", "ERROR"
    source_ref: str = ""
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "action": self.action,
            "source_ref": self.source_ref,
            "details": self.details,
        }


@dataclass(frozen=True)
class TaskSourceConfig:
    source_type: str = "github"
    repo: str | None = None
    labels: tuple[str, ...] = ()
    state: str = "open"
    dry_run: bool = False
    options: dict[str, Any] = field(default_factory=dict)


class TaskSource(ABC):
    """Abstract interface for external task discovery and state synchronization."""

    @abstractmethod
    def discover_tasks(self, session) -> list[SyncResult]:
        """Fetch tasks from the external source and synchronize into the coordinator queue."""

    @abstractmethod
    def sync_outbound(
        self,
        session,
        task_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Propagate coordinator lifecycle state and evidence back to the external task source."""
    def sync_objective_outbound(
        self,
        session,
        objective_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Propagate objective lifecycle state and evidence back to the external task source.

        Default implementation is a no-op returning True.
        """
        return True

