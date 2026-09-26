"""Provider-neutral task source interface for StageMesh.

A TaskSource allows StageMesh to discover engineering work from external systems
(e.g., GitHub Issues, local task files, ticketing backends) and maintain that
work in its durable task queue, without delegating lifecycle state to the external system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

SOURCE_ELIGIBLE = "ELIGIBLE"
SOURCE_DEFERRED = "DEFERRED"
SOURCE_WAITING = "WAITING"
SOURCE_HUMAN_ONLY = "HUMAN_ONLY"
SOURCE_INFORMATIONAL = "INFORMATIONAL"
SOURCE_ELIGIBILITY_VALUES = frozenset(
    {
        SOURCE_ELIGIBLE,
        SOURCE_DEFERRED,
        SOURCE_WAITING,
        SOURCE_HUMAN_ONLY,
        SOURCE_INFORMATIONAL,
    }
)


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


def source_identity_metadata(
    *,
    source_type: str,
    source_ref: str,
    source_owner: str | None = None,
    source_url: str | None = None,
    source_state: str | None = None,
    source_eligibility: str | None = None,
    source_eligibility_reason: str | None = None,
    legacy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the provider-neutral task-source identity envelope.

    Task-source identity is only provenance/idempotency metadata. Routing,
    review policy, validation, permissions, and protected paths remain owned by
    StageMesh project/config policy even when source text mentions them.
    """

    metadata: dict[str, Any] = {
        "task_source": source_type,
        "source_type": source_type,
        "source_ref": str(source_ref),
    }
    if source_owner:
        metadata["source_owner"] = str(source_owner)
    if source_url:
        metadata["source_url"] = str(source_url)
    if source_state:
        metadata["source_state"] = str(source_state).upper()
    if source_eligibility:
        eligibility = str(source_eligibility).upper()
        if eligibility not in SOURCE_ELIGIBILITY_VALUES:
            raise ValueError(f"unknown source eligibility: {source_eligibility}")
        metadata["source_eligibility"] = eligibility
    if source_eligibility_reason:
        metadata["source_eligibility_reason"] = str(source_eligibility_reason)
    if legacy:
        metadata.update(legacy)
    return metadata


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

