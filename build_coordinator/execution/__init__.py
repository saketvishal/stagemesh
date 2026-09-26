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
from build_coordinator.execution.sdk import (
    ACCEPTANCE_MATRIX,
    ADAPTER_ERROR_TAXONOMY,
    ADAPTER_RESULT_SEMANTICS,
    AUTH_ADAPTER_ERRORS,
    EXHAUSTION_ADAPTER_ERRORS,
    RETRYABLE_ADAPTER_ERRORS,
    RUNTIME_ACCEPTANCE_MATRIX,
    SUPPORTED_ADAPTER_PROTOCOL_VERSION,
    acceptance_matrix_by_stage,
    runtime_matrix_by_runtime,
)
from build_coordinator.execution.subprocess_executor import SubprocessExecutor

__all__ = [
    "ACCEPTANCE_MATRIX",
    "ADAPTER_ERROR_TAXONOMY",
    "ADAPTER_RESULT_SEMANTICS",
    "AUTH_ADAPTER_ERRORS",
    "ExecutionHandle",
    "ExecutionLaunch",
    "ExecutionObservation",
    "ExecutorResult",
    "ExecutorResultError",
    "EXHAUSTION_ADAPTER_ERRORS",
    "FakeExecutor",
    "RETRYABLE_ADAPTER_ERRORS",
    "RUNTIME_ACCEPTANCE_MATRIX",
    "SUPPORTED_ADAPTER_PROTOCOL_VERSION",
    "SubprocessExecutor",
    "WorkerExecutor",
    "acceptance_matrix_by_stage",
    "parse_executor_result",
    "runtime_matrix_by_runtime",
]
