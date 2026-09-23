"""Deterministic orchestration loop for coordinator-managed work."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
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
    BuildRunnerExecution,
    BuildTask,
    BuildTaskEvent,
    new_uuid,
)
from build_coordinator.objectives import (
    apply_validated_plan,
    get_objective,
    get_planner_task,
    is_planner_task,
    list_objectives,
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
from build_coordinator.runner.models import (
    ReviewVerdict,
    ReviewVerdictContradiction,
    RunnerConfig,
    WorkerConfig,
)
from build_coordinator.runner.routing import (
    RoutingDecision,
    StageRequirement,
    reviewer_exclusions,
    role_to_stage,
    route_worker,
)
from build_coordinator.runner.worktree import WorktreeValidationError, validate_worktree_path
from build_coordinator.service import (
    ClaimRequest,
    checkpoint,
    claim_integration,
    claim_review,
    claim_task,
    ensure_state,
    get_resume_context,
    list_available_tasks,
    reconcile_stale_executions,
    recover_expired,
    recover_lost_execution_claims,
    release_active_claims,
    request_task_input,
    transition_task,
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


class BuildRunner:
    def __init__(
        self,
        session_factory,
        config: RunnerConfig,
        *,
        executors: dict[str, WorkerExecutor] | None = None,
        git: GitBackend | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._executors = executors or {}
        self._git = git if git is not None else RealGit()
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
        recovered_tasks = recover_expired(session, actor="runner")
        result.recovered = [task.task_id for task in recovered_tasks]
        reconcile_stale_executions(session)
        for task in recover_lost_execution_claims(session, actor="runner"):
            if task.task_id not in result.recovered:
                result.recovered.append(task.task_id)
        self._recover_reviewed_sha_drift(session, result)
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
        self._reconcile_objectives(session, result)
        return result

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
            self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
                execution.human_escalation_type = "COORDINATOR_INVARIANT_FAILURE"
                execution.completed_at = _now()
                result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
            self._block_task(session, execution.task_id, escalation)
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
                execution.human_escalation_type = "COORDINATOR_INVARIANT_FAILURE"
                execution.completed_at = _now()
                result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
                if execution.role == "PLANNER":
                    planner_task = session.get(BuildTask, execution.task_id)
                    if planner_task is not None and planner_task.objective_id:
                        record_planner_failed(
                            session,
                            get_objective(session, planner_task.objective_id),
                            reason=f"planner output failed validation: {exc}",
                        )
                self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
        execution.human_escalation_type = "COORDINATOR_INVARIANT_FAILURE"
        execution.result_data = {
            **merged,
            "error": f"invalid executor result status: {observation.status}",
        }
        execution.completed_at = _now()
        result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
        self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")

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
            self._block_task(session, execution.task_id, "SCOPE_EXPANSION_REQUIRED")
            return
        if builder and builder.blockers:
            result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
            self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
            return
        if execution.claim_id:
            checkpoint(
                session,
                execution.claim_id,
                worker_id=execution.worker_id,
                data=CheckpointInput(
                    current_step="runner observed builder success",
                    current_head_sha=builder.feature_sha if builder else execution.result_data.get("feature_sha"),
                    completed_work=["external builder process completed"],
                    files_changed=list(builder.files_changed) if builder else execution.result_data.get("files_changed") or [],
                    commits_created=list(builder.commits_created) if builder else execution.result_data.get("commits_created") or [],
                    last_successful_tests=list(builder.tests.items) if builder else execution.result_data.get("tests") or [],
                ),
            )
        transition_task(session, execution.task_id, "IN_PROGRESS", actor="runner")
        transition_task(session, execution.task_id, "VALIDATING", actor="runner")
        task = session.get(BuildTask, execution.task_id)
        transition_task(
            session,
            execution.task_id,
            "REVIEW_READY" if task and task.review_policy != "NONE" else "DONE",
            actor="runner",
        )

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
        except ReviewVerdictContradiction:
            result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
            self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
            return
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
                    blockers=[] if eligible else list(verdict.findings),
                ),
            )
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
            cycles = self._remediation_cycles(session, execution.task_id)
            if cycles >= self._config.max_remediation_cycles:
                result.escalations.append(f"{execution.task_id}:REMEDIATION_LIMIT_REACHED")
                self._block_task(session, execution.task_id, "REMEDIATION_LIMIT_REACHED")
                return
            transition_task(
                session,
                execution.task_id,
                "REWORK_REQUIRED",
                actor="runner",
                reason="structured review requires remediation",
            )
            return
        result.escalations.append(f"{execution.task_id}:COORDINATOR_INVARIANT_FAILURE")
        self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")

    def _integration_succeeded(
        self,
        session: Session,
        execution: BuildRunnerExecution,
        result: RunnerCycleResult,
        parsed,
    ) -> None:
        integrator = parsed.integrator if parsed is not None else None
        if not self._config.auto_push_allowed:
            result.escalations.append(f"{execution.task_id}:REMOTE_PUSH_APPROVAL_REQUIRED")
            self._block_task(session, execution.task_id, "REMOTE_PUSH_APPROVAL_REQUIRED")
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

    def _dispatch_builders(self, session: Session, result: RunnerCycleResult) -> None:
        for task in list_available_tasks(session):
            if is_planner_task(task):
                continue
            if task.state == "REWORK_REQUIRED":
                prompt_builder = RemediationPromptBuilder()
                role = "REMEDIATION"
            else:
                prompt_builder = BuilderPromptBuilder()
                role = "BUILDER"
            worker, availability, decision = self._select_worker(
                role, task_id=task.task_id, session=session
            )
            if availability == "slots_occupied":
                result.capacity_full = True
                continue
            if worker is None:
                result.escalations.append(f"{task.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
                continue
            try:
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{task.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, task.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
                )
            except CoordinatorCapacityError:
                result.capacity_full = True
                return
            except CoordinatorPolicyError:
                continue
            context = get_resume_context(session, task.task_id)
            prompt = prompt_builder.build(context)
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
            self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
            self._block_task(session, execution.task_id, "COORDINATOR_INVARIANT_FAILURE")
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

    def _dispatch_planners(self, session: Session, result: RunnerCycleResult) -> None:
        worker, availability, decision = self._select_worker("PLANNER", session=session)
        if availability == "slots_occupied":
            result.capacity_full = True
            return
        for objective in list_objectives(session):
            if objective.state != "PLANNING":
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
            context = get_resume_context(session, planner_task.task_id)
            prompt = PlannerPromptBuilder().build(
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
            worker, availability, decision = self._select_worker(
                "REVIEWER", task_id=task.task_id, session=session
            )
            if availability == "slots_occupied":
                result.capacity_full = True
                continue
            if worker is None:
                result.escalations.append(f"{task.task_id}:EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED")
                continue
            try:
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{task.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, task.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
                        branch_name=worker.branch_name or task.branch_name,
                        worktree_path=worker.worktree_path,
                    ),
                )
            except CoordinatorPolicyError:
                continue
            prompt = ReviewerPromptBuilder().build(
                get_resume_context(session, task.task_id),
                extra={"reviewed_feature_sha": reviewed_sha, "sha_source": "runner-owned-git"},
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
            try:
                self._validate_worker_worktree(worker)
            except WorktreeValidationError as exc:
                result.escalations.append(f"{row.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, row.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
                result.escalations.append(f"{row.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, row.task_id, "COORDINATOR_INVARIANT_FAILURE")
                continue
            task = session.get(BuildTask, row.task_id)
            if task is None or task.state != "REVIEWING":
                continue
            assessment = None
            try:
                assessment = assess_mechanical_merge(
                    self._git,
                    cwd=self._git_cwd(worker, task),
                    branch_name=worker.branch_name or task.branch_name or row.branch_name or "HEAD",
                    reviewed_feature_sha=reviewed_sha,
                    remote=self._config.remote_name,
                    main_ref=self._config.main_ref,
                )
            except GitSafetyError as exc:
                result.escalations.append(f"{row.task_id}:COORDINATOR_INVARIANT_FAILURE")
                self._block_task(session, row.task_id, "COORDINATOR_INVARIANT_FAILURE")
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
                result.escalations.append(f"{row.task_id}:MERGE_CONFLICT")
                self._block_task(session, row.task_id, "MERGE_CONFLICT")
                continue
            try:
                claim = claim_integration(
                    session,
                    ClaimRequest(
                        row.task_id,
                        worker_id=worker.worker_id,
                        provider=worker.provider,
                        branch_name=worker.branch_name or task.branch_name,
                        worktree_path=worker.worktree_path,
                    ),
                )
            except CoordinatorPolicyError:
                continue
            except IntegrityError:
                continue
            q_outcome = None
            try:
                context = get_resume_context(session, row.task_id)
            except QRecordError as exc:
                record_event(
                    session,
                    EventInput(
                        task_id=row.task_id,
                        actor="runner",
                        event_data={"error": str(exc)},
                    ),
                )
                continue
            extra = {
                "reviewed_feature_sha": reviewed_sha,
                "current_main_sha": assessment.current_main_sha,
                "merge_base": assessment.merge_base,
                "feature_remote_sha": assessment.feature_remote_sha,
                "current_main_compatibility_required": True,
                "auto_push_allowed": self._config.auto_push_allowed,                "mechanical_conflict": False,
            }
            prompt = IntegrationPromptBuilder().build(
                get_resume_context(session, row.task_id),
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
                    "q_record_path": str(q_outcome.path) if q_outcome else None,                    "current_main_sha": assessment.current_main_sha,
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

    def _select_worker(
        self,
        role: str,
        *,
        task_id: str | None = None,
        session: Session | None = None,
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
            providers=self._config.providers,
            runtimes=self._config.runtimes,
            routing_policy=self._config.routing_policy,
            session=session,
            task_id=task_id,
            excluded_workers=reviewer_exclusions(session, task_id) if role == "REVIEWER" else set(),
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

    def _executor_for_worker(self, worker: WorkerConfig) -> WorkerExecutor:
        if worker.worker_id in self._executors:
            return self._executors[worker.worker_id]
        if worker.adapter == "fake":
            executor = FakeExecutor()
        elif worker.adapter == "subprocess":
            executor = SubprocessExecutor(
                list(worker.command),
                log_dir=self._log_dir(),
            )
        else:
            raise CoordinatorPolicyError(f"Unknown executor adapter: {worker.adapter}")
        self._executors[worker.worker_id] = executor
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
        if execution.adapter == "subprocess":
            worker = next(
                (item for item in self._config.workers if item.worker_id == execution.worker_id),
                None,
            )
            command = list(worker.command) if worker and worker.command else ["_unattached"]
            executor = SubprocessExecutor(
                command,
                log_dir=self._log_dir(),
                result_paths={execution.execution_id: execution.result_path}
                if execution.result_path
                else {},
            )
            self._executors[execution.worker_id] = executor
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
                branch_name=worker.branch_name or task.branch_name,
                remote=self._config.remote_name,
            )
        except GitSafetyError as exc:
            if worker.adapter == "fake":
                return None
            result.escalations.append(f"{task.task_id}:COORDINATOR_INVARIANT_FAILURE")
            self._block_task(session, task.task_id, "COORDINATOR_INVARIANT_FAILURE")
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

    def _validate_worker_worktree(self, worker: WorkerConfig) -> None:
        if not worker.worktree_path:
            return
        validate_worktree_path(
            worker.worktree_path,
            allowed_roots=self._config.allowed_workspace_roots,
            require_git=worker.adapter == "subprocess",
        )

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
        return session.scalar(
            select(func.count())
            .select_from(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REVIEWER")
            .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING", "SUCCEEDED")))
        ) or 0

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

    def _recover_reviewed_sha_drift(self, session: Session, result: RunnerCycleResult) -> None:
        blocked = session.scalars(select(BuildTask).where(BuildTask.state == "BLOCKED")).all()
        for task in blocked:
            if self._latest_block_reason(session, task.task_id) != "REVIEWED_SHA_CHANGED":
                continue
            self._request_rereview(session, task.task_id, result, reason="REVIEWED_SHA_CHANGED")


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

    def _block_task(self, session: Session, task_id: str, reason: str) -> None:
        task = session.get(BuildTask, task_id)
        if task and task.state != "BLOCKED":
            try:
                transition_task(session, task_id, "BLOCKED", actor="runner", reason=reason)
            except CoordinatorPolicyError:
                pass

    def _remediation_cycles(self, session: Session, task_id: str) -> int:
        return session.scalar(
            select(func.count())
            .select_from(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task_id)
            .where(BuildRunnerExecution.role == "REMEDIATION")
            .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING", "SUCCEEDED")))
        ) or 0


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
        human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
        result_path=observation.result_path,
    )


def _now() -> datetime:
    return datetime.now(UTC)
