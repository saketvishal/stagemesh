"""Idempotent ingestion of authorized GitHub issues into coordinator objectives."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.github.client import GitHubClient, GitHubIssue
from build_coordinator.github.sanitizer import (
    sanitize_issue_to_spec,
    validate_repository_name,
)
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildObjectivePlan,
    BuildRunnerExecution,
    BuildTask,
)
from build_coordinator.objectives import (
    create_objective,
    objective_work_tasks,
    resume_objective,
)
from build_coordinator.planner import planner_task_id
from build_coordinator.service import transition_task

logger = logging.getLogger(__name__)

def ingest_github_issue(
    session: Session,
    client: GitHubClient,
    repo: str,
    issue: GitHubIssue,
) -> tuple[BuildObjective, bool]:
    """Ingest a GitHub issue idempotently.

    Returns (objective, is_new).
    If the objective already exists, returns (existing_objective, False) without re-planning
    or duplicating tasks.
    """
    validated_repo = validate_repository_name(repo)
    spec = sanitize_issue_to_spec(
        validated_repo,
        issue.number,
        issue.title,
        issue.body,
        labels=list(issue.labels),
    )

    existing = session.get(BuildObjective, spec.objective_id)
    if existing is not None:
        repaired = _repair_goal_if_unplanned(session, existing, spec)
        _requeue_stale_failed_planner_if_unplanned(session, existing)
        if repaired and existing.state == "PAUSED":
            resume_objective(session, existing.objective_id, actor="github_reconciler")
        session.commit()
        return existing, False

    objective = create_objective(session, spec)

    session.add(
        BuildObjectiveEvent(
            objective_id=objective.objective_id,
            event_type="github.issue_ingested",
            actor=f"gh:{issue.author}",
            event_data={
                "repo": validated_repo,
                "issue_number": issue.number,
                "author": issue.author,
                "html_url": issue.html_url,
                "title": issue.title,
            },
        )
    )
    session.commit()

    # Synchronize initial state to GitHub
    try:
        client.set_issue_status_label(repo, issue.number, "PLANNING")
        client.add_issue_comment(
            repo,
            issue.number,
            f"🤖 **StageMesh Autonomous Build Coordinator** ingested this issue as Objective `{objective.objective_id}`.\n\n"
            f"- **Target Scope**: `{validated_repo}`\n"
            f"- **Initial State**: `PLANNING`\n"
            f"- **Authoritative Datastore**: Coordinator SQLite (`{objective.objective_id}`)\n\n"
            "Autonomous lifecycle active: Planner will determine child tasks and allocation.",
        )
    except Exception as exc:
        logger.warning("failed to notify GitHub on issue ingestion: %s", exc)

    return objective, True


def _repair_goal_if_unplanned(
    session: Session, objective: BuildObjective, spec: Any
) -> bool:
    """Re-sync an objective's goal text from the current, corrected GitHub
    issue body.

    Handles operator mistakes such as accidentally pasting the issue-creation
    command itself into the issue body instead of the intended objective
    text: once the issue body is fixed on GitHub, a later poll must pick up
    the correction without requiring a second issue or a manual DB edit.

    An objective's goal is otherwise canonical once ingested: a later edit
    to the GitHub issue text must never silently rewrite an ACTIVE
    objective's scope (that would let anyone with issue-edit access
    scope-creep in-flight work). Repair is therefore limited to objectives
    an operator has explicitly taken out of automatic reconciliation via
    `objective pause` -- e.g. because the ingested goal was recognized as
    malformed -- and that have not yet had a plan applied or any work
    tasks, so it can never change the scope of already-planned or
    in-flight work either.
    """
    if objective.state != "PAUSED":
        return False
    if objective.goal == spec.goal:
        return False
    if objective_work_tasks(session, objective.objective_id):
        return False
    existing_plan = session.scalar(
        select(BuildObjectivePlan)
        .where(BuildObjectivePlan.objective_id == objective.objective_id)
        .order_by(BuildObjectivePlan.version.desc())
    )
    if existing_plan is not None:
        return False

    old_goal_excerpt = objective.goal[:200]
    objective.goal = spec.goal
    session.add(
        BuildObjectiveEvent(
            objective_id=objective.objective_id,
            event_type="github.objective_goal_repaired",
            actor="github_reconciler",
            event_data={"old_goal_excerpt": old_goal_excerpt},
        )
    )
    return True


def _requeue_stale_failed_planner_if_unplanned(session: Session, objective: BuildObjective) -> bool:
    """Retry stale planner contract failures after canonical GitHub reconciliation.

    This intentionally does not reset objectives that already have an applied
    plan, work tasks, open business gates, or a non-stale operational blocker.
    """
    if objective.state != "PLANNING":
        return False
    if objective_work_tasks(session, objective.objective_id):
        return False
    existing_plan = session.scalar(
        select(BuildObjectivePlan)
        .where(BuildObjectivePlan.objective_id == objective.objective_id)
        .order_by(BuildObjectivePlan.version.desc())
    )
    if existing_plan is not None:
        return False

    planner = session.get(BuildTask, planner_task_id(objective.objective_id))
    if planner is None or planner.state != "BLOCKED":
        return False

    latest = session.scalar(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == planner.task_id)
        .where(BuildRunnerExecution.role == "PLANNER")
        .order_by(BuildRunnerExecution.completed_at.desc())
    )
    if latest is None or not _is_stale_planner_contract_failure(latest.result_data, latest.human_escalation_type):
        return False

    transition_task(
        session,
        planner.task_id,
        "READY",
        actor="github_reconciler",
        reason="stale planner contract failure requeued for current executor schema",
    )
    session.add(
        BuildObjectiveEvent(
            objective_id=objective.objective_id,
            event_type="github.orphaned_objective_planner_requeued",
            actor="github_reconciler",
            event_data={
                "task_id": planner.task_id,
                "execution_id": latest.execution_id,
                "reason": "stale planner contract failure",
            },
        )
    )
    return True


def _is_stale_planner_contract_failure(result_data: Any, human_escalation_type: str | None) -> bool:
    if human_escalation_type and human_escalation_type != "COORDINATOR_INVARIANT_FAILURE":
        return False
    if not isinstance(result_data, dict):
        return False
    text = " ".join(str(value) for value in result_data.values())
    stale_markers = (
        "unsupported executor result schema_version",
        "planner result missing plan",
    )
    return any(marker in text for marker in stale_markers)
def poll_and_ingest_issues(
    session: Session,
    client: GitHubClient,
    repo: str = "saketvishal/stagemesh",
    *,
    label: str = "build:objective",
) -> list[BuildObjective]:
    """Poll GitHub for authorized issues and ingest any newly opened ones."""
    issues = client.get_authorized_issues(repo, label=label, state="open")
    ingested: list[BuildObjective] = []
    for issue in issues:
        obj, is_new = ingest_github_issue(session, client, repo, issue)
        if is_new:
            ingested.append(obj)
    return ingested
