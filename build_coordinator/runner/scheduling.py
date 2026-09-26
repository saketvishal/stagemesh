"""Authoritative objective DAG scheduler and capacity governor (StageMesh GH-88).

This module implements dependency-aware runnable frontier computation, per-objective
parallelism budgets, parallel_safe serialization boundaries, ownership scope conflict
checking with conservative unknown-scope policies, worker/worktree single-owner invariants,
bounded fairness, and auditable typed scheduling reasons.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.claims import (
    CLAIMABLE_STATES,
    active_claim,
    active_migration_claim,
    get_task_scope,
    task_source_is_executable,
    utcnow,
)
from build_coordinator.models import (
    BuildObjective,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
    BuildWorkerLease,
)
from build_coordinator.objectives import open_gates
from build_coordinator.types import EventInput, TaskOwnershipScope

REASON_DEPENDENCY_NOT_DONE = "dependency_not_done"
REASON_OBJECTIVE_PARALLELISM_FULL = "objective_parallelism_full"
REASON_PROJECT_BUILDER_CAPACITY_FULL = "project_builder_capacity_full"
REASON_PARALLEL_SAFE_SERIALIZATION = "parallel_safe_serialization"
REASON_OWNERSHIP_SCOPE_CONFLICT = "ownership_scope_conflict"
REASON_MIGRATION_SERIALIZATION = "migration_serialization"
REASON_WORKER_UNAVAILABLE = "worker_unavailable"
REASON_HUMAN_GATE_OPEN = "human_gate_open"
REASON_WORKTREE_OR_WORKER_OWNED = "worktree_or_worker_owned"
REASON_SOURCE_CLOSED = "source_closed"
REASON_SOURCE_DEFERRED = "source_deferred"

SCHEDULER_REASONS = frozenset({
    REASON_DEPENDENCY_NOT_DONE,
    REASON_OBJECTIVE_PARALLELISM_FULL,
    REASON_PROJECT_BUILDER_CAPACITY_FULL,
    REASON_PARALLEL_SAFE_SERIALIZATION,
    REASON_OWNERSHIP_SCOPE_CONFLICT,
    REASON_MIGRATION_SERIALIZATION,
    REASON_WORKER_UNAVAILABLE,
    REASON_HUMAN_GATE_OPEN,
    REASON_WORKTREE_OR_WORKER_OWNED,
    REASON_SOURCE_CLOSED,
    REASON_SOURCE_DEFERRED,
})


def normalize_worktree_path(path: str | Path | None) -> str | None:
    """Return canonical lowercase normalized absolute path for worktree comparison."""
    if not path:
        return None
    try:
        resolved = Path(path).expanduser().resolve()
        return os.path.normcase(os.path.normpath(str(resolved)))
    except Exception:
        return str(path)


def active_implementation_tasks(session: Session, now: datetime | None = None) -> list[BuildTask]:
    """Return all tasks currently holding an active IMPLEMENTATION claim or live BUILDER/REMEDIATION execution."""
    now = now or utcnow()
    active_claim_tasks = session.scalars(
        select(BuildTaskClaim.task_id)
        .where(BuildTaskClaim.claim_type == "IMPLEMENTATION")
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
    ).all()
    live_exec_tasks = session.scalars(
        select(BuildRunnerExecution.task_id)
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
        .where(BuildRunnerExecution.role.in_(("BUILDER", "REMEDIATION")))
    ).all()
    all_ids = set(active_claim_tasks) | set(live_exec_tasks)
    if not all_ids:
        return []
    return list(session.scalars(select(BuildTask).where(BuildTask.task_id.in_(all_ids))).all())


def active_objective_implementations(
    session: Session, objective_id: str, now: datetime | None = None
) -> list[BuildTask]:
    """Return active implementation/remediation tasks belonging to the specified objective."""
    all_active = active_implementation_tasks(session, now)
    return [t for t in all_active if t.objective_id == objective_id]


def active_worktrees(session: Session, now: datetime | None = None) -> set[str]:
    """Return normalized paths of all worktrees currently occupied by active claims or live executions."""
    now = now or utcnow()
    paths: set[str] = set()
    claim_paths = session.scalars(
        select(BuildTaskClaim.worktree_path)
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
        .where(BuildTaskClaim.worktree_path.isnot(None))
    ).all()
    for p in claim_paths:
        norm = normalize_worktree_path(p)
        if norm:
            paths.add(norm)
    exec_paths = session.scalars(
        select(BuildRunnerExecution.worktree_path)
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
        .where(BuildRunnerExecution.worktree_path.isnot(None))
    ).all()
    for p in exec_paths:
        norm = normalize_worktree_path(p)
        if norm:
            paths.add(norm)
    return paths


def active_worker_counts(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Return count of active claims/executions per worker ID."""
    now = now or utcnow()
    counts: dict[str, int] = {}
    claim_workers = session.scalars(
        select(BuildTaskClaim.worker_id)
        .where(BuildTaskClaim.status == "ACTIVE")
        .where(BuildTaskClaim.lease_expires_at > now)
    ).all()
    for w in claim_workers:
        counts[w] = counts.get(w, 0) + 1
    exec_rows = session.execute(
        select(BuildRunnerExecution.worker_id, BuildRunnerExecution.execution_id)
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
    ).all()
    exec_counts: dict[str, int] = {}
    live_execution_ids = {execution_id for _worker_id, execution_id in exec_rows}
    for worker_id, _execution_id in exec_rows:
        exec_counts[worker_id] = exec_counts.get(worker_id, 0) + 1
    for worker_id, count in exec_counts.items():
        counts[worker_id] = max(counts.get(worker_id, 0), count)
    lease_rows = session.execute(
        select(BuildWorkerLease.worker_id, BuildWorkerLease.execution_id)
        .where(BuildWorkerLease.status == "ACTIVE")
        .where(BuildWorkerLease.lease_expires_at > now)
    ).all()
    lease_counts: dict[str, int] = {}
    for worker_id, execution_id in lease_rows:
        if execution_id in live_execution_ids:
            continue
        lease_counts[worker_id] = lease_counts.get(worker_id, 0) + 1
    for worker_id, count in lease_counts.items():
        counts[worker_id] = max(counts.get(worker_id, 0), count)
    return counts


def active_workers(session: Session, now: datetime | None = None) -> set[str]:
    """Return worker IDs currently occupied by active claims or live executions."""
    return set(active_worker_counts(session, now).keys())


def launch_counts(session: Session) -> dict[str, int]:
    """Return the total number of launches ever recorded per worker ID.

    Distinct from active_worker_counts (point-in-time occupancy): this is a
    cumulative usage counter for reporting how much a worker/provider has run.
    """
    counts: dict[str, int] = {}
    worker_ids = session.scalars(select(BuildRunnerExecution.worker_id)).all()
    for worker_id in worker_ids:
        counts[worker_id] = counts.get(worker_id, 0) + 1
    return counts


def is_parallel_safe_serialized(task: BuildTask, active_objective_tasks: list[BuildTask]) -> bool:
    """Check if task cannot execute due to parallel_safe serialization within its objective.

    Invariant:
    - If task has parallel_safe=False, it cannot run while another implementation/remediation task is active in objective.
    - If any other active implementation/remediation task has parallel_safe=False, no new task in objective can run.
    """
    other_active = [t for t in active_objective_tasks if t.task_id != task.task_id]
    if not other_active:
        return False
    if not task.parallel_safe:
        return True
    if any(not t.parallel_safe for t in other_active):
        return True
    return False


def is_high_risk_or_migration_with_unknown_scope(task: BuildTask, scope: TaskOwnershipScope | None) -> bool:
    """Return True if task is high risk or migration and lacks an explicit allowed_paths scope."""
    if task.risk_level in {"HIGH", "CRITICAL"} or task.migration_allowed:
        if scope is None or not scope.allowed_paths:
            return True
    return False


def has_scope_conflict(task: BuildTask, active_tasks: list[BuildTask]) -> bool:
    """Determine if task has an ownership/scope collision against any currently active task.

    Enforces both structured scope overlap rules and conservative unknown-scope serialization
    for high-risk or migration tasks.
    """
    from build_coordinator.service import _scope_conflicts

    task_scope = get_task_scope(task)
    task_has_unknown_risk = is_high_risk_or_migration_with_unknown_scope(task, task_scope)

    for other in active_tasks:
        if other.task_id == task.task_id:
            continue
        other_scope = get_task_scope(other)
        other_has_unknown_risk = is_high_risk_or_migration_with_unknown_scope(other, other_scope)

        # Conservative policy: unknown/incomplete scope under HIGH/CRITICAL or migration
        # serializes against any active task in the same project or objective.
        if task_has_unknown_risk or other_has_unknown_risk:
            # Same project check
            if task_scope is not None and other_scope is not None:
                if task_scope.project == other_scope.project:
                    return True
            # Same objective check
            if task.objective_id and other.objective_id and task.objective_id == other.objective_id:
                return True
            # If either lacks project scope entirely, conservative policy serializes
            if task_scope is None or other_scope is None:
                return True

        if task_scope is not None and other_scope is not None:
            if _scope_conflicts(task_scope, other_scope):
                return True

    return False


def check_task_readiness(
    session: Session,
    task: BuildTask,
    *,
    now: datetime | None = None,
    max_active_builders: int | None = None,
    active_tasks: list[BuildTask] | None = None,
) -> tuple[bool, str | None]:
    """Authoritative scheduler check for whether a task is eligible to launch implementation.

    Evaluates:
    1. Dependency readiness
    2. Objective human gates
    3. Objective parallelism budget
    4. parallel_safe serialization
    5. Project/global builder capacity
    6. Migration serialization
    7. Ownership / scope conflict

    Returns (is_ready, reason_code_if_withheld).
    """
    from build_coordinator.service import get_max_active_builders

    now = now or utcnow()
    if active_tasks is None:
        active_tasks = active_implementation_tasks(session, now)

    if not task_source_is_executable(task):
        metadata = task.definition_metadata or {}
        if str(metadata.get("source_state") or "").upper() == "CLOSED":
            return False, REASON_SOURCE_CLOSED
        return False, REASON_SOURCE_DEFERRED

    # 1. Dependency readiness
    for dep in task.dependencies:
        objective_dependency = session.get(BuildObjective, dep)
        if objective_dependency is not None:
            if objective_dependency.state != "COMPLETED":
                return False, REASON_DEPENDENCY_NOT_DONE
            continue
        dependency = session.get(BuildTask, dep)
        if dependency is None or not task_source_is_executable(dependency) or dependency.state != "DONE":
            return False, REASON_DEPENDENCY_NOT_DONE

    # 2. Objective human gates
    if task.objective_id:
        objective = session.get(BuildObjective, task.objective_id)
        if objective is not None:
            if objective.state in {"HUMAN_GATE", "PAUSED"} or open_gates(session, objective.objective_id):
                return False, REASON_HUMAN_GATE_OPEN

    # 3. Objective parallelism & 4. parallel_safe
    if task.objective_id:
        objective = session.get(BuildObjective, task.objective_id)
        if objective is not None:
            active_obj_tasks = [
                t for t in active_tasks
                if t.objective_id == task.objective_id and t.task_id != task.task_id
            ]
            if len(active_obj_tasks) >= objective.parallelism:
                return False, REASON_OBJECTIVE_PARALLELISM_FULL

            if is_parallel_safe_serialized(task, active_obj_tasks):
                return False, REASON_PARALLEL_SAFE_SERIALIZATION

    # 5. Project/global builder capacity
    if max_active_builders is None:
        max_active_builders = get_max_active_builders(session)
    active_builder_count = len({t.task_id for t in active_tasks if t.task_id != task.task_id})
    if active_builder_count >= max_active_builders:
        return False, REASON_PROJECT_BUILDER_CAPACITY_FULL

    # 6. Migration serialization
    if task.migration_allowed:
        active_mig = active_migration_claim(session, now)
        if active_mig is not None and active_mig.task_id != task.task_id:
            return False, REASON_MIGRATION_SERIALIZATION
        if any(t.migration_allowed for t in active_tasks if t.task_id != task.task_id):
            return False, REASON_MIGRATION_SERIALIZATION

    # 7. Ownership/scope conflicts
    if has_scope_conflict(task, active_tasks):
        return False, REASON_OWNERSHIP_SCOPE_CONFLICT

    return True, None


def sort_tasks_for_dispatch(
    session: Session,
    tasks: list[BuildTask],
    priorities: dict[str, int],
    active_tasks: list[BuildTask] | None = None,
) -> list[BuildTask]:
    """Sort candidate tasks implementing deterministic bounded fairness.

    Fairness rule:
    1. Lower priority numbers run first (P0 before P1, etc.).
    2. Among tasks with equal priority, capacity is shared fairly across objectives
       (and standalone tasks) using effective load = (currently active workers + candidate index).
       This interleaves candidate tasks across objectives and prevents any single large
       objective from monopolizing builder capacity.
    3. Standalone tasks (objective_id is None) are treated as having 0 active workers
       and independent queue slots.
    4. Deterministic tie-breaking by (objective_id or "", task_id).
    """
    if active_tasks is None:
        active_tasks = active_implementation_tasks(session)

    active_by_objective: dict[str, int] = {}
    for t in active_tasks:
        if t.objective_id:
            active_by_objective[t.objective_id] = active_by_objective.get(t.objective_id, 0) + 1

    # Deterministic initial sort within each objective/task by (priority, task_id)
    initial_sorted = sorted(
        tasks,
        key=lambda t: (priorities.get(t.task_id, 100), t.task_id),
    )

    candidate_index_by_obj: dict[str, int] = {}
    task_keys: dict[str, tuple[int, int, str, str]] = {}
    for t in initial_sorted:
        prio = priorities.get(t.task_id, 100)
        obj_key = t.objective_id or f"__standalone_{t.task_id}"
        idx = candidate_index_by_obj.get(obj_key, 0)
        candidate_index_by_obj[obj_key] = idx + 1
        base_load = active_by_objective.get(t.objective_id, 0) if t.objective_id else 0
        effective_load = base_load + idx
        task_keys[t.task_id] = (prio, effective_load, t.objective_id or "", t.task_id)

    return sorted(tasks, key=lambda t: task_keys[t.task_id])


def record_task_withheld(
    session: Session,
    task_id: str,
    reason: str,
    objective_id: str | None = None,
) -> None:
    """Record a deduplicated runner.task_withheld event for auditability."""
    from build_coordinator.service import record_event

    recent = session.scalar(
        select(BuildTaskEvent)
        .where(BuildTaskEvent.task_id == task_id)
        .where(BuildTaskEvent.event_type == "runner.task_withheld")
        .order_by(BuildTaskEvent.created_at.desc())
        .limit(1)
    )
    if recent and (recent.event_data or {}).get("reason") == reason:
        return

    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="runner.task_withheld",
            actor="runner",
            event_data={"reason": reason, "objective_id": objective_id},
        ),
    )
