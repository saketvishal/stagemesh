"""GitHub state synchronization for build-coordinator objectives."""

from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.github.client import GitHubClient
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildTask,
)
from build_coordinator.objectives import (
    get_planner_task,
    objective_work_tasks,
    open_gates,
)
from build_coordinator.task_source.github import check_objective_fully_delivered

logger = logging.getLogger(__name__)


def compute_objective_github_status(
    session: Session, objective: BuildObjective, repo: str | None = None
) -> str:
    """Derive the human-meaningful status of an objective for GitHub reporting."""
    if open_gates(session, objective.objective_id):
        return "HUMAN_GATE"

    if objective.state == "PLANNING":
        planner = get_planner_task(session, objective.objective_id)
        if planner is not None and planner.state == "BLOCKED":
            return "BLOCKED"
        return "PLANNING"
    if objective.state in {"FAILED", "ABORTED"}:
        return "FAILED"
    if objective.state in {"COMPLETED", "DONE"}:
        # #87: never report GitHub-visible "DONE" unless the completion side
        # effect (label apply / issue close) has actually succeeded -- an
        # internally COMPLETED objective whose delivery is still pending/failed
        # must stay in a retryable, non-terminal status.
        if not check_objective_fully_delivered(session, objective.objective_id, repo=repo):
            return "REMEDIATING"
        return "DONE"
    if objective.state == "PAUSED":
        return "BLOCKED"

    work_tasks = objective_work_tasks(session, objective.objective_id)
    if not work_tasks:
        return "QUEUED"

    states = {t.state for t in work_tasks}

    if "REWORK_REQUIRED" in states:
        return "REMEDIATING"
    if "REVIEWING" in states or "REVIEW_READY" in states:
        return "REVIEWING"
    if all(s == "DONE" for s in states):
        return "DONE"
    if any(s in {"IN_PROGRESS", "CLAIMED"} for s in states):
        return "IN_PROGRESS"
    if "BLOCKED" in states:
        return "BLOCKED"
    if "FAILED" in states:
        return "FAILED"

    return "IN_PROGRESS"


def sync_objective_status_to_github(
    session: Session,
    client: GitHubClient,
    repo: str,
    issue_number: int,
    objective: BuildObjective,
) -> str | None:
    """Synchronize meaningful state transitions to GitHub without heartbeat noise."""
    status = compute_objective_github_status(session, objective, repo)

    # Get last reported status
    last_event = session.scalar(
        select(BuildObjectiveEvent)
        .where(BuildObjectiveEvent.objective_id == objective.objective_id)
        .where(BuildObjectiveEvent.event_type == "github.status_synced")
        .order_by(BuildObjectiveEvent.created_at.desc())
    )

    last_status = last_event.event_data.get("status") if (last_event and last_event.event_data) else None

    if status == last_status:
        return None  # No change -> no heartbeat noise

    try:
        client.set_issue_status_label(repo, issue_number, status)
        client.add_issue_comment(
            repo,
            issue_number,
            f"🔄 **StageMesh Lifecycle Update**: Status changed to `{status}`\n\n"
            f"- **Objective**: `{objective.objective_id}`\n"
            f"- **Lifecycle State**: `{objective.state}`\n"
            f"- **Tasks Recorded**: {len(objective_work_tasks(session, objective.objective_id))}\n",
        )
    except Exception as exc:
        logger.warning("failed to update GitHub status for %s: %s", objective.objective_id, exc)

    session.add(
        BuildObjectiveEvent(
            objective_id=objective.objective_id,
            event_type="github.status_synced",
            actor="github_sync",
            event_data={"status": status, "issue_number": issue_number, "repo": repo},
        )
    )
    session.commit()
    return status
