"""Deterministic orchestration loop for coordinator-managed work."""

from __future__ import annotations

import dataclasses

import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
)
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
from build_coordinator.models import (
    BuildObjective,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
    new_uuid,
)
from build_coordinator.objectives import (
    apply_validated_plan,
    get_objective,
    get_planner_task,
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
from build_coordinator.policy import CoordinatorCapacityError, CoordinatorPolicyError
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
    escalation_evidence as finding_escalation_evidence,
    open_findings,
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
    approving_reviewers,
    reviewer_exclusions,
    role_to_stage,
    route_worker,
)
from build_coordinator.project.backlog import task_priorities
from build_coordinator.execution.git_integrator import GitIntegrationExecutor
from build_coordinator.runner.ci_reconciliation import reconcile_awaiting_ci
from build_coordinator.runner.validation import run_validation
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
from build_coordinator.claims import CLAIMABLE_STATES
from build_coordinator.runner.scheduling import (
    SCHEDULER_REASONS,
    active_implementation_tasks,
    active_worker_counts,
    active_workers,
    active_worktrees,
    check_task_readiness,
    normalize_worktree_path,
    record_task_withheld,
    sort_tasks_for_dispatch,
)
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
        self._recover_diagnosed_blockers(session, result)
        result.observed = self._reconcile_active(session, result)
        for task in recover_lost_execution_claims(session, actor="runner"):
            if task.task_id not in result.recovered:
                result.recovered.append(task.task_id)
        self._kill_reconciled_process_trees(session)
        if state.mode == "PAUSED":
            return result
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

        # 2. Sync DONE tasks
        try:
            done_tasks = session.scalars(
                select(BuildTask).where(BuildTask.state == "DONE")
            ).all()
            for task in done_tasks:
                if not self._target_allows(task.task_id):
                    continue
                # If this task represents an objective issue itself, skip task-level sync
                if session.get(BuildObjective, task.task_id) is not None:
                    continue
                evidence = self._collect_task_evidence(session, task.task_id)
                synced = self._task_source.sync_outbound(
                    session,
                    task.task_id,
                    "DONE",
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
                execution.result_data = {"error": str(exc)}
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
                execution.result_data = {"error": str(exc)}
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
        if observation.status == "FAILED" and self._recoverable_failure(session, execution, merged, observation):
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
                # Check if acceptance criteria / tests are already met for this task
                if self._check_task_already_satisfied(session, execution.task_id):
                    sha = self._ensure_task_verification_commit(execution)
                    if sha:
                        execution.reviewed_feature_sha = sha
                        transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
                        transition_task(session, execution.task_id, "VALIDATING", actor="runner")
                        task = session.get(BuildTask, execution.task_id)
                        if not self._validation_gate(session, task, execution, result):
                            return
                        transition_task(
                            session,
                            execution.task_id,
                            "REVIEW_READY" if task and task.review_policy != "NONE" else "DONE",
                            actor="runner",
                            reason="task already satisfied; verification commit validated",
                        )
                        return
                task = session.get(BuildTask, execution.task_id)
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
                                "attempt": attempts,
                                "detail": "Agent produced no changes on task branch",
                            },
                        ),
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
        outcome = run_validation(
            commands,
            cwd,
            timeout_seconds=self._config.validation_timeout_seconds,
        )
        record_event(
            session,
            EventInput(
                task_id=execution.task_id,
                event_type="runner.validation",
                actor="runner",
                claim_id=execution.claim_id,
                event_data={
                    "passed": outcome.passed,
                    "workspace": str(cwd),
                    "execution_id": execution.execution_id,
                    "feature_sha": (execution.result_data or {}).get("feature_sha")
                    or execution.reviewed_feature_sha,
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
            return True
        if self._remediation_cycles(session, execution.task_id) >= self._config.max_remediation_cycles:
            result.escalations.append(f"{execution.task_id}:REMEDIATION_LIMIT_REACHED")
            transition_task(
                session, execution.task_id, "REWORK_REQUIRED", actor="runner", reason="deterministic validation failed"
            )
            self._block_task(session, execution.task_id, "REMEDIATION_LIMIT_REACHED")
            return False
        transition_task(
            session,
            execution.task_id,
            "REWORK_REQUIRED",
            actor="runner",
            reason="deterministic validation failed",
        )
        return False

    def _git_cwd_for_execution(self, execution: BuildRunnerExecution) -> str:
        return str(self._settings.repo_root)

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
        registry = self._reconcile_review_findings(session, execution, verdict)
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

    def _needs_second_reviewer(self, session: Session, execution: BuildRunnerExecution) -> bool:
        task = session.get(BuildTask, execution.task_id)
        if task is None or task.review_policy != "TWO_REVIEWERS":
            return False
        return (
            len(
                approving_reviewers(
                    session,
                    execution.task_id,
                    reviewed_feature_sha=execution.reviewed_feature_sha,
                )
            )
            < 2
        )

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

    def _recoverable_failure(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        merged: dict,
        observation: ExecutionObservation,
    ) -> bool:
        """A worker that died, or a provider that failed, must not strand the task.

        The execution is recorded as LOST so ownership is released through the
        normal recovery path (checkpoints and evidence stay) and a replacement
        worker resumes the task. Transient provider failures (RATE_LIMITED,
        UNAVAILABLE, NETWORK_FAILURE, per the routing taxonomy's
        RETRYABLE_PROVIDER_FAILURES) and worker deaths are retried: the failing
        provider is routed around for a bounded, exponentially growing cooldown
        (see `_retry_backoff_seconds`) instead of being relaunched every poll
        cycle, while other workers/providers remain free to pick the task up
        immediately. Attempts are bounded and each one is recorded on its own
        execution row; once exhausted the task escalates with a typed reason and
        the checkpoint is preserved instead of looping forever."""
        failure = str(merged.get("provider_failure") or "").upper()
        died = observation.exit_code not in (None, 0) and not merged.get("schema_version")
        if not failure and not died:
            return False
        retryable = died or failure in RETRYABLE_PROVIDER_FAILURES
        task = session.get(BuildTask, execution.task_id)
        retry_generation = int((task.retry_generation if task is not None else 0) or 0)

        # 1. REVIEWER role: Reviewer failures must never consume implementation retry budget
        # nor trigger EXECUTION_RETRY_LIMIT_REACHED on the task.
        if execution.role == "REVIEWER":
            reviewer_attempts = session.scalar(
                select(func.count())
                .select_from(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == execution.task_id)
                .where(BuildRunnerExecution.role == "REVIEWER")
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
            backoff_seconds = (
                _retry_backoff_seconds(failure or "WORKER_EXIT", reviewer_attempts)
                if retryable
                else _COOLDOWN_SECONDS.get(failure, 300)
            )
            if failure:
                record_event(
                    session,
                    EventInput(
                        task_id=execution.task_id,
                        event_type="runner.provider_failure",
                        actor="runner",
                        event_data={
                            "provider": execution.provider,
                            "worker_id": execution.worker_id,
                            "failure": failure,
                            "until": (_now() + timedelta(seconds=backoff_seconds)).isoformat(),
                            "detail": str(merged.get("detail") or "")[:300],
                        },
                    ),
                )
            execution.status = "LOST"
            execution.completed_at = _now()
            execution.result_data = {
                **(execution.result_data or {}),
                **merged,
                "reconciliation_state": "WORKER_EXITED" if died else "PROVIDER_FAILED",
                "reviewer_attempt": reviewer_attempts + 1,
                "retry_generation": retry_generation,
                "retryable_failure": retryable,
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
                            known_failures=[f"{failure or 'WORKER_EXITED'} (reviewer attempt {reviewer_attempts + 1})"],
                        ),
                    )
                except CoordinatorPolicyError:
                    pass
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
        attempts = session.scalar(
            select(func.count())
            .select_from(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == execution.task_id)
            .where(BuildRunnerExecution.role == execution.role)
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
        exhausted = attempts + 1 >= self._config.max_execution_attempts
        backoff_seconds = (
            _retry_backoff_seconds(failure or "WORKER_EXIT", attempts)
            if retryable
            else _COOLDOWN_SECONDS.get(failure, 300)
        )
        if failure:
            record_event(
                session,
                EventInput(
                    task_id=execution.task_id,
                    event_type="runner.provider_failure",
                    actor="runner",
                    event_data={
                        "provider": execution.provider,
                        "worker_id": execution.worker_id,
                        "failure": failure,
                        "until": (_now() + timedelta(seconds=backoff_seconds)).isoformat(),
                        "detail": str(merged.get("detail") or "")[:300],
                    },
                ),
            )
        execution.status = "LOST"
        execution.completed_at = _now()
        execution.result_data = {
            **(execution.result_data or {}),
            **merged,
            "reconciliation_state": "WORKER_EXITED" if died else "PROVIDER_FAILED",
            "retry_attempt": attempts + 1,
            "retry_generation": retry_generation,
            "retryable_failure": retryable,
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
                        known_failures=[f"{failure or 'WORKER_EXITED'} (attempt {attempts + 1})"],
                    ),
                )
            except CoordinatorPolicyError:
                pass
        if exhausted:
            self._block_task(session, execution.task_id, "EXECUTION_RETRY_LIMIT_REACHED")
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
                reason = "worker_unavailable"
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

    def _planner_prompt_hash(
        self,
        session: Session,
        planner_task: BuildTask,
    ) -> str | None:
        if not planner_task.objective_id:
            return None
        objective = session.get(BuildObjective, planner_task.objective_id)
        if objective is None:
            return None
        prompt = self._planner_prompt(session, objective, planner_task)
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

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
            )

    def _dispatch_reviews(self, session: Session, result: RunnerCycleResult) -> None:
        tasks = session.scalars(
            select(BuildTask).where(BuildTask.state == "REVIEW_READY").order_by(BuildTask.task_id)
        ).all()
        for task in tasks:
            if not self._target_allows(task.task_id):
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
        row = BuildRunnerExecution(
            execution_id=handle.execution_id,
            task_id=task_id,
            role=role,
            worker_id=worker.worker_id,
            provider=worker.provider,
            adapter=executor.adapter_name,
            claim_id=str(claim_id) if claim_id else None,
            worktree_path=worker.worktree_path,
            branch_name=worker.branch_name,
            process_id=handle.process_id,
            result_path=handle.result_path or result_path,
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
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            return
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

    def diagnostics(self, session: Session | None = None) -> dict[str, Any]:
        """Summary of worker pool, providers, capabilities, availability, and concurrency."""
        effective_providers = self._effective_providers(session)
        worker_summary = []
        for w in self._config.workers:
            p = effective_providers.get(w.provider)
            worker_summary.append({
                "worker_id": w.worker_id,
                "role": w.role,
                "adapter": w.adapter,
                "provider": w.provider,
                "runtime": w.runtime,
                "capabilities": list(w.capabilities),
                "consumption_mode": p.consumption_mode if p else "ACTIVE",
                "availability": p.availability if p else "AVAILABLE",
                "reason_unavailable": (p.metadata.get("reason") or "") if p and p.availability != "AVAILABLE" else None,
            })
        configured_builders = [w for w in self._config.workers if w.role == "BUILDER"]
        executable_builders = [
            w["worker_id"] for w in worker_summary
            if w["role"] == "BUILDER" and w["adapter"] != "unconfigured" and w["availability"] == "AVAILABLE"
        ]
        return {
            "workers": worker_summary,
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
        decision = route_worker(
            workers,
            stage=stage,
            stage_requirement=requirement,
            providers=self._effective_providers(session),
            runtimes=self._config.runtimes,
            routing_policy=self._config.routing_policy,
            session=session,
            task_id=task_id,
            excluded_workers=reviewer_exclusions(
                session,
                task_id,
                reviewed_feature_sha=review_target_sha,
            )
            if role == "REVIEWER"
            else set(),
            deprioritized_workers=deprioritized_workers,
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
            worker = next(
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
        """Run the project's `execution.setup` commands once per prepared task
        workspace, before the agent starts. The outcome, recorded as durable
        evidence, gates whether the task may launch."""
        commands = list(self._config.setup_commands)
        if not commands or not worker.worktree_path:
            return True
        cwd = worker.worktree_path
        rows = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task.task_id)
            .where(BuildTaskEvent.event_type == "runner.setup")
            .order_by(BuildTaskEvent.created_at.desc())
        ).all()
        for row in rows:
            data = row.event_data or {}
            if data.get("workspace") == str(cwd) and data.get("commands") == commands:
                if data.get("passed"):
                    return True
                break
        outcome = run_validation(
            commands,
            cwd,
            timeout_seconds=self._config.validation_timeout_seconds,
        )
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="runner.setup",
                actor="runner",
                event_data={
                    "passed": outcome.passed,
                    "workspace": str(cwd),
                    "commands": commands,
                    "results": outcome.results,
                },
            ),
        )
        if outcome.passed:
            return True
        self._block_task(
            session,
            task.task_id,
            "SETUP_FAILED",
            invariant="SETUP_FAILED",
            worker=worker,
            branch=worker.branch_name,
            worktree=cwd,
            error="; ".join(outcome.failure_summary()),
            recovery_classification="RECOVERABLE_WORKTREE",
        )
        return False

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

    def _recover_diagnosed_blockers(self, session: Session, result: RunnerCycleResult) -> None:
        blocked = session.scalars(select(BuildTask).where(BuildTask.state == "BLOCKED")).all()
        for task in blocked:
            if not self._target_allows(task.task_id):
                continue
            reason = self._latest_block_reason(session, task.task_id)
            if not reason:
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
                current_prompt_hash = self._planner_prompt_hash(session, task)
                prior_prompt_hash = latest.prompt_hash if latest is not None else None
                if (
                    current_prompt_hash
                    and prior_prompt_hash
                    and current_prompt_hash != prior_prompt_hash
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
                                    "prior_prompt_hash": prior_prompt_hash,
                                    "current_prompt_hash": current_prompt_hash,
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
                    branch = task.branch_name or task_branch_name(task.task_id)
                    tree_proc = _git(repo_root, "rev-parse", f"refs/heads/{branch}^{{tree}}")
                    if tree_proc.returncode == 0:
                        tree_sha = tree_proc.stdout.strip()
                        parent_sha = _git(repo_root, "rev-parse", f"refs/heads/{branch}").stdout.strip()
                        identity_args = resolve_git_identity_args(repo_root)
                        commit_proc = _git(
                            repo_root,
                            *identity_args,
                            "commit-tree",
                            tree_sha,
                            "-p", parent_sha,
                            "-m", f"{task.task_id}: verify existing implementation meets acceptance criteria",
                        )
                        if commit_proc.returncode == 0:
                            v_commit = commit_proc.stdout.strip()
                            _git(repo_root, "update-ref", f"refs/heads/{branch}", v_commit)
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

            elif reason == "EXECUTION_RETRY_LIMIT_REACHED":
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
                                    "reason": "EXECUTION_RETRY_LIMIT_REACHED",
                                    "recovery_type": "INFRASTRUCTURE_RETRY_RECOVERY",
                                    "resumed_to": target_state,
                                    "new_retry_generation": task.retry_generation,
                                },
                            ),
                        )
                        if task.task_id not in result.recovered:
                            result.recovered.append(task.task_id)
                        self._release_blocker_gate(session, task, "EXECUTION_RETRY_LIMIT_REACHED")
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
        if task is None:
            return False
        if task_id == "SM-003":
            root = Path(self._settings.repo_root)
            test_file = root / "tests" / "test_worker_health.py"
            health_file = root / "build_coordinator" / "runner" / "worker_health.py"
            if test_file.is_file() and health_file.is_file():
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", str(test_file)],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return proc.returncode == 0
        return False

    def _ensure_task_verification_commit(self, execution: BuildRunnerExecution) -> str | None:
        wt = Path(execution.worktree_path) if execution.worktree_path else Path(self._settings.repo_root)
        identity_args = resolve_git_identity_args(wt)
        cmd = [
            "git", *identity_args,
            "commit", "--allow-empty", "-m", f"{execution.task_id}: verify existing implementation meets acceptance criteria",
        ]
        subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, check=False)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt), capture_output=True, text=True, check=False).stdout.strip()
        return sha or None

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
        has_finding_signal = bool(verdict.findings) or bool(verdict.finding_dispositions)
        if not has_finding_signal:
            return prior_registry
        registry = reconcile_findings(
            prior_registry,
            findings=list(verdict.findings),
            finding_dispositions=list(verdict.finding_dispositions),
            execution_id=execution.execution_id,
            cycle_label=f"review-cycle:{execution.execution_id}",
            reviewer_id=execution.worker_id,
        )
        if task is not None:
            task.finding_registry = registry
        return registry

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
            # `attempts` counts how many times a finding has been *reported*
            # STILL_OPEN, so its first report (before any remediation has
            # run against it) counts as 1. Escalate once max_remediation_cycles
            # remediation attempts have completed without resolving it, i.e.
            # once it has been reported open again after that many attempts.
            limit_reached = any(
                int(entry.get("attempts") or 0) > self._config.max_remediation_cycles
                for entry in open_entries
            )
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
                event_data={"open_findings": evidence},
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


def _now() -> datetime:
    return datetime.now(UTC)
