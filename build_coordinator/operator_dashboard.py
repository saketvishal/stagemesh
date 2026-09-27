"""Operator dashboard view derived from durable StageMesh state."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.config import get_settings
from build_coordinator.coordinator_config import load_coordinator_config
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveGate,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.objectives import list_objectives
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.service import ensure_state, list_available_tasks

LIVE_EXECUTION_STATUSES = frozenset({"LAUNCHED", "RUNNING"})
HUMAN_ATTENTION_TASK_STATES = frozenset({"WAITING_FOR_INPUT", "FAILED"})
PASSIVE_WAIT_TASK_STATES = frozenset({"AWAITING_EXTERNAL_CI"})
AUTONOMOUS_RECOVERY_TASK_STATES = frozenset({"RESUMABLE", "REWORK_REQUIRED", "STALE"})
AUTONOMOUS_RECOVERY_CLASSIFICATIONS = frozenset(
    {
        "RECOVERABLE_GIT_STATE",
        "RECOVERABLE_WORKTREE",
    }
)
EXHAUSTED_RECOVERY_CLASSIFICATIONS = frozenset(
    {
        "TASK_REDESIGN_REQUIRED",
        "REWORK_REQUIRED",
    }
)
HUMAN_ACTION_RECOVERY_CLASSIFICATIONS = frozenset({"OPERATOR_ACTION_REQUIRED"})

CONFIGURATION_ESCALATIONS = frozenset(
    {
        "AGENT_AUTHENTICATION_REQUIRED",
        "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED",
        "SETUP_FAILED",
        "WORKTREE_INVALID",
        "WORKING_CHECKOUT_DIRTY",
        "GIT_SAFETY_FAILURE",
    }
)
POLICY_ESCALATIONS = frozenset(
    {
        "ARCHITECTURE_DECISION_REQUIRED",
        "MIGRATION_SCOPE_VIOLATION",
        "SECURITY_POLICY_BLOCK",
        "SCOPE_EXPANSION_REQUIRED",
        "REMOTE_PUSH_APPROVAL_REQUIRED",
        "TEST_FAILURE_REQUIRES_JUDGMENT",
    }
)
EXHAUSTED_RECOVERY_ESCALATIONS = frozenset(
    {
        "EXECUTION_RETRY_LIMIT_REACHED",
        "MERGE_CONFLICT_RECOVERY_FAILED",
        "REMEDIATION_LIMIT_REACHED",
        "REVIEW_ENVIRONMENT_BLOCKED",
    }
)


def operator_dashboard(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the operator-facing control-plane payload.

    The payload intentionally contains links/ids for durable evidence instead of
    copying logs or result bodies into the UI view.
    """

    observed_at = now or datetime.now(UTC)
    runner_config = RunnerConfig.default(dry_run=True)
    diagnostics = BuildRunner(lambda: None, runner_config, executors={}).diagnostics(session)
    tasks = session.scalars(select(BuildTask).order_by(BuildTask.task_id)).all()
    executions = session.scalars(
        select(BuildRunnerExecution).order_by(BuildRunnerExecution.launched_at.desc())
    ).all()
    claims = session.scalars(select(BuildTaskClaim).where(BuildTaskClaim.status == "ACTIVE")).all()
    latest_events = _latest_events_by_task(session)
    latest_executions = _latest_executions_by_task(executions)
    objectives = list_objectives(session)

    return {
        "observed_at": observed_at.isoformat(),
        "mode": ensure_state(session).mode,
        "workspace": _workspace_payload(runner_config),
        "projects": _projects_payload(),
        "objectives": [_objective_payload(session, objective) for objective in objectives],
        "tasks": {
            "count": len(tasks),
            "by_state": dict(sorted(Counter(task.state for task in tasks).items())),
            "claimable_count": len(list_available_tasks(session)),
            "lifecycle": [_task_lifecycle_payload(task, latest_events, latest_executions) for task in tasks],
        },
        "fleet": {
            "providers": diagnostics["providers"],
            "workers": diagnostics["workers"],
            "runtimes": runner_config.public_summary()["runtimes"],
            "assignments": [_assignment_payload(claim) for claim in claims],
            "active_executions": [
                _execution_payload(row)
                for row in executions
                if row.status in LIVE_EXECUTION_STATUSES
            ],
            "configured_concurrency": diagnostics["configured_concurrency"],
            "executable_builders": diagnostics["executable_builders"],
            "has_configured_builders": diagnostics["has_configured_builders"],
        },
        "attention_queue": _attention_queue(
            session,
            tasks=tasks,
            executions=executions,
            latest_events=latest_events,
            latest_executions=latest_executions,
        ),
        "non_actionable": {
            "autonomous_recovery": [
                _task_queue_item(task, "autonomous_recoverable", latest_events, latest_executions)
                for task in tasks
                if _is_autonomous_recovery_task(task)
            ],
            "passive_waits": [
                _task_queue_item(task, "passive_wait", latest_events, latest_executions)
                for task in tasks
                if task.state in PASSIVE_WAIT_TASK_STATES
            ],
        },
    }


def _workspace_payload(runner_config: RunnerConfig) -> dict[str, Any]:
    settings = get_settings()
    coordinator_config = load_coordinator_config()
    result_dir = Path(runner_config.result_dir) if runner_config.result_dir else settings.data_dir / "results"
    return {
        "control_repo_root": str(settings.repo_root),
        "data_dir": str(settings.data_dir),
        "database_url": settings.database_url,
        "result_dir": str(result_dir),
        "log_artifact_dir": str(settings.data_dir / "execution-logs"),
        "project_roots": settings.project_roots,
        "worktrees": coordinator_config.worktrees,
    }


def _projects_payload() -> list[dict[str, Any]]:
    settings = get_settings()
    return [{"project_root": root} for root in settings.project_roots]


def _objective_payload(session: Session, objective: BuildObjective) -> dict[str, Any]:
    from build_coordinator.cli import _objective_summary

    return _objective_summary(session, objective)


def _latest_events_by_task(session: Session) -> dict[str, BuildTaskEvent]:
    rows = session.scalars(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id.is_not(None))
        .order_by(BuildTaskEvent.created_at.desc())
    ).all()
    latest: dict[str, BuildTaskEvent] = {}
    for row in rows:
        if row.task_id is not None:
            latest.setdefault(row.task_id, row)
    return latest


def _latest_executions_by_task(executions: list[BuildRunnerExecution]) -> dict[str, BuildRunnerExecution]:
    latest: dict[str, BuildRunnerExecution] = {}
    for row in executions:
        latest.setdefault(row.task_id, row)
    return latest


def _task_lifecycle_payload(
    task: BuildTask,
    latest_events: dict[str, BuildTaskEvent],
    latest_executions: dict[str, BuildRunnerExecution],
) -> dict[str, Any]:
    latest_execution = latest_executions.get(task.task_id)
    return {
        "task_id": task.task_id,
        "objective_id": task.objective_id,
        "title": task.title,
        "state": task.state,
        "review_policy": task.review_policy,
        "requires_integration": task.requires_integration,
        "retry_generation": task.retry_generation,
        "waiting_input": task.waiting_input,
        "review_remediation_validation_integration": _stage_state(task, latest_execution),
        "evidence": _evidence_links(task.task_id, latest_events, latest_execution),
    }


def _stage_state(task: BuildTask, latest_execution: BuildRunnerExecution | None) -> dict[str, Any]:
    return {
        "review": "active" if task.state == "REVIEWING" else ("ready" if task.state == "REVIEW_READY" else "idle"),
        "remediation": "needed" if task.state == "REWORK_REQUIRED" else "idle",
        "validation": "active" if task.state == "VALIDATING" else "idle",
        "integration": "active" if task.state in {"INTEGRATING", "AWAITING_EXTERNAL_CI"} else "idle",
        "latest_execution_status": latest_execution.status if latest_execution else None,
        "latest_execution_role": latest_execution.role if latest_execution else None,
    }


def _attention_queue(
    session: Session,
    *,
    tasks: list[BuildTask],
    executions: list[BuildRunnerExecution],
    latest_events: dict[str, BuildTaskEvent],
    latest_executions: dict[str, BuildRunnerExecution],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for gate in session.scalars(select(BuildObjectiveGate).where(BuildObjectiveGate.status == "OPEN")).all():
        queue.append(
            {
                "kind": "human_gate",
                "category": "human_action_required",
                "objective_id": gate.objective_id,
                "task_id": gate.source_task_id,
                "reason": gate.reason,
                "gate_id": gate.gate_id,
                "gate_type": gate.gate_type,
                "evidence": {"gate_id": gate.gate_id, "objective_id": gate.objective_id},
            }
        )

    for task in tasks:
        if _task_needs_human_attention(task):
            queue.append(_task_queue_item(task, _task_attention_category(task), latest_events, latest_executions))

    for execution in executions:
        if execution.status == "HUMAN_ACTION_REQUIRED":
            queue.append(_execution_attention_item(execution))

    return sorted(queue, key=lambda item: (item.get("task_id") or "", item.get("kind") or ""))


def _is_autonomous_recovery_task(task: BuildTask) -> bool:
    if task.state in AUTONOMOUS_RECOVERY_TASK_STATES:
        return True
    return task.state == "BLOCKED" and _task_recovery_classification(task) in AUTONOMOUS_RECOVERY_CLASSIFICATIONS


def _task_needs_human_attention(task: BuildTask) -> bool:
    if task.state in HUMAN_ATTENTION_TASK_STATES:
        return True
    if task.state != "BLOCKED":
        return False
    recovery_classification = _task_recovery_classification(task)
    if recovery_classification in AUTONOMOUS_RECOVERY_CLASSIFICATIONS:
        return False
    return recovery_classification in (
        HUMAN_ACTION_RECOVERY_CLASSIFICATIONS | EXHAUSTED_RECOVERY_CLASSIFICATIONS
    ) or _task_waiting_type(task) in (
        CONFIGURATION_ESCALATIONS | POLICY_ESCALATIONS | EXHAUSTED_RECOVERY_ESCALATIONS
    )


def _task_attention_category(task: BuildTask) -> str:
    recovery_classification = _task_recovery_classification(task)
    if recovery_classification in EXHAUSTED_RECOVERY_CLASSIFICATIONS:
        return "exhausted_automated_recovery"
    waiting_type = _task_waiting_type(task)
    if waiting_type in CONFIGURATION_ESCALATIONS:
        return "credentials_or_configuration"
    if waiting_type in POLICY_ESCALATIONS:
        return "unresolved_policy_decision"
    if waiting_type in EXHAUSTED_RECOVERY_ESCALATIONS:
        return "exhausted_automated_recovery"
    if task.state == "WAITING_FOR_INPUT":
        return "human_action_required"
    if task.state == "FAILED":
        return "exhausted_automated_recovery"
    return "human_action_required"


def _task_waiting_type(task: BuildTask) -> str:
    return str((task.waiting_input or {}).get("type") or "").upper()


def _task_recovery_classification(task: BuildTask) -> str:
    failure_evidence = (task.waiting_input or {}).get("failure_evidence") or {}
    if not isinstance(failure_evidence, dict):
        return ""
    return str(failure_evidence.get("recovery_classification") or "").upper()


def _task_queue_item(
    task: BuildTask,
    category: str,
    latest_events: dict[str, BuildTaskEvent],
    latest_executions: dict[str, BuildRunnerExecution],
) -> dict[str, Any]:
    reason = (task.waiting_input or {}).get("question") or (task.waiting_input or {}).get("reason")
    event = latest_events.get(task.task_id)
    if not reason and event is not None:
        reason = (event.event_data or {}).get("reason")
    return {
        "kind": "task",
        "category": category,
        "task_id": task.task_id,
        "objective_id": task.objective_id,
        "state": task.state,
        "reason": reason,
        "evidence": _evidence_links(task.task_id, latest_events, latest_executions.get(task.task_id)),
    }


def _execution_attention_item(execution: BuildRunnerExecution) -> dict[str, Any]:
    escalation = str(execution.human_escalation_type or "")
    if escalation in CONFIGURATION_ESCALATIONS:
        category = "credentials_or_configuration"
    elif escalation in POLICY_ESCALATIONS:
        category = "unresolved_policy_decision"
    elif escalation in EXHAUSTED_RECOVERY_ESCALATIONS:
        category = "exhausted_automated_recovery"
    else:
        category = "human_action_required"
    return {
        "kind": "execution",
        "category": category,
        "task_id": execution.task_id,
        "role": execution.role,
        "worker_id": execution.worker_id,
        "provider": execution.provider,
        "reason": execution.human_escalation_type,
        "evidence": _execution_evidence(execution),
    }


def _assignment_payload(claim: BuildTaskClaim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "task_id": claim.task_id,
        "claim_type": claim.claim_type,
        "worker_id": claim.worker_id,
        "provider": claim.provider,
        "lease_expires_at": claim.lease_expires_at.isoformat(),
        "worktree_path": claim.worktree_path,
        "branch_name": claim.branch_name,
    }


def _execution_payload(row: BuildRunnerExecution) -> dict[str, Any]:
    return {
        "execution_id": row.execution_id,
        "task_id": row.task_id,
        "role": row.role,
        "worker_id": row.worker_id,
        "provider": row.provider,
        "adapter": row.adapter,
        "status": row.status,
        "evidence": _execution_evidence(row),
    }


def _evidence_links(
    task_id: str,
    latest_events: dict[str, BuildTaskEvent],
    latest_execution: BuildRunnerExecution | None,
) -> dict[str, Any]:
    event = latest_events.get(task_id)
    evidence: dict[str, Any] = {}
    if event is not None:
        evidence["latest_event_id"] = event.event_id
        evidence["latest_event_type"] = event.event_type
    if latest_execution is not None:
        evidence.update(_execution_evidence(latest_execution))
    return evidence


def _execution_evidence(row: BuildRunnerExecution) -> dict[str, Any]:
    evidence: dict[str, Any] = {"execution_id": row.execution_id}
    if row.result_path:
        evidence["result_path"] = row.result_path
    if row.worktree_path:
        evidence["worktree_path"] = row.worktree_path
    if row.branch_name:
        evidence["branch_name"] = row.branch_name
    return evidence
