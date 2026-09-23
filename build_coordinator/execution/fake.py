"""Deterministic executor for tests and dry-run orchestration."""

from __future__ import annotations

from collections import deque
from uuid import uuid4

from build_coordinator.execution.base import (
    ExecutionHandle,
    ExecutionLaunch,
    ExecutionObservation,
)


class FakeExecutor:
    adapter_name = "fake"

    def __init__(self, observations: list[ExecutionObservation] | None = None) -> None:
        self.launches: list[ExecutionLaunch] = []
        self._observations = deque(observations or [])

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        self.launches.append(launch)
        return ExecutionHandle(
            execution_id=launch.execution_id or f"fake-{uuid4()}",
            result_path=launch.result_path,
        )

    def poll(self, execution_id: str) -> ExecutionObservation:
        if self._observations:
            return self._observations.popleft()
        return ExecutionObservation(status="RUNNING")

    def terminate(self, execution_id: str) -> ExecutionObservation:
        return ExecutionObservation(status="TERMINATED")
