"""State and transition policy for the Build Coordinator."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class ReviewPolicySpec:
    """Deterministic review governance semantics.

    `required_approvals` is the count of eligible GREEN/GREEN_WITH_NOTES
    approvals required for the exact feature SHA. Worker/provider independence
    are deliberately separate so cross-worker and cross-provider governance are
    not conflated.
    """

    policy: str
    required_approvals: int
    independent_worker: bool = False
    independent_provider: bool = False


def normalize_review_policy(review_policy: str) -> str:
    value = str(review_policy).strip().upper()
    if value == "INDEPENDENT":
        return "INDEPENDENT_WORKER"
    return value


def review_policy_spec(review_policy: str) -> ReviewPolicySpec:
    policy = normalize_review_policy(review_policy)
    require_valid_review_policy(policy)
    specs = {
        "NONE": ReviewPolicySpec("NONE", 0),
        "SELF": ReviewPolicySpec("SELF", 1),
        "INDEPENDENT_WORKER": ReviewPolicySpec(
            "INDEPENDENT_WORKER", 1, independent_worker=True
        ),
        "INDEPENDENT_PROVIDER": ReviewPolicySpec(
            "INDEPENDENT_PROVIDER", 1, independent_provider=True
        ),
        "TWO_REVIEWERS": ReviewPolicySpec(
            "TWO_REVIEWERS", 2, independent_worker=True
        ),
        "TWO_PROVIDERS": ReviewPolicySpec(
            "TWO_PROVIDERS", 2, independent_provider=True
        ),
    }
    return specs[policy]


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
    return review_policy_spec(review_policy).required_approvals > 0


def independent_review_required(review_policy: str) -> bool:
    spec = review_policy_spec(review_policy)
    return spec.independent_worker or spec.independent_provider
