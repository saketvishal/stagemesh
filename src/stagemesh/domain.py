from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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
        if not self.is_known or not other.is_known:
            return False
        if self.pid != other.pid:
            return False
        if self.boot_id != other.boot_id:
            return False
        if abs((self.create_time or 0.0) - (other.create_time or 0.0)) > 1e-4:
            return False
        if self.executable and other.executable:
            s_name = Path(self.executable).name.lower()
            o_name = Path(other.executable).name.lower()
            if s_name == o_name or self.executable == other.executable:
                return True
            alias_groups = [
                {"claude", "claude.cmd", "claude.exe", "node", "node.exe"},
                {"codex", "codex.cmd", "codex.exe", "node", "node.exe", "python", "python.exe"},
            ]
            for group in alias_groups:
                if s_name in group and o_name in group:
                    return True
            return False
        return True


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
