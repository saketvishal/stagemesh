"""Claim, query, and locking helpers for the Build Coordinator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from build_coordinator.config import get_settings
from build_coordinator.events import record_event
from build_coordinator.policy import (
    CLAIMABLE_STATES,
    CoordinatorPolicyError,
    require_transition,
)
from build_coordinator.types import (
    ClaimRequest,
    EventInput,
    TaskOwnershipScope,
)
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildCoordinatorState,
    BuildTask,
    BuildTaskClaim,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_max_active_builders(session: Session | None = None) -> int:
    """Return the configured maximum concurrent active builder capacity."""
    return get_settings().max_active_builders


def count_active_builders(session: Session, now: datetime | None = None) -> int:
    """Count currently active unexpired IMPLEMENTATION claims."""
    if now is None:
        now = utcnow()
    return session.scalar(
        select(func.count())
        .select_from(BuildTaskClaim)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
    ) or 0


def active_builder_slots(session: Session, now: datetime | None = None) -> set[int]:
    """Return active implementation slots occupied by unexpired claims."""
    if now is None:
        now = utcnow()
    return set(
        session.scalars(
            select(BuildTaskClaim.builder_slot)
            .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
            .where(BuildTaskClaim.status == "ACTIVE")
            .where(BuildTaskClaim.lease_expires_at > now)
            .where(BuildTaskClaim.builder_slot.is_not(None))
        ).all()
    )


def next_builder_slot(session: Session, max_active_builders: int, now: datetime) -> int | None:
    occupied = active_builder_slots(session, now)
    for slot in range(1, max_active_builders + 1):
        if slot not in occupied:
            return slot
    return None


def get_task_scope(task: BuildTask) -> TaskOwnershipScope | None:
    """Return the structured TaskOwnershipScope for a task, or None if not set."""
    if not task.ownership_scope:
        return None
    return TaskOwnershipScope.from_dict(task.ownership_scope)


def locked_task(session: Session, task_id: str) -> BuildTask:
    task = session.scalar(
        select(BuildTask).where(BuildTask.task_id == task_id).with_for_update()
    )
    if task is None:
        raise CoordinatorPolicyError(f"Unknown task: {task_id}")
    return task


def locked_claim(session: Session, claim_id: UUID) -> BuildTaskClaim:
    claim = session.scalar(
        select(BuildTaskClaim).where(BuildTaskClaim.claim_id == str(claim_id)).with_for_update()
    )
    if claim is None:
        raise CoordinatorPolicyError(f"Unknown claim: {claim_id}")
    return claim


def lock_coordinator_state(session: Session) -> BuildCoordinatorState:
    state = session.scalar(
        select(BuildCoordinatorState)
        .where(BuildCoordinatorState.singleton_id == 1)
        .with_for_update()
    )
    if state is None:
        state = BuildCoordinatorState(singleton_id=1, mode="RUNNING")
        session.add(state)
        session.flush()
        state = session.scalar(
            select(BuildCoordinatorState)
            .where(BuildCoordinatorState.singleton_id == 1)
            .with_for_update()
        )
    return state


def active_migration_claim(session: Session, now: datetime) -> BuildTaskClaim | None:
    return session.scalar(
        select(BuildTaskClaim)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.migration_allowed.is_(True))
        .where(BuildTaskClaim.lease_expires_at > now)
    )


def task_source_is_closed(task: BuildTask) -> bool:
    metadata = task.definition_metadata or {}
    return bool(str(metadata.get("task_source") or "").strip()) and str(
        metadata.get("source_state") or ""
    ).upper() == "CLOSED"


def task_source_eligibility(task: BuildTask) -> str:
    metadata = task.definition_metadata or {}
    eligibility = str(metadata.get("source_eligibility") or "ELIGIBLE").upper()
    return eligibility or "ELIGIBLE"


def task_source_is_executable(task: BuildTask) -> bool:
    if task_source_is_closed(task):
        return False
    return task_source_eligibility(task) == "ELIGIBLE"


def objective_source_is_executable(session: Session, objective: BuildObjective) -> bool:
    from build_coordinator.planner import planner_task_id

    planner = session.get(BuildTask, planner_task_id(objective.objective_id))
    if planner is not None:
        return task_source_is_executable(planner)
    latest = session.scalar(
        select(BuildObjectiveEvent)
        .where(BuildObjectiveEvent.objective_id == objective.objective_id)
        .where(BuildObjectiveEvent.event_type == "objective.source_state_changed")
        .order_by(BuildObjectiveEvent.created_at.desc())
    )
    if latest is not None and str((latest.event_data or {}).get("to_state") or "").upper() == "CLOSED":
        return False
    return True


def objective_dependency_is_satisfied(session: Session, objective: BuildObjective) -> bool:
    return objective_source_is_executable(session, objective) and objective.state == "COMPLETED"


def task_is_claimable(
    session: Session,
    task: BuildTask,
    now: datetime,
    *,
    check_migration_lock: bool = True,
) -> bool:
    if task.reason_created == "OBJECTIVE_ROOT_COMPAT":
        return False
    if not task_source_is_executable(task):
        return False
    if task.state not in CLAIMABLE_STATES:
        return False
    for dep in task.dependencies:
        objective_dependency = session.get(BuildObjective, dep)
        if objective_dependency is not None:
            if not objective_dependency_is_satisfied(session, objective_dependency):
                return False
            continue
        dependency = session.get(BuildTask, dep)
        if dependency is None or not task_source_is_executable(dependency) or dependency.state != "DONE":
            return False
    if active_claim(session, task.task_id, "IMPLEMENTATION", now) is not None:
        return False
    if check_migration_lock and task.migration_allowed:
        if active_migration_claim(session, now) is not None:
            return False
    return True


def active_claim(
    session: Session, task_id: str, claim_type: str, now: datetime
) -> BuildTaskClaim | None:
    return session.scalar(
        select(BuildTaskClaim)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.claim_type == claim_type)
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
    )


def create_claim(
    session: Session,
    task: BuildTask,
    request: ClaimRequest,
    *,
    claim_type: str,
    next_state: str,
    builder_slot: int | None = None,
) -> BuildTaskClaim:
    now = utcnow()
    from_state = task.state
    require_transition(from_state, next_state)
    migration_allowed = task.migration_allowed if claim_type == "IMPLEMENTATION" else False
    claim = BuildTaskClaim(
        task_id=task.task_id,
        claim_type=claim_type,
        worker_id=request.worker_id,
        provider=request.provider,
        worker_metadata=request.worker_metadata,
        migration_allowed=migration_allowed,
        builder_slot=builder_slot,
        branch_name=request.branch_name,
        worktree_path=request.worktree_path,
        lease_expires_at=now + timedelta(seconds=request.lease_seconds),
        last_heartbeat_at=now,
    )
    session.add(claim)
    session.flush()
    task.state = next_state
    task.current_claim_id = claim.claim_id
    task.last_heartbeat_at = claim.last_heartbeat_at
    task.lease_expires_at = claim.lease_expires_at
    task.branch_name = request.branch_name
    task.worktree_path = request.worktree_path
    task.updated_at = now
    record_event(session, EventInput(
        task_id=task.task_id,
        event_type="task.claimed",
        actor=request.worker_id,
        from_state=from_state,
        to_state=next_state,
        claim_id=claim.claim_id,
        event_data={
            "claim_type": claim_type,
            "provider": request.provider,
            "migration_allowed": migration_allowed,
        },
    ))
    return claim


def last_implementation_worker(session: Session, task_id: str) -> str | None:
    return session.scalar(
        select(BuildTaskClaim.worker_id)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .order_by(BuildTaskClaim.claimed_at.desc())
        .limit(1)
    )


def last_implementation_provider(session: Session, task_id: str) -> str | None:
    return session.scalar(
        select(BuildTaskClaim.provider)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .order_by(BuildTaskClaim.claimed_at.desc())
        .limit(1)
    )


def release_active_claims(session: Session, task_id: str, *, completed: bool) -> None:
    claims = session.scalars(
        select(BuildTaskClaim)
        .where(BuildTaskClaim.task_id == task_id)
        .where(BuildTaskClaim.status == "ACTIVE")
        .with_for_update()
    ).all()
    for claim in claims:
        claim.status = "COMPLETED" if completed else "RELEASED"
