"""State and transition policy for the Build Coordinator."""

from __future__ import annotations

from build_coordinator.models import COORDINATOR_MODES, REVIEW_POLICIES, TASK_STATES

CLAIMABLE_STATES = frozenset({"READY", "STALE", "RESUMABLE", "REWORK_REQUIRED"})

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "READY": frozenset({"CLAIMED", "BLOCKED", "FAILED"}),
    "CLAIMED": frozenset({"IN_PROGRESS", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "STALE", "RESUMABLE"}),
    "IN_PROGRESS": frozenset({"VALIDATING", "WAITING_FOR_INPUT", "BLOCKED", "FAILED", "STALE", "RESUMABLE"}),
    "WAITING_FOR_INPUT": frozenset({"IN_PROGRESS", "RESUMABLE", "BLOCKED", "FAILED"}),
    "VALIDATING": frozenset({"REVIEW_READY", "DONE", "REWORK_REQUIRED", "FAILED"}),
    "REVIEW_READY": frozenset({"REVIEWING", "REWORK_REQUIRED", "DONE", "FAILED", "BLOCKED"}),
    "REVIEWING": frozenset(
        {"DONE", "REWORK_REQUIRED", "BLOCKED", "FAILED", "REVIEW_READY", "INTEGRATING"}
    ),
    "INTEGRATING": frozenset(
        {"DONE", "BLOCKED", "FAILED", "REVIEW_READY", "REVIEWING", "AWAITING_EXTERNAL_CI"}
    ),
    "AWAITING_EXTERNAL_CI": frozenset({"DONE", "REWORK_REQUIRED", "BLOCKED"}),
    "REWORK_REQUIRED": frozenset({"CLAIMED", "BLOCKED", "FAILED"}),
    "BLOCKED": frozenset({"READY", "RESUMABLE", "FAILED", "REVIEW_READY", "REVIEWING", "DONE", "REWORK_REQUIRED"}),
    "FAILED": frozenset({"RESUMABLE", "READY"}),
    "STALE": frozenset({"RESUMABLE", "CLAIMED", "FAILED", "READY", "BLOCKED"}),
    "RESUMABLE": frozenset({"CLAIMED", "BLOCKED", "FAILED"}),
    "DONE": frozenset(),
}


class CoordinatorPolicyError(ValueError):
    """Raised when a requested coordinator operation violates policy."""


class CoordinatorCapacityError(CoordinatorPolicyError):
    """Raised when active builder capacity is reached."""


def require_valid_mode(mode: str) -> None:
    if mode not in COORDINATOR_MODES:
        raise CoordinatorPolicyError(f"Invalid coordinator mode: {mode}")


def require_valid_task_state(state: str) -> None:
    if state not in TASK_STATES:
        raise CoordinatorPolicyError(f"Invalid task state: {state}")


def require_valid_review_policy(review_policy: str) -> None:
    if review_policy not in REVIEW_POLICIES:
        raise CoordinatorPolicyError(f"Invalid review policy: {review_policy}")


def can_transition(from_state: str, to_state: str) -> bool:
    require_valid_task_state(from_state)
    require_valid_task_state(to_state)
    return to_state in VALID_TRANSITIONS[from_state]


def require_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise CoordinatorPolicyError(f"Invalid task transition: {from_state} -> {to_state}")


def review_required(review_policy: str) -> bool:
    require_valid_review_policy(review_policy)
    return review_policy in {"SELF", "INDEPENDENT", "TWO_REVIEWERS"}


def independent_review_required(review_policy: str) -> bool:
    require_valid_review_policy(review_policy)
    return review_policy in {"INDEPENDENT", "TWO_REVIEWERS"}
