"""Transactional service operations for the Build Coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from build_coordinator.claims import (
    active_claim,
    active_migration_claim,
    count_active_builders,
    create_claim,
    get_max_active_builders,
    get_task_scope,
    last_implementation_worker,
    lock_coordinator_state,
    locked_claim,
    locked_task,
    next_builder_slot,
    release_active_claims,
    task_is_claimable,
    utcnow,
)
from build_coordinator.events import record_event
from build_coordinator.policy import (
    CoordinatorCapacityError,
    CoordinatorPolicyError,
    independent_review_required,
    require_transition,
    review_required,
)
from build_coordinator.types import (
    CheckpointInput,
    ClaimRequest,
    EventInput,
    ResumeContext,
    TaskOwnershipScope,
    TaskSpec,
)
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)

DEFAULT_LEASE_SECONDS = 1800


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

# Re-exports for public API compatibility
__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "CheckpointInput",
    "ClaimRequest",
    "EventInput",
    "ResumeContext",
    "TaskOwnershipScope",
    "TaskSpec",
    "checkpoint",
    "claim_integration",
    "claim_review",
    "claim_task",
    "count_active_builders",
    "ensure_state",
    "get_max_active_builders",
    "get_resume_context",
    "get_task_scope",
    "heartbeat",
    "list_available_tasks",
    "provide_task_input",
    "recover_expired",
    "recover_lost_execution_claims",
    "recover_execution_retry_exhausted",
    "request_task_input",
    "reconcile_stale_executions",
    "recover_review_environment_blocked",
    "release_active_claims",
    "set_mode",
    "transition_task",
    "upsert_task",
    "utcnow",
]


def ensure_state(session: Session) -> BuildCoordinatorState:
    state = session.get(BuildCoordinatorState, 1)
    if state is None:
        state = BuildCoordinatorState(singleton_id=1, mode="RUNNING")
        session.add(state)
        session.flush()
    return state


def set_mode(
    session: Session, mode: str, *, actor: str = "cli"
) -> BuildCoordinatorState:
    if mode not in {"RUNNING", "PAUSED", "DRAINING"}:
        raise CoordinatorPolicyError(f"Invalid coordinator mode: {mode}")
    state = ensure_state(session)
    old_mode = state.mode
    state.mode = mode
    state.updated_by = actor
    state.updated_at = utcnow()
    record_event(session, EventInput(
        task_id=None,
        event_type="coordinator.mode_changed",
        actor=actor,
        event_data={"from_mode": old_mode, "to_mode": mode},
    ))
    return state


def upsert_task(session: Session, spec: TaskSpec) -> BuildTask:
    task = session.get(BuildTask, spec.task_id)
    scope_dict = spec.ownership_scope.to_dict() if spec.ownership_scope else {}
    if task is None:
        task = BuildTask(
            task_id=spec.task_id,
            title=spec.title,
            description=spec.description,
            acceptance_criteria=spec.acceptance_criteria,
            dependencies=spec.dependencies,
            risk_level=spec.risk_level,
            review_policy=spec.review_policy,
            permitted_scope=spec.permitted_scope,
            required_validation=spec.required_validation,
            implementation_notes=spec.implementation_notes,
            program_key=spec.program_key,
            base_sha=spec.base_sha,
            migration_allowed=spec.migration_allowed,
            ownership_scope=scope_dict,
        )
        session.add(task)
        record_event(session, EventInput(
            task_id=spec.task_id,
            event_type="task.seeded",
            event_data={
                "title": spec.title,
                "program_key": spec.program_key,
                "base_sha": spec.base_sha,
                "migration_allowed": spec.migration_allowed,
            },
        ))
    else:
        task.title = spec.title
        task.description = spec.description
        task.acceptance_criteria = spec.acceptance_criteria
        task.dependencies = spec.dependencies
        task.risk_level = spec.risk_level
        task.review_policy = spec.review_policy
        task.permitted_scope = spec.permitted_scope
        task.required_validation = spec.required_validation
        task.implementation_notes = spec.implementation_notes
        task.program_key = spec.program_key
        task.base_sha = spec.base_sha
        task.migration_allowed = spec.migration_allowed
        if spec.ownership_scope is not None:
            task.ownership_scope = scope_dict
    return task


def list_available_tasks(session: Session) -> list[BuildTask]:
    now = utcnow()
    tasks = session.scalars(select(BuildTask).order_by(BuildTask.task_id)).all()
    return [task for task in tasks if task_is_claimable(session, task, now)]


def _admission_guard(session: Session) -> BuildCoordinatorState:
    """Serialize the short implementation-claim admission transaction."""
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        state = ensure_state(session)
        session.execute(
            update(BuildCoordinatorState)
            .where(BuildCoordinatorState.singleton_id == 1)
            .values(updated_at=BuildCoordinatorState.updated_at)
        )
        session.flush()
        return state
    return lock_coordinator_state(session)


def _path_prefix(pattern: str) -> str:
    return pattern.replace("\\", "/").rstrip("*").rstrip("/")


def _patterns_overlap(left: str, right: str) -> bool:
    left_prefix = _path_prefix(left)
    right_prefix = _path_prefix(right)
    if not left_prefix or not right_prefix:
        return False
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def _scope_conflicts(left: TaskOwnershipScope, right: TaskOwnershipScope) -> bool:
    if left.project != right.project:
        return False
    for left_path in left.allowed_paths:
        for right_path in right.allowed_paths:
            if _patterns_overlap(left_path, right_path):
                return True
        for right_forbidden in right.forbidden_paths:
            if _patterns_overlap(left_path, right_forbidden):
                return True
    for left_forbidden in left.forbidden_paths:
        for right_path in right.allowed_paths:
            if _patterns_overlap(left_forbidden, right_path):
                return True
    return False


def _require_no_active_scope_conflict(session: Session, task: BuildTask, now: datetime) -> None:
    scope = get_task_scope(task)
    if scope is None:
        return
    active_claims = session.scalars(
        select(BuildTaskClaim)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
    ).all()
    for claim in active_claims:
        active_task = session.get(BuildTask, claim.task_id)
        if active_task is None:
            continue
        active_scope = get_task_scope(active_task)
        if active_scope is not None and _scope_conflicts(scope, active_scope):
            raise CoordinatorPolicyError(
                f"Task scope conflicts with active task: {active_task.task_id}"
            )


def claim_task(
    session: Session,
    request: ClaimRequest,
    *,
    max_active_builders: int | None = None,
) -> BuildTaskClaim:
    return _admit_implementation_claim(
        session,
        request,
        max_active_builders=max_active_builders,
    )


def _admit_implementation_claim(
    session: Session,
    request: ClaimRequest,
    *,
    max_active_builders: int | None = None,
) -> BuildTaskClaim:
    now = utcnow()
    if max_active_builders is None:
        max_active_builders = get_max_active_builders(session)

    state = _admission_guard(session)
    if state.mode != "RUNNING":
        raise CoordinatorPolicyError(f"Coordinator mode prevents new claims: {state.mode}")

    task = locked_task(session, request.task_id)
    if not task_is_claimable(session, task, now, check_migration_lock=False):
        raise CoordinatorPolicyError(f"Task is not claimable: {request.task_id}")
    _require_no_active_scope_conflict(session, task, now)

    # Check active builder capacity (implementation claims only)
    active_count = count_active_builders(session, now)
    if active_count >= max_active_builders:
        raise CoordinatorCapacityError(
            f"Active builder capacity reached: {active_count}/{max_active_builders}"
        )
    builder_slot = next_builder_slot(session, max_active_builders, now)
    if builder_slot is None:
        raise CoordinatorCapacityError(
            f"Active builder capacity reached: {active_count}/{max_active_builders}"
        )

    # Enforce queue-wide migration serialization: at most one active migration-capable claim
    if task.migration_allowed:
        active_migration = active_migration_claim(session, now)
        if active_migration is not None:
            raise CoordinatorPolicyError(
                f"Active migration-capable claim already exists: {active_migration.task_id}"
            )

    try:
        return create_claim(
            session,
            task,
            request,
            claim_type="IMPLEMENTATION",
            next_state="CLAIMED",
            builder_slot=builder_slot,
        )
    except IntegrityError as exc:
        raise CoordinatorCapacityError("Active builder capacity claim raced") from exc


def claim_review(
    session: Session,
    request: ClaimRequest,
) -> BuildTaskClaim:
    now = utcnow()
    task = locked_task(session, request.task_id)
    state = ensure_state(session)
    if state.mode != "RUNNING":
        raise CoordinatorPolicyError(f"Coordinator mode prevents new claims: {state.mode}")

    if task.state != "REVIEW_READY" or not review_required(task.review_policy):
        raise CoordinatorPolicyError(
            f"Task is not review-claimable: {request.task_id}"
        )
    implementer = last_implementation_worker(session, request.task_id)
    if independent_review_required(task.review_policy) and implementer == request.worker_id:
        raise CoordinatorPolicyError(
            "Independent review cannot be claimed by the implementer"
        )
    existing_review = active_claim(session, request.task_id, "REVIEW", now)
    if existing_review is not None:
        raise CoordinatorPolicyError(
            f"Task already has an active review claim: {request.task_id}"
        )
    return create_claim(
        session,
        task,
        request,
        claim_type="REVIEW",
        next_state="REVIEWING",
    )


def claim_integration(
    session: Session,
    request: ClaimRequest,
) -> BuildTaskClaim:
    """Claim privileged integration work. Does not consume builder capacity."""
    now = utcnow()
    state = _admission_guard(session)
    if state.mode == "PAUSED":
        raise CoordinatorPolicyError(f"Coordinator mode prevents new claims: {state.mode}")
    task = locked_task(session, request.task_id)
    if task.state not in ("REVIEWING", "REVIEW_READY"):
        raise CoordinatorPolicyError(
            f"Task is not integration-claimable: {request.task_id}"
        )
    existing = active_claim(session, request.task_id, "INTEGRATION", now)
    if existing is not None:
        raise CoordinatorPolicyError(
            f"Task already has an active integration claim: {request.task_id}"
        )
    try:
        with session.begin_nested():
            return create_claim(
                session,
                task,
                request,
                claim_type="INTEGRATION",
                next_state="INTEGRATING",
            )
    except IntegrityError as exc:
        raise CoordinatorPolicyError("Active integration claim raced") from exc


def heartbeat(
    session: Session,
    claim_id: UUID | str,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> BuildTaskClaim:
    now = utcnow()
    claim = locked_claim(session, claim_id)
    if claim.worker_id != worker_id or claim.status != "ACTIVE":
        raise CoordinatorPolicyError("Only the active claiming worker may heartbeat")
    claim.last_heartbeat_at = now
    claim.lease_expires_at = now + timedelta(seconds=lease_seconds)
    task = locked_task(session, claim.task_id)
    task.last_heartbeat_at = claim.last_heartbeat_at
    task.lease_expires_at = claim.lease_expires_at
    task.updated_at = now
    record_event(session, EventInput(
        task_id=claim.task_id,
        event_type="claim.heartbeat",
        actor=worker_id,
        claim_id=claim.claim_id,
        event_data={"lease_expires_at": claim.lease_expires_at.isoformat()},
    ))
    return claim


def checkpoint(
    session: Session,
    claim_id: UUID | str,
    *,
    worker_id: str,
    data: CheckpointInput,
) -> BuildTaskCheckpoint:
    claim = locked_claim(session, claim_id)
    if claim.worker_id != worker_id or claim.status != "ACTIVE":
        raise CoordinatorPolicyError("Only the active claiming worker may checkpoint")
    row = BuildTaskCheckpoint(
        task_id=claim.task_id,
        claim_id=claim.claim_id,
        worker_id=worker_id,
        current_step=data.current_step,
        current_head_sha=data.current_head_sha,
        completed_work=data.completed_work,
        remaining_work=data.remaining_work,
        files_changed=data.files_changed,
        branch_name=claim.branch_name,
        worktree_path=claim.worktree_path,
        commits_created=data.commits_created,
        last_successful_tests=data.last_successful_tests,
        known_failures=data.known_failures,
        decisions=data.decisions,
        blockers=data.blockers,
    )
    session.add(row)
    record_event(session, EventInput(
        task_id=claim.task_id,
        event_type="task.checkpointed",
        actor=worker_id,
        claim_id=claim.claim_id,
        event_data={"current_step": data.current_step},
    ))
    return row


def get_resume_context(session: Session, task_id: str) -> ResumeContext:
    task = locked_task(session, task_id)
    scope = get_task_scope(task)
    latest_checkpoint = session.scalar(
        select(BuildTaskCheckpoint)
        .where(BuildTaskCheckpoint.task_id == task_id)
        .order_by(BuildTaskCheckpoint.created_at.desc())
        .limit(1)
    )
    current_claim = None
    if task.current_claim_id:
        current_claim = session.get(BuildTaskClaim, task.current_claim_id)
    previous_worker_id = (
        latest_checkpoint.worker_id
        if latest_checkpoint is not None
        else last_implementation_worker(session, task_id)
    )
    return ResumeContext(
        task_id=task.task_id,
        title=task.title,
        task_state=task.state,
        project=scope.project if scope else None,
        primary_module=scope.primary_module if scope else None,
        allowed_modules=scope.allowed_modules if scope else (),
        allowed_paths=scope.allowed_paths if scope else (),
        public_dependencies=scope.public_dependencies if scope else (),
        forbidden_paths=scope.forbidden_paths if scope else (),
        primary_tests=scope.primary_tests if scope else (),
        base_sha=task.base_sha,
        migration_allowed=task.migration_allowed,
        review_policy=task.review_policy,
        independent_review_required=independent_review_required(task.review_policy),
        branch_name=(latest_checkpoint.branch_name if latest_checkpoint else task.branch_name),
        worktree_path=(latest_checkpoint.worktree_path if latest_checkpoint else task.worktree_path),
        current_head_sha=latest_checkpoint.current_head_sha if latest_checkpoint else None,
        last_checkpoint_id=latest_checkpoint.checkpoint_id if latest_checkpoint else None,
        last_checkpoint_at=latest_checkpoint.created_at if latest_checkpoint else None,
        current_step=latest_checkpoint.current_step if latest_checkpoint else None,
        completed_work=tuple(latest_checkpoint.completed_work if latest_checkpoint else ()),
        remaining_work=tuple(latest_checkpoint.remaining_work if latest_checkpoint else ()),
        files_changed=tuple(latest_checkpoint.files_changed if latest_checkpoint else ()),
        commits_created=tuple(latest_checkpoint.commits_created if latest_checkpoint else ()),
        last_successful_tests=tuple(
            latest_checkpoint.last_successful_tests if latest_checkpoint else ()
        ),
        known_failures=tuple(latest_checkpoint.known_failures if latest_checkpoint else ()),
        explicit_decisions=tuple(latest_checkpoint.decisions if latest_checkpoint else ()),
        blockers=tuple(latest_checkpoint.blockers if latest_checkpoint else ()),
        previous_worker_id=previous_worker_id,
        current_claim_id=task.current_claim_id,
        current_claim_worker_id=current_claim.worker_id if current_claim else None,
        current_claim_type=current_claim.claim_type if current_claim else None,
        waiting_input=dict(task.waiting_input or {}),
    )


def recover_review_environment_blocked(
    session: Session,
    task_id: str,
    *,
    actor: str = "operator",
    reason: str | None = None,
) -> BuildTask:
    """Safely recover a task blocked by review environment failure back to REVIEW_READY.

    Preserves all prior events, executions, checkpoints, evidence, and feature SHA.
    """
    task = locked_task(session, task_id)
    if task.state != "BLOCKED":
        raise CoordinatorPolicyError(
            f"Cannot recover task {task_id}: state is {task.state}, expected BLOCKED"
        )
    recovered_task = transition_task(
        session,
        task_id,
        "REVIEW_READY",
        actor=actor,
        reason=reason or "operator recovery: review environment failure",
    )
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="runner.review_environment_recovered",
            actor=actor,
            event_data={
                "prior_state": "BLOCKED",
                "target_state": "REVIEW_READY",
                "reason": reason or "operator recovery: review environment failure",
            },
        ),
    )
    return recovered_task


def _latest_block_reason(session: Session, task_id: str) -> str | None:
    event = session.scalar(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id == task_id)
        .where(BuildTaskEvent.to_state.in_(("BLOCKED", "FAILED")))
        .order_by(BuildTaskEvent.created_at.desc())
        .limit(1)
    )
    return (event.event_data or {}).get("reason") if event else None


def recover_execution_retry_exhausted(
    session: Session,
    task_id: str,
    *,
    actor: str = "operator",
    reason: str | None = None,
) -> BuildTask:
    """Open a new bounded retry generation for retry-exhausted work only."""
    task = locked_task(session, task_id)
    if task.state not in {"BLOCKED", "FAILED"}:
        raise CoordinatorPolicyError(
            f"Cannot recover task {task_id}: state is {task.state}, expected BLOCKED or FAILED"
        )
    latest_reason = _latest_block_reason(session, task_id)
    if latest_reason != "EXECUTION_RETRY_LIMIT_REACHED":
        raise CoordinatorPolicyError(
            f"Cannot recover task {task_id}: latest terminal reason is {latest_reason!r}, "
            "expected EXECUTION_RETRY_LIMIT_REACHED"
        )
    prior_state = task.state
    task.retry_generation = int(task.retry_generation or 0) + 1
    recovered = transition_task(
        session,
        task_id,
        "RESUMABLE",
        actor=actor,
        reason=reason or "operator recovery: new execution retry generation",
    )
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="runner.execution_retry_generation_opened",
            actor=actor,
            event_data={
                "retry_generation": recovered.retry_generation,
                "prior_state": prior_state,
                "reason": reason or "operator recovery: new execution retry generation",
            },
        ),
    )
    return recovered


def transition_task(
    session: Session,
    task_id: str,
    to_state: str,
    *,
    actor: str = "cli",
    reason: str | None = None,
) -> BuildTask:
    task = locked_task(session, task_id)
    from_state = task.state
    require_transition(from_state, to_state)
    task.state = to_state
    task.updated_at = utcnow()
    if to_state in {"DONE", "FAILED", "BLOCKED", "REWORK_REQUIRED", "REVIEW_READY", "WAITING_FOR_INPUT"}:
        release_active_claims(session, task_id, completed=to_state == "DONE")
        task.current_claim_id = None
        task.lease_expires_at = None
        task.last_heartbeat_at = None
    record_event(session, EventInput(
        task_id=task_id,
        event_type="task.transitioned",
        actor=actor,
        from_state=from_state,
        to_state=to_state,
        event_data={"reason": reason} if reason else {},
    ))
    return task


def request_task_input(
    session: Session,
    task_id: str,
    question: str,
    *,
    actor: str = "worker",
    claim_id: str | None = None,
) -> BuildTask:
    """Move a live task into WAITING_FOR_INPUT and persist the required question.

    The implementation claim is released so builder slots are not held while
    the coordinator waits for a human/external answer. Resume is
    WAITING_FOR_INPUT -> RESUMABLE after provide_task_input, then a new
    claim reaches IN_PROGRESS. Keeping WAITING_FOR_INPUT out of
    CLAIMABLE_STATES prevents redispatch until input arrives. Lease expiry
    does not convert this state into STALE/worker-failure.
    """
    if not question or not str(question).strip():
        raise CoordinatorPolicyError("waiting-for-input question must be non-empty")
    task = locked_task(session, task_id)
    from_state = task.state
    payload = {
        **(task.waiting_input or {}),
        "question": str(question).strip(),
        "asked_at": utcnow().isoformat(),
        "asked_by": actor,
        "response": None,
        "responded_at": None,
        "responded_by": None,
    }
    task.waiting_input = payload
    if task.state in {"CLAIMED", "IN_PROGRESS"}:
        terminate_executions_for_claim(session, task.current_claim_id, reason="WAITING_FOR_INPUT")
        transition_task(
            session,
            task_id,
            "WAITING_FOR_INPUT",
            actor=actor,
            reason=question.strip(),
        )
    elif task.state != "WAITING_FOR_INPUT":
        raise CoordinatorPolicyError(
            f"Task cannot wait for input from {task.state}: {task_id}"
        )
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="task.waiting_for_input",
            actor=actor,
            claim_id=claim_id,
            from_state=from_state,
            to_state="WAITING_FOR_INPUT",
            event_data={"question": question.strip()},
        ),
    )
    return locked_task(session, task_id)


def provide_task_input(
    session: Session,
    task_id: str,
    response: str,
    *,
    actor: str = "operator",
) -> BuildTask:
    """Record operator input and make the waiting task eligible to resume."""
    if not response or not str(response).strip():
        raise CoordinatorPolicyError("waiting-for-input response must be non-empty")
    task = locked_task(session, task_id)
    if task.state != "WAITING_FOR_INPUT":
        raise CoordinatorPolicyError(f"Task is not waiting for input: {task_id}")
    payload = dict(task.waiting_input or {})
    payload["response"] = str(response).strip()
    payload["responded_at"] = utcnow().isoformat()
    payload["responded_by"] = actor
    task.waiting_input = payload
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="task.input_provided",
            actor=actor,
            from_state="WAITING_FOR_INPUT",
            to_state="RESUMABLE",
            event_data={"response": str(response).strip()},
        ),
    )
    return transition_task(
        session,
        task_id,
        "RESUMABLE",
        actor=actor,
        reason="input provided",
    )


def recover_expired(session: Session, *, actor: str = "cli") -> list[BuildTask]:
    now = utcnow()
    expired_claims = session.scalars(
        select(BuildTaskClaim)
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at <= now)
        .with_for_update()
    ).all()
    recovered: list[BuildTask] = []
    for claim in expired_claims:
        task = locked_task(session, claim.task_id)
        claim.status = "EXPIRED"
        from_state = task.state
        task.current_claim_id = None
        task.last_heartbeat_at = None
        task.lease_expires_at = None
        if task.state == "WAITING_FOR_INPUT":
            task.updated_at = now
        elif claim.claim_type == "REVIEW":
            task.state = "REVIEW_READY"
        elif claim.claim_type == "INTEGRATION":
            task.state = "REVIEWING"
        else:
            task.state = "STALE"
        task.updated_at = now
        terminate_executions_for_claim(session, claim.claim_id, reason="STALE_CLAIM")
        recovered.append(task)
        record_event(session, EventInput(
            task_id=task.task_id,
            event_type="claim.expired",
            actor=actor,
            from_state=from_state,
            to_state=task.state,
            claim_id=claim.claim_id,
            event_data={"claim_type": claim.claim_type, "worker_id": claim.worker_id},
        ))
    reconcile_stale_executions(session)
    return recovered


def recover_lost_execution_claims(
    session: Session, *, actor: str = "runner"
) -> list[BuildTask]:
    """Recover active task ownership when durable execution evidence is terminal.

    Lease expiry and execution loss are two ways to learn that a worker no longer
    authoritatively owns a task. This path intentionally preserves the same
    durable rows as lease recovery: the claim remains in history as EXPIRED, the
    terminal execution remains LOST/TERMINATED, checkpoints remain attached to
    the old claim, and a later worker must claim/resume from coordinator state.
    """
    terminal = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.status.in_(("LOST", "TERMINATED")))
        .where(BuildRunnerExecution.claim_id.is_not(None))
        .where(BuildRunnerExecution.completed_at.is_not(None))
    ).all()
    recovered: list[BuildTask] = []
    now = utcnow()
    for execution in terminal:
        claim = session.get(BuildTaskClaim, execution.claim_id)
        if claim is None or claim.status != "ACTIVE":
            continue
        live_successor = session.scalar(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.claim_id == execution.claim_id)
            .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
            .where(BuildRunnerExecution.execution_id != execution.execution_id)
            .limit(1)
        )
        if live_successor is not None:
            continue
        task = locked_task(session, claim.task_id)
        if task.current_claim_id != claim.claim_id:
            continue
        from_state = task.state
        claim.status = "EXPIRED"
        task.current_claim_id = None
        task.last_heartbeat_at = None
        task.lease_expires_at = None
        if task.state == "WAITING_FOR_INPUT":
            task.updated_at = now
        elif claim.claim_type == "REVIEW":
            task.state = "REVIEW_READY"
        elif claim.claim_type == "INTEGRATION":
            task.state = "REVIEWING"
        else:
            task.state = "STALE"
        task.updated_at = now
        recovered.append(task)
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="claim.recovered_from_lost_execution",
                actor=actor,
                from_state=from_state,
                to_state=task.state,
                claim_id=claim.claim_id,
                event_data={
                    "claim_type": claim.claim_type,
                    "worker_id": claim.worker_id,
                    "execution_id": execution.execution_id,
                    "execution_status": execution.status,
                },
            ),
        )
    return recovered


def terminate_executions_for_claim(
    session: Session,
    claim_id: str | None,
    *,
    reason: str,
) -> list[BuildRunnerExecution]:
    if not claim_id:
        return []
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.claim_id == str(claim_id))
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
    ).all()
    now = utcnow()
    for row in rows:
        row.status = "TERMINATED"
        row.completed_at = now
        row.last_observed_at = now
        row.result_data = {
            **(row.result_data or {}),
            "reconciliation_state": reason,
        }
    return list(rows)


def reconcile_stale_executions(session: Session) -> list[BuildRunnerExecution]:
    """Terminate live execution rows whose coordinator claim is no longer authoritative."""
    now = utcnow()
    live = session.scalars(
        select(BuildRunnerExecution).where(
            BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING"))
        )
    ).all()
    terminated: list[BuildRunnerExecution] = []
    for row in live:
        if not row.claim_id:
            continue
        claim = session.get(BuildTaskClaim, row.claim_id)
        if (
            claim is not None
            and claim.status == "ACTIVE"
            and _as_utc(claim.lease_expires_at) > now
        ):
            continue
        row.status = "TERMINATED"
        row.completed_at = now
        row.last_observed_at = now
        row.result_data = {
            **(row.result_data or {}),
            "reconciliation_state": "STALE_CLAIM",
        }
        terminated.append(row)
    return terminated
