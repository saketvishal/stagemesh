from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    VALIDATE = "VALIDATE"
    REVIEW = "REVIEW"
    INTEGRATE = "INTEGRATE"
    DONE = "DONE"


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    BLOCKED = "BLOCKED"
    DONE = "DONE"


class ExecutionKind(StrEnum):
    IMPLEMENTATION = "IMPLEMENTATION"
    VALIDATION = "VALIDATION"
    REVIEW = "REVIEW"
    INTEGRATION = "INTEGRATION"


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class EvidenceKind(StrEnum):
    VALIDATION = "VALIDATION"
    REVIEW = "REVIEW"
    INTEGRATION = "INTEGRATION"
    HANDOFF = "HANDOFF"


class EvidenceStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    CAPACITY = "CAPACITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int | None
    create_time: float | None
    boot_id: str | None
    executable: str | None = None

    @property
    def is_known(self) -> bool:
        return self.pid is not None and self.create_time is not None and self.boot_id is not None

    def matches(self, other: "ProcessIdentity") -> bool:
        return self.is_known and other.is_known and self == other


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    stage: Stage = Stage.PLAN
    status: TaskStatus = TaskStatus.OPEN
    source: str = "local"
    source_id: str | None = None
    project: str | None = None


@dataclass(frozen=True)
class Candidate:
    id: str
    task_id: str
    sha: str
    produced_by: str
    durable_handoff: bool


@dataclass(frozen=True)
class Evidence:
    id: str
    task_id: str
    candidate_sha: str
    kind: EvidenceKind
    status: EvidenceStatus
    payload: dict[str, Any]
