"""Durable, recoverable upstream delivery.

A task whose integration succeeded locally but whose push failed is BLOCKED with
UPSTREAM_PUSH_FAILED. The merge commit and evidence stay durable; delivery is
retried here (automatically on every `continue`, or on demand) and only a
verified push completes the task."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.events import record_event
from build_coordinator.execution.git_integrator import push_branch
from build_coordinator.models import BuildRunnerExecution, BuildTask
from build_coordinator.runner.worktree import cleanup_task_branch
from build_coordinator.service import transition_task
from build_coordinator.types import EventInput


def pending_pushes(session: Session) -> list[BuildTask]:
    tasks = session.scalars(select(BuildTask).where(BuildTask.state == "BLOCKED")).all()
    pending = []
    for task in tasks:
        execution = _last_integration(session, task.task_id)
        if execution is not None and execution.human_escalation_type == "UPSTREAM_PUSH_FAILED":
            pending.append(task)
    return pending


def _last_integration(session: Session, task_id: str) -> BuildRunnerExecution | None:
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == task_id)
        .where(BuildRunnerExecution.role == "INTEGRATION")
        .order_by(BuildRunnerExecution.launched_at.desc())
    ).all()
    return rows[0] if rows else None


def retry_pending_pushes(
    session: Session,
    *,
    repo_root: Any,
    remote: str,
    main_ref: str,
    cleanup: bool = True,
    actor: str = "runner",
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for task in pending_pushes(session):
        execution = _last_integration(session, task.task_id)
        merged = (execution.result_data or {}).get("merge_commit_sha") if execution else None
        if not merged:
            outcomes.append({"task_id": task.task_id, "pushed": False, "detail": "no merge commit recorded"})
            continue
        ok, detail = push_branch(repo_root, remote, merged, main_ref)
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="runner.push_retry",
                actor=actor,
                event_data={"pushed": ok, "merge_commit_sha": merged, "detail": detail},
            ),
        )
        if ok:
            transition_task(session, task.task_id, "REVIEW_READY", actor=actor, reason="upstream push recovered")
            transition_task(session, task.task_id, "DONE", actor=actor, reason="upstream push verified")
            if cleanup and task.branch_name:
                cleanup_task_branch(repo_root, task.branch_name, main_ref=main_ref, reviewed_sha=execution.reviewed_feature_sha)
        outcomes.append({"task_id": task.task_id, "pushed": ok, "detail": detail})
    return outcomes
