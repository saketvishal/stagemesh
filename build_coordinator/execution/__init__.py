"""Provider-neutral execution adapters for the build runner."""

from build_coordinator.execution.base import (
    ExecutionHandle,
    ExecutionLaunch,
    ExecutionObservation,
    WorkerExecutor,
)
from build_coordinator.execution.fake import FakeExecutor
from build_coordinator.execution.results import (
    ExecutorResult,
    ExecutorResultError,
    parse_executor_result,
)
from build_coordinator.execution.subprocess_executor import SubprocessExecutor

__all__ = [
    "ExecutionHandle",
    "ExecutionLaunch",
    "ExecutionObservation",
    "ExecutorResult",
    "ExecutorResultError",
    "FakeExecutor",
    "SubprocessExecutor",
    "WorkerExecutor",
    "parse_executor_result",
]
