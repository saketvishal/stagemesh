"""Provider-neutral execution contracts.

The coordinator owns lifecycle state. Executors only launch and observe
external worker processes or deterministic test fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ExecutionLaunch:
    task_id: str
    role: str
    worker_id: str
    provider: str | None
    worktree_path: str | None
    branch_name: str | None
    prompt: str
    execution_id: str | None = None
    result_path: str | None = None
    reviewed_feature_sha: str | None = None
    timeout_seconds: int | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionHandle:
    execution_id: str
    process_id: str | None = None
    result_path: str | None = None


@dataclass(frozen=True)
class ExecutionObservation:
    status: str
    exit_code: int | None = None
    result_data: dict = field(default_factory=dict)
    human_escalation_type: str | None = None
    result_path: str | None = None


class WorkerExecutor(Protocol):
    adapter_name: str

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        """Start execution and return provider-neutral process metadata."""

    def poll(self, execution_id: str) -> ExecutionObservation:
        """Observe execution without changing coordinator ownership."""

    def terminate(self, execution_id: str) -> ExecutionObservation:
        """Request termination of a launched execution."""
