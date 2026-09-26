"""Provider/runtime adapter SDK contracts and acceptance matrix.

This module is intentionally data-only: adapters and product layers can import
the contract without pulling in runner orchestration. The coordinator remains
the owner of lifecycle state; adapters only launch headless runtimes, report
typed observations, and write structured result JSON.
"""

from __future__ import annotations

from dataclasses import dataclass

from build_coordinator.execution.results import RESULT_STATUS_VALUES


SUPPORTED_ADAPTER_PROTOCOL_VERSION = 1

ADAPTER_RESULT_SEMANTICS = {
    "result_channel": "BUILD_COORDINATOR_RESULT_PATH",
    "status_values": RESULT_STATUS_VALUES,
    "stdout_semantics": "diagnostic_only",
    "identity_fields": ("schema_version", "execution_id", "task_id", "role"),
    "secret_handling": "adapters must not persist secrets, credentials, or hidden reasoning",
}

ADAPTER_ERROR_TAXONOMY = (
    "AUTH_FAILURE",
    "QUOTA_EXHAUSTED",
    "RATE_LIMITED",
    "UNAVAILABLE",
    "NETWORK_FAILURE",
    "EXECUTION_FAILURE",
    "PLANNER_CONTRACT_INVALID",
    "NO_CHANGES_PRODUCED",
)

RETRYABLE_ADAPTER_ERRORS = frozenset({"RATE_LIMITED", "UNAVAILABLE", "NETWORK_FAILURE"})
EXHAUSTION_ADAPTER_ERRORS = frozenset({"QUOTA_EXHAUSTED", "RATE_LIMITED"})
AUTH_ADAPTER_ERRORS = frozenset({"AUTH_FAILURE"})


@dataclass(frozen=True)
class StageAcceptance:
    stage: str
    role: str
    required_capabilities: tuple[str, ...]
    structured_result: str
    cancellation_resume: str
    auth_exhaustion: str
    headless_requirement: str


@dataclass(frozen=True)
class RuntimeAcceptance:
    runtime: str
    provider: str
    support: str
    headless_gate: str
    reason: str


ACCEPTANCE_MATRIX: tuple[StageAcceptance, ...] = (
    StageAcceptance(
        stage="planning",
        role="PLANNER",
        required_capabilities=("ADVANCED_REASONING",),
        structured_result="must emit executor envelope with validated ObjectivePlan in plan",
        cancellation_resume="lost planner execution is recovered by durable objective state and replanned",
        auth_exhaustion="AUTH_FAILURE and QUOTA_EXHAUSTED block or fail over without consuming task work",
        headless_requirement="only runtimes with a proven headless non-interactive result path are eligible",
    ),
    StageAcceptance(
        stage="coding",
        role="BUILDER",
        required_capabilities=("CODING",),
        structured_result="must emit executor envelope with builder fields such as feature_sha, tests, blockers",
        cancellation_resume="lost builder execution preserves checkpoints/worktree and resumes on a replacement worker",
        auth_exhaustion="auth failures block/fail over; retryable provider failures use bounded backoff and exhausted attempts create a typed blocker",
        headless_requirement="builder runtime must run in workspace-write headless mode and return a result",
    ),
    StageAcceptance(
        stage="review",
        role="REVIEWER",
        required_capabilities=("CODE_REVIEW",),
        structured_result="must emit executor envelope with a validated review verdict for the exact reviewed SHA",
        cancellation_resume="lost reviewer execution returns task to review-ready without spending build budget",
        auth_exhaustion="review auth/exhaustion/provider failures are isolated from implementation retry budgets",
        headless_requirement="review runtime must run read-only headlessly; GUI-only review is unsupported",
    ),
)

RUNTIME_ACCEPTANCE_MATRIX: tuple[RuntimeAcceptance, ...] = (
    RuntimeAcceptance(
        runtime="codex",
        provider="openai",
        support="SUPPORTED_WHEN_READY",
        headless_gate="live headless probe must answer with the expected result",
        reason="CLI profile exposes a non-interactive command and stdin prompt transport",
    ),
    RuntimeAcceptance(
        runtime="claude",
        provider="anthropic",
        support="SUPPORTED_WHEN_READY",
        headless_gate="live headless probe must answer with the expected result",
        reason="CLI profile exposes a non-interactive command and stdin prompt transport",
    ),
    RuntimeAcceptance(
        runtime="grok",
        provider="xai",
        support="SUPPORTED_WHEN_READY",
        headless_gate="live headless probe must answer with the expected result",
        reason="CLI profile exposes a non-interactive command and argv prompt transport",
    ),
    RuntimeAcceptance(
        runtime="antigravity",
        provider="google",
        support="UNSUPPORTED_GUI_ONLY",
        headless_gate="not eligible until a reliable headless automation path is proven",
        reason="current profile is GUI-only and returns without a durable structured result",
    ),
)


def acceptance_matrix_by_stage() -> dict[str, StageAcceptance]:
    return {entry.stage: entry for entry in ACCEPTANCE_MATRIX}


def runtime_matrix_by_runtime() -> dict[str, RuntimeAcceptance]:
    return {entry.runtime: entry for entry in RUNTIME_ACCEPTANCE_MATRIX}
