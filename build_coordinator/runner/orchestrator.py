"""Deterministic orchestration loop for coordinator-managed work."""

from __future__ import annotations

import dataclasses

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from build_coordinator.config import get_settings
from build_coordinator.events import record_event
from build_coordinator.execution.base import (
    ExecutionLaunch,
    ExecutionObservation,
    WorkerExecutor,
)
from build_coordinator.execution.fake import FakeExecutor
from build_coordinator.execution.results import (
    RESULT_STATUSES,
    ExecutorResultError,
    parse_executor_result,
    result_file_contract_for_role,
)
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
from build_coordinator.models import (
    BuildObjective,
    BuildRunnerExecution,
    TASK_STATES,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
    BuildWorkerLease,
    new_uuid,
)
from build_coordinator.objectives import (
    apply_validated_plan,
    get_objective,
    get_planner_task,
    objective_source_is_executable,
    is_planner_task,
    list_objectives,
    objective_dependencies_satisfied,
    open_gates,
    record_planner_failed,
    record_planner_unavailable,
    resolve_gate,
    run_objective_cycle,
)
from build_coordinator.planner import parse_planner_plan
from build_coordinator.policy import CoordinatorCapacityError, CoordinatorPolicyError, review_policy_spec
from build_coordinator.prompts import (
    BuilderPromptBuilder,
    IntegrationPromptBuilder,
    PlannerPromptBuilder,
    RemediationPromptBuilder,
    ReviewerPromptBuilder,
)
from build_coordinator.runner.git_safety import (
    GitBackend,
    GitSafetyError,
    RealGit,
    assess_mechanical_merge,
    capture_feature_sha,
)
from build_coordinator.runner.findings import (
    comprehensive_reconciliation_missing_ids,
    escalation_evidence as finding_escalation_evidence,
    open_findings,
    record_convergence_generation,
    reconcile_findings,
)
from build_coordinator.runner.models import (
    ReviewVerdict,
    ReviewVerdictContradiction,
    RunnerConfig,
    WorkerConfig,
)
from build_coordinator.runner.routing import (
    _naive,
    ProviderConfig,
    PROVIDER_FAILURES,
    RETRYABLE_PROVIDER_FAILURES,
    RoutingDecision,
    StageRequirement,
    approving_providers,
    approving_reviewers,
    execution_evidence_by_worker,
    merge_worker_evidence,
    reviewer_exclusions,
    role_to_stage,
    route_worker,
)
from build_coordinator.project.backlog import load_backlog, persist_delivery_evidence_in_history, task_priorities
from build_coordinator.project.definition import find_project_root, load_project
from build_coordinator.execution.git_integrator import GitIntegrationExecutor
from build_coordinator.runner.ci_reconciliation import reconcile_awaiting_ci
from build_coordinator.runner.validation import (
    ValidationExecutor,
    ValidationOutcome,
    run_validation,
    validation_environment_fingerprint,
)
from build_coordinator.runner.worktree import (
    cleanup_task_branch,
    WorktreeValidationError,
    ensure_worktree,
    prepare_task_workspace,
    task_branch_name,
    validate_worktree_path,
    reconcile_displaced_task_work,
    preserve_unknown_operator_work,
    _ref_exists,
    _git,
)
from build_coordinator.runner.clone_pool import (
    ensure_repo_clone,
    expected_remote_url,
    repo_identity,
    sync_task_branch,
    verify_clone_remote,
)
from build_coordinator.runner.git_safety import resolve_git_identity_args
from build_coordinator.claims import CLAIMABLE_STATES, task_source_is_executable
from build_coordinator.runner.scheduling import (
    SCHEDULER_REASONS,
    active_implementation_tasks,
    active_worker_counts,
    active_workers,
    active_worktrees,
    check_task_readiness,
    launch_counts,
    normalize_worktree_path,
    record_task_withheld,
    sort_tasks_for_dispatch,
)
from build_coordinator.runner.steward import run_steward_cycle
from build_coordinator.service import (
    ClaimRequest,
    checkpoint,
    claim_integration,
    claim_review,
    claim_task,
    ensure_state,
    get_max_active_builders,
    get_resume_context,
    list_available_tasks,
    reconcile_stale_executions,
    recover_expired,
    recover_lost_execution_claims,
    release_worker_leases_for_execution,
    release_active_claims,
    request_task_input,
    transition_task,
    _task_branch_has_real_work,
)
from build_coordinator.types import CheckpointInput, EventInput


TERMINAL_EXECUTION_STATUSES = RESULT_STATUSES
LIVE_EXECUTION_STATUSES = {"LAUNCHED", "RUNNING"}
EXECUTION_STATUSES = LIVE_EXECUTION_STATUSES | set(TERMINAL_EXECUTION_STATUSES)


@dataclass
class RunnerCycleResult:
    mode: str
    recovered: list[str] = field(default_factory=list)
    launched: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    capacity_full: bool = False
    objectives_reconciled: list[str] = field(default_factory=list)
    objective_follow_ups_created: list[str] = field(default_factory=list)
    objective_unrelated_tasks_created: list[str] = field(default_factory=list)
    objective_gates_raised: list[str] = field(default_factory=list)
    objectives_completed: list[str] = field(default_factory=list)
    outbound_synced: list[str] = field(default_factory=list)
    scheduling_reasons: dict[str, str] = field(default_factory=dict)
    provider_state_changes: list[dict[str, Any]] = field(default_factory=list)
    steward: dict[str, Any] = field(default_factory=dict)


class BuildRunner:
    def __init__(
        self,
        session_factory,
        config: RunnerConfig,
        *,
        executors: dict[str, WorkerExecutor] | None = None,
        git: GitBackend | None = None,
        task_source: Any = None,
        target_task_ids: set[str] | frozenset[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._executors = executors or {}
        self._executor_workers: dict[str, WorkerConfig] = {
            w.worker_id: w for w in self._config.workers if w.worker_id in self._executors
        }
        self._git = git if git is not None else RealGit()
        self._settings = get_settings()
        self._task_source = task_source
        self._target_task_ids = frozenset(str(task_id) for task_id in (target_task_ids or ()))
        self._validation_executor = ValidationExecutor(
            timeout_seconds=self._config.validation_timeout_seconds
        )

    def reload_config(
        self,
        config: RunnerConfig,
        *,
        live_worker_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Apply fresh routing/worker settings without disturbing live runs."""
        live_worker_ids = live_worker_ids or set()
        previous = {worker.worker_id: worker for worker in self._config.workers}
        current = {worker.worker_id: worker for worker in config.workers}
        for worker_id, executor in list(self._executors.items()):
            if worker_id in live_worker_ids:
                continue
            active_worker = self._executor_workers.get(worker_id) or previous.get(worker_id)
            if active_worker != current.get(worker_id):
                terminate = getattr(executor, "terminate_all", None)
                if callable(terminate):
                    terminate()
                self._executors.pop(worker_id, None)
                self._executor_workers.pop(worker_id, None)
        self._config = config
        self._settings = get_settings()

    def run_forever(self) -> None:
        while True:
            result = self.run_once()
            if result.mode == "DRAINING" and not result.launched and not result.observed:
                return
            time.sleep(self._config.poll_seconds)

    def run_once(self) -> RunnerCycleResult:
        with self._session_factory() as session:
            result = self._run_once(session)
            session.commit()
            return result

    def _run_once(self, session: Session) -> RunnerCycleResult:
        state = ensure_state(session)
        result = RunnerCycleResult(mode=state.mode)
        if self._task_source is not None and state.mode != "PAUSED":
            try:
                self._task_source.discover_tasks(session)
            except Exception as exc:
                record_event(
                    session,
                    EventInput(
                        task_id=None,
                        event_type="runner.task_source_sync_error",
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
        recovered_tasks = recover_expired(session, actor="runner")
        result.recovered = [task.task_id for task in recovered_tasks]
        reconcile_stale_executions(session)
        for task in recover_lost_execution_claims(session, actor="runner"):
            if task.task_id not in result.recovered:
                result.recovered.append(task.task_id)
        self._reconcile_git_reality(session, result)
        self._reconcile_provider_capacity_transitions(session, result)
        self._recover_diagnosed_blockers(session, result)
        self._run_steward_maintenance(session, result)
        result.observed = self._reconcile_active(session, result)
        for task in recover_lost_execution_claims(session, actor="runner"):
            if task.task_id not in result.recovered:
                result.recovered.append(task.task_id)
        self._kill_reconciled_process_trees(session)
        if state.mode == "PAUSED":
            return result
        self._dispatch_validation(session, result)
        self._dispatch_reviews(session, result)
        if state.mode == "RUNNING":
            self._dispatch_planners(session, result)
            self._dispatch_builders(session, result)
        self._dispatch_integration(session, result)
        self._reconcile_awaiting_external_ci(session, result)
        self._reconcile_objectives(session, result)
        if self._task_source is not None:
            self._sync_outbound(session, result)
        return result

    def _run_steward_maintenance(self, session: Session, result: RunnerCycleResult) -> None:
        steward = run_steward_cycle(
            session,
            workers=self._config.workers,
            config=self._config.steward,
        )
        if steward.skipped_reason == "DISABLED":
            return
        result.steward = steward.to_dict()
        for task_id in steward.recovered_tasks:
            if task_id not in result.recovered:
                result.recovered.append(task_id)

    def _reconcile_awaiting_external_ci(self, session: Session, result: RunnerCycleResult) -> None:
        """#65: non-blocking. A PENDING observation here leaves tasks in
        AWAITING_EXTERNAL_CI (which holds no worker capacity) and simply
        returns, so the rest of this cycle -- and the next -- keeps
        dispatching other independent READY work normally."""
        if not self._config.external_ci_enabled or not self._config.external_ci_repo:
            return
        outcomes = reconcile_awaiting_ci(
            session,
            repo=self._config.external_ci_repo,
            max_consecutive_errors=self._config.external_ci_max_consecutive_errors,
            delivery_evidence_recorder=lambda task_id, sha: self._persist_project_delivery_evidence(
                session,
                task_id,
                sha,
                push_required=True,
            ),
        )
        for outcome in outcomes:
            if outcome["status"] in {"DONE", "SUCCESS"}:
                result.observed.append(outcome["task_id"])

    def _target_allows(self, task_id: str) -> bool:
        return not self._target_task_ids or task_id in self._target_task_ids

    def _sync_outbound(self, session: Session, result: RunnerCycleResult | None = None) -> None:
        if self._task_source is None:
            return

        # 1. Sync completed objectives
        try:
            completed_objectives = session.scalars(
                select(BuildObjective).where(BuildObjective.state == "COMPLETED")
            ).all()
            sync_obj_fn = getattr(self._task_source, "sync_objective_outbound", None)
            if callable(sync_obj_fn):
                for obj in completed_objectives:
                    if not self._target_allows(obj.objective_id):
                        continue
                    planner_task = get_planner_task(session, obj.objective_id)
                    if planner_task is not None and not task_source_is_executable(planner_task):
                        continue
                    evidence = self._collect_objective_evidence(session, obj.objective_id)
                    synced = sync_obj_fn(
                        session,
                        obj.objective_id,
                        "COMPLETED",
                        evidence=evidence,
                    )
                    if synced and result is not None:
                        result.outbound_synced.append(obj.objective_id)
        except Exception as exc:
            record_event(
                session,
                EventInput(
                    task_id=None,
                    event_type="runner.objective_source_sync_error",
                    actor="runner",
                    event_data={"error": str(exc)},
                ),
            )

        # 2. Sync task lifecycle labels. GitHub lifecycle labels are mutually
        # exclusive, so non-DONE states need outbound reconciliation too; a
        # reopened issue may still carry stagemesh:done from an earlier close.
        try:
            lifecycle_tasks = session.scalars(
                select(BuildTask).where(BuildTask.state.in_(TASK_STATES))
            ).all()
            for task in lifecycle_tasks:
                if not self._target_allows(task.task_id):
                    continue
                # If this task represents an objective issue itself, skip task-level sync
                if session.get(BuildObjective, task.task_id) is not None:
                    continue
                if not task_source_is_executable(task):
                    continue
                evidence = self._collect_task_evidence(session, task.task_id)
                synced = self._task_source.sync_outbound(
                    session,
                    task.task_id,
                    task.state,
                    evidence=evidence,
                )
                if synced and result is not None:
                    result.outbound_synced.append(task.task_id)
        except Exception as exc:
            record_event(
                session,
                EventInput(
                    task_id=None,
                    event_type="runner.task_source_sync_error",
                    actor="runner",
                    event_data={"error": str(exc)},
                ),
            )

    def _collect_objective_evidence(self, session: Session, objective_id: str) -> dict[str, Any]:
        obj = session.get(BuildObjective, objective_id)
        if obj is None:
            return {}
        return {
            "objective_id": obj.objective_id,
            "goal": obj.goal,
            "completion_criteria": list(obj.completion_criteria or []),
        }

    def _collect_task_evidence(self, session: Session, task_id: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        task = session.get(BuildTask, task_id)
        if task is None:
            return evidence

        checkpoint = session.scalars(
            select(BuildTaskCheckpoint)
            .where(BuildTaskCheckpoint.task_id == task_id)
            .order_by(BuildTaskCheckpoint.created_at.desc())
        ).first()
        if checkpoint:
            if checkpoint.worker_id:
                evidence["worker_id"] = checkpoint.worker_id
            if checkpoint.claim_id:
                evidence["claim_id"] = checkpoint.claim_id
            if checkpoint.current_head_sha:
                evidence["feature_sha"] = checkpoint.current_head_sha
            if checkpoint.completed_work:
                evidence["summary"] = "; ".join(checkpoint.completed_work)

        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .order_by(BuildRunnerExecution.launched_at.desc())
        ).all()
        for exc in executions:
            rdata = exc.result_data or {}
            if not evidence.get("worker_id") and (exc.worker_id or rdata.get("worker_id")):
                evidence["worker_id"] = exc.worker_id or rdata.get("worker_id")
            if not evidence.get("claim_id") and exc.claim_id:
                evidence["claim_id"] = exc.claim_id
            if not evidence.get("feature_sha"):
                sha = rdata.get("feature_sha") or rdata.get("integrated_sha") or rdata.get("head_sha")
                if sha:
                    evidence["feature_sha"] = sha
            if not evidence.get("review_verdict") and exc.role == "REVIEWER":
                rev = rdata.get("review") or rdata
                verdict = rev.get("verdict") or rev.get("status")
                if verdict:
                    evidence["review_verdict"] = verdict
            if not evidence.get("summary") and rdata.get("summary"):
                evidence["summary"] = rdata.get("summary")

        if not evidence.get("review_verdict"):
            rev_event = session.scalars(
                select(BuildTaskEvent)
                .where(
                    BuildTaskEvent.task_id == task_id,
                    BuildTaskEvent.event_type.in_(("review.completed", "runner.review_completed")),
                )
                .order_by(BuildTaskEvent.created_at.desc())
            ).first()
            if rev_event and rev_event.event_data:
                verdict = (
                    rev_event.event_data.get("verdict")
                    or rev_event.event_data.get("review_verdict")
                    or (rev_event.event_data.get("review") or {}).get("verdict")
                )
                if verdict:
                    evidence["review_verdict"] = verdict

        if not evidence.get("summary") and task.description:
            evidence["summary"] = task.description[:200]

        return evidence

    def _reconcile_objectives(self, session: Session, result: RunnerCycleResult) -> None:
        for summary in run_objective_cycle(session):
            result.objectives_reconciled.append(summary.objective_id)
            result.objective_follow_ups_created.extend(summary.follow_ups_created)
            result.objective_unrelated_tasks_created.extend(summary.unrelated_tasks_created)
            result.objective_gates_raised.extend(summary.gates_raised)
            if summary.completed:
                result.objectives_completed.append(summary.objective_id)

    def _reconcile_active(self, session: Session, result: RunnerCycleResult) -> list[str]:
        active = session.scalars(
            select(BuildRunnerExecution).where(
                BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING"))
            )
        ).all()
        observed = []
        for row in active:
            executor = self._executor_for_execution(row)
            observation = _sanitize_observation(executor.poll(row.execution_id))
            row.exit_code = observation.exit_code
            row.last_observed_at = _now()
            if observation.status in LIVE_EXECUTION_STATUSES:
                row.status = observation.status
                row.human_escalation_type = observation.human_escalation_type
                if observation.result_data:
                    row.result_data = {**(row.result_data or {}), **observation.result_data}
            else:
                self._apply_terminal_result(session, row, result, observation)
                self._release_worker_lease(session, row.execution_id)
            observed.append(row.execution_id)
        return observed

    def _apply_terminal_result(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        observation: ExecutionObservation,
    ) -> None:
        raw_result = observation.result_data if observation.result_data is not None else execution.result_data
        if execution.adapter == "validation":
            self._apply_validation_result(session, execution, result, observation)
            return
        if observation.human_escalation_type == "COORDINATOR_INVARIANT_FAILURE":
            execution.status = (
                observation.status if observation.status in TERMINAL_EXECUTION_STATUSES else "FAILED"
            )
            execution.human_escalation_type = "COORDINATOR_INVARIANT_FAILURE"
            execution.result_data = {**(execution.result_data or {}), **(raw_result or {})}
            execution.completed_at = _now()
            result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
            self._block_task(
                session,
                execution.task_id,
                "COORDINATOR_INVARIANT_FAILURE",
                invariant="COORDINATOR_INVARIANT_OBSERVATION",
                execution=execution,
                error=(raw_result or {}).get("error") if isinstance(raw_result, dict) else None,
            )
            return
        if observation.status in {"TERMINATED", "LOST"}:
            existing = execution.result_data or {}
            execution.status = observation.status
            execution.human_escalation_type = observation.human_escalation_type
            execution.completed_at = _now()
            execution.result_data = {
                **existing,
                **(raw_result or {}),
                "reconciliation_state": existing.get("reconciliation_state")
                or ("LOST" if observation.status == "LOST" else "TERMINATED"),
            }
            if execution.claim_id:
                try:
                    checkpoint(
                        session,
                        execution.claim_id,
                        worker_id=execution.worker_id,
                        data=CheckpointInput(
                            current_step=f"{execution.role.lower()} execution {observation.status.lower()}",
                            known_failures=[f"execution {observation.status.lower()} after observation"],
                        ),
                    )
                except CoordinatorPolicyError:
                    pass
            return
        parsed = None
        merged = {**(execution.result_data or {}), **(raw_result or {})}
        if observation.status == "WAITING_FOR_INPUT":
            question = ""
            if isinstance(merged.get("waiting_for_input"), dict):
                question = str(merged["waiting_for_input"].get("question") or "")
            question = question or str(merged.get("question") or "required input")
            execution.status = "WAITING_FOR_INPUT"
            execution.result_data = merged
            execution.completed_at = _now()
            request_task_input(
                session,
                execution.task_id,
                question,
                actor=execution.worker_id,
                claim_id=execution.claim_id,
            )
            return
        if observation.status == "HUMAN_ACTION_REQUIRED":
            try:
                parsed = parse_executor_result(
                    merged,
                    execution_id=execution.execution_id,
                    task_id=execution.task_id,
                    role=execution.role,
                    reviewed_feature_sha=execution.reviewed_feature_sha,
                    require_identity=execution.adapter != "fake",
                )
            except (ExecutorResultError, ReviewVerdictContradiction) as exc:
                execution.status = "FAILED"
                execution.result_data = {
                    **(execution.result_data or {}),
                    "error": str(exc),
                }
                execution.human_escalation_type = "MALFORMED_EXECUTOR_RESULT"
                execution.completed_at = _now()
                result.escalations.append(f"{execution.task_id}:MALFORMED_EXECUTOR_RESULT")
                self._block_task(
                    session,
                    execution.task_id,
                    "MALFORMED_EXECUTOR_RESULT",
                    invariant="EXECUTOR_RESULT_CONTRACT",
                    execution=execution,
                    error=str(exc),
                    recovery_classification="REWORK_REQUIRED",
                )
                return
            escalation = (
                parsed.human_escalation_type
                or observation.human_escalation_type
                or "COORDINATOR_INVARIANT_FAILURE"
            )
            execution.status = "HUMAN_ACTION_REQUIRED"
            execution.human_escalation_type = escalation
            execution.result_data = parsed.persisted
            execution.completed_at = _now()
            result.escalations.append(f"{execution.task_id}:{escalation}")
            self._block_task(session, execution.task_id, escalation, execution=execution)
            return
        if observation.status == "SUCCEEDED":
            try:
                parsed = parse_executor_result(
                    merged,
                    execution_id=execution.execution_id,
                    task_id=execution.task_id,
                    role=execution.role,
                    reviewed_feature_sha=execution.reviewed_feature_sha,
                    require_identity=execution.adapter != "fake",
                )
            except (ExecutorResultError, ReviewVerdictContradiction) as exc:
                execution.status = "FAILED"
                execution.result_data = {
                    **(execution.result_data or {}),
                    "error": str(exc),
                }
                execution.human_escalation_type = "MALFORMED_EXECUTOR_RESULT"
                execution.completed_at = _now()
                result.escalations.append(f"{execution.task_id}:MALFORMED_EXECUTOR_RESULT")
                if execution.role == "PLANNER":
                    planner_task = session.get(BuildTask, execution.task_id)
                    if planner_task is not None and planner_task.objective_id:
                        record_planner_failed(
                            session,
                            get_objective(session, planner_task.objective_id),
                            reason=f"planner output failed validation: {exc}",
                        )
                self._block_task(
                    session,
                    execution.task_id,
                    "MALFORMED_EXECUTOR_RESULT",
                    invariant="EXECUTOR_RESULT_CONTRACT",
                    execution=execution,
                    error=str(exc),
                    recovery_classification="REWORK_REQUIRED",
                )
                return
            execution.status = "SUCCEEDED"
            execution.result_data = parsed.persisted
            execution.human_escalation_type = observation.human_escalation_type
            execution.completed_at = _now()
            if execution.role in {"BUILDER", "REMEDIATION"}:
                self._builder_succeeded(session, execution, result, parsed)
            elif execution.role == "REVIEWER":
                self._review_succeeded(session, execution, result, parsed)
            elif execution.role == "INTEGRATION":
                self._integration_succeeded(session, execution, result, parsed)
            elif execution.role == "PLANNER":
                self._planner_succeeded(session, execution, result, parsed)
            return
        if observation.status == "FAILED" and self._recoverable_failure(session, execution, result, merged, observation):
            return
        if observation.status == "FAILED":
            execution.status = "FAILED"
            execution.human_escalation_type = observation.human_escalation_type
            execution.result_data = merged
            execution.completed_at = _now()
            if execution.claim_id:
                try:
                    checkpoint(
                        session,
                        execution.claim_id,
                        worker_id=execution.worker_id,
                        data=CheckpointInput(
                            current_step=f"{execution.role.lower()} process failed",
                            known_failures=[f"process exit code {execution.exit_code}"],
                        ),
                    )
                except CoordinatorPolicyError:
                    pass
            return
        execution.status = "FAILED"
        execution.human_escalation_type = "INVALID_EXECUTOR_STATUS"
        execution.result_data = {
            **merged,
            "error": f"invalid executor result status: {observation.status}",
        }
        execution.completed_at = _now()
        result.escalations.append(f"{execution.task_id}:INVALID_EXECUTOR_STATUS")
        self._block_task(
            session,
            execution.task_id,
            "INVALID_EXECUTOR_STATUS",
            invariant="EXECUTOR_STATUS_CONTRACT",
            execution=execution,
            error=f"invalid executor result status: {observation.status}",
            recovery_classification="OPERATOR_ACTION_REQUIRED",
        )

    def _builder_succeeded(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        parsed,
    ) -> None:
        builder = parsed.builder if parsed is not None else None
        if builder and builder.scope_expansion_required:
            result.escalations.append(f"{execution.task_id}:SCOPE_EXPANSION_REQUIRED")
            self._block_task(
                session,
                execution.task_id,
                "SCOPE_EXPANSION_REQUIRED",
                execution=execution,
                error="Scope expansion required",
            )
            return
        if builder and builder.blockers:
            blocker_list = list(builder.blockers)
            if blocker_list == ["the agent produced no changes on the task branch"]:
                task = session.get(BuildTask, execution.task_id)
                retry_generation = int((task.retry_generation if task is not None else 0) or 0)
                outcome = self._reconcile_no_changes_produced(
                    session,
                    execution,
                    result,
                    detail="Agent produced no changes on task branch",
                    retry_generation=retry_generation,
                )
                if outcome != "UNRESOLVED":
                    return
                # Check if existing task branch already has useful commits ahead of base
                repo_root = Path(self._settings.repo_root)
                branch = task.branch_name or task_branch_name(task.task_id) if task else None
                has_work = False
                if branch and _ref_exists(repo_root, branch):
                    base = task.base_sha or self._config.main_ref if task else self._config.main_ref
                    ahead_proc = _git(repo_root, "rev-list", "--count", f"{base}..{branch}")
                    if ahead_proc.returncode == 0 and int(ahead_proc.stdout.strip() or 0) > 0:
                        has_work = True
                if has_work:
                    transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
                    transition_task(session, execution.task_id, "VALIDATING", actor="runner")
                    if self._validation_gate(session, task, execution, result):
                        transition_task(
                            session,
                            execution.task_id,
                            "REVIEW_READY" if task and task.review_policy != "NONE" else "DONE",
                            actor="runner",
                            reason="task branch contains commits; proceeding to review",
                        )
                    return

                retry_generation = int((task.retry_generation if task is not None else 0) or 0)
                attempts = session.scalar(
                    select(func.count())
                    .select_from(BuildRunnerExecution)
                    .where(BuildRunnerExecution.task_id == execution.task_id)
                    .where(BuildRunnerExecution.role.in_(("BUILDER", "REMEDIATION")))
                    .where(BuildRunnerExecution.adapter != "validation")
                    .where(BuildRunnerExecution.status.notin_(("LOST", "TERMINATED")))
                    .where(
                        or_(
                            BuildRunnerExecution.result_data["retry_generation"].as_integer() == retry_generation,
                            BuildRunnerExecution.result_data["retry_generation"].as_integer().is_(None)
                            if retry_generation == 0
                            else False,
                        )
                    )
                ) or 0
                if attempts < self._config.max_execution_attempts:
                    self._record_no_changes_produced(
                        session,
                        execution,
                        retry_generation=retry_generation,
                        attempt=attempts,
                        detail="Agent produced no changes on task branch",
                    )
                    release_active_claims(session, execution.task_id, completed=False)
                    target_state = "RESUMABLE" if task and task.state in ("CLAIMED", "IN_PROGRESS") else "READY"
                    transition_task(
                        session,
                        execution.task_id,
                        target_state,
                        actor="runner",
                        reason=f"agent produced no changes; retrying with alternate worker/provider (attempt {attempts}/{self._config.max_execution_attempts})",
                    )
                    return
                result.escalations.append(f"{execution.task_id}:NO_CHANGES_PRODUCED")
                self._block_task(
                    session,
                    execution.task_id,
                    "NO_CHANGES_PRODUCED",
                    invariant="BUILDER_COMMIT_CONTRACT",
                    execution=execution,
                    error="Agent produced no changes on task branch and acceptance criteria not met",
                    recovery_classification="REWORK_REQUIRED",
                )
                return
            result.escalations.append(f"{execution.task_id}:BUILDER_BLOCKER")
            self._block_task(
                session,
                execution.task_id,
                "BUILDER_BLOCKER",
                invariant="BUILDER_BLOCKER",
                execution=execution,
                error="; ".join(blocker_list),
                recovery_classification="OPERATOR_ACTION_REQUIRED",
                extra_data={"blockers": blocker_list},
            )
            return
        feature_sha = builder.feature_sha if builder else execution.result_data.get("feature_sha")
        task = session.get(BuildTask, execution.task_id)
        assigned_branch = execution.branch_name or (task.branch_name if task else None)
        if assigned_branch and not feature_sha:
            result.escalations.append(f"{execution.task_id}:BUILDER_COMMIT_CONTRACT")
            self._block_task(
                session,
                execution.task_id,
                "BUILDER_COMMIT_CONTRACT",
                invariant="BUILDER_COMMIT_CONTRACT",
                execution=execution,
                error="Builder result for an assigned task branch did not provide a reviewable feature commit",
                recovery_classification="REWORK_REQUIRED",
            )
            return
        conflict_rec = (
            (task.waiting_input or {}).get("conflict_recovery")
            if task is not None and isinstance(task.waiting_input, dict)
            else None
        )
        if conflict_rec and isinstance(conflict_rec, dict):
            resolved_sha = feature_sha
            original_sha = str(conflict_rec.get("task_sha") or "")
            if not resolved_sha or resolved_sha == original_sha:
                result.escalations.append(f"{execution.task_id}:MERGE_CONFLICT_RECOVERY_FAILED")
                self._block_task(
                    session,
                    execution.task_id,
                    "MERGE_CONFLICT_RECOVERY_FAILED",
                    invariant="MERGE_CONFLICT_RECOVERY_FAILED",
                    execution=execution,
                    error="conflict recovery did not produce a distinct conflict-resolved feature SHA",
                    recovery_classification="OPERATOR_ACTION_REQUIRED",
                    extra_data={
                        "conflict_recovery": conflict_rec,
                        "conflict_resolved_sha": resolved_sha,
                    },
                )
                return
            waiting = dict(task.waiting_input or {})
            updated_conflict = dict(conflict_rec)
            updated_conflict.update(
                {
                    "original_reviewed_sha": original_sha,
                    "conflict_resolved_sha": resolved_sha,
                    "old_review_non_authoritative": True,
                    "requires_exact_sha_rereview": True,
                    "recovery_worker": execution.worker_id,
                    "recovery_provider": execution.provider,
                }
            )
            waiting["conflict_recovery"] = updated_conflict
            task.waiting_input = waiting
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.merge_conflict_recovery_resolved",
                    actor="runner",
                    claim_id=execution.claim_id,
                    event_data=updated_conflict,
                ),
            )
        if execution.claim_id:
            checkpoint(
                session,
                execution.claim_id,
                worker_id=execution.worker_id,
                data=CheckpointInput(
                    current_step="runner observed builder success",
                    current_head_sha=feature_sha,
                    completed_work=["external builder process completed"],
                    files_changed=list(builder.files_changed) if builder else execution.result_data.get("files_changed") or [],
                    commits_created=list(builder.commits_created) if builder else execution.result_data.get("commits_created") or [],
                    last_successful_tests=list(builder.tests.items) if builder else execution.result_data.get("tests") or [],
                ),
            )
        transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
        transition_task(session, execution.task_id, "VALIDATING", actor="runner")
        if not self._validation_gate(session, task, execution, result):
            return
        if conflict_rec and isinstance(conflict_rec, dict) and task is not None:
            waiting = dict(task.waiting_input or {})
            updated_conflict = dict(waiting.get("conflict_recovery") or conflict_rec)
            updated_conflict["validation_completed_for_sha"] = updated_conflict.get("conflict_resolved_sha")
            waiting["conflict_recovery"] = updated_conflict
            task.waiting_input = waiting
        transition_task(
            session,
            execution.task_id,
            "REVIEW_READY" if task and task.review_policy != "NONE" else "DONE",
            actor="runner",
        )

    def _validation_gate(
        self,
        session: Session,
        task: BuildTask | None,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
    ) -> bool:
        """Run the task's validation commands in its workspace; the outcome, not
        an agent's claim, decides whether the task may proceed."""
        commands = list(task.required_validation or []) if task else []
        if not commands or not self._config.run_validation:
            return True
        cwd = execution.worktree_path or self._git_cwd_for_execution(execution)
        self._launch_validation(
            session,
            result,
            task=task,
            source_execution=execution,
            commands=commands,
            cwd=str(cwd),
            next_state="REVIEW_READY" if task and task.review_policy != "NONE" else "DONE",
            success_reason=None,
        )
        return False

    def _launch_validation(
        self,
        session: Session,
        result: RunnerCycleResult,
        *,
        task: BuildTask | None,
        source_execution: BuildRunnerExecution,
        commands: list[str],
        cwd: str,
        next_state: str,
        success_reason: str | None,
    ) -> None:
        if self._active_validation(session, source_execution.task_id) is not None:
            return
        execution_id = new_uuid()
        feature_sha = (source_execution.result_data or {}).get("feature_sha") or source_execution.reviewed_feature_sha
        validated_sha = feature_sha or self._current_head_sha(cwd)
        env_fingerprint = validation_environment_fingerprint()
        context = {
            "task_id": source_execution.task_id,
            "source_execution_id": source_execution.execution_id,
            "validated_sha": validated_sha,
            "workspace": str(cwd),
            "commands": commands,
            "environment_fingerprint": env_fingerprint,
        }
        validation_context_hash = hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        handle = self._validation_executor.launch(
            ExecutionLaunch(
                task_id=source_execution.task_id,
                role="BUILDER",
                worker_id="runner-validation",
                provider="runner",
                worktree_path=cwd,
                branch_name=source_execution.branch_name,
                prompt="",
                execution_id=execution_id,
                reviewed_feature_sha=validated_sha,
                metadata={"commands": commands},
            )
        )
        row = BuildRunnerExecution(
            execution_id=handle.execution_id,
            task_id=source_execution.task_id,
            role="BUILDER",
            worker_id="runner-validation",
            provider="runner",
            adapter="validation",
            claim_id=source_execution.claim_id,
            worktree_path=cwd,
            branch_name=source_execution.branch_name,
            process_id=handle.process_id,
            reviewed_feature_sha=validated_sha,
            prompt_hash=validation_context_hash,
            status="LAUNCHED",
            launched_at=_now(),
            result_data={
                "commands": commands,
                "workspace": str(cwd),
                "source_execution_id": source_execution.execution_id,
                "feature_sha": feature_sha,
                "validated_sha": validated_sha,
                "environment_fingerprint": env_fingerprint,
                "validation_context_hash": validation_context_hash,
                "started_at": _now().isoformat(),
                "next_state": next_state,
                "success_reason": success_reason,
            },
        )
        session.add(row)
        record_event(
            session,
            EventInput(
                task_id=source_execution.task_id,
                event_type="runner.validation_launched",
                actor="runner",
                claim_id=source_execution.claim_id,
                event_data={
                    "workspace": str(cwd),
                    "commands": commands,
                    "source_execution_id": source_execution.execution_id,
                    "validation_execution_id": handle.execution_id,
                    "validated_sha": validated_sha,
                    "environment_fingerprint": env_fingerprint,
                    "validation_context_hash": validation_context_hash,
                },
            ),
        )
        result.launched.append(handle.execution_id)

    def _active_validation(self, session: Session, task_id: str) -> BuildRunnerExecution | None:
        return session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.adapter == "validation")
            .where(BuildRunnerExecution.status.in_(LIVE_EXECUTION_STATUSES))
            .limit(1)
        )

    def _dispatch_validation(self, session: Session, result: RunnerCycleResult) -> None:
        tasks = session.scalars(select(BuildTask).where(BuildTask.state == "VALIDATING")).all()
        for task in tasks:
            if not self._target_allows(task.task_id):
                continue
            if self._active_validation(session, task.task_id) is not None:
                continue
            source = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == task.task_id)
                .where(BuildRunnerExecution.role.in_(("BUILDER", "REMEDIATION")))
                .where(BuildRunnerExecution.adapter != "validation")
                .where(BuildRunnerExecution.status == "SUCCEEDED")
                .order_by(BuildRunnerExecution.completed_at.desc())
            ).first()
            if source is None:
                continue
            commands = list(task.required_validation or [])
            if not commands or not self._config.run_validation:
                transition_task(
                    session,
                    task.task_id,
                    "REVIEW_READY" if task.review_policy != "NONE" else "DONE",
                    actor="runner",
                )
                continue
            cwd = source.worktree_path or task.worktree_path or self._git_cwd_for_execution(source)
            self._launch_validation(
                session,
                result,
                task=task,
                source_execution=source,
                commands=commands,
                cwd=str(cwd),
                next_state="REVIEW_READY" if task.review_policy != "NONE" else "DONE",
                success_reason=None,
            )

    def _apply_validation_result(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        observation: ExecutionObservation,
    ) -> None:
        merged = {**(execution.result_data or {}), **(observation.result_data or {})}
        execution.exit_code = observation.exit_code
        execution.human_escalation_type = observation.human_escalation_type
        execution.completed_at = _now()
        merged.setdefault("ended_at", execution.completed_at.isoformat())
        if observation.status in {"LOST", "TERMINATED"}:
            execution.status = observation.status
            execution.result_data = {
                **merged,
                "reconciliation_state": "LOST" if observation.status == "LOST" else "TERMINATED",
                "validation_terminal_type": merged.get("validation_terminal_type")
                or ("LOST" if observation.status == "LOST" else "CANCELLED"),
            }
            return
        passed = observation.status == "SUCCEEDED" and bool(merged.get("passed", True))
        current_sha = self._current_head_sha(str(merged.get("workspace") or execution.worktree_path or ""))
        if (
            passed
            and _looks_like_git_sha(merged.get("validated_sha"))
            and _looks_like_git_sha(current_sha)
            and current_sha != merged.get("validated_sha")
        ):
            passed = False
            stale_result = {
                "command": "<validation-context>",
                "exit_code": 1,
                "failure_type": "STALE_VALIDATION_CONTEXT",
                "started_at": merged.get("started_at"),
                "completed_at": merged.get("ended_at"),
                "duration_seconds": 0,
                "output_tail": (
                    "validation completed for "
                    f"{merged.get('validated_sha')} but workspace is now {current_sha}"
                )[-2000:],
            }
            merged["results"] = [*(merged.get("results") or []), stale_result]
            merged["validation_terminal_type"] = "STALE_VALIDATION_CONTEXT"
            merged["passed"] = False
        execution.status = "SUCCEEDED" if passed else "FAILED"
        execution.result_data = merged
        outcome = ValidationOutcome(
            passed=passed,
            results=list(merged.get("results") or []),
        )
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.validation",
                actor="runner",
                claim_id=execution.claim_id,
                event_data={
                    "passed": passed,
                    "workspace": str(merged.get("workspace") or execution.worktree_path),
                    "execution_id": execution.execution_id,
                    "feature_sha": merged.get("feature_sha") or execution.reviewed_feature_sha,
                    "validated_sha": merged.get("validated_sha") or execution.reviewed_feature_sha,
                    "environment_fingerprint": merged.get("environment_fingerprint"),
                    "validation_context_hash": merged.get("validation_context_hash") or execution.prompt_hash,
                    "started_at": merged.get("started_at"),
                    "ended_at": merged.get("ended_at"),
                    "validation_terminal_type": merged.get("validation_terminal_type"),
                    "results": outcome.results,
                },
            ),
        )
        if execution.claim_id:
            checkpoint(
                session,
                execution.claim_id,
                worker_id=execution.worker_id,
                data=CheckpointInput(
                    current_step="runner validation " + ("passed" if outcome.passed else "failed"),
                    last_successful_tests=[r["command"] for r in outcome.results if r["exit_code"] == 0],
                    known_failures=outcome.failure_summary(),
                ),
            )
        if outcome.passed:
            task = session.get(BuildTask, execution.task_id)
            transition_task(
                session,
                execution.task_id,
                str(merged.get("next_state") or ("REVIEW_READY" if task and task.review_policy != "NONE" else "DONE")),
                actor="runner",
                reason=merged.get("success_reason") or None,
            )
            conflict_rec = (
                (task.waiting_input or {}).get("conflict_recovery")
                if task is not None and isinstance(task.waiting_input, dict)
                else None
            )
            if conflict_rec and isinstance(conflict_rec, dict) and task is not None:
                waiting = dict(task.waiting_input or {})
                updated_conflict = dict(waiting.get("conflict_recovery") or conflict_rec)
                updated_conflict["validation_completed_for_sha"] = updated_conflict.get("conflict_resolved_sha")
                waiting["conflict_recovery"] = updated_conflict
                task.waiting_input = waiting
            return
        if self._remediation_cycles(session, execution.task_id) >= self._config.max_remediation_cycles:
            result.escalations.append(f"{execution.task_id}:REMEDIATION_LIMIT_REACHED")
            transition_task(
                session, execution.task_id, "REWORK_REQUIRED", actor="runner", reason="deterministic validation failed"
            )
            self._block_task(session, execution.task_id, "REMEDIATION_LIMIT_REACHED")
            return
        transition_task(
            session,
            execution.task_id,
            "REWORK_REQUIRED",
            actor="runner",
            reason="deterministic validation failed",
        )

    def _git_cwd_for_execution(self, execution: BuildRunnerExecution) -> str:
        return str(self._settings.repo_root)

    def _current_head_sha(self, cwd: str | Path) -> str | None:
        if not cwd:
            return None
        proc = _git(Path(cwd), "rev-parse", "HEAD")
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _review_succeeded(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        parsed,
    ) -> None:
        try:
            if parsed is not None and parsed.reviewer is not None:
                verdict = parsed.reviewer.verdict
            else:
                verdict = ReviewVerdict.from_mapping(
                    execution.result_data.get("review") or execution.result_data
                )
                verdict.validate_consistency()
            eligible = verdict.integration_eligible()
        except ReviewVerdictContradiction as exc:
            result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
            self._block_task(
                session,
                execution.task_id,
                "COORDINATOR_INVARIANT_FAILURE",
                invariant="REVIEW_VERDICT_CONTRADICTION",
                execution=execution,
                error=str(exc),
                recovery_classification="OPERATOR_ACTION_REQUIRED",
            )
            return
        registry = (
            self._reconcile_review_findings(session, execution, verdict)
            if verdict.verdict != "REVIEW_ENVIRONMENT_BLOCKED"
            else {}
        )
        if registry.get("comprehensive_incomplete_ids"):
            self._review_protocol_blocked(
                session,
                execution,
                verdict,
                result,
                reason="COMPREHENSIVE_REVIEW_MISSING_DISPOSITIONS",
                why_not_remediation=(
                    "The comprehensive convergence review did not return an "
                    "explicit disposition for every previously open finding "
                    f"({', '.join(registry['comprehensive_incomplete_ids'])}), so "
                    "there is no evidence it is safe to reconcile them; "
                    "retrying the review rather than clearing the registry."
                ),
                transition_reason=(
                    "review protocol blocked: comprehensive convergence review "
                    "did not reconcile all prior open findings"
                ),
                error="Comprehensive convergence review omitted required finding dispositions",
            )
            return
        blockers = [entry["description"] for entry in open_findings(registry)] if registry.get("entries") else list(verdict.findings)
        if execution.claim_id:
            checkpoint(
                session,
                execution.claim_id,
                worker_id=execution.worker_id,
                data=CheckpointInput(
                    current_step=f"review verdict: {verdict.verdict}",
                    completed_work=["independent review completed"],
                    remaining_work=list(verdict.required_remediation),
                    decisions=list(verdict.architecture_notes),
                    blockers=[] if eligible else blockers,
                ),
            )
        if eligible and self._needs_second_reviewer(session, execution):
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.review_approval_recorded",
                    actor="runner",
                    event_data={
                        "reviewer": execution.worker_id,
                        "reviewed_feature_sha": execution.reviewed_feature_sha,
                        "approvals": sorted(
                            approving_reviewers(
                                session,
                                execution.task_id,
                                reviewed_feature_sha=execution.reviewed_feature_sha,
                            )
                        ),
                        "provider_approvals": sorted(
                            approving_providers(
                                session,
                                execution.task_id,
                                reviewed_feature_sha=execution.reviewed_feature_sha,
                            )
                        ),
                    },
                ),
            )
            transition_task(
                session,
                execution.task_id,
                "REVIEW_READY",
                actor="runner",
                reason="TWO_REVIEWERS: awaiting a second independent approval",
            )
            return
        if eligible:
            release_active_claims(session, execution.task_id, completed=True)
            task = session.get(BuildTask, execution.task_id)
            if task is not None:
                task.current_claim_id = None
                task.lease_expires_at = None
                task.last_heartbeat_at = None
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.integration_eligible",
                    actor="runner",
                    event_data=execution.result_data,
                ),
            )
            return
        if verdict.verdict == "REMEDIATION_REQUIRED":
            if not verdict.findings and not verdict.finding_dispositions:
                self._review_protocol_blocked(session, execution, verdict, result)
                return
            if registry.get("entries") and not open_findings(registry):
                self._review_protocol_blocked(
                    session,
                    execution,
                    verdict,
                    result,
                    reason="REMEDIATION_REQUIRED_WITHOUT_OPEN_FINDINGS",
                    why_not_remediation=(
                        "Review finding_dispositions closed all tracked findings, "
                        "so there is no currently open finding for a remediation "
                        "worker to target."
                    ),
                    transition_reason=(
                        "review protocol blocked: remediation required without open findings"
                    ),
                    error="Reviewer requested remediation after closing all tracked findings",
                )
                return
            if self._review_disagreement_deadlocked(session, execution, registry, result):
                return
            if self._request_comprehensive_convergence_review(session, execution, registry, result):
                return
            if self._remediation_limit_reached(session, execution, verdict, result):
                return
            transition_task(
                session,
                execution.task_id,
                "REWORK_REQUIRED",
                actor="runner",
                reason="structured review requires remediation",
            )
            return
        if verdict.verdict == "REVIEW_ENVIRONMENT_BLOCKED":
            release_active_claims(session, execution.task_id, completed=False)
            task = session.get(BuildTask, execution.task_id)
            if task is not None:
                task.current_claim_id = None
                task.lease_expires_at = None
                task.last_heartbeat_at = None
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.review_environment_blocked",
                    actor="runner",
                    event_data={
                        "reviewer": execution.worker_id,
                        "reviewed_feature_sha": execution.reviewed_feature_sha,
                        "verdict": verdict.verdict,
                        "findings": list(verdict.findings),
                        "architecture_notes": list(verdict.architecture_notes),
                    },
                ),
            )
            attempts = self._review_environment_attempts(session, execution.task_id)
            if attempts >= self._config.max_review_environment_attempts:
                result.escalations.append(f"{execution.task_id}:REVIEW_ENVIRONMENT_BLOCKED")
                self._block_task(session, execution.task_id, "REVIEW_ENVIRONMENT_BLOCKED")
                return
            transition_task(
                session,
                execution.task_id,
                "REVIEW_READY",
                actor="runner",
                reason=f"review environment blocked on {execution.worker_id}; retrying review",
            )
            return
        result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
        self._block_task(
            session,
            execution.task_id,
            "COORDINATOR_INVARIANT_FAILURE",
            invariant="UNRECOGNIZED_REVIEW_VERDICT",
            execution=execution,
            error=f"unrecognized review verdict: {verdict.verdict}",
            recovery_classification="OPERATOR_ACTION_REQUIRED",
        )

    def _review_disagreement_deadlocked(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        registry: dict,
        result: RunnerCycleResult,
    ) -> bool:
        """Fail closed when independent reviewers disagree on a finding.

        A registry disagreement means at least two reviewers classified the
        same durable finding into opposite categories (open vs closed). That
        is reviewer deadlock, not meaningful unresolved builder work, so do
        not spend remediation budget or ask a builder to re-fix a disputed
        item without human tie-break evidence.
        """
        disputed = [
            entry
            for entry in open_findings(registry)
            if isinstance(entry.get("disagreement"), dict)
        ]
        if not disputed:
            return False
        evidence = {
            "reason": "REVIEWER_DISAGREEMENT_DEADLOCK",
            "classification": "reviewer_disagreement",
            "reviewer": execution.worker_id,
            "reviewed_feature_sha": execution.reviewed_feature_sha,
            "open_findings": [
                {
                    "id": entry.get("id"),
                    "description": entry.get("description"),
                    "attempts": entry.get("attempts"),
                    "disagreement": entry.get("disagreement"),
                    "history": entry.get("history"),
                }
                for entry in disputed
            ],
            "why_not_remediation": (
                "Independent reviewers disagree about whether the finding is open, "
                "so another autonomous remediation would be adjudicating review "
                "deadlock rather than targeting currently agreed unresolved work."
            ),
        }
        result.escalations.append(f"{execution.task_id}:REVIEWER_DISAGREEMENT_DEADLOCK")
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.review_disagreement_deadlock",
                actor="runner",
                event_data=evidence,
            ),
        )
        self._block_task(
            session,
            execution.task_id,
            "REVIEWER_DISAGREEMENT_DEADLOCK",
            invariant="REVIEWER_DISAGREEMENT_DEADLOCK",
            execution=execution,
            error="Independent reviewers disagreed about open remediation findings",
            recovery_classification="OPERATOR_ACTION_REQUIRED",
            extra_data=evidence,
        )
        return True

    def _review_protocol_blocked(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        verdict: ReviewVerdict,
        result: RunnerCycleResult,
        *,
        reason: str = "REMEDIATION_REQUIRED_WITHOUT_FINDING_SIGNAL",
        why_not_remediation: str | None = None,
        transition_reason: str = "review protocol blocked: remediation required without finding signal",
        error: str = "Reviewer requested remediation without findings or finding dispositions",
    ) -> None:
        """Retry malformed REMEDIATION_REQUIRED reviews as review failures.

        A remediation request without a finding id, finding text, or
        disposition has no durable work item for #81's finding-aware budget.
        Treating it as implementation failure would spend remediation attempts
        on unstructured reviewer output and can converge to an empty-evidence
        REMEDIATION_LIMIT_REACHED.
        """
        release_active_claims(session, execution.task_id, completed=False)
        task = session.get(BuildTask, execution.task_id)
        if task is not None:
            task.current_claim_id = None
            task.lease_expires_at = None
            task.last_heartbeat_at = None
        evidence = {
            "reviewer": execution.worker_id,
            "reviewed_feature_sha": execution.reviewed_feature_sha,
            "verdict": verdict.verdict,
            "required_remediation": list(verdict.required_remediation),
            "reason": reason,
            "classification": "review_protocol_blocked",
            "why_not_remediation": why_not_remediation
            or (
                "No findings or finding_dispositions were supplied, so there is no "
                "currently open finding for a remediation worker to target."
            ),
        }
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.review_protocol_blocked",
                actor="runner",
                event_data=evidence,
            ),
        )
        attempts = self._review_environment_attempts(session, execution.task_id)
        if attempts >= self._config.max_review_environment_attempts:
            result.escalations.append(f"{execution.task_id}:REVIEW_ENVIRONMENT_BLOCKED")
            self._block_task(
                session,
                execution.task_id,
                "REVIEW_ENVIRONMENT_BLOCKED",
                invariant="REVIEW_PROTOCOL_BLOCKED",
                execution=execution,
                error=error,
                recovery_classification="OPERATOR_ACTION_REQUIRED",
                extra_data=evidence,
            )
            return
        transition_task(
            session,
            execution.task_id,
            "REVIEW_READY",
            actor="runner",
            reason=transition_reason,
        )

    def _needs_second_reviewer(self, session: Session, execution: BuildRunnerExecution) -> bool:
        task = session.get(BuildTask, execution.task_id)
        if task is None:
            return False
        spec = review_policy_spec(task.review_policy)
        if spec.required_approvals < 2:
            return False
        if spec.independent_provider:
            return len(
                approving_providers(
                    session,
                    execution.task_id,
                    reviewed_feature_sha=execution.reviewed_feature_sha,
                )
            ) < spec.required_approvals
        return len(
            approving_reviewers(
                session,
                execution.task_id,
                reviewed_feature_sha=execution.reviewed_feature_sha,
            )
        ) < spec.required_approvals

    def _integration_succeeded(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        parsed,
    ) -> None:
        integrator = parsed.integrator if parsed is not None else None
        if execution.adapter == "builtin-git":
            if integrator is None or integrator.push_status not in {"PUSHED", "NOT_REQUIRED"}:
                result.escalations.append(f"{execution.task_id}:UPSTREAM_PUSH_FAILED")
                self._block_task(session, execution.task_id, "UPSTREAM_PUSH_FAILED")
                return
            self._complete_or_await_ci(session, execution)
            self._cleanup_integrated_task(session, execution)
            return
        if not self._config.auto_push_allowed:
            result.escalations.append(f"{execution.task_id}:REMOTE_PUSH_APPROVAL_REQUIRED")
            self._block_task(session, execution.task_id, "REMOTE_PUSH_APPROVAL_REQUIRED")
            return
        self._complete_or_await_ci(session, execution)

    def _complete_or_await_ci(self, session: Session, execution: BuildRunnerExecution) -> None:
        """#65: when external_ci is enabled and this integration actually
        pushed a SHA, move to AWAITING_EXTERNAL_CI instead of DONE so a
        later cycle's reconciliation (not this worker slot) confirms CI
        before the task is considered done. Strictly additive: disabled by
        default, and a push with no recorded SHA still goes straight to
        DONE as before (nothing to reconcile against)."""
        task = session.get(BuildTask, execution.task_id)
        if task is not None and task.state == "DONE":
            return
        has_sha = bool((execution.result_data or {}).get("merge_commit_sha"))
        if self._config.external_ci_enabled and self._config.external_ci_repo and has_sha:
            transition_task(
                session, execution.task_id, "AWAITING_EXTERNAL_CI", actor="runner", reason="awaiting external CI"
            )
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.integration_completed",
                    actor="runner",
                    event_data=execution.result_data,
                ),
            )
            return
        evidence = self._persist_project_delivery_evidence(
            session,
            execution.task_id,
            (execution.result_data or {}).get("merge_commit_sha")
            or (execution.result_data or {}).get("final_main_sha"),
            push_required=bool((execution.result_data or {}).get("push_status") == "PUSHED"),
        )
        if evidence is not None and evidence.get("status") in {"COMMIT_FAILED", "PUSH_FAILED"}:
            result_detail = evidence.get("detail") or evidence.get("status")
            self._block_task(session, execution.task_id, f"PROJECT_DELIVERY_EVIDENCE_FAILED: {result_detail}")
            return
        transition_task(session, execution.task_id, "DONE", actor="runner", reason="integration completed")
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.integration_completed",
                actor="runner",
                event_data=execution.result_data,
            ),
        )

    def _persist_project_delivery_evidence(
        self,
        session: Session,
        task_id: str,
        sha: Any,
        *,
        push_required: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(sha, str) or not sha:
            return None
        repo_root = find_project_root(Path(self._settings.repo_root))
        if repo_root is None:
            return None
        try:
            project = load_project(repo_root)
            expected_url = None
            if push_required:
                try:
                    expected_url = expected_remote_url(repo_root, remote=self._config.git_remote)
                except Exception:
                    expected_url = None
            result = persist_delivery_evidence_in_history(
                repo_root,
                load_backlog(project),
                task_id,
                sha=sha,
                push_remote=self._config.git_remote if push_required else None,
                push_branch_name=self._config.git_main_branch,
                expected_remote_url=expected_url,
            )
        except Exception as exc:
            record_event(
                session,
                EventInput(
                    task_id=task_id,
                    event_type="project.delivery_evidence_failed",
                    actor="runner",
                    event_data={"sha": sha, "error": str(exc)},
                ),
            )
            return {"status": "COMMIT_FAILED", "detail": str(exc), "sha": sha}
        if result.get("changed"):
            record_event(
                session,
                EventInput(
                    task_id=task_id,
                    event_type="project.delivery_evidence_persisted",
                    actor="runner",
                    event_data=result,
                ),
            )
        return result

    def _recoverable_failure(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        merged: dict,
        observation: ExecutionObservation,
    ) -> bool:
        """A worker that died, or a provider that failed, must not strand the task.

        The execution is recorded as LOST so ownership is released through the
        normal recovery path (checkpoints and evidence stay) and a replacement
        worker resumes the task. Transient provider failures (RATE_LIMITED,
        UNAVAILABLE, NETWORK_FAILURE, per the routing taxonomy's
        RETRYABLE_PROVIDER_FAILURES) and worker deaths are retried: the failing
        provider is routed around for a bounded cooldown instead of being
        relaunched every poll cycle, while other workers/providers remain free
        to pick the task up immediately. RATE_LIMITED and QUOTA_EXHAUSTED are
        provider-capacity observations and therefore do not consume substantive
        task/reviewer retry budgets. Other failures remain bounded; once their
        budget is exhausted the task escalates with typed evidence and preserved
        checkpoints instead of looping forever."""
        failure = str(merged.get("provider_failure") or "").upper()
        died = observation.exit_code not in (None, 0) and not merged.get("schema_version")
        if not failure and not died:
            return False
        capacity_failure = failure in _PROVIDER_CAPACITY_FAILURES
        retryable = died or failure in RETRYABLE_PROVIDER_FAILURES or capacity_failure
        task = session.get(BuildTask, execution.task_id)
        retry_generation = int((task.retry_generation if task is not None else 0) or 0)

        if failure == "NO_CHANGES_PRODUCED" and execution.role in {"BUILDER", "REMEDIATION"}:
            detail = str(merged.get("detail") or "Agent produced no changes on task branch")[:300]
            outcome = self._reconcile_no_changes_produced(
                session,
                execution,
                result,
                detail=detail,
                retry_generation=retry_generation,
            )
            if outcome != "UNRESOLVED":
                execution.status = "LOST"
                execution.completed_at = _now()
                execution.result_data = {
                    **(execution.result_data or {}),
                    **merged,
                    "reconciliation_state": outcome,
                    "retry_generation": retry_generation,
                    "retryable_failure": False,
                    "retry_backoff_seconds": None,
                }
                return True
            attempts = session.scalar(
                select(func.count())
                .select_from(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == execution.task_id)
                .where(BuildRunnerExecution.role == execution.role)
                .where(BuildRunnerExecution.adapter != "validation")
                .where(BuildRunnerExecution.status.in_(("LOST", "FAILED")))
                .where(
                    or_(
                        BuildRunnerExecution.result_data["retry_generation"].as_integer() == retry_generation,
                        BuildRunnerExecution.result_data["retry_generation"].as_integer().is_(None)
                        if retry_generation == 0
                        else False,
                    )
                )
            ) or 0
            attempt = attempts + 1
            detail = str(merged.get("detail") or "Agent produced no changes on task branch")[:300]
            self._record_no_changes_produced(
                session,
                execution,
                retry_generation=retry_generation,
                attempt=attempt,
                detail=detail,
            )
            execution.status = "LOST"
            execution.completed_at = _now()
            execution.result_data = {
                **(execution.result_data or {}),
                **merged,
                "reconciliation_state": "NO_CHANGES_PRODUCED",
                "retry_attempt": attempt,
                "retry_generation": retry_generation,
                "retryable_failure": True,
                "retry_backoff_seconds": None,
            }
            if execution.claim_id:
                try:
                    checkpoint(
                        session,
                        execution.claim_id,
                        worker_id=execution.worker_id,
                        data=CheckpointInput(
                            current_step=f"{execution.role.lower()} execution produced no changes",
                            known_failures=[f"NO_CHANGES_PRODUCED (attempt {attempt})"],
                        ),
                    )
                except CoordinatorPolicyError:
                    pass
            if attempt >= self._config.max_execution_attempts:
                result.escalations.append(f"{execution.task_id}:NO_CHANGES_PRODUCED")
                self._block_task(
                    session,
                    execution.task_id,
                    "NO_CHANGES_PRODUCED",
                    invariant="BUILDER_COMMIT_CONTRACT",
                    execution=execution,
                    error="Agent produced no changes on task branch and acceptance criteria not met",
                    recovery_classification="REWORK_REQUIRED",
                    extra_data={
                        "provider_failure": "NO_CHANGES_PRODUCED",
                        "retry_attempts": attempt,
                        "max_attempts": self._config.max_execution_attempts,
                        "retry_generation": retry_generation,
                        "preserved_checkpoint": bool(execution.claim_id),
                    },
                )
            return True

        # 1. REVIEWER role: Reviewer failures must never consume implementation retry budget
        # nor trigger EXECUTION_RETRY_LIMIT_REACHED on the task.
        if execution.role == "REVIEWER":
            reviewer_attempts = _substantive_failure_attempts(
                session,
                execution.task_id,
                role="REVIEWER",
                retry_generation=retry_generation,
            )
            backoff_seconds = (
                _COOLDOWN_SECONDS.get(failure, 300)
                if capacity_failure
                else (
                    _retry_backoff_seconds(failure or "WORKER_EXIT", reviewer_attempts)
                    if retryable
                    else _COOLDOWN_SECONDS.get(failure, 300)
                )
            )
            if failure in PROVIDER_FAILURES:
                provider_was_unavailable = (
                    capacity_failure
                    and _provider_capacity_is_active(
                        session,
                        execution.provider,
                        now=_now(),
                    )
                )
                unavailable_until = _provider_unavailable_until(
                    merged,
                    fallback_seconds=backoff_seconds,
                )
                record_event(
                    session,
                    EventInput(
                        task_id=execution.task_id,
                        event_type="runner.provider_failure",
                        actor="runner",
                        event_data={
                            "provider": execution.provider,
                            "worker_id": execution.worker_id,
                            "runtime": next(
                                (
                                    worker.runtime
                                    for worker in self._config.workers
                                    if worker.worker_id == execution.worker_id
                                ),
                                None,
                            ),
                            "failure": failure,
                            "until": unavailable_until.isoformat(),
                            "provider_reset_at": merged.get("provider_reset_at"),
                            "detail": str(merged.get("detail") or "")[:300],
                        },
                    ),
                )
                if capacity_failure and not provider_was_unavailable:
                    result.provider_state_changes.append(
                        {
                            "state": "UNAVAILABLE",
                            "task_id": execution.task_id,
                            "provider": execution.provider,
                            "runtime": next(
                                (
                                    worker.runtime
                                    for worker in self._config.workers
                                    if worker.worker_id == execution.worker_id
                                ),
                                None,
                            ),
                            "worker_id": execution.worker_id,
                            "failure": failure,
                            "until": unavailable_until.isoformat(),
                            "reset_source": (
                                "provider"
                                if merged.get("provider_reset_at")
                                else "fallback"
                            ),
                        }
                    )
            execution.status = "LOST"
            execution.completed_at = _now()
            execution.result_data = {
                **(execution.result_data or {}),
                **merged,
                "reconciliation_state": "WORKER_EXITED" if died else "PROVIDER_FAILED",
                "reviewer_attempt": reviewer_attempts if capacity_failure else reviewer_attempts + 1,
                "retry_generation": retry_generation,
                "retryable_failure": retryable,
                "provider_capacity_failure": capacity_failure,
                "retry_backoff_seconds": backoff_seconds if retryable else None,
            }
            if execution.claim_id:
                try:
                    checkpoint(
                        session,
                        execution.claim_id,
                        worker_id=execution.worker_id,
                        data=CheckpointInput(
                            current_step=f"reviewer execution lost after {failure or 'worker exit'}",
                            known_failures=[
                                (
                                    f"{failure} (provider capacity)"
                                    if capacity_failure
                                    else f"{failure or 'WORKER_EXITED'} (reviewer attempt {reviewer_attempts + 1})"
                                )
                            ],
                        ),
                    )
                except CoordinatorPolicyError:
                    pass
            if capacity_failure:
                try:
                    transition_task(
                        session,
                        execution.task_id,
                        "REVIEW_READY",
                        actor="runner",
                        reason=f"reviewer provider capacity unavailable: {failure}; trying another eligible provider",
                    )
                except CoordinatorPolicyError:
                    release_active_claims(session, execution.task_id, completed=False)
                return True
            if reviewer_attempts + 1 >= self._config.max_review_environment_attempts:
                if failure:
                    self._block_task(
                        session,
                        execution.task_id,
                        f"PROVIDER_FAILURE:{failure}",
                        invariant="REVIEWER_PROVIDER_FAILURE",
                        execution=execution,
                        error=f"Reviewer provider repeatedly failed due to {failure}",
                        extra_data={
                            "provider_failure": failure,
                            "reviewer_attempts": reviewer_attempts + 1,
                            "retryable_failure": retryable,
                        },
                    )
                else:
                    self._block_task(
                        session,
                        execution.task_id,
                        "REVIEW_ENVIRONMENT_BLOCKED",
                        invariant="REVIEW_ENVIRONMENT",
                        execution=execution,
                        error="Reviewer repeatedly failed due to worker exit",
                    )
            else:
                try:
                    transition_task(
                        session,
                        execution.task_id,
                        "REVIEW_READY",
                        actor="runner",
                        reason=f"reviewer provider failure retry: {failure or 'WORKER_EXIT'}",
                    )
                except CoordinatorPolicyError:
                    release_active_claims(session, execution.task_id, completed=False)
            return True

        # 2. BUILDER / REMEDIATION roles:
        attempts = _substantive_failure_attempts(
            session,
            execution.task_id,
            role=execution.role,
            retry_generation=retry_generation,
        )
        exhausted = (not capacity_failure) and attempts + 1 >= self._config.max_execution_attempts
        backoff_seconds = (
            _COOLDOWN_SECONDS.get(failure, 300)
            if capacity_failure
            else (
                _retry_backoff_seconds(failure or "WORKER_EXIT", attempts)
                if retryable
                else _COOLDOWN_SECONDS.get(failure, 300)
            )
        )
        if failure in PROVIDER_FAILURES:
            provider_was_unavailable = (
                capacity_failure
                and _provider_capacity_is_active(
                    session,
                    execution.provider,
                    now=_now(),
                )
            )
            unavailable_until = _provider_unavailable_until(
                merged,
                fallback_seconds=backoff_seconds,
            )
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.provider_failure",
                    actor="runner",
                    event_data={
                        "provider": execution.provider,
                        "worker_id": execution.worker_id,
                        "runtime": next(
                                (
                                    worker.runtime
                                    for worker in self._config.workers
                                    if worker.worker_id == execution.worker_id
                                ),
                                None,
                            ),
                        "failure": failure,
                        "until": unavailable_until.isoformat(),
                        "provider_reset_at": merged.get("provider_reset_at"),
                        "detail": str(merged.get("detail") or "")[:300],
                    },
                ),
            )
            if capacity_failure and not provider_was_unavailable:
                result.provider_state_changes.append(
                    {
                        "state": "UNAVAILABLE",
                        "task_id": execution.task_id,
                        "provider": execution.provider,
                        "runtime": next(
                                (
                                    worker.runtime
                                    for worker in self._config.workers
                                    if worker.worker_id == execution.worker_id
                                ),
                                None,
                            ),
                        "worker_id": execution.worker_id,
                        "failure": failure,
                        "until": unavailable_until.isoformat(),
                        "reset_source": (
                            "provider"
                            if merged.get("provider_reset_at")
                            else "fallback"
                        ),
                    }
                )
        execution.status = "LOST"
        execution.completed_at = _now()
        execution.result_data = {
            **(execution.result_data or {}),
            **merged,
            "reconciliation_state": "WORKER_EXITED" if died else "PROVIDER_FAILED",
            "retry_attempt": attempts if capacity_failure else attempts + 1,
            "retry_generation": retry_generation,
            "retryable_failure": retryable,
            "provider_capacity_failure": capacity_failure,
            "retry_backoff_seconds": backoff_seconds if retryable else None,
        }
        if execution.claim_id:
            try:
                checkpoint(
                    session,
                    execution.claim_id,
                    worker_id=execution.worker_id,
                    data=CheckpointInput(
                        current_step=f"{execution.role.lower()} execution lost after {failure or 'worker exit'}",
                        known_failures=[
                            (
                                f"{failure} (provider capacity)"
                                if capacity_failure
                                else f"{failure or 'WORKER_EXITED'} (attempt {attempts + 1})"
                            )
                        ],
                    ),
                )
            except CoordinatorPolicyError:
                pass
        if exhausted:
            typed_reason = f"PROVIDER_FAILURE_RETRIES_EXHAUSTED:{failure}" if failure else "EXECUTION_RETRY_LIMIT_REACHED"
            result.escalations.append(f"{execution.task_id}:{typed_reason}")
            self._block_task(
                session,
                execution.task_id,
                typed_reason,
                invariant="EXECUTION_RETRY_LIMIT_REACHED",
                execution=execution,
                error=(
                    f"{execution.role} retry attempts exhausted after "
                    f"{attempts + 1}/{self._config.max_execution_attempts} attempts"
                ),
                extra_data={
                    "provider_failure": failure or None,
                    "retry_attempts": attempts + 1,
                    "max_attempts": self._config.max_execution_attempts,
                    "retryable_failure": retryable,
                    "retry_generation": retry_generation,
                    "preserved_checkpoint": bool(execution.claim_id),
                },
            )
        return True

    def _cleanup_integrated_task(self, session: Session, execution: BuildRunnerExecution) -> None:
        if not self._config.cleanup_branches:
            return
        task = session.get(BuildTask, execution.task_id)
        if task is None or not task.branch_name:
            return
        removed, detail = cleanup_task_branch(
            self._settings.repo_root,
            task.branch_name,
            main_ref=self._config.main_ref,
            reviewed_sha=execution.reviewed_feature_sha,
        )
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.workspace_cleaned" if removed else "runner.cleanup_skipped",
                actor="runner",
                event_data={"branch": task.branch_name, "detail": detail},
            ),
        )

    def _dispatch_builders(self, session: Session, result: RunnerCycleResult) -> None:
        now = _now()
        stmt = (
            select(BuildTask)
            .where(BuildTask.state.in_(CLAIMABLE_STATES))
            .order_by(BuildTask.task_id)
        )
        candidates = list(session.scalars(stmt).all())
        active_claims = {
            c.task_id
            for c in session.scalars(
                select(BuildTaskClaim)
                .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
                .where(BuildTaskClaim.status == "ACTIVE")
                .where(BuildTaskClaim.lease_expires_at > now)
            ).all()
        }
        candidates = [t for t in candidates if t.task_id not in active_claims]
        if self._target_task_ids:
            candidates = [task for task in candidates if self._target_allows(task.task_id)]
        candidates = [task for task in candidates if not is_planner_task(task)]

        active_impl = active_implementation_tasks(session, now)
        active_wt = active_worktrees(session, now)
        active_wk_counts = active_worker_counts(session, now)
        max_builders = get_max_active_builders(session)

        priorities = task_priorities(session, [task.task_id for task in candidates])
        sorted_candidates = sort_tasks_for_dispatch(session, candidates, priorities, active_impl)

        has_unlaunched_p0 = False
        for task in sorted_candidates:
            task_priority = priorities.get(task.task_id, 100)
            if task_priority > 0 and has_unlaunched_p0:
                # Lower-priority work does not jump ahead while executable P0 work exists
                break

            ready, reason = check_task_readiness(
                session,
                task,
                now=now,
                max_active_builders=max_builders,
                active_tasks=active_impl,
            )
            if not ready:
                assert reason is not None
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                if reason in {"objective_parallelism_full", "project_builder_capacity_full"}:
                    result.capacity_full = True
                continue

            if task.state == "REWORK_REQUIRED":
                prompt_builder = RemediationPromptBuilder()
                role = "REMEDIATION"
            else:
                prompt_builder = BuilderPromptBuilder()
                role = "BUILDER"

            worker, availability, decision = self._select_worker(
                role,
                task_id=task.task_id,
                session=session,
                deprioritized_workers=self._no_change_workers(session, task.task_id),
                deprioritized_providers=self._no_change_providers(session, task.task_id),
            )
            if availability == "slots_occupied":
                result.capacity_full = True
                reason = "worktree_or_worker_owned"
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                if task_priority == 0:
                    has_unlaunched_p0 = True
                continue
            if availability in {"providers_unavailable", "provider_backoff"}:
                result.capacity_full = True
                reason = "provider_capacity_wait"
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                if task_priority == 0:
                    has_unlaunched_p0 = True
                continue
            if worker is None:
                reason = "worker_unavailable"
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                result.escalations.append(f"{task.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
                if task_priority == 0:
                    has_unlaunched_p0 = True
                continue

            # Check single-owner invariants for worker and worktree
            if active_wk_counts.get(worker.worker_id, 0) >= worker.max_concurrency:
                reason = "worktree_or_worker_owned"
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                if task_priority == 0:
                    has_unlaunched_p0 = True
                continue

            norm_wt = normalize_worktree_path(self._worker_slot_worktree_path(worker))
            if norm_wt and norm_wt in active_wt:
                reason = "worktree_or_worker_owned"
                result.scheduling_reasons[task.task_id] = reason
                record_task_withheld(session, task.task_id, reason, task.objective_id)
                if task_priority == 0:
                    has_unlaunched_p0 = True
                continue

            try:
                if self._config.task_branches and (
                    worker.worktree_path
                    or (self._config.use_clone_pool and self._config.clone_pool_root)
                ):
                    worker = self._prepare_task_worker(worker, task)
                else:
                    self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{task.task_id}:WORKTREE_INVALID")
                self._block_task(
                    session,
                    task.task_id,
                    "WORKTREE_INVALID",
                    invariant="WORKTREE_INVALID",
                    worker=worker,
                    branch=worker.branch_name,
                    worktree=worker.worktree_path,
                    error=str(exc),
                    recovery_classification="RECOVERABLE_WORKTREE",
                )
                record_event(
                    session,
                    EventInput(
                        task_id=task.task_id,
                        event_type="runner.worktree_invalid",
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
                continue
            except CoordinatorPolicyError as exc:
                result.scheduling_reasons[task.task_id] = "branch_collision"
                record_task_withheld(session, task.task_id, "branch_collision", task.objective_id)
                record_event(
                    session,
                    EventInput(
                        task_id=task.task_id,
                        event_type="runner.branch_collision",
                        actor="runner",
                        event_data={"error": str(exc), "branch": task.branch_name or task_branch_name(task.task_id)},
                    ),
                )
                continue

            if not self._run_task_setup(session, task, worker):
                result.escalations.append(f"{task.task_id}:SETUP_FAILED")
                continue

            try:
                claim = claim_task(
                    session,
                    ClaimRequest(
                        task.task_id,
                        worker_id=worker.worker_id,
                        provider=worker.provider,
                        branch_name=worker.branch_name,
                        worktree_path=worker.worktree_path,
                    ),
                    max_active_builders=max_builders,
                )
            except CoordinatorCapacityError as exc:
                result.capacity_full = True
                if "Objective parallelism" in str(exc):
                    result.scheduling_reasons[task.task_id] = "objective_parallelism_full"
                    continue
                else:
                    result.scheduling_reasons[task.task_id] = "project_builder_capacity_full"
                    return
            except CoordinatorPolicyError as exc:
                if "parallel_safe" in str(exc):
                    result.scheduling_reasons[task.task_id] = "parallel_safe_serialization"
                elif "scope" in str(exc):
                    result.scheduling_reasons[task.task_id] = "ownership_scope_conflict"
                elif "gate" in str(exc):
                    result.scheduling_reasons[task.task_id] = "human_gate_open"
                continue

            context = get_resume_context(session, task.task_id)
            extra: dict[str, Any] = {"task_definition": _task_definition(task)}
            if role == "REMEDIATION":
                extra["open_findings"] = finding_escalation_evidence(task.finding_registry or {})
                waiting = task.waiting_input if isinstance(task.waiting_input, dict) else {}
                if isinstance(waiting.get("conflict_recovery"), dict):
                    conflict_recovery = dict(waiting["conflict_recovery"])
                    conflict_recovery.update(
                        {
                            "recovery_worker": worker.worker_id,
                            "recovery_provider": worker.provider,
                        }
                    )
                    waiting = dict(waiting)
                    waiting["conflict_recovery"] = conflict_recovery
                    task.waiting_input = waiting
                    extra["conflict_recovery"] = conflict_recovery
                    record_event(
                        session,
                        EventInput(
                            task_id=task.task_id,
                            event_type="runner.merge_conflict_recovery_worker_selected",
                            actor="runner",
                            event_data=conflict_recovery,
                        ),
                    )
            prompt = prompt_builder.build(context, extra=extra)
            self._launch(
                session,
                result,
                task.task_id,
                role,
                worker,
                claim.claim_id,
                prompt,
                routing_decision=decision,
            )
            # Update tracking sets dynamically for the rest of this cycle
            active_impl.append(task)
            active_wk_counts[worker.worker_id] = active_wk_counts.get(worker.worker_id, 0) + 1
            if norm_wt:
                active_wt.add(norm_wt)

    def _planner_succeeded(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        parsed,
    ) -> None:
        from build_coordinator.types import StructuredContractError

        task = session.get(BuildTask, execution.task_id)
        if task is None or task.objective_id is None:
            self._block_task(
                session,
                execution.task_id,
                "COORDINATOR_INVARIANT_FAILURE",
                invariant="TASK_OBJECTIVE_LINK_MISSING",
                execution=execution,
                error="task is missing objective_id",
                recovery_classification="OPERATOR_ACTION_REQUIRED",
            )
            return
        objective = get_objective(session, task.objective_id)
        plan_payload = parsed.plan if parsed is not None else (execution.result_data or {}).get("plan")
        try:
            plan = parse_planner_plan(plan_payload, source="PLANNER")
            apply_validated_plan(session, objective, plan)
        except StructuredContractError as exc:
            record_planner_failed(
                session,
                objective,
                reason=f"planner output failed validation: {exc}",
            )
            self._block_task(
                session,
                execution.task_id,
                "COORDINATOR_INVARIANT_FAILURE",
                invariant="PLANNER_CONTRACT_INVALID",
                execution=execution,
                error=str(exc),
                recovery_classification="OPERATOR_ACTION_REQUIRED",
            )
            return
        if execution.claim_id:
            checkpoint(
                session,
                execution.claim_id,
                worker_id=execution.worker_id,
                data=CheckpointInput(
                    current_step="planner produced a validated structured plan",
                    completed_work=["structured ObjectivePlan applied"],
                ),
            )
        current = session.get(BuildTask, execution.task_id)
        if current is not None and current.state == "CLAIMED":
            current = transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
        if current is not None and current.state == "IN_PROGRESS":
            current = transition_task(session, execution.task_id, "VALIDATING", actor="runner")
        if current is not None and current.state == "VALIDATING":
            transition_task(session, execution.task_id, "DONE", actor="runner", reason="planner plan applied")

    def _planner_prompt(
        self,
        session: Session,
        objective: BuildObjective,
        planner_task: BuildTask,
    ) -> str:
        context = get_resume_context(session, planner_task.task_id)
        return PlannerPromptBuilder().build(
            context,
            extra={
                "objective": {
                    "objective_id": objective.objective_id,
                    "goal": objective.goal,
                    "constraints": list(objective.constraints),
                    "allowed_scope": list(objective.allowed_scope),
                    "prohibited_scope": list(objective.prohibited_scope),
                    "completion_criteria": list(objective.completion_criteria),
                },
                "planner_policy": (
                    "Propose work only. Do not mutate coordinator state, "
                    "choose worktrees, or authorize remote main push."
                ),
            },
        )

    def _planner_contract_hash(self) -> str:
        payload = {
            "role_policy": PlannerPromptBuilder.role_policy,
            "result_file_contract": result_file_contract_for_role("PLANNER"),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _dispatch_planners(self, session: Session, result: RunnerCycleResult) -> None:
        if self._target_task_ids:
            return
        worker, availability, decision = self._select_worker("PLANNER", session=session)
        if availability == "slots_occupied":
            result.capacity_full = True
            return
        for objective in list_objectives(session):
            if objective.state != "PLANNING":
                continue
            if not objective_source_is_executable(session, objective):
                continue
            if not objective_dependencies_satisfied(session, objective):
                continue
            planner_task = get_planner_task(session, objective.objective_id)
            if planner_task is None:
                continue
            live = session.scalar(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == planner_task.task_id)
                .where(BuildRunnerExecution.role == "PLANNER")
                .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
            )
            if live is not None:
                continue
            succeeded = session.scalar(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == planner_task.task_id)
                .where(BuildRunnerExecution.role == "PLANNER")
                .where(BuildRunnerExecution.status == "SUCCEEDED")
            )
            if succeeded is not None:
                continue
            if availability in {"providers_unavailable", "provider_backoff"}:
                result.capacity_full = True
                result.scheduling_reasons[planner_task.task_id] = "provider_capacity_wait"
                record_planner_unavailable(
                    session,
                    objective,
                    reason=(
                        "planner executor is configured but its provider is temporarily "
                        f"unavailable ({availability}); retry on the next run cycle"
                    ),
                )
                continue
            if worker is None or worker.adapter == "unconfigured":
                record_planner_unavailable(
                    session,
                    objective,
                    reason="no planner executor is configured; retry on the next run cycle",
                )
                result.escalations.append(
                    f"{planner_task.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED"
                )
                continue
            try:
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                record_planner_unavailable(session, objective, reason=str(exc))
                continue
            try:
                claim = claim_task(
                    session,
                    ClaimRequest(
                        planner_task.task_id,
                        worker_id=worker.worker_id,
                        provider=worker.provider,
                        branch_name=None,
                        worktree_path=None,
                    ),
                )
            except CoordinatorCapacityError:
                result.capacity_full = True
                return
            except CoordinatorPolicyError:
                continue
            prompt = self._planner_prompt(session, objective, planner_task)
            self._launch(
                session,
                result,
                planner_task.task_id,
                "PLANNER",
                worker,
                claim.claim_id,
                prompt,
                routing_decision=decision,
                extra_result={
                    "planner_contract_hash": self._planner_contract_hash(),
                },
            )

    def _dispatch_reviews(self, session: Session, result: RunnerCycleResult) -> None:
        tasks = session.scalars(
            select(BuildTask).where(BuildTask.state == "REVIEW_READY").order_by(BuildTask.task_id)
        ).all()
        for task in tasks:
            if not self._target_allows(task.task_id):
                continue
            if not task_source_is_executable(task):
                continue
            review_target_sha = self._task_review_target_sha(task)
            worker, availability, decision = self._select_worker(
                "REVIEWER",
                task_id=task.task_id,
                session=session,
                deprioritized_workers=self._environment_blocked_reviewers(session, task.task_id),
                review_target_sha=review_target_sha,
            )
            if availability == "slots_occupied":
                result.capacity_full = True
                continue
            if availability in {"providers_unavailable", "provider_backoff"}:
                result.capacity_full = True
                result.scheduling_reasons[task.task_id] = "provider_capacity_wait"
                continue
            if worker is None:
                if self._review_environment_attempts(session, task.task_id) > 0:
                    result.escalations.append(f"{task.task_id}:REVIEW_ENVIRONMENT_BLOCKED")
                    self._block_task(session, task.task_id, "REVIEW_ENVIRONMENT_BLOCKED")
                    continue
                result.escalations.append(f"{task.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
                continue
            if self._worker_slot_is_active(session, worker):
                result.capacity_full = True
                continue
            try:
                worker = self._prepare_git_stage_worker(worker, task)
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{task.task_id}:WORKTREE_INVALID")
                self._block_task(
                    session,
                    task.task_id,
                    "WORKTREE_INVALID",
                    invariant="WORKTREE_INVALID",
                    worker=worker,
                    branch=worker.branch_name,
                    worktree=worker.worktree_path,
                    error=str(exc),
                    recovery_classification="RECOVERABLE_WORKTREE",
                )
                record_event(
                    session,
                    EventInput(
                        task_id=task.task_id,
                        event_type="runner.worktree_invalid",
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
                continue
            reviewed_sha = self._capture_reviewed_sha(session, task, worker, result)
            if reviewed_sha is None and worker.adapter != "fake":
                continue
            try:
                claim = claim_review(
                    session,
                    ClaimRequest(
                        task.task_id,
                        worker_id=worker.worker_id,
                        provider=worker.provider,
                        branch_name=task.branch_name or worker.branch_name,
                        worktree_path=worker.worktree_path,
                    ),
                )
            except CoordinatorPolicyError:
                continue
            prompt = ReviewerPromptBuilder().build(
                get_resume_context(session, task.task_id),
                extra={
                    "reviewed_feature_sha": reviewed_sha,
                    "sha_source": "runner-owned-git",
                    "task_definition": _task_definition(task),
                    "open_findings_from_prior_review": finding_escalation_evidence(
                        task.finding_registry or {}
                    ),
                    "convergence_review": (task.finding_registry or {}).get("convergence")
                    or {},
                },
            )
            self._launch(
                session,
                result,
                task.task_id,
                "REVIEWER",
                worker,
                claim.claim_id,
                prompt,
                reviewed_feature_sha=reviewed_sha,
                routing_decision=decision,
            )

    def _dispatch_integration(self, session: Session, result: RunnerCycleResult) -> None:
        rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status == "SUCCEEDED")
            .order_by(BuildRunnerExecution.completed_at.desc())
        ).all()
        seen_tasks: set[str] = set()
        for row in rows:
            if not self._target_allows(row.task_id):
                continue
            if row.task_id in seen_tasks:
                continue
            seen_tasks.add(row.task_id)
            if self._has_blocking_integration(session, row.task_id):
                continue
            if self._has_live_reviewer(session, row.task_id):
                continue
            try:
                parsed = parse_executor_result(
                    row.result_data or {},
                    execution_id=row.execution_id,
                    task_id=row.task_id,
                    role="REVIEWER",
                    reviewed_feature_sha=row.reviewed_feature_sha,
                    require_identity=row.adapter != "fake",
                )
                if parsed.reviewer is None or not parsed.reviewer.verdict.integration_eligible():
                    continue
                verdict = parsed.reviewer.verdict
            except (ExecutorResultError, ReviewVerdictContradiction):
                continue
            worker, availability, decision = self._select_worker("INTEGRATION", session=session)
            if availability == "slots_occupied":
                result.capacity_full = True
                continue
            if worker is None:
                result.escalations.append(f"{row.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
                continue
            task = session.get(BuildTask, row.task_id)
            if task is None or task.state != "REVIEWING":
                continue
            if not task_source_is_executable(task):
                continue
            if self._worker_slot_is_active(session, worker):
                result.capacity_full = True
                continue
            try:
                worker = self._prepare_git_stage_worker(worker, task)
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{row.task_id}:WORKTREE_INVALID")
                self._block_task(
                    session,
                    row.task_id,
                    "WORKTREE_INVALID",
                    invariant="WORKTREE_INVALID",
                    worker=worker,
                    branch=worker.branch_name,
                    worktree=worker.worktree_path,
                    error=str(exc),
                    recovery_classification="RECOVERABLE_WORKTREE",
                )
                record_event(
                    session,
                    EventInput(
                        task_id=row.task_id,
                        event_type="runner.worktree_invalid",
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
                continue
            reviewed_sha = row.reviewed_feature_sha or (
                parsed.reviewer.reviewed_feature_sha if parsed.reviewer else None
            )
            if not reviewed_sha:
                result.escalations.append(f"{row.task_id}:MISSING_REVIEWED_SHA")
                self._block_task(
                    session,
                    row.task_id,
                    "MISSING_REVIEWED_SHA",
                    invariant="REVIEWED_SHA_CONTRACT",
                    worker=worker,
                    branch=row.branch_name,
                    worktree=row.worktree_path,
                    error="integration requires a reviewed feature SHA",
                    recovery_classification="RECOVERABLE_GIT_STATE",
                )
                continue
            assessment = None
            try:
                assessment = assess_mechanical_merge(
                    self._git,
                    cwd=self._git_cwd(worker, task),
                    branch_name=task.branch_name or row.branch_name or worker.branch_name or "HEAD",
                    reviewed_feature_sha=reviewed_sha,
                    remote=self._config.remote_name,
                    main_ref=self._config.main_ref,
                )
            except GitSafetyError as exc:
                result.escalations.append(f"{row.task_id}:GIT_SAFETY_FAILURE")
                self._block_task(
                    session,
                    row.task_id,
                    "GIT_SAFETY_FAILURE",
                    invariant="GIT_SAFETY_FAILURE",
                    worker=worker,
                    branch=task.branch_name or row.branch_name,
                    worktree=self._git_cwd(worker, task),
                    relevant_shas={"reviewed_sha": reviewed_sha},
                    error=str(exc),
                    recovery_classification="RECOVERABLE_GIT_STATE",
                )
                record_event(
                    session,
                    EventInput(
                        task_id=row.task_id,
                        event_type="runner.git_safety_failed",
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
                continue
            if not assessment.reviewed_sha_matches:
                self._request_rereview(session, row.task_id, result, reason="REVIEWED_SHA_CHANGED")
                continue
            if assessment.conflict:
                self._handle_merge_conflict(session, row.task_id, assessment, result)
                continue
            try:
                claim = claim_integration(
                    session,
                    ClaimRequest(
                        row.task_id,
                        worker_id=worker.worker_id,
                        provider=worker.provider,
                        branch_name=task.branch_name or worker.branch_name,
                        worktree_path=worker.worktree_path,
                    ),
                )
            except CoordinatorPolicyError:
                continue
            except IntegrityError:
                continue
            context = get_resume_context(session, row.task_id)
            extra = {
                "reviewed_feature_sha": reviewed_sha,
                "current_main_sha": assessment.current_main_sha,
                "merge_base": assessment.merge_base,
                "feature_remote_sha": assessment.feature_remote_sha,
                "current_main_compatibility_required": True,
                "auto_push_allowed": self._config.auto_push_allowed,                "mechanical_conflict": False,
            }
            prompt = IntegrationPromptBuilder().build(
                context,
                extra=extra,
            )
            self._launch(
                session,
                result,
                row.task_id,
                "INTEGRATION",
                worker,
                claim.claim_id,
                prompt,
                reviewed_feature_sha=reviewed_sha,
                extra_result={
                    "current_main_sha": assessment.current_main_sha,
                    "merge_base": assessment.merge_base,
                    "reviewed_feature_sha": reviewed_sha,
                },
                routing_decision=decision,
            )

    def _launch(
        self,
        session: Session,
        result: RunnerCycleResult,
        task_id: str,
        role: str,
        worker: WorkerConfig,
        claim_id,
        prompt: str,
        *,
        reviewed_feature_sha: str | None = None,
        extra_result: dict | None = None,
        routing_decision: RoutingDecision | None = None,
    ) -> None:
        if worker.adapter == "unconfigured":
            result.escalations.append(f"{task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
            self._block_task(session, task_id, "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
            return
        execution_id = new_uuid()
        result_path = str(self._result_dir() / f"{execution_id}.json")
        executor = self._executor_for_worker(worker)
        row = BuildRunnerExecution(
            execution_id=execution_id,
            task_id=task_id,
            role=role,
            worker_id=worker.worker_id,
            provider=worker.provider,
            adapter=executor.adapter_name,
            claim_id=str(claim_id) if claim_id else None,
            worktree_path=worker.worktree_path,
            branch_name=worker.branch_name,
            result_path=result_path,
            reviewed_feature_sha=reviewed_feature_sha,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            status="LAUNCHED",
            launched_at=_now(),
            result_data={
                **(extra_result or {}),
                **(
                    {"routing": routing_decision.to_audit_dict(worker)}
                    if routing_decision is not None
                    else {}
                ),
            },
        )
        slot_count = max(1, int(worker.max_concurrency or 1))
        reserved_slot: int | None = None
        for slot_index in range(slot_count):
            now = _now()
            stale_leases = session.scalars(
                select(BuildWorkerLease)
                .where(BuildWorkerLease.worker_id == worker.worker_id)
                .where(BuildWorkerLease.slot_index == slot_index)
                .where(BuildWorkerLease.status == "ACTIVE")
                .where(BuildWorkerLease.lease_expires_at <= now)
            ).all()
            for stale in stale_leases:
                stale.status = "EXPIRED"
                stale.lease_expires_at = now
            if stale_leases:
                session.flush()
            lease = BuildWorkerLease(
                worker_id=worker.worker_id,
                provider=worker.provider,
                slot_index=slot_index,
                machine_id=socket.gethostname(),
                process_id=str(os.getpid()),
                task_id=task_id,
                execution_id=execution_id,
                lease_expires_at=now + timedelta(seconds=worker.timeout_seconds or 3600),
                status="ACTIVE",
            )
            try:
                with session.begin_nested():
                    session.add(lease)
                    session.flush()
                reserved_slot = slot_index
                break
            except IntegrityError:
                continue
        if reserved_slot is None:
            result.capacity_full = True
            return
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            release_worker_leases_for_execution(session, execution_id)
            session.commit()
            return
        session.commit()
        try:
            if isinstance(executor, SubprocessExecutor):
                executor.remember_result_path(execution_id, result_path)
            handle = executor.launch(
                ExecutionLaunch(
                    task_id=task_id,
                    role=role,
                    worker_id=worker.worker_id,
                    provider=worker.provider,
                    worktree_path=worker.worktree_path,
                    branch_name=worker.branch_name,
                    prompt=prompt,
                    execution_id=execution_id,
                    result_path=result_path,
                    reviewed_feature_sha=reviewed_feature_sha,
                    timeout_seconds=worker.timeout_seconds,
                    extra_env=worker.resolved_env(),
                )
            )
        except Exception:
            release_worker_leases_for_execution(session, execution_id)
            session.delete(row)
            session.commit()
            raise
        row.execution_id = handle.execution_id
        row.process_id = handle.process_id
        row.result_path = handle.result_path or result_path
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="runner.execution_launched",
                actor="runner",
                claim_id=claim_id,
                event_data={
                    "role": role,
                    "worker_id": worker.worker_id,
                    "provider": worker.provider,
                    "runtime": worker.runtime,
                    "model": worker.model,
                    "adapter": executor.adapter_name,
                    "reviewed_feature_sha": reviewed_feature_sha,
                    "routing": routing_decision.to_audit_dict(worker)
                    if routing_decision is not None
                    else None,
                },
            ),
        )
        result.launched.append(handle.execution_id)

    def _release_worker_lease(self, session: Session, execution_id: str) -> None:
        release_worker_leases_for_execution(session, execution_id)

    def _kill_reconciled_process_trees(self, session: Session) -> None:
        from build_coordinator.execution.process_tree import kill_process_tree

        rows = session.scalars(
            select(BuildRunnerExecution).where(
                BuildRunnerExecution.status.in_(("TERMINATED", "LOST"))
            )
        ).all()
        for row in rows:
            if (row.result_data or {}).get("process_tree_reaped"):
                continue
            executor = self._executors.get(row.worker_id)
            if isinstance(executor, SubprocessExecutor):
                executor.terminate(row.execution_id)
                if row.process_id:
                    executor.terminate_pid(row.process_id)
            elif row.process_id:
                kill_process_tree(int(row.process_id))
            if row.process_id:
                row.result_data = {
                    **(row.result_data or {}),
                    "process_tree_reaped": True,
                }

    def _reconcile_provider_capacity_transitions(
        self,
        session: Session,
        result: RunnerCycleResult,
    ) -> None:
        """Record one provider re-entry event when a capacity window expires."""
        now = _now()
        failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
            .order_by(BuildTaskEvent.created_at.desc())
        ).all()
        recovered = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.event_type == "runner.provider_recovered")
            .order_by(BuildTaskEvent.created_at.desc())
        ).all()
        latest_recovery_by_provider: dict[str, BuildTaskEvent] = {}
        for event in recovered:
            provider = str((event.event_data or {}).get("provider") or "")
            if provider and provider not in latest_recovery_by_provider:
                latest_recovery_by_provider[provider] = event

        seen: set[str] = set()
        for event in failures:
            data = event.event_data or {}
            provider = str(data.get("provider") or "")
            failure = str(data.get("failure") or "").upper()
            if not provider or provider in seen or failure not in _PROVIDER_CAPACITY_FAILURES:
                continue
            seen.add(provider)
            try:
                until = datetime.fromisoformat(str(data.get("until")))
            except (TypeError, ValueError):
                continue
            if until > now:
                continue
            prior_recovery = latest_recovery_by_provider.get(provider)
            if (
                prior_recovery is not None
                and prior_recovery.created_at is not None
                and event.created_at is not None
                and prior_recovery.created_at >= event.created_at
            ):
                continue
            payload = {
                "provider": provider,
                "runtime": data.get("runtime"),
                "worker_id": data.get("worker_id"),
                "prior_failure": failure,
                "available_at": now.isoformat(),
                "prior_until": until.isoformat(),
            }
            record_event(
                session,
                EventInput(
                    task_id=event.task_id,
                    event_type="runner.provider_recovered",
                    actor="runner",
                    event_data=payload,
                ),
            )
            result.provider_state_changes.append(
                {
                    "state": "AVAILABLE",
                    "task_id": event.task_id,
                    **payload,
                }
            )

    def provider_capacity_recheck_seconds(self, *, fallback_seconds: float = 30.0) -> float:
        """Return a bounded sleep before rechecking temporary provider capacity."""
        fallback_seconds = max(float(self._config.poll_seconds), float(fallback_seconds))
        with self._session_factory() as session:
            now = _now()
            delays: list[float] = []
            rows = session.scalars(
                select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.provider_failure")
            ).all()
            for row in rows:
                data = row.event_data or {}
                failure = str(data.get("failure") or "").upper()
                if failure not in _PROVIDER_CAPACITY_FAILURES:
                    continue
                try:
                    until = datetime.fromisoformat(str(data.get("until")))
                except (TypeError, ValueError):
                    continue
                if until > now:
                    delays.append((until - now).total_seconds())
        if not delays:
            return fallback_seconds
        return max(float(self._config.poll_seconds), min(min(delays), fallback_seconds))

    def _effective_providers(self, session: Session | None) -> dict:
        """Configured providers, with any provider that recently failed marked
        unavailable so routing deterministically falls back to other runtimes."""
        providers = dict(self._config.providers)
        if session is None:
            return providers
        now = _now()
        rows = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).all()
        for row in sorted(rows, key=lambda r: (r.event_data or {}).get("until", "")):
            data = row.event_data or {}
            try:
                until = datetime.fromisoformat(str(data.get("until")))
            except ValueError:
                continue
            failure = str(data.get("failure") or "").upper()
            if failure not in PROVIDER_FAILURES:
                continue
            provider = data.get("provider")
            if provider and until > now:
                current = providers.get(provider) or ProviderConfig(provider)
                providers[provider] = dataclasses.replace(
                    current,
                    availability=failure,
                    consumption_mode="FALLBACK",
                )
        return providers

    def _provider_failure_visibility(self, session: Session | None) -> dict[str, dict[str, Any]]:
        if session is None:
            return {}
        now = _now()
        visibility: dict[str, dict[str, Any]] = {}
        rows = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).all()
        for row in rows:
            data = row.event_data or {}
            provider = data.get("provider")
            if not provider:
                continue
            detail = visibility.setdefault(str(provider), {"failures": 0})
            detail["failures"] += 1
            try:
                until = datetime.fromisoformat(str(data.get("until")))
            except ValueError:
                until = None
            failure = str(data.get("failure") or "").upper()
            if until and until > now:
                active = detail.get("active_failure")
                if not active or str(active.get("reset_at") or "") < until.isoformat():
                    detail["active_failure"] = {
                        "failure": failure,
                        "reset_at": until.isoformat(),
                        "seconds_until_reset": max(0.0, (until - now).total_seconds()),
                    }
            elif until:
                detail["last_reset_at"] = max(str(detail.get("last_reset_at") or ""), until.isoformat())
        return visibility

    def diagnostics(self, session: Session | None = None) -> dict[str, Any]:
        """Summary of worker pool, providers, capabilities, availability, and concurrency."""
        effective_providers = self._effective_providers(session)
        active_counts = active_worker_counts(session, _now()) if session is not None else {}
        launches = launch_counts(session) if session is not None else {}
        active_by_provider: dict[str, int] = {}
        launches_by_provider: dict[str, int] = {}
        for worker in self._config.workers:
            active_by_provider[worker.provider] = active_by_provider.get(worker.provider, 0) + active_counts.get(worker.worker_id, 0)
            launches_by_provider[worker.provider] = launches_by_provider.get(worker.provider, 0) + launches.get(worker.worker_id, 0)
        failure_visibility = self._provider_failure_visibility(session)
        observed_evidence = execution_evidence_by_worker(session, self._config.workers)
        worker_summary = []
        for w in self._config.workers:
            p = effective_providers.get(w.provider)
            stage = role_to_stage(w.role)
            requirement = self._config.stage_requirements.get(stage, StageRequirement(stage))
            evidence = merge_worker_evidence(
                w.evidence,
                observed_evidence.get(w.worker_id, w.evidence),
                w,
                requirement,
            )
            worker_summary.append({
                "worker_id": w.worker_id,
                "role": w.role,
                "adapter": w.adapter,
                "provider": w.provider,
                "runtime": w.runtime,
                "capabilities": list(w.capabilities),
                "active_workers": active_counts.get(w.worker_id, 0),
                "active_provider_workers": active_by_provider.get(w.provider, 0),
                "launches": launches.get(w.worker_id, 0),
                "evidence": evidence.to_public_dict(),
                "mode": p.consumption_mode if p else "ACTIVE",
                "consumption_mode": p.consumption_mode if p else "ACTIVE",
                "availability": p.availability if p else "AVAILABLE",
                "eligible": (
                    w.enabled
                    and w.adapter != "unconfigured"
                    and (p is None or (p.enabled and p.consumption_mode != "DISABLED" and p.availability == "AVAILABLE"))
                ),
                "reason_unavailable": (p.metadata.get("reason") or "") if p and p.availability != "AVAILABLE" else None,
            })
        configured_builders = [w for w in self._config.workers if w.role == "BUILDER"]
        executable_builders = [
            w["worker_id"] for w in worker_summary
            if w["role"] == "BUILDER" and w["adapter"] != "unconfigured" and w["availability"] == "AVAILABLE"
        ]
        return {
            "workers": worker_summary,
            "providers": {
                provider: {
                    "mode": provider_config.consumption_mode,
                    "consumption_mode": provider_config.consumption_mode,
                    "availability": provider_config.availability,
                    "eligible": (
                        provider_config.enabled
                        and provider_config.consumption_mode != "DISABLED"
                        and provider_config.availability == "AVAILABLE"
                    ),
                    "active_workers": active_by_provider.get(provider, 0),
                    "launches": launches_by_provider.get(provider, 0),
                    **failure_visibility.get(provider, {}),
                }
                for provider, provider_config in effective_providers.items()
            },
            "configured_concurrency": sum(1 for w in configured_builders),
            "executable_builders": executable_builders,
            "has_configured_builders": any(w.adapter != "unconfigured" for w in configured_builders),
        }

    def _select_worker(
        self,
        role: str,
        *,
        task_id: str | None = None,
        session: Session | None = None,
        deprioritized_workers: set[str] | None = None,
        deprioritized_providers: set[str] | None = None,
        review_target_sha: str | None = None,
    ) -> tuple[WorkerConfig | None, str, RoutingDecision]:
        """Return (worker, availability, deterministic routing decision).

        availability is one of:
        - selected
        - no_eligible: no configured worker for the role
        - slots_occupied: eligible workers exist but all execution slots are busy
        """
        stage = role_to_stage(role)
        workers = [worker for worker in self._config.workers if stage in worker.stage_names()]
        if role == "REMEDIATION":
            workers = workers or [
                worker for worker in self._config.workers if "implementation" in worker.stage_names()
            ]
        requirement = self._config.stage_requirements.get(stage, StageRequirement(stage))
        exclusions = (
            reviewer_exclusions(
                session,
                task_id,
                reviewed_feature_sha=review_target_sha,
            )
            if role == "REVIEWER"
            else None
        )
        observed_evidence = execution_evidence_by_worker(session, workers)
        evidence_by_worker = {
            worker.worker_id: merge_worker_evidence(
                worker.evidence,
                observed_evidence.get(worker.worker_id, worker.evidence),
                worker,
                requirement,
            )
            for worker in workers
        }
        decision = route_worker(
            workers,
            stage=stage,
            stage_requirement=requirement,
            providers=self._effective_providers(session),
            runtimes=self._config.runtimes,
            routing_policy=self._config.routing_policy,
            session=session,
            task_id=task_id,
            excluded_workers=set(exclusions.workers) if exclusions else set(),
            excluded_providers=set(exclusions.providers) if exclusions else set(),
            deprioritized_workers=deprioritized_workers,
            deprioritized_providers=deprioritized_providers,
            evidence_by_worker=evidence_by_worker,
        )
        worker = next(
            (candidate for candidate in workers if candidate.worker_id == decision.selected_worker_id),
            None,
        )
        return worker, decision.availability, decision

    def _worker_for(
        self,
        role: str,
        *,
        task_id: str | None = None,
        session: Session | None = None,
    ) -> WorkerConfig | None:
        worker, _availability, _decision = self._select_worker(role, task_id=task_id, session=session)
        return worker

    def _git_integrator(self) -> GitIntegrationExecutor:
        expected_url = None
        if self._config.use_clone_pool and self._config.upstream_remote:
            try:
                expected_url = expected_remote_url(
                    self._settings.repo_root, remote=self._config.upstream_remote
                )
            except WorktreeValidationError:
                expected_url = None
        return GitIntegrationExecutor(
            main_ref=self._config.main_ref,
            upstream_remote=self._config.upstream_remote,
            push=self._config.push_upstream,
            expected_remote_url=expected_url,
        )

    def _executor_for_worker(self, worker: WorkerConfig) -> WorkerExecutor:
        if worker.worker_id in self._executors:
            return self._executors[worker.worker_id]
        if worker.adapter == "fake":
            executor = FakeExecutor()
        elif worker.adapter == "subprocess":
            executor = SubprocessExecutor(
                list(worker.command),
                log_dir=self._log_dir(),
                temp_dir=self._temp_dir(),
            )
        elif worker.adapter == "builtin-git":
            executor = self._git_integrator()
        else:
            raise CoordinatorPolicyError(f"Unknown executor adapter: {worker.adapter}")
        self._executors[worker.worker_id] = executor
        self._executor_workers[worker.worker_id] = worker
        return executor

    def _executor_for_execution(self, execution: BuildRunnerExecution) -> WorkerExecutor:
        if execution.adapter == "validation":
            return self._validation_executor
        executor = self._executors.get(execution.worker_id)
        if executor is not None:
            if isinstance(executor, SubprocessExecutor):
                executor.remember_result_path(execution.execution_id, execution.result_path)
            return executor
        if execution.adapter == "fake":
            executor = FakeExecutor()
            self._executors[execution.worker_id] = executor
            return executor
        if execution.adapter == "builtin-git":
            executor = self._git_integrator()
            executor.remember_result_path(execution.execution_id, execution.result_path)
            self._executors[execution.worker_id] = executor
            return executor
        if execution.adapter == "subprocess":
            worker = self._executor_workers.get(execution.worker_id) or next(
                (item for item in self._config.workers if item.worker_id == execution.worker_id),
                None,
            )
            command = list(worker.command) if worker and worker.command else ["_unattached"]
            executor = SubprocessExecutor(
                command,
                log_dir=self._log_dir(),
                temp_dir=self._temp_dir(),
                result_paths={execution.execution_id: execution.result_path}
                if execution.result_path
                else {},
            )
            self._executors[execution.worker_id] = executor
            if worker is not None:
                self._executor_workers[execution.worker_id] = worker
            return executor
        raise CoordinatorPolicyError(
            f"Cannot reconcile executor adapter after restart: {execution.adapter}"
        )

    def _capture_reviewed_sha(
        self,
        session: Session,
        task: BuildTask,
        worker: WorkerConfig,
        result: RunnerCycleResult,
    ) -> str | None:
        try:
            return capture_feature_sha(
                self._git,
                cwd=self._git_cwd(worker, task),
                branch_name=task.branch_name or worker.branch_name,
                remote=self._config.remote_name,
            )
        except GitSafetyError as exc:
            if worker.adapter == "fake":
                return None
            result.escalations.append(f"{task.task_id}:GIT_SAFETY_FAILURE")
            self._block_task(
                session,
                task.task_id,
                "GIT_SAFETY_FAILURE",
                invariant="GIT_SAFETY_FAILURE",
                worker=worker,
                branch=task.branch_name or worker.branch_name,
                worktree=self._git_cwd(worker, task),
                error=str(exc),
                recovery_classification="RECOVERABLE_GIT_STATE",
                extra_data={"phase": "review_sha_capture"},
            )
            record_event(
                session,
                EventInput(
                    task_id=task.task_id,
                    event_type="runner.git_safety_failed",
                    actor="runner",
                    event_data={"error": str(exc), "phase": "review_sha_capture"},
                ),
            )
            return None

    def _task_review_target_sha(self, task: BuildTask) -> str | None:
        branch = task.branch_name
        if not branch:
            return None
        try:
            if self._config.remote_name:
                self._git.fetch_prune(str(self._settings.repo_root), self._config.remote_name)
                return self._git.rev_parse(
                    str(self._settings.repo_root),
                    f"{self._config.remote_name}/{branch}",
                )
            return self._git.rev_parse(str(self._settings.repo_root), branch)
        except GitSafetyError:
            return None

    def _prepare_task_worker(self, worker: WorkerConfig, task: BuildTask) -> WorkerConfig:
        resume = bool(task.branch_name)
        branch = task.branch_name if resume else task_branch_name(task.task_id)
        self._require_no_cross_objective_branch_collision_before_prepare(task, branch)
        if self._config.use_clone_pool and self._config.clone_pool_root:
            clone_path = ensure_repo_clone(
                self._config.clone_pool_root,
                repo_root=self._settings.repo_root,
                slot_id=worker.worker_id,
                remote=self._config.remote_name or "origin",
                allowed_roots=self._config.allowed_workspace_roots,
            )
            sync_task_branch(
                clone_path,
                branch_name=branch,
                base_ref=self._config.main_ref,
                remote=self._config.remote_name or "origin",
                resume=resume,
            )
            return dataclasses.replace(worker, worktree_path=str(clone_path), branch_name=branch)
        prepare_task_workspace(
            worker.worktree_path,
            repo_root=self._settings.repo_root,
            branch_name=branch,
            base_ref=self._config.main_ref,
            remote=self._config.remote_name,
            resume=resume,
            allowed_roots=self._config.allowed_workspace_roots,
        )
        waiting = task.waiting_input if isinstance(task.waiting_input, dict) else {}
        conflict_rec = waiting.get("conflict_recovery")
        if conflict_rec and worker.worktree_path:
            wt = Path(worker.worktree_path)
            main_ref = self._config.main_ref
            identity_args = resolve_git_identity_args(wt)
            _git(wt, *identity_args, "merge", "--no-ff", "-m", f"Merge {main_ref} into {branch}", f"refs/heads/{main_ref}")
        return dataclasses.replace(worker, branch_name=branch)

    def _prepare_git_stage_worker(self, worker: WorkerConfig, task: BuildTask) -> WorkerConfig:
        if self._config.use_clone_pool and self._config.clone_pool_root:
            return self._prepare_task_worker(worker, task)
        return worker

    def _worker_slot_worktree_path(self, worker: WorkerConfig) -> str | None:
        if worker.worktree_path:
            return worker.worktree_path
        if not (self._config.use_clone_pool and self._config.clone_pool_root):
            return None
        pool_dir = (
            Path(self._config.clone_pool_root).expanduser().resolve()
            / repo_identity(self._settings.repo_root, remote=self._config.remote_name or "origin")
        )
        return str(pool_dir / worker.worker_id)

    def _worker_slot_is_active(self, session: Session, worker: WorkerConfig) -> bool:
        norm_wt = normalize_worktree_path(self._worker_slot_worktree_path(worker))
        return bool(norm_wt and norm_wt in active_worktrees(session, _now()))

    def _run_task_setup(self, session: Session, task: BuildTask, worker: WorkerConfig) -> bool:
        """Run the project's bootstrap commands once per prepared task
        workspace, before the agent starts. The outcome, recorded as durable
        evidence, gates whether the task may launch."""
        bootstrap_commands = self._bootstrap_command_rows()
        if not bootstrap_commands or not worker.worktree_path:
            return True
        cwd = worker.worktree_path
        command_strings = [str(row["command"]) for row in bootstrap_commands]
        rows = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task.task_id)
            .where(BuildTaskEvent.event_type == "runner.setup")
            .order_by(BuildTaskEvent.created_at.desc())
        ).all()
        for row in rows:
            data = row.event_data or {}
            same_commands = data.get("commands") == command_strings
            same_bootstrap = data.get("bootstrap_commands") in (None, bootstrap_commands)
            if data.get("workspace") == str(cwd) and same_commands and same_bootstrap:
                if data.get("passed"):
                    return True
                break
        results: list[dict[str, object]] = []
        passed = True
        for row in bootstrap_commands:
            outcome = run_validation(
                [str(row["command"])],
                cwd,
                timeout_seconds=float(row.get("timeout_seconds") or 900.0),
            )
            results.extend(outcome.results)
            if not outcome.passed:
                passed = False
                break
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="runner.setup",
                actor="runner",
                event_data={
                    "phase": "bootstrap",
                    "passed": passed,
                    "workspace": str(cwd),
                    "commands": command_strings,
                    "bootstrap_commands": bootstrap_commands,
                    "results": results,
                },
            ),
        )
        if passed:
            return True
        failure_summary = [
            f"{item['command']} -> {item.get('failure_type') or 'EXIT_CODE'} exit {item['exit_code']}: {item['output_tail'][-500:]}"
            for item in results
            if item["exit_code"] != 0
        ]
        first_failure_type = next((str(item.get("failure_type") or "EXIT_CODE") for item in results if item["exit_code"] != 0), "EXIT_CODE")
        self._block_task(
            session,
            task.task_id,
            "SETUP_FAILED",
            invariant="SETUP_FAILED",
            worker=worker,
            branch=worker.branch_name,
            worktree=cwd,
            error="; ".join(failure_summary),
            recovery_classification="RECOVERABLE_WORKTREE",
            extra_data={"failure_type": first_failure_type, "phase": "bootstrap"},
        )
        return False

    def _bootstrap_command_rows(self) -> list[dict[str, object]]:
        rows = [dict(row) for row in self._config.bootstrap_commands]
        if rows:
            return rows
        return [
            {"command": command, "timeout_seconds": self._config.validation_timeout_seconds, "required_tools": []}
            for command in self._config.setup_commands
        ]

    def _require_no_cross_objective_branch_collision_before_prepare(
        self, task: BuildTask, branch: str
    ) -> None:
        """Fail before workspace preparation can reset a colliding task branch."""
        with self._session_factory() as session:
            owner = session.scalar(
                select(BuildTask)
                .where(BuildTask.task_id != task.task_id)
                .where(BuildTask.branch_name == branch)
                .where(BuildTask.state.notin_(("DONE", "FAILED", "STALE")))
                .limit(1)
            )
            if owner is None or owner.objective_id == task.objective_id:
                return
            owner_task_id = owner.task_id
        if _task_branch_has_real_work(branch):
            raise CoordinatorPolicyError(
                f"Branch {branch} already belongs to task {owner_task_id}; refusing cross-objective collision"
            )

    def _validate_worker_worktree(self, worker: WorkerConfig) -> None:
        if not worker.worktree_path:
            return
        if self._config.use_clone_pool and self._config.clone_pool_root:
            # The clone was already provisioned and verified in
            # _prepare_task_worker; re-verify its remote here too since a
            # pooled slot can be reused by a later task run in a separate
            # process, and remote drift must fail closed rather than let a
            # worker silently operate on the wrong repository.
            expected = expected_remote_url(
                self._settings.repo_root, remote=self._config.remote_name or "origin"
            )
            verify_clone_remote(
                Path(worker.worktree_path), expected, remote=self._config.remote_name or "origin"
            )
            return
        try:
            ensure_worktree(
                worker.worktree_path,
                repo_root=self._settings.repo_root,
                branch_name=worker.branch_name,
                base_sha=self._config.main_ref,
                allowed_roots=self._config.allowed_workspace_roots,
            )
        except WorktreeValidationError as provisioning_error:
            try:
                validate_worktree_path(
                    worker.worktree_path,
                    allowed_roots=self._config.allowed_workspace_roots,
                    require_git=worker.adapter == "subprocess",
                )
            except WorktreeValidationError as validation_error:
                raise WorktreeValidationError(
                    f"{validation_error} (automatic provisioning failed: {provisioning_error})"
                ) from validation_error

    def _git_cwd(self, worker: WorkerConfig, task: BuildTask | None = None) -> str:
        if worker.worktree_path:
            return worker.worktree_path
        if task is not None and task.worktree_path:
            return task.worktree_path
        return str(self._settings.repo_root)

    def _has_blocking_integration(self, session: Session, task_id: str) -> bool:
        existing = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "INTEGRATION")
        ).all()
        return any(row.status in LIVE_EXECUTION_STATUSES | {"SUCCEEDED"} for row in existing)

    def _has_live_reviewer(self, session: Session, task_id: str) -> bool:
        live = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status.in_(tuple(LIVE_EXECUTION_STATUSES)))
        )
        return live is not None

    def _review_cycles(self, session: Session, task_id: str) -> int:
        rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING", "SUCCEEDED")))
        ).all()
        count = 0
        for row in rows:
            data = row.result_data or {}
            review = data.get("review") if isinstance(data.get("review"), dict) else data
            if str(review.get("verdict", "")).upper() == "REVIEW_ENVIRONMENT_BLOCKED":
                continue
            count += 1
        return count

    def _review_environment_attempts(self, session: Session, task_id: str) -> int:
        since = session.scalar(
            select(BuildTaskClaim.claimed_at)
            .where(BuildTaskClaim.task_id == task_id)
            .where(BuildTaskClaim.claim_type.in_(("IMPLEMENTATION", "REMEDIATION")))
            .order_by(BuildTaskClaim.claimed_at.desc())
            .limit(1)
        )
        rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status == "SUCCEEDED")
        ).all()
        count = 0
        for row in rows:
            if since is not None and row.launched_at is not None and _naive(row.launched_at) < _naive(since):
                continue
            data = row.result_data or {}
            review = data.get("review") if isinstance(data.get("review"), dict) else data
            if str(review.get("verdict", "")).upper() == "REVIEW_ENVIRONMENT_BLOCKED":
                count += 1
        protocol_events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.review_protocol_blocked")
        ).all()
        for event in protocol_events:
            if since is not None and event.created_at is not None and _naive(event.created_at) < _naive(since):
                continue
            count += 1
        return count

    def _no_change_workers(self, session: Session, task_id: str) -> set[str]:
        task = session.get(BuildTask, task_id)
        retry_generation = int((task.retry_generation if task is not None else 0) or 0)
        rows = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.no_changes_produced")
        ).all()
        workers: set[str] = set()
        for row in rows:
            data = row.event_data or {}
            try:
                event_generation = int(data.get("retry_generation", 0) or 0)
            except (TypeError, ValueError):
                event_generation = 0
            worker_id = str(data.get("worker_id") or "").strip()
            if worker_id and event_generation == retry_generation:
                workers.add(worker_id)
        return workers

    def _no_change_providers(self, session: Session, task_id: str) -> set[str]:
        task = session.get(BuildTask, task_id)
        retry_generation = int((task.retry_generation if task is not None else 0) or 0)
        rows = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.no_changes_produced")
        ).all()
        providers: set[str] = set()
        for row in rows:
            data = row.event_data or {}
            try:
                event_generation = int(data.get("retry_generation", 0) or 0)
            except (TypeError, ValueError):
                event_generation = 0
            provider = str(data.get("provider") or "").strip()
            if provider and event_generation == retry_generation:
                providers.add(provider)
        return providers

    def _record_no_changes_produced(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        *,
        retry_generation: int,
        attempt: int,
        detail: str,
    ) -> None:
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.no_changes_produced",
                actor="runner",
                event_data={
                    "provider": execution.provider,
                    "worker_id": execution.worker_id,
                    "retry_generation": retry_generation,
                    "attempt": attempt,
                    "detail": detail,
                },
            ),
        )

    def _reconcile_no_changes_produced(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        *,
        detail: str,
        retry_generation: int,
    ) -> str:
        task = session.get(BuildTask, execution.task_id)
        evidence = self._no_change_reconciliation_evidence(session, task, execution, detail=detail)
        evidence["retry_generation"] = retry_generation
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.no_changes_reconciled",
                actor="runner",
                claim_id=execution.claim_id,
                event_data=evidence,
            ),
        )
        outcome = str(evidence.get("outcome") or "UNRESOLVED")
        if task is None:
            return outcome
        if outcome == "ALREADY_SATISFIED":
            sha = evidence.get("reviewed_feature_sha")
            if isinstance(sha, str) and sha:
                execution.reviewed_feature_sha = sha
                execution.result_data = {
                    **(execution.result_data or {}),
                    "feature_sha": sha,
                    "reviewed_feature_sha": sha,
                    "reconciliation_state": outcome,
                    "satisfied_by_existing_implementation": True,
                    "reconciliation_evidence": evidence,
                }
                transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
                transition_task(session, execution.task_id, "VALIDATING", actor="runner")
                if not self._validation_gate(session, task, execution, result):
                    return outcome
                transition_task(
                    session,
                    execution.task_id,
                    "REVIEW_READY" if task.review_policy != "NONE" else "DONE",
                    actor="runner",
                    reason="task already satisfied by existing implementation; deterministic validation passed",
                    event_data=evidence,
                )
            return outcome
        if outcome == "STALE_OR_OBSOLETE":
            result.escalations.append(f"{execution.task_id}:STALE_OR_OBSOLETE_TASK_DEFINITION")
            self._block_task(
                session,
                execution.task_id,
                "STALE_OR_OBSOLETE_TASK_DEFINITION",
                invariant="TASK_DEFINITION_RECONCILIATION",
                execution=execution,
                error="Task definition is stale or obsolete after reconciliation with current main",
                recovery_classification="TASK_REDESIGN_REQUIRED",
                extra_data=evidence,
            )
            return outcome
        return outcome

    def _no_change_reconciliation_evidence(
        self,
        session: Session,
        task: BuildTask | None,
        execution: BuildRunnerExecution,
        *,
        detail: str,
    ) -> dict:
        repo_root = Path(self._settings.repo_root)
        current_sha = self._rev_parse(repo_root, "HEAD") or self._rev_parse(repo_root, self._config.main_ref)
        task_base = task.base_sha if task is not None else None
        changed_paths = self._changed_paths_since_base(repo_root, task_base, current_sha)
        scope = list(task.permitted_scope or []) if task is not None else []
        scope_changed = self._scope_changed(scope, changed_paths)
        metadata = task.definition_metadata if task is not None and isinstance(task.definition_metadata, dict) else {}
        marked_stale = bool(metadata.get("stale") or metadata.get("obsolete") or metadata.get("superseded_by"))
        satisfaction = self._task_satisfaction_evidence(session, task)
        already_satisfied = bool(satisfaction.get("satisfied"))
        recent_integrations = self._recent_integration_evidence(session, execution.task_id)
        superseding_integration = self._find_superseding_integration(repo_root, scope, recent_integrations)
        scope_touched_by_recent_integration = bool(scope_changed and superseding_integration is not None)
        integration_explicitly_reconciles_task = self._integration_explicitly_reconciles_task(
            task, superseding_integration
        )
        stale_by_scope_reconciliation = self._stale_by_scope_reconciliation(
            task,
            satisfaction,
            superseding_integration,
            scope_touched_by_recent_integration,
        )
        outcome = "UNRESOLVED"
        if already_satisfied:
            outcome = "ALREADY_SATISFIED"
        elif marked_stale or stale_by_scope_reconciliation:
            outcome = "STALE_OR_OBSOLETE"
        return {
            "outcome": outcome,
            "detail": detail,
            "deterministic_recheck": True,
            "acceptance_appears_satisfied": already_satisfied,
            "acceptance_satisfaction_evidence": satisfaction,
            "definition_marked_stale": marked_stale,
            "stale_by_scope_reconciliation": stale_by_scope_reconciliation,
            "integration_explicitly_reconciles_task": integration_explicitly_reconciles_task,
            "scope_touched_by_recent_integration": scope_touched_by_recent_integration,
            "superseding_integration": superseding_integration,
            "scope_changed_since_base": scope_changed,
            "changed_paths_since_base": changed_paths[:50],
            "permitted_scope": scope,
            "base_sha": task_base,
            "current_main_sha": current_sha,
            "reviewed_feature_sha": current_sha if already_satisfied else None,
            "reconciled_against": {
                "main_ref": self._config.main_ref,
                "recent_integrations": recent_integrations,
            },
        }

    def _recent_integration_evidence(self, session: Session, task_id: str) -> list[dict]:
        rows = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.event_type == "runner.integration_completed")
            .order_by(BuildTaskEvent.created_at.desc())
            .limit(5)
        ).all()
        evidence: list[dict] = []
        for row in rows:
            data = row.event_data or {}
            feature_sha = data.get("feature_sha") or data.get("reviewed_feature_sha")
            merge_commit_sha = data.get("merge_commit_sha")
            final_main_sha = data.get("final_main_sha")
            evidence.append(
                {
                    "task_id": row.task_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "feature_sha": feature_sha,
                    "merge_commit_sha": merge_commit_sha,
                    "final_main_sha": final_main_sha,
                    "integrated_sha": merge_commit_sha or final_main_sha or feature_sha,
                    "same_task": row.task_id == task_id,
                    "supersedes_task_ids": _string_list(data.get("supersedes_task_ids")),
                    "obsolete_task_ids": _string_list(data.get("obsolete_task_ids")),
                    "stale_task_ids": _string_list(data.get("stale_task_ids")),
                    "reconciles_task_ids": _string_list(data.get("reconciles_task_ids")),
                }
            )
        return evidence

    def _stale_by_scope_reconciliation(
        self,
        task: BuildTask | None,
        satisfaction: dict,
        superseding_integration: dict | None,
        scope_touched_by_recent_integration: bool,
    ) -> bool:
        if task is None or superseding_integration is None or not scope_touched_by_recent_integration:
            return False
        if satisfaction.get("satisfied"):
            return False
        return True

    def _integration_explicitly_reconciles_task(
        self, task: BuildTask | None, superseding_integration: dict | None
    ) -> bool:
        if task is None or superseding_integration is None:
            return False
        superseded_ids = {
            task_id
            for field in (
                "supersedes_task_ids",
                "obsolete_task_ids",
                "stale_task_ids",
                "reconciles_task_ids",
            )
            for task_id in _string_list(superseding_integration.get(field))
        }
        return task.task_id in superseded_ids

    def _find_superseding_integration(
        self, repo_root: Path, scope: list[str], recent_integrations: list[dict]
    ) -> dict | None:
        """Find a different task integration that concretely touched this task's scope.

        Scope drift alone is not enough to mark a task stale; the deterministic
        reconciliation tie is the recorded integration's own commit changing a
        permitted-scope path. Without that tie, genuine no-change cases stay
        UNRESOLVED and keep the bounded retry path.
        """
        if not scope:
            return None
        for integration in recent_integrations:
            if integration.get("same_task"):
                continue
            integrated_sha = integration.get("integrated_sha")
            if not isinstance(integrated_sha, str) or not integrated_sha:
                continue
            integration_paths = self._changed_paths_for_commit(repo_root, integrated_sha)
            if self._scope_changed(scope, integration_paths):
                return integration
        return None

    def _changed_paths_for_commit(self, repo_root: Path, sha: str) -> list[str]:
        proc = _git(repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", sha)
        if proc.returncode != 0:
            return []
        return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]

    def _rev_parse(self, repo_root: Path, ref: str | None) -> str | None:
        if not ref:
            return None
        proc = _git(repo_root, "rev-parse", "--verify", ref)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _changed_paths_since_base(self, repo_root: Path, base_sha: str | None, current_sha: str | None) -> list[str]:
        if not base_sha or not current_sha:
            return []
        proc = _git(repo_root, "diff", "--name-only", f"{base_sha}..{current_sha}")
        if proc.returncode != 0:
            return []
        return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]

    def _scope_changed(self, scope: list[str], changed_paths: list[str]) -> bool:
        if not scope or not changed_paths:
            return False
        normalized_scope = [item.strip().replace("\\", "/").rstrip("/") for item in scope if str(item).strip()]
        for changed in changed_paths:
            for item in normalized_scope:
                if changed == item or changed.startswith(item + "/"):
                    return True
        return False

    def _environment_blocked_reviewers(self, session: Session, task_id: str) -> set[str]:
        since = session.scalar(
            select(BuildTaskClaim.claimed_at)
            .where(BuildTaskClaim.task_id == task_id)
            .where(BuildTaskClaim.claim_type.in_(("IMPLEMENTATION", "REMEDIATION")))
            .order_by(BuildTaskClaim.claimed_at.desc())
            .limit(1)
        )
        rows = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status == "SUCCEEDED")
        ).all()
        blocked: set[str] = set()
        for row in rows:
            if since is not None and row.launched_at is not None and _naive(row.launched_at) < _naive(since):
                continue
            data = row.result_data or {}
            review = data.get("review") if isinstance(data.get("review"), dict) else data
            if str(review.get("verdict", "")).upper() == "REVIEW_ENVIRONMENT_BLOCKED":
                blocked.add(row.worker_id)
        return blocked

    def _latest_block_reason(self, session: Session, task_id: str) -> str | None:
        event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "task.transitioned")
            .where(BuildTaskEvent.to_state == "BLOCKED")
            .order_by(BuildTaskEvent.created_at.desc())
            .limit(1)
        )
        if event is None:
            task = session.get(BuildTask, task_id)
            if task and isinstance(task.waiting_input, dict):
                return task.waiting_input.get("blocked_reason")
            return None
        return (event.event_data or {}).get("reason")

    def _release_sha_drift_conflict_gate(self, session: Session, task: BuildTask) -> None:
        if not task.objective_id:
            return
        for gate in open_gates(session, task.objective_id):
            if gate.gate_type != "UNRESOLVABLE_CONFLICT":
                continue
            if gate.source_task_id != task.task_id:
                continue
            if "REVIEWED_SHA_CHANGED" not in (gate.reason or ""):
                continue
            resolve_gate(
                session,
                gate.gate_id,
                resolved_by="runner",
                resolution_note="automatic rereview after reviewed SHA drift",
            )

    def _request_rereview(
        self,
        session: Session,
        task_id: str,
        result: RunnerCycleResult,
        *,
        reason: str,
    ) -> None:
        result.escalations.append(f"{task_id}:{reason}")
        if self._has_live_reviewer(session, task_id):
            return
        if self._review_cycles(session, task_id) >= self._config.max_remediation_cycles + 1:
            result.escalations.append(f"{task_id}:REMEDIATION_LIMIT_REACHED")
            self._block_task(session, task_id, "REMEDIATION_LIMIT_REACHED")
            return
        task = session.get(BuildTask, task_id)
        if task is None:
            return
        if task.state == "REVIEW_READY":
            self._release_sha_drift_conflict_gate(session, task)
            return
        if task.state not in {"REVIEWING", "BLOCKED"}:
            return
        try:
            release_active_claims(session, task_id, completed=True)
        except CoordinatorPolicyError:
            pass
        try:
            transition_task(
                session,
                task_id,
                "REVIEW_READY",
                actor="runner",
                reason=f"automatic rereview required: {reason}",
            )
        except CoordinatorPolicyError:
            self._block_task(session, task_id, reason)
            return
        if task_id not in result.recovered:
            result.recovered.append(task_id)
        self._release_sha_drift_conflict_gate(session, task)

    def _release_blocker_gate(self, session: Session, task: BuildTask, reason: str) -> None:
        if not task.objective_id:
            return
        try:
            for gate in open_gates(session, task.objective_id):
                if gate.source_task_id == task.task_id:
                    resolve_gate(
                        session,
                        gate.gate_id,
                        resolved_by="runner",
                        resolution_note=f"automatic recovery of {reason}",
                    )
        except Exception:
            pass

    def _is_execution_retry_exhausted_reason(self, task: BuildTask, reason: str) -> bool:
        if reason == "EXECUTION_RETRY_LIMIT_REACHED":
            return True
        if not reason.startswith("PROVIDER_FAILURE_RETRIES_EXHAUSTED:"):
            return False
        waiting = task.waiting_input if isinstance(task.waiting_input, dict) else {}
        evidence = waiting.get("failure_evidence") if isinstance(waiting.get("failure_evidence"), dict) else {}
        return evidence.get("underlying_invariant") == "EXECUTION_RETRY_LIMIT_REACHED"

    def _recover_diagnosed_blockers(self, session: Session, result: RunnerCycleResult) -> None:
        blocked = session.scalars(select(BuildTask).where(BuildTask.state == "BLOCKED")).all()
        for task in blocked:
            if not self._target_allows(task.task_id):
                continue
            if not task_source_is_executable(task):
                continue
            reason = self._latest_block_reason(session, task.task_id)
            if not reason:
                continue

            capacity_failure = _provider_capacity_failure_from_block_reason(reason)
            if capacity_failure:
                latest_execution = session.scalar(
                    select(BuildRunnerExecution)
                    .where(BuildRunnerExecution.task_id == task.task_id)
                    .order_by(
                        BuildRunnerExecution.completed_at.desc(),
                        BuildRunnerExecution.launched_at.desc(),
                    )
                    .limit(1)
                )
                role = latest_execution.role if latest_execution is not None else "BUILDER"
                if role == "REVIEWER":
                    target_state = "REVIEW_READY"
                elif role == "PLANNER":
                    target_state = "READY"
                else:
                    target_state = "RESUMABLE"
                try:
                    transition_task(
                        session,
                        task.task_id,
                        target_state,
                        actor="runner",
                        reason=(
                            f"provider capacity recovered from legacy block {capacity_failure}; "
                            f"resuming to {target_state}"
                        ),
                    )
                    record_event(
                        session,
                        EventInput(
                            task_id=task.task_id,
                            event_type="runner.provider_capacity_blocker_recovered",
                            actor="runner",
                            event_data={
                                "provider_failure": capacity_failure,
                                "prior_reason": reason,
                                "resumed_to": target_state,
                            },
                        ),
                    )
                    if task.task_id not in result.recovered:
                        result.recovered.append(task.task_id)
                    self._release_blocker_gate(session, task, reason)
                except CoordinatorPolicyError:
                    pass
                continue

            if is_planner_task(task) and reason == "MALFORMED_EXECUTOR_RESULT":
                latest = session.scalar(
                    select(BuildRunnerExecution)
                    .where(BuildRunnerExecution.task_id == task.task_id)
                    .where(BuildRunnerExecution.role == "PLANNER")
                    .where(BuildRunnerExecution.status == "FAILED")
                    .order_by(BuildRunnerExecution.completed_at.desc())
                    .limit(1)
                )
                current_contract_hash = self._planner_contract_hash()
                prior_contract_hash = (
                    (latest.result_data or {}).get("planner_contract_hash")
                    if latest is not None
                    else None
                )
                # Legacy malformed planner executions did not persist a
                # planner_contract_hash. Treat them as an older contract so
                # deployment of this hardening change gets exactly one retry.
                if (
                    latest is not None
                    and (
                        not prior_contract_hash
                        or current_contract_hash != prior_contract_hash
                    )
                ):
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "READY",
                            actor="runner",
                            reason=(
                                "planner prompt/contract changed after malformed result; "
                                "retrying once with the new contract"
                            ),
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.planner_contract_recovered",
                                actor="runner",
                                event_data={
                                    "prior_contract_hash": prior_contract_hash,
                                    "current_contract_hash": current_contract_hash,
                                    "resumed_to": "READY",
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(
                            session, task, "MALFORMED_EXECUTOR_RESULT"
                        )
                    except CoordinatorPolicyError:
                        pass
                # An unchanged malformed planner prompt is deliberately left
                # blocked so the same bad contract cannot burn provider quota
                # on every orchestration cycle.
                continue

            if reason == "WORKING_CHECKOUT_DIRTY":
                repo_root = Path(self._settings.repo_root)
                reconcile_displaced_task_work(repo_root)
                proc = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0 and not proc.stdout.strip():
                    # The checkout is clean! Check if the task was already approved for integration
                    has_approved_review = False
                    rows = session.scalars(
                        select(BuildRunnerExecution)
                        .where(BuildRunnerExecution.task_id == task.task_id)
                        .where(BuildRunnerExecution.role == "REVIEWER")
                        .where(BuildRunnerExecution.status == "SUCCEEDED")
                        .order_by(BuildRunnerExecution.completed_at.desc())
                    ).all()
                    for r in rows:
                        data = r.result_data or {}
                        review = data.get("review") if isinstance(data.get("review"), dict) else data
                        v = str(review.get("verdict", "")).upper()
                        if v in ("GREEN", "GREEN_WITH_NOTES") and review.get("ready_for_integration", True):
                            has_approved_review = True
                            break

                    target_state = "REVIEWING" if has_approved_review else "READY"
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            target_state,
                            actor="runner",
                            reason=f"working checkout is clean; automatically resuming to {target_state}",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": "WORKING_CHECKOUT_DIRTY",
                                    "resumed_to": target_state,
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "WORKING_CHECKOUT_DIRTY")
                    except CoordinatorPolicyError:
                        pass

            elif reason in ("COORDINATOR_INVARIANT_FAILURE", "NO_CHANGES_PRODUCED", "MALFORMED_EXECUTOR_RESULT"):
                if self._check_task_already_satisfied(session, task.task_id):
                    repo_root = Path(self._settings.repo_root)
                    current_sha = self._rev_parse(repo_root, "HEAD") or self._rev_parse(repo_root, self._config.main_ref)
                    evidence = self._task_satisfaction_evidence(session, task)
                    execution = session.scalar(
                        select(BuildRunnerExecution)
                        .where(BuildRunnerExecution.task_id == task.task_id)
                        .order_by(BuildRunnerExecution.completed_at.desc(), BuildRunnerExecution.launched_at.desc())
                        .limit(1)
                    )
                    if execution is not None and current_sha:
                        execution.reviewed_feature_sha = current_sha
                        execution.result_data = {
                            **(execution.result_data or {}),
                            "feature_sha": current_sha,
                            "reviewed_feature_sha": current_sha,
                            "satisfied_by_existing_implementation": True,
                            "reconciliation_state": "ALREADY_SATISFIED",
                            "reconciliation_evidence": evidence,
                        }
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "REVIEW_READY",
                            actor="runner",
                            reason="existing implementation satisfies acceptance criteria; automatically resuming to REVIEW_READY",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": reason,
                                    "resumed_to": "REVIEW_READY",
                                    "satisfied_by_existing_implementation": True,
                                    "reconciliation_evidence": evidence,
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, reason)
                    except CoordinatorPolicyError:
                        pass
                else:
                    repo_root = Path(self._settings.repo_root)
                    branch = task.branch_name or task_branch_name(task.task_id)
                    has_work = False
                    if branch and _ref_exists(repo_root, branch):
                        base = task.base_sha or self._config.main_ref
                        ahead_proc = _git(repo_root, "rev-list", "--count", f"{base}..{branch}")
                        if ahead_proc.returncode == 0 and int(ahead_proc.stdout.strip() or 0) > 0:
                            has_work = True
                    if has_work:
                        try:
                            transition_task(
                                session,
                                task.task_id,
                                "REVIEW_READY" if task.review_policy != "NONE" else "DONE",
                                actor="runner",
                                reason=f"task branch contains existing commits; recovering {reason} to REVIEW_READY",
                            )
                            record_event(
                                session,
                                EventInput(
                                    task_id=task.task_id,
                                    event_type="runner.blocker_recovered",
                                    actor="runner",
                                    event_data={
                                        "reason": reason,
                                        "recovery_type": "EXISTING_WORK_FOUND",
                                        "resumed_to": "REVIEW_READY",
                                    },
                                ),
                            )
                            if task.task_id not in result.recovered:
                                result.recovered.append(task.task_id)
                            self._release_blocker_gate(session, task, reason)
                        except CoordinatorPolicyError:
                            pass
                    else:
                        builder_attempts = session.scalar(
                            select(func.count())
                            .select_from(BuildRunnerExecution)
                            .where(BuildRunnerExecution.task_id == task.task_id)
                            .where(BuildRunnerExecution.role.in_(("BUILDER", "REMEDIATION")))
                            .where(BuildRunnerExecution.adapter != "validation")
                            .where(BuildRunnerExecution.status.in_(("FAILED", "SUCCEEDED")))
                        ) or 0
                        if builder_attempts < self._config.max_execution_attempts:
                            release_active_claims(session, task.task_id, completed=False)
                            try:
                                transition_task(
                                    session,
                                    task.task_id,
                                    "READY",
                                    actor="runner",
                                    reason=f"recovering {reason} with {self._config.max_execution_attempts - builder_attempts} attempts remaining; resuming to READY",
                                )
                                record_event(
                                    session,
                                    EventInput(
                                        task_id=task.task_id,
                                        event_type="runner.blocker_recovered",
                                        actor="runner",
                                        event_data={
                                            "reason": reason,
                                            "recovery_type": "ATTEMPTS_REMAINING",
                                            "resumed_to": "READY",
                                            "attempts_used": builder_attempts,
                                        },
                                    ),
                                )
                                if task.task_id not in result.recovered:
                                    result.recovered.append(task.task_id)
                                self._release_blocker_gate(session, task, reason)
                            except CoordinatorPolicyError:
                                pass

            elif self._is_execution_retry_exhausted_reason(task, reason):
                rows = session.scalars(
                    select(BuildRunnerExecution)
                    .where(BuildRunnerExecution.task_id == task.task_id)
                    .order_by(BuildRunnerExecution.completed_at.desc())
                ).all()
                has_transient_failures = any(
                    r.role == "REVIEWER"
                    or (r.result_data or {}).get("reconciliation_state") in ("WORKER_EXITED", "LOST", "STALE_CLAIM", "NO_CLAIM", "PROVIDER_FAILED")
                    or (r.result_data or {}).get("provider_failure")
                    for r in rows
                )
                if has_transient_failures or not rows:
                    task.retry_generation = int((task.retry_generation or 0)) + 1
                    release_active_claims(session, task.task_id, completed=False)
                    has_feature_sha = any(bool(r.reviewed_feature_sha or (r.result_data or {}).get("feature_sha")) for r in rows)
                    target_state = "REVIEW_READY" if has_feature_sha and task.review_policy != "NONE" else "READY"
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            target_state,
                            actor="runner",
                            reason=f"transient infrastructure/provider failures recovered; advanced retry generation to {task.retry_generation}; resuming to {target_state}",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": reason,
                                    "underlying_invariant": "EXECUTION_RETRY_LIMIT_REACHED",
                                    "recovery_type": "INFRASTRUCTURE_RETRY_RECOVERY",
                                    "resumed_to": target_state,
                                    "new_retry_generation": task.retry_generation,
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, reason)
                    except CoordinatorPolicyError:
                        pass

            elif reason == "WORKTREE_INVALID":
                if self._recover_worktree_for_task(session, task):
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "READY",
                            actor="runner",
                            reason="worktree pruned and re-ensured; resuming task to READY",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": "WORKTREE_INVALID",
                                    "resumed_to": "READY",
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "WORKTREE_INVALID")
                    except CoordinatorPolicyError:
                        pass

            elif reason == "GIT_SAFETY_FAILURE":
                repo_root = Path(self._settings.repo_root)
                reconcile_displaced_task_work(repo_root)
                proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True, check=False)
                if proc.returncode == 0 and not proc.stdout.strip():
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "READY",
                            actor="runner",
                            reason="git safety condition recovered; resuming to READY",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": "GIT_SAFETY_FAILURE",
                                    "resumed_to": "READY",
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "GIT_SAFETY_FAILURE")
                    except CoordinatorPolicyError:
                        pass

            elif reason == "REVIEWED_SHA_CHANGED":
                self._request_rereview(session, task.task_id, result, reason="REVIEWED_SHA_CHANGED")

            elif reason == "BRANCH_MOVED_CONCURRENTLY":
                reviewed_sha = session.scalar(
                    select(BuildRunnerExecution.reviewed_feature_sha)
                    .where(BuildRunnerExecution.task_id == task.task_id)
                    .where(BuildRunnerExecution.role.in_(("REVIEWER", "INTEGRATION")))
                    .where(BuildRunnerExecution.reviewed_feature_sha.is_not(None))
                    .order_by(BuildRunnerExecution.completed_at.desc())
                    .limit(1)
                )
                if not reviewed_sha:
                    self._request_rereview(session, task.task_id, result, reason="REVIEWED_SHA_CHANGED")
                    continue

                repo_root = Path(self._settings.repo_root)
                branch = task.branch_name or task_branch_name(task.task_id)
                current_feature_sha = None
                if branch:
                    proc = _git(repo_root, "rev-parse", "--verify", f"{branch}^{{commit}}")
                    if proc.returncode == 0:
                        current_feature_sha = proc.stdout.strip()

                if current_feature_sha and current_feature_sha != reviewed_sha:
                    self._request_rereview(session, task.task_id, result, reason="REVIEWED_SHA_CHANGED")
                    continue

                try:
                    release_active_claims(session, task.task_id, completed=False)
                except CoordinatorPolicyError:
                    pass
                try:
                    transition_task(
                        session,
                        task.task_id,
                        "REVIEWING",
                        actor="runner",
                        reason="main moved concurrently during integration; retrying exact reviewed SHA against current main",
                    )
                    record_event(
                        session,
                        EventInput(
                            task_id=task.task_id,
                            event_type="runner.blocker_recovered",
                            actor="runner",
                            event_data={
                                "reason": "BRANCH_MOVED_CONCURRENTLY",
                                "resumed_to": "REVIEWING",
                                "reviewed_feature_sha": reviewed_sha,
                            },
                        ),
                    )
                    if task.task_id not in result.recovered:
                        result.recovered.append(task.task_id)
                    self._release_blocker_gate(session, task, "BRANCH_MOVED_CONCURRENTLY")
                except CoordinatorPolicyError:
                    pass

            elif reason == "REVIEW_ENVIRONMENT_BLOCKED":
                attempts = self._review_environment_attempts(session, task.task_id)
                if attempts < self._config.max_review_environment_attempts:
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "REVIEW_READY",
                            actor="runner",
                            reason="adaptive recovery: retrying review after review environment blocked",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": "REVIEW_ENVIRONMENT_BLOCKED",
                                    "resumed_to": "REVIEW_READY",
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "REVIEW_ENVIRONMENT_BLOCKED")
                    except CoordinatorPolicyError:
                        pass

            elif reason == "UPSTREAM_PUSH_FAILED":
                if not self._config.auto_push_allowed or not getattr(self._config, "push_upstream", True):
                    try:
                        transition_task(
                            session,
                            task.task_id,
                            "DONE",
                            actor="runner",
                            reason="upstream push not required; task completed locally",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.blocker_recovered",
                                actor="runner",
                                event_data={
                                    "reason": "UPSTREAM_PUSH_FAILED",
                                    "resumed_to": "DONE",
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "UPSTREAM_PUSH_FAILED")
                    except CoordinatorPolicyError:
                        pass

            elif reason == "MALFORMED_EXECUTOR_RESULT":
                repo_root = Path(self._settings.repo_root)
                has_work = False
                if task.branch_name:
                    try:
                        count = _git(repo_root, "rev-list", "--count", f"{main_ref}..{task.branch_name}").stdout.strip()
                        has_work = int(count) > 0
                    except Exception:
                        pass
                target_state = "REVIEW_READY" if has_work else "READY"
                try:
                    transition_task(
                        session,
                        task.task_id,
                        target_state,
                        actor="runner",
                        reason=f"malformed executor result recovered: resuming to {target_state}",
                    )
                    record_event(
                        session,
                        EventInput(
                            task_id=task.task_id,
                            event_type="runner.blocker_recovered",
                            actor="runner",
                            event_data={
                                "reason": "MALFORMED_EXECUTOR_RESULT",
                                "resumed_to": target_state,
                            },
                        ),
                    )
                    if task.task_id not in result.recovered:
                        result.recovered.append(task.task_id)
                    self._release_blocker_gate(session, task, "MALFORMED_EXECUTOR_RESULT")
                except CoordinatorPolicyError:
                    pass

            elif reason == "MERGE_CONFLICT":
                reviewed_sha = session.scalar(
                    select(BuildRunnerExecution.reviewed_feature_sha)
                    .where(BuildRunnerExecution.task_id == task.task_id)
                    .where(BuildRunnerExecution.role.in_(("REVIEWER", "INTEGRATION")))
                    .where(BuildRunnerExecution.reviewed_feature_sha.is_not(None))
                    .order_by(BuildRunnerExecution.completed_at.desc())
                    .limit(1)
                )
                if not reviewed_sha and isinstance(task.waiting_input, dict):
                    ev = task.waiting_input.get("failure_evidence") or {}
                    if isinstance(ev, dict) and ev.get("task_sha"):
                        reviewed_sha = str(ev["task_sha"])
                if not reviewed_sha and task.branch_name:
                    try:
                        reviewed_sha = self._git.rev_parse(str(self._settings.repo_root), task.branch_name)
                    except Exception:
                        pass
                if reviewed_sha:
                    try:
                        assessment = assess_mechanical_merge(
                            self._git,
                            cwd=str(self._settings.repo_root),
                            branch_name=task.branch_name or "HEAD",
                            reviewed_feature_sha=reviewed_sha,
                            remote=self._config.remote_name,
                            main_ref=self._config.main_ref,
                        )
                        if not assessment.conflict:
                            transition_task(
                                session,
                                task.task_id,
                                "REVIEW_READY",
                                actor="runner",
                                reason="merge conflict resolved on main; re-reviewing feature branch",
                            )
                            record_event(
                                session,
                                EventInput(
                                    task_id=task.task_id,
                                    event_type="runner.blocker_recovered",
                                    actor="runner",
                                    event_data={
                                        "reason": "MERGE_CONFLICT",
                                        "resumed_to": "REVIEW_READY",
                                    },
                                ),
                            )
                            if task.task_id not in result.recovered:
                                result.recovered.append(task.task_id)
                            self._release_blocker_gate(session, task, "MERGE_CONFLICT")
                        else:
                            waiting = dict(task.waiting_input) if isinstance(task.waiting_input, dict) else {}
                            conflict_state = waiting.get("conflict_recovery") if isinstance(waiting.get("conflict_recovery"), dict) else {}
                            attempts = int(conflict_state.get("attempts", 0) or 0)
                            max_attempts = getattr(self._config, "max_conflict_recovery_attempts", 2)
                            if attempts < max_attempts:
                                conflict_data = {
                                    "conflict_type": "MERGE_CONFLICT",
                                    "original_reviewed_sha": reviewed_sha,
                                    "conflict_paths": list(assessment.conflict_paths),
                                    "current_main_sha": assessment.current_main_sha,
                                    "conflicting_current_main_sha": assessment.current_main_sha,
                                    "task_sha": reviewed_sha,
                                    "merge_base": assessment.merge_base,
                                    "attempts": attempts + 1,
                                    "max_attempts": max_attempts,
                                }
                                waiting["conflict_recovery"] = conflict_data
                                task.waiting_input = waiting
                                try:
                                    release_active_claims(session, task.task_id, completed=False)
                                except CoordinatorPolicyError:
                                    pass
                                transition_task(
                                    session,
                                    task.task_id,
                                    "REWORK_REQUIRED",
                                    actor="runner",
                                    reason=f"recovering MERGE_CONFLICT with automated conflict remediation ({len(assessment.conflict_paths)} paths, attempt {attempts + 1}/{max_attempts})",
                                )
                                record_event(
                                    session,
                                    EventInput(
                                        task_id=task.task_id,
                                        event_type="runner.blocker_recovered",
                                        actor="runner",
                                        event_data={
                                            "reason": "MERGE_CONFLICT",
                                            "resumed_to": "REWORK_REQUIRED",
                                            "conflict_paths": list(assessment.conflict_paths),
                                            "attempt": attempts + 1,
                                        },
                                    ),
                                )
                                if task.task_id not in result.recovered:
                                    result.recovered.append(task.task_id)
                                self._release_blocker_gate(session, task, "MERGE_CONFLICT")
                    except Exception:
                        waiting = dict(task.waiting_input) if isinstance(task.waiting_input, dict) else {}
                        conflict_state = waiting.get("conflict_recovery") if isinstance(waiting.get("conflict_recovery"), dict) else {}
                        attempts = int(conflict_state.get("attempts", 0) or 0)
                        max_attempts = getattr(self._config, "max_conflict_recovery_attempts", 2)
                        if attempts < max_attempts:
                            ev = waiting.get("failure_evidence") or {}
                            conflict_paths = ev.get("conflict_files") or ev.get("conflict_paths") or []
                            conflict_data = {
                                "conflict_type": "MERGE_CONFLICT",
                                "original_reviewed_sha": reviewed_sha or ev.get("task_sha", ""),
                                "conflict_paths": list(conflict_paths),
                                "current_main_sha": ev.get("current_main_sha", ""),
                                "conflicting_current_main_sha": ev.get("current_main_sha", ""),
                                "task_sha": reviewed_sha or ev.get("task_sha", ""),
                                "merge_base": ev.get("merge_base", ""),
                                "attempts": attempts + 1,
                                "max_attempts": max_attempts,
                            }
                            waiting["conflict_recovery"] = conflict_data
                            task.waiting_input = waiting
                            try:
                                release_active_claims(session, task.task_id, completed=False)
                            except CoordinatorPolicyError:
                                pass
                            transition_task(
                                session,
                                task.task_id,
                                "REWORK_REQUIRED",
                                actor="runner",
                                reason=f"recovering MERGE_CONFLICT from stored failure evidence (attempt {attempts + 1}/{max_attempts})",
                            )
                            record_event(
                                session,
                                EventInput(
                                    task_id=task.task_id,
                                    event_type="runner.blocker_recovered",
                                    actor="runner",
                                    event_data={
                                        "reason": "MERGE_CONFLICT",
                                        "resumed_to": "REWORK_REQUIRED",
                                        "attempt": attempts + 1,
                                    },
                                ),
                            )
                            if task.task_id not in result.recovered:
                                result.recovered.append(task.task_id)
                            self._release_blocker_gate(session, task, "MERGE_CONFLICT")

    def _handle_merge_conflict(
        self,
        session: Session,
        task_id: str,
        assessment: MechanicalMergeAssessment,
        result: RunnerCycleResult,
    ) -> None:
        task = session.get(BuildTask, task_id)
        if task is None:
            return

        waiting = dict(task.waiting_input) if isinstance(task.waiting_input, dict) else {}
        conflict_state = waiting.get("conflict_recovery") if isinstance(waiting.get("conflict_recovery"), dict) else {}
        attempts = int(conflict_state.get("attempts", 0) or 0)
        max_attempts = getattr(self._config, "max_conflict_recovery_attempts", 2)

        if attempts >= max_attempts:
            result.escalations.append(f"{task_id}:MERGE_CONFLICT_RECOVERY_FAILED")
            self._block_task(
                session,
                task_id,
                "MERGE_CONFLICT_RECOVERY_FAILED",
                invariant="MERGE_CONFLICT_RECOVERY_FAILED",
                error=f"merge conflict in {list(assessment.conflict_paths)} after {attempts} automated remediation attempts",
                recovery_classification="OPERATOR_ACTION_REQUIRED",
                extra_data={
                    "conflict_type": "MERGE_CONFLICT",
                    "conflict_recovery": conflict_state,
                    "original_reviewed_sha": assessment.feature_remote_sha,
                    "conflict_paths": list(assessment.conflict_paths),
                    "current_main_sha": assessment.current_main_sha,
                    "conflicting_current_main_sha": assessment.current_main_sha,
                    "task_sha": assessment.feature_remote_sha,
                    "merge_base": assessment.merge_base,
                    "attempts": attempts,
                    "max_attempts": max_attempts,
                    "conflict_recovery_worker": conflict_state.get("recovery_worker"),
                    "conflict_recovery_provider": conflict_state.get("recovery_provider"),
                    "next_action": "manual conflict resolution or task redesign required after bounded automated recovery attempts failed",
                },
            )
            return

        conflict_data = {
            "conflict_type": "MERGE_CONFLICT",
            "original_reviewed_sha": assessment.feature_remote_sha,
            "conflict_paths": list(assessment.conflict_paths),
            "current_main_sha": assessment.current_main_sha,
            "conflicting_current_main_sha": assessment.current_main_sha,
            "task_sha": assessment.feature_remote_sha,
            "merge_base": assessment.merge_base,
            "attempts": attempts + 1,
            "max_attempts": max_attempts,
        }
        waiting["conflict_recovery"] = conflict_data
        task.waiting_input = waiting
        try:
            release_active_claims(session, task_id, completed=False)
        except CoordinatorPolicyError:
            pass
        transition_task(
            session,
            task_id,
            "REWORK_REQUIRED",
            actor="runner",
            reason=f"merge conflict detected against main ({len(assessment.conflict_paths)} paths); routing to conflict remediation (attempt {attempts + 1}/{max_attempts})",
        )
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="runner.merge_conflict_remediation_scheduled",
                actor="runner",
                event_data=conflict_data,
            ),
        )

    def _reconcile_git_reality(self, session: Session, result: RunnerCycleResult) -> None:
        repo_root = Path(self._settings.repo_root)
        main_ref = self._config.main_ref
        try:
            current_main = self._git.rev_parse(str(repo_root), main_ref)
        except Exception:
            return

        # Clean up any aborted or lingering git operations in integration worktrees
        for worker in self._config.workers:
            if worker.role == "INTEGRATION" and worker.worktree_path:
                wt = Path(worker.worktree_path)
                if wt.exists():
                    git_dir = wt / ".git"
                    if git_dir.is_file():
                        try:
                            content = git_dir.read_text().strip()
                            if content.startswith("gitdir:"):
                                git_dir = Path(content.split(":", 1)[1].strip())
                        except Exception:
                            pass
                    if (git_dir / "MERGE_HEAD").exists() or (git_dir / "REBASE_HEAD").exists():
                        _git(wt, "merge", "--abort")
                        _git(wt, "rebase", "--abort")
                        _git(wt, "reset", "--hard", "HEAD")
                        _git(wt, "clean", "-fd")

        # Check tasks in INTEGRATING, REVIEWING, REVIEW_READY, or BLOCKED that are already in main
        candidates = session.scalars(
            select(BuildTask).where(
                BuildTask.state.in_(("INTEGRATING", "REVIEWING", "REVIEW_READY", "BLOCKED"))
            )
        ).all()
        for task in candidates:
            if not self._target_allows(task.task_id):
                continue
            # If an execution is currently live for this task, let normal execution observation handle it
            active_exec = session.scalar(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == task.task_id)
                .where(BuildRunnerExecution.status.in_(LIVE_EXECUTION_STATUSES))
                .limit(1)
            )
            if active_exec is not None:
                continue
            commit_candidates = []
            if task.branch_name:
                try:
                    commit_candidates.append(self._git.rev_parse(str(repo_root), task.branch_name))
                except Exception:
                    pass
            last_exec_sha = session.scalar(
                select(BuildRunnerExecution.reviewed_feature_sha)
                .where(BuildRunnerExecution.task_id == task.task_id)
                .where(BuildRunnerExecution.reviewed_feature_sha.is_not(None))
                .order_by(BuildRunnerExecution.completed_at.desc())
                .limit(1)
            )
            if last_exec_sha:
                commit_candidates.append(last_exec_sha)

            for cand in commit_candidates:
                if not cand:
                    continue
                commit_msg = _git(repo_root, "log", "-n", "1", "--format=%s", cand).stdout.strip()
                is_task_commit = (
                    cand == last_exec_sha
                    or task.task_id.lower() in commit_msg.lower()
                    or task.task_id.replace("-", "").lower() in commit_msg.lower()
                )
                if not is_task_commit:
                    continue
                res = _git(repo_root, "merge-base", "--is-ancestor", cand, current_main)
                if res.returncode == 0:
                    try:
                        release_active_claims(session, task.task_id, completed=True)
                    except CoordinatorPolicyError:
                        pass
                    if task.state != "DONE":
                        transition_task(
                            session,
                            task.task_id,
                            "DONE",
                            actor="runner",
                            reason=f"task commit {cand[:7]} is already an ancestor of {main_ref}; converged to Git reality",
                        )
                        record_event(
                            session,
                            EventInput(
                                task_id=task.task_id,
                                event_type="runner.converged_to_git_reality",
                                actor="runner",
                                event_data={"commit": cand, "target_state": "DONE"},
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "MERGE_CONFLICT")
                        self._release_blocker_gate(session, task, "WORKING_CHECKOUT_DIRTY")
                        self._release_blocker_gate(session, task, "UPSTREAM_PUSH_FAILED")
                        cleanup_task_branch(
                            repo_root,
                            task.branch_name or task_branch_name(task.task_id),
                            main_ref=main_ref,
                            reviewed_sha=cand,
                        )
                    break


    def _temp_dir(self) -> Path:
        path = Path(self._settings.data_dir) / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _result_dir(self) -> Path:
        if self._config.result_dir:
            path = Path(self._config.result_dir)
        else:
            path = Path(self._settings.data_dir) / "results"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _log_dir(self) -> Path:
        path = Path(self._settings.data_dir) / "execution-logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _block_task(
        self,
        session: Session,
        task_id: str,
        reason: str,
        *,
        invariant: str | None = None,
        execution: BuildRunnerExecution | None = None,
        worker: WorkerConfig | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        relevant_shas: dict[str, Any] | None = None,
        error: str | Exception | None = None,
        recovery_classification: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> None:
        task = session.get(BuildTask, task_id)
        if task and task.state != "BLOCKED":
            inv = invariant or reason
            err_str = str(error) if error else (
                (execution.result_data.get("error") if execution and execution.result_data else None)
            )
            rec_class = recovery_classification or (
                "RECOVERABLE_WORKTREE" if "worktree" in inv.lower()
                else "RECOVERABLE_GIT_STATE" if any(k in inv.lower() for k in ("git", "sha", "branch", "dirty"))
                else "OPERATOR_ACTION_REQUIRED"
            )
            evidence = {
                "underlying_invariant": inv,
                "task_id": task_id,
                "execution_id": execution.execution_id if execution else None,
                "worker": (execution.worker_id if execution else None) or (worker.worker_id if worker else None),
                "branch": branch or (execution.branch_name if execution else None) or (task.branch_name if task else None),
                "worktree": worktree or (execution.worktree_path if execution else None) or (worker.worktree_path if worker else None),
                "relevant_shas": relevant_shas or {
                    "reviewed_feature_sha": execution.reviewed_feature_sha if execution else None,
                    "feature_sha": (execution.result_data or {}).get("feature_sha") if execution else None,
                },
                "original_error": err_str,
                "recovery_classification": rec_class,
                **(extra_data or {}),
            }
            if execution:
                execution.result_data = {
                    **(execution.result_data or {}),
                    "failure_evidence": evidence,
                }
            waiting = dict(task.waiting_input or {})
            waiting["failure_evidence"] = evidence
            task.waiting_input = waiting
            try:
                transition_task(session, task_id, "BLOCKED", actor="runner", reason=reason, event_data=evidence)
                record_event(
                    session,
                    EventInput(
                        task_id=task_id,
                        event_type="runner.coordinator_invariant_failed",
                        actor="runner",
                        event_data=evidence,
                    ),
                )
            except CoordinatorPolicyError:
                pass

    def _check_task_already_satisfied(self, session: Session, task_id: str) -> bool:
        task = session.get(BuildTask, task_id)
        return bool(self._task_satisfaction_evidence(session, task).get("satisfied"))

    def _task_satisfaction_evidence(self, session: Session, task: BuildTask | None) -> dict:
        if task is None:
            return {"satisfied": False, "reason": "TASK_NOT_FOUND"}
        root = Path(self._settings.repo_root)
        commands = list(task.required_validation or [])
        if commands:
            if not self._config.run_validation:
                return {
                    "satisfied": False,
                    "reason": "VALIDATION_DISABLED",
                    "required_validation": commands,
                }
            outcome = run_validation(
                commands,
                root,
                timeout_seconds=self._config.validation_timeout_seconds,
            )
            return {
                "satisfied": outcome.passed,
                "reason": "REQUIRED_VALIDATION_PASSED" if outcome.passed else "REQUIRED_VALIDATION_FAILED",
                "required_validation": commands,
                "validation_results": outcome.results,
            }

        criteria = [str(item).strip() for item in list(task.acceptance_criteria or []) if str(item).strip()]
        checks = [self._file_contains_acceptance_evidence(root, criterion) for criterion in criteria]
        actionable_checks = [check for check in checks if check is not None]
        if actionable_checks:
            satisfied = all(bool(check.get("satisfied")) for check in actionable_checks)
            return {
                "satisfied": satisfied,
                "reason": "FILE_CONTENT_ACCEPTANCE_PASSED" if satisfied else "FILE_CONTENT_ACCEPTANCE_FAILED",
                "acceptance_checks": actionable_checks,
                "unchecked_acceptance_criteria": [
                    criterion for criterion, check in zip(criteria, checks, strict=False) if check is None
                ],
            }

        return {
            "satisfied": False,
            "reason": "NO_DETERMINISTIC_ACCEPTANCE_CHECK_AVAILABLE",
            "acceptance_criteria": criteria,
            "required_validation": commands,
        }

    def _file_contains_acceptance_evidence(self, root: Path, criterion: str) -> dict | None:
        marker = " contains "
        lowered = criterion.lower()
        if marker not in lowered:
            return None
        marker_index = lowered.index(marker)
        path_text = criterion[:marker_index].strip().strip("`'\"")
        expected = criterion[marker_index + len(marker) :].strip().strip("`'\"")
        if not path_text or not expected:
            return None
        candidate = (root / path_text).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return {
                "satisfied": False,
                "criterion": criterion,
                "reason": "PATH_OUTSIDE_REPOSITORY",
                "path": path_text,
            }
        if not candidate.is_file():
            return {
                "satisfied": False,
                "criterion": criterion,
                "reason": "FILE_NOT_FOUND",
                "path": path_text,
            }
        try:
            content = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = candidate.read_text(errors="replace")
        return {
            "satisfied": expected in content,
            "criterion": criterion,
            "reason": "FILE_CONTAINS_TEXT" if expected in content else "TEXT_NOT_FOUND",
            "path": path_text,
            "expected_text": expected,
        }

    def _recover_worktree_for_task(self, session: Session, task: BuildTask) -> bool:
        root = Path(self._settings.repo_root)
        _git(root, "worktree", "prune")
        if task.worktree_path:
            wt = Path(task.worktree_path)
            try:
                ensure_worktree(
                    wt,
                    repo_root=root,
                    branch_name=task.branch_name,
                    base_sha=self._config.main_ref,
                    allowed_roots=self._config.allowed_workspace_roots,
                )
                return True
            except WorktreeValidationError:
                return False
        return True

    def _reconcile_review_findings(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        verdict: ReviewVerdict,
    ) -> dict:
        """Reconciles this review's findings into the task's durable finding
        registry (content/fuzzy-fingerprinted, so repeated/reworded findings
        reconcile to the same entry and findings not restated are presumed
        resolved). Idempotent per `execution.execution_id`, so replayed or
        duplicate review results are safe to reprocess. A cycle that carries
        no finding signal at all (empty `findings` and no
        `finding_dispositions`) is not treated as evidence that previously
        open findings were resolved -- the registry is left untouched, so a
        reviewer that stops restating findings cannot silently resolve them."""
        task = session.get(BuildTask, execution.task_id)
        prior_registry = dict(task.finding_registry or {}) if task is not None else {}
        prior_convergence = dict((prior_registry.get("convergence") or {}))
        comprehensive_review = bool(prior_convergence.get("pending_comprehensive_review"))
        has_finding_signal = bool(verdict.findings) or bool(verdict.finding_dispositions)
        if comprehensive_review:
            missing_ids = comprehensive_reconciliation_missing_ids(
                prior_registry, verdict.findings, verdict.finding_dispositions
            )
            if missing_ids:
                incomplete = dict(prior_registry)
                incomplete["comprehensive_incomplete_ids"] = missing_ids
                return incomplete
        if not has_finding_signal:
            if comprehensive_review and verdict.integration_eligible():
                registry = reconcile_findings(
                    prior_registry,
                    findings=[],
                    finding_dispositions=[],
                    execution_id=execution.execution_id,
                    cycle_label=f"review-cycle:{execution.execution_id}",
                    reviewer_id=execution.worker_id,
                )
                registry = record_convergence_generation(
                    registry,
                    prior_registry=prior_registry,
                    execution_id=execution.execution_id,
                    cycle_label=f"review-cycle:{execution.execution_id}",
                    reviewer_id=execution.worker_id,
                    comprehensive_review=True,
                )
                convergence = dict(registry.get("convergence") or {})
                convergence["pending_comprehensive_review"] = False
                convergence["comprehensive_used"] = True
                convergence["comprehensive_execution_id"] = execution.execution_id
                registry["convergence"] = convergence
                if task is not None:
                    task.finding_registry = registry
                return registry
            return prior_registry
        registry = reconcile_findings(
            prior_registry,
            findings=list(verdict.findings),
            finding_dispositions=list(verdict.finding_dispositions),
            execution_id=execution.execution_id,
            cycle_label=f"review-cycle:{execution.execution_id}",
            reviewer_id=execution.worker_id,
        )
        registry = record_convergence_generation(
            registry,
            prior_registry=prior_registry,
            execution_id=execution.execution_id,
            cycle_label=f"review-cycle:{execution.execution_id}",
            reviewer_id=execution.worker_id,
            comprehensive_review=comprehensive_review,
        )
        convergence = dict(registry.get("convergence") or {})
        if comprehensive_review:
            convergence["pending_comprehensive_review"] = False
            convergence["comprehensive_used"] = True
            convergence["comprehensive_execution_id"] = execution.execution_id
            registry["convergence"] = convergence
        if task is not None:
            task.finding_registry = registry
        return registry

    def _request_comprehensive_convergence_review(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        registry: dict,
        result: RunnerCycleResult,
    ) -> bool:
        task = session.get(BuildTask, execution.task_id)
        if task is None:
            return False
        convergence = dict((registry or {}).get("convergence") or {})
        generations = int(convergence.get("generations") or 0)
        if generations < self._config.max_convergence_generations:
            return False
        if convergence.get("comprehensive_used") or convergence.get("pending_comprehensive_review"):
            return False
        convergence["pending_comprehensive_review"] = True
        convergence["threshold"] = self._config.max_convergence_generations
        convergence["stop_reason"] = "serial_new_finding_convergence_threshold_reached"
        registry = dict(registry or {})
        registry["convergence"] = convergence
        task.finding_registry = registry
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.comprehensive_convergence_review_requested",
                actor="runner",
                event_data={
                    "convergence_generations": generations,
                    "threshold": self._config.max_convergence_generations,
                    "open_findings": finding_escalation_evidence(registry),
                    "history": convergence.get("history") or [],
                    "reason": convergence["stop_reason"],
                },
            ),
        )
        result.escalations.append(f"{execution.task_id}:COMPREHENSIVE_CONVERGENCE_REVIEW_REQUIRED")
        release_active_claims(session, execution.task_id, completed=False)
        transition_task(
            session,
            execution.task_id,
            "REVIEW_READY",
            actor="runner",
            reason="serial finding convergence threshold reached; requesting comprehensive review",
        )
        return True

    def _remediation_limit_reached(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        verdict: ReviewVerdict,
        result: RunnerCycleResult,
    ) -> bool:
        """Finding-aware convergence gate for REMEDIATION_REQUIRED verdicts.

        Escalation is driven by a substantive finding remaining STILL_OPEN
        across `max_remediation_cycles` reports of its own, not by the raw
        count of remediation executions on the task -- an unrelated finding
        introduced later must not inherit an already-exhausted budget, and a
        finding that keeps getting fixed and replaced by new ones must not
        stall convergence. The raw remediation-cycle cap is used only as a
        backstop when the registry has no tracked entries, or this cycle
        carried no finding signal at all (the reviewer never populated
        structured findings/dispositions this cycle, so nothing was
        reconciled and there is nothing finding-aware to gate this cycle
        on)."""
        task_id = execution.task_id
        has_finding_signal = bool(verdict.findings) or bool(verdict.finding_dispositions)
        registry = self._reconcile_review_findings(session, execution, verdict)
        if registry.get("entries") and has_finding_signal:
            open_entries = open_findings(registry)
            convergence = dict(registry.get("convergence") or {})
            convergence_limit_reached = bool(
                open_entries
                and convergence.get("comprehensive_used")
                and int(convergence.get("generations") or 0)
                >= self._config.max_convergence_generations
            )
            # `attempts` counts how many times a finding has been *reported*
            # STILL_OPEN, so its first report (before any remediation has
            # run against it) counts as 1. Escalate once max_remediation_cycles
            # remediation attempts have completed without resolving it, i.e.
            # once it has been reported open again after that many attempts.
            limit_reached = any(
                int(entry.get("attempts") or 0) > self._config.max_remediation_cycles
                for entry in open_entries
            ) or convergence_limit_reached
        else:
            limit_reached = (
                self._remediation_cycles(session, task_id) >= self._config.max_remediation_cycles
            )
        evidence = finding_escalation_evidence(registry)
        if not limit_reached:
            return False
        result.escalations.append(f"{task_id}:REMEDIATION_LIMIT_REACHED")
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type="runner.remediation_limit_reached",
                actor="runner",
                event_data={
                    "open_findings": evidence,
                    "convergence": (registry or {}).get("convergence") or {},
                    "why_autonomous_convergence_stopped": (
                        "comprehensive_convergence_review_still_found_unresolved_work"
                        if (registry or {}).get("convergence", {}).get("comprehensive_used")
                        else "per_finding_remediation_budget_exhausted"
                    ),
                },
            ),
        )
        self._block_task(session, task_id, "REMEDIATION_LIMIT_REACHED")
        return True

    def _remediation_cycles(self, session: Session, task_id: str) -> int:
        return session.scalar(
            select(func.count())
            .select_from(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REMEDIATION")
            .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING", "SUCCEEDED")))
        ) or 0


_PROVIDER_CAPACITY_FAILURES = frozenset({"RATE_LIMITED", "QUOTA_EXHAUSTED"})


def _provider_capacity_is_active(
    session: Session,
    provider: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not provider:
        return False
    now = now or _now()
    rows = session.scalars(
        select(BuildTaskEvent).where(BuildTaskEvent.event_type == "runner.provider_failure")
    ).all()
    for row in rows:
        data = row.event_data or {}
        if str(data.get("provider") or "") != str(provider):
            continue
        if str(data.get("failure") or "").upper() not in _PROVIDER_CAPACITY_FAILURES:
            continue
        try:
            until = datetime.fromisoformat(str(data.get("until")))
        except (TypeError, ValueError):
            continue
        if until > now:
            return True
    return False


def _provider_unavailable_until(
    merged: dict[str, Any],
    *,
    fallback_seconds: float,
) -> datetime:
    reset_at = merged.get("provider_reset_at")
    if reset_at:
        try:
            parsed = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed > _now():
                return parsed.astimezone(UTC)
    return _now() + timedelta(seconds=fallback_seconds)


def _provider_capacity_failure_from_block_reason(reason: str) -> str | None:
    for prefix in ("PROVIDER_FAILURE:", "PROVIDER_FAILURE_RETRIES_EXHAUSTED:"):
        if reason.startswith(prefix):
            failure = reason[len(prefix):].strip().upper()
            if failure in _PROVIDER_CAPACITY_FAILURES:
                return failure
    return None


def _substantive_failure_attempts(
    session: Session,
    task_id: str,
    *,
    role: str,
    retry_generation: int,
) -> int:
    """Count task failures that are actually about the task/worker, not provider capacity."""
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == task_id)
        .where(BuildRunnerExecution.role == role)
        .where(BuildRunnerExecution.status.in_(("LOST", "FAILED")))
    ).all()
    attempts = 0
    for row in rows:
        data = row.result_data or {}
        try:
            generation = int(data.get("retry_generation", 0) or 0)
        except (TypeError, ValueError):
            generation = 0
        if generation != retry_generation:
            continue
        failure = str(data.get("provider_failure") or "").upper()
        if failure in _PROVIDER_CAPACITY_FAILURES:
            continue
        attempts += 1
    return attempts


_COOLDOWN_SECONDS = {
    "AUTH_FAILURE": 900,
    "RATE_LIMITED": 600,
    "QUOTA_EXHAUSTED": 1800,
    "NETWORK_FAILURE": 120,
    "UNAVAILABLE": 300,
    "EXECUTION_FAILURE": 60,
}
_RETRY_BACKOFF_CAP_SECONDS = 3600


def _retry_backoff_seconds(failure: str, attempt: int) -> int:
    """Bounded exponential backoff before a retryable failure is relaunched.

    `attempt` is the number of prior LOST/FAILED executions for this task and
    role (0 for the first retry), so each successive retry waits longer, up to
    `_RETRY_BACKOFF_CAP_SECONDS`."""
    base = _COOLDOWN_SECONDS.get(failure, 300)
    return min(base * (2 ** max(attempt, 0)), _RETRY_BACKOFF_CAP_SECONDS)


def _task_definition(task: BuildTask) -> dict:
    """What the task asks for, in the words of its version-controlled definition."""
    return {
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description,
        "acceptance_criteria": list(task.acceptance_criteria or []),
        "required_validation": list(task.required_validation or []),
        "permitted_scope": list(task.permitted_scope or []),
        "implementation_notes": task.implementation_notes,
        "review_policy": task.review_policy,
        "risk_level": task.risk_level,
        "metadata": dict(task.definition_metadata or {}),
    }


def _sanitize_observation(observation: ExecutionObservation) -> ExecutionObservation:
    """Reject untrusted executor statuses before any DB write."""
    if observation.status in EXECUTION_STATUSES:
        return observation
    return ExecutionObservation(
        status="FAILED",
        exit_code=observation.exit_code,
        result_data={
            "error": f"invalid executor result status: {observation.status}",
            **(observation.result_data or {}),
        },
        human_escalation_type="INVALID_EXECUTOR_STATUS",
        result_path=observation.result_path,
    )


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _now() -> datetime:
    return datetime.now(UTC)


def _looks_like_git_sha(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return 7 <= len(value) <= 64 and all(ch in "0123456789abcdefABCDEF" for ch in value)
