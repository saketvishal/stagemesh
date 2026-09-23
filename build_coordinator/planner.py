"""Planner output adapter.

The planner is an executor *role* that uses the existing WorkerExecutor
launch/poll/result-file machinery. It may only propose a structured
ObjectivePlan. It never writes coordinator rows -- `objectives.py` applies
a plan only after this module validates it.

Unknown fields, worktree selection, remote-main push authorization, and
review-policy weakening all fail closed.
"""

from __future__ import annotations

from typing import Any

from build_coordinator.types import (
    PLANNER_ALLOWED_REVIEW_POLICIES,
    ObjectivePlan,
    StructuredContractError,
)


PLANNER_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "execution_id",
        "task_id",
        "role",
        "status",
        "completed_at",
        "plan",
        "human_escalation_type",
        "objective_signal",
    }
)


class PlannerUnavailable(LookupError):
    """No planner executor is configured. The objective stays PLANNING and
    is retried on the next run cycle -- not a scope decision."""


def parse_planner_plan(data: Any, *, source: str = "PLANNER") -> ObjectivePlan:
    """Schema-validate planner output, then enforce planner policy.

    `data` is the `plan` object (or a list of child tasks). Extra keys on
    the surrounding executor result are stripped by sanitize_result_mapping
    / parse_executor_result before this is called.
    """
    plan = ObjectivePlan.from_mapping(data, source=source)
    enforce_planner_policy(plan)
    return plan


def enforce_planner_policy(plan: ObjectivePlan) -> None:
    """Planner output may request typed human gates; it may not authorize
    them, choose worktrees, weaken review, or push main."""
    for task in plan.tasks:
        if task.review_policy not in PLANNER_ALLOWED_REVIEW_POLICIES:
            raise StructuredContractError(
                f"planner cannot set review_policy={task.review_policy!r} on {task.task_id}; "
                "independent review remains required"
            )
    # requested_human_gates are already type-checked by ObjectivePlan.
    # Opening them is a stop, not an authorization.


def planner_task_id(objective_id: str) -> str:
    suffix = "-PLANNER"
    if len(objective_id) + len(suffix) <= 80:
        return f"{objective_id}{suffix}"
    return f"{objective_id[: 80 - len(suffix)]}{suffix}"
