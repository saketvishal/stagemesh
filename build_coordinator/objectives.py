"""Autonomous objective lifecycle: plan a high-level goal into coordinator
tasks, then keep reconciling structured task results into follow-up work,
unrelated-finding tasks, and typed human gates until the objective is
actually complete.

This is NOT a second orchestrator. It reuses the existing task/claim/review
lifecycle in `service.py` and `runner/orchestrator.py` entirely -- an
objective-generated task is a normal `BuildTask` row, claimed, reviewed,
and integrated exactly like any other. This module only adds: (1) a
structured plan -> child task translation, and (2) a reconciliation step
that reads `BuildRunnerExecution.result_data` and turns it into more tasks
or a human gate, instead of a human doing that translation by hand.

No chain-of-thought is ever persisted here: only structured spec fields,
reason codes, ids, and outcomes go into `BuildObjectiveEvent.event_data`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildObjectiveGate,
    BuildObjectivePlan,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskEvent,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.service import upsert_task, utcnow
from build_coordinator.planner import planner_task_id
from build_coordinator.types import (
    PLANNER_TASK_REASON,
    FindingSpec,
    ObjectivePlan,
    ObjectiveSpec,
    PlannedChildTask,
    StructuredContractError,
    StructuredTaskResult,
    TaskSpec,
)

DEFAULT_PLAN_SOURCE_EXPLICIT = "EXPLICIT_INPUT"
DEFAULT_PLAN_SOURCE_PLANNER = "PLANNER"

TERMINAL_TASK_STATES = {"DONE"}
UNRECOVERABLE_TASK_STATES = {"FAILED"}

# Existing execution-level escalation reasons (runner/models.py
# HUMAN_ESCALATION_TYPES), reused rather than reinvented, mapped onto the
# objective-level typed gate they correspond to. This is the bridge between
# the existing, already-tested runner escalation mechanism and objective
# gates -- not a second escalation system.
BLOCKED_REASON_TO_GATE_TYPE = {
    "REMOTE_PUSH_APPROVAL_REQUIRED": "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED",
    "ARCHITECTURE_DECISION_REQUIRED": "ARCHITECTURE_DECISION_REQUIRED",
    "SECURITY_POLICY_BLOCK": "SECURITY_DECISION_REQUIRED",
    "MIGRATION_SCOPE_VIOLATION": "MAJOR_SCOPE_EXPANSION_REQUIRED",
    "MERGE_CONFLICT": "UNRESOLVABLE_CONFLICT",
    "SCOPE_EXPANSION_REQUIRED": "MAJOR_SCOPE_EXPANSION_REQUIRED",
    "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED": "CREDENTIAL_REQUIRED",
    "COORDINATOR_INVARIANT_FAILURE": "UNRESOLVABLE_CONFLICT",
    "REVIEWED_SHA_CHANGED": "UNRESOLVABLE_CONFLICT",
    "WORKING_CHECKOUT_DIRTY": "UNRESOLVABLE_CONFLICT",
    "GIT_SAFETY_FAILURE": "UNRESOLVABLE_CONFLICT",
    "WORKTREE_INVALID": "UNRESOLVABLE_CONFLICT",
    "NO_CHANGES_PRODUCED": "UNRESOLVABLE_CONFLICT",
    "BUILDER_BLOCKER": "UNRESOLVABLE_CONFLICT",
    "MISSING_REVIEWED_SHA": "UNRESOLVABLE_CONFLICT",
    "BRANCH_MOVED_CONCURRENTLY": "UNRESOLVABLE_CONFLICT",
}


class ObjectiveError(CoordinatorPolicyError):
    """Raised for objective-level policy violations (unknown objective,
    invalid gate transition, plan validation failure)."""


def _record_event(session: Session, objective_id: str, event_type: str, *, actor: str | None = None, **data) -> None:
    session.add(
        BuildObjectiveEvent(
            objective_id=objective_id,
            event_type=event_type,
            actor=actor,
            event_data=data,
        )
    )


def _capture_resume_state(objective: BuildObjective) -> None:
    """Record the state to return to once the current HUMAN_GATE/PAUSED
    interruption clears. Only captures on the way OUT of a normal
    progressing state -- a second interruption stacked on top of an
    already-gated/paused objective must not overwrite the original target
    (e.g. pausing an already-gated objective, then resolving the gate,
    must still leave it PAUSED with the pre-gate resume target intact)."""
    if objective.state not in {"HUMAN_GATE", "PAUSED"} and objective.resume_state is None:
        objective.resume_state = objective.state


def _resume_target_state(session: Session, objective: BuildObjective) -> str:
    """Decide what state to resume an objective to once its HUMAN_GATE/
    PAUSED interruption clears.

    `resume_state` captures where the objective was interrupted from, but
    it can go stale: a gate raised while still PLANNING (e.g. a plan
    requesting human gates) is followed, in the same call, by the plan's
    child tasks actually being created -- so by the time the gate is
    resolved there is real work to reconcile and PLANNING would strand
    it. Whether work tasks exist now is the authoritative signal; a
    captured PLANNING target is only honored when no plan has been
    applied yet."""
    if not objective_work_tasks(session, objective.objective_id):
        return "PLANNING"
    if objective.resume_state and objective.resume_state != "PLANNING":
        return objective.resume_state
    return "ACTIVE"


def _dedup_key(*parts: str) -> str:
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


def get_objective(session: Session, objective_id: str) -> BuildObjective:
    objective = session.get(BuildObjective, objective_id)
    if objective is None:
        raise ObjectiveError(f"unknown objective: {objective_id}")
    return objective


def list_objectives(session: Session) -> list[BuildObjective]:
    return list(session.scalars(select(BuildObjective).order_by(BuildObjective.created_at)).all())


def objective_tasks(session: Session, objective_id: str) -> list[BuildTask]:
    return list(
        session.scalars(
            select(BuildTask).where(BuildTask.objective_id == objective_id).order_by(BuildTask.task_id)
        ).all()
    )


def is_planner_task(task: BuildTask) -> bool:
    return task.reason_created == PLANNER_TASK_REASON


def objective_work_tasks(session: Session, objective_id: str) -> list[BuildTask]:
    return [task for task in objective_tasks(session, objective_id) if not is_planner_task(task)]


def get_planner_task(session: Session, objective_id: str) -> BuildTask | None:
    return session.get(BuildTask, planner_task_id(objective_id))


def open_gates(session: Session, objective_id: str) -> list[BuildObjectiveGate]:
    return list(
        session.scalars(
            select(BuildObjectiveGate)
            .where(BuildObjectiveGate.objective_id == objective_id)
            .where(BuildObjectiveGate.status == "OPEN")
            .order_by(BuildObjectiveGate.created_at)
        ).all()
    )


# ---------------------------------------------------------------------------
# Creation and planning
# ---------------------------------------------------------------------------


def create_objective(session: Session, spec: ObjectiveSpec) -> BuildObjective:
    """Create an objective and synchronously generate + apply its plan.

    Idempotent: calling this again with the same `objective_id` returns the
    existing objective untouched (restart-safe -- an operator retry or a
    crashed-and-restarted controller must never re-plan or duplicate child
    tasks).
    """
    existing = session.get(BuildObjective, spec.objective_id)
    if existing is not None:
        return existing

    objective = BuildObjective(
        objective_id=spec.objective_id,
        goal=spec.goal,
        constraints=list(spec.constraints),
        allowed_scope=list(spec.allowed_scope),
        prohibited_scope=list(spec.prohibited_scope),
        completion_criteria=list(spec.completion_criteria),
        human_gate_policy=dict(spec.human_gate_policy),
        parallelism=spec.parallelism,
        main_push_policy=spec.main_push_policy,
        state="PLANNING",
        max_auto_created_tasks=spec.max_auto_created_tasks,
        max_child_depth=spec.max_child_depth,
    )
    session.add(objective)
    session.flush()
    _record_event(
        session,
        objective.objective_id,
        "objective.created",
        goal=spec.goal,
        allowed_scope=list(spec.allowed_scope),
        prohibited_scope=list(spec.prohibited_scope),
    )
    if spec.child_tasks:
        plan = ObjectivePlan(
            tasks=spec.child_tasks,
            requested_human_gates=spec.requested_human_gates,
            source=DEFAULT_PLAN_SOURCE_EXPLICIT,
        )
        apply_validated_plan(session, objective, plan)
    else:
        _ensure_planner_task(session, objective)
        _record_event(
            session,
            objective.objective_id,
            "objective.planner_required",
            planner_task_id=planner_task_id(objective.objective_id),
        )
    return objective


def _ensure_planner_task(session: Session, objective: BuildObjective) -> BuildTask:
    task_id = planner_task_id(objective.objective_id)
    existing = session.get(BuildTask, task_id)
    if existing is not None:
        return existing
    task = upsert_task(
        session,
        TaskSpec(
            task_id=task_id,
            title=f"Plan objective {objective.objective_id}",
            description=(
                "Controller-owned planner step. Decompose the free-text "
                "objective into a validated structured plan. Do not implement "
                "the work, choose worktrees, or authorize remote main push."
            ),
            acceptance_criteria=[
                "Emit a schema-valid ObjectivePlan with at least one child task",
                "Do not persist chain-of-thought",
            ],
            risk_level="LOW",
            review_policy="NONE",
            permitted_scope=list(objective.allowed_scope),
        ),
    )
    task.objective_id = objective.objective_id
    task.reason_created = PLANNER_TASK_REASON
    task.parallel_safe = True
    task.requires_integration = False
    task.dedup_key = _dedup_key(objective.objective_id, "PLANNER")
    _record_event(
        session,
        objective.objective_id,
        "objective.planner_task_created",
        task_id=task.task_id,
    )
    return task


def apply_validated_plan(
    session: Session,
    objective: BuildObjective,
    plan: ObjectivePlan,
) -> BuildObjectivePlan:
    """Persist a validated plan and create its child tasks. Idempotent:
    a second call after the plan is already applied does not duplicate
    tasks. The planner executor must not call this -- only the controller
    does, after schema+policy validation.
    """
    existing_work = objective_work_tasks(session, objective.objective_id)
    if existing_work:
        existing_plan = session.scalar(
            select(BuildObjectivePlan)
            .where(BuildObjectivePlan.objective_id == objective.objective_id)
            .order_by(BuildObjectivePlan.version.desc())
        )
        if existing_plan is not None:
            return existing_plan

    version = 1
    latest = session.scalar(
        select(BuildObjectivePlan)
        .where(BuildObjectivePlan.objective_id == objective.objective_id)
        .order_by(BuildObjectivePlan.version.desc())
    )
    if latest is not None:
        version = latest.version + 1

    row = BuildObjectivePlan(
        objective_id=objective.objective_id,
        version=version,
        source=plan.source,
        plan_data=[item.to_mapping() for item in plan.tasks],
    )
    session.add(row)
    session.flush()
    _record_event(
        session,
        objective.objective_id,
        "objective.plan_generated",
        plan_id=row.plan_id,
        source=plan.source,
        child_task_count=len(plan.tasks),
        requested_human_gates=list(plan.requested_human_gates),
    )
    _apply_plan(session, objective, list(plan.tasks), row)
    for gate_type in plan.requested_human_gates:
        _raise_gate(
            session,
            objective,
            gate_type=gate_type,
            reason=f"structured plan requested human gate {gate_type}",
            source_task_id=None,
        )
    if not open_gates(session, objective.objective_id):
        objective.state = "ACTIVE"
        objective.updated_at = utcnow()
    return row


def record_planner_unavailable(session: Session, objective: BuildObjective, *, reason: str) -> None:
    """Stay in PLANNING and retry on the next run. Not a scope gate -- the
    operator must not be forced to write a JSON plan by hand."""
    already = session.scalars(
        select(BuildObjectiveEvent)
        .where(BuildObjectiveEvent.objective_id == objective.objective_id)
        .where(BuildObjectiveEvent.event_type == "objective.planner_unavailable")
    ).all()
    if already:
        return
    _record_event(
        session,
        objective.objective_id,
        "objective.planner_unavailable",
        reason=reason,
        resumable=True,
    )


def record_planner_failed(session: Session, objective: BuildObjective, *, reason: str) -> None:
    _raise_gate(
        session,
        objective,
        gate_type="UNRESOLVABLE_CONFLICT",
        reason=reason,
        source_task_id=planner_task_id(objective.objective_id),
    )


def planner_status(session: Session, objective: BuildObjective) -> str:
    if objective_work_tasks(session, objective.objective_id):
        plan = session.scalar(
            select(BuildObjectivePlan)
            .where(BuildObjectivePlan.objective_id == objective.objective_id)
            .order_by(BuildObjectivePlan.version.desc())
        )
        if plan is not None and plan.source == DEFAULT_PLAN_SOURCE_PLANNER:
            return "APPLIED"
        if plan is not None:
            return "EXPLICIT"
    unavailable = session.scalar(
        select(BuildObjectiveEvent)
        .where(BuildObjectiveEvent.objective_id == objective.objective_id)
        .where(BuildObjectiveEvent.event_type == "objective.planner_unavailable")
    )
    planner = get_planner_task(session, objective.objective_id)
    if planner is None:
        return "NOT_REQUIRED"
    live = session.scalar(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == planner.task_id)
        .where(BuildRunnerExecution.role == "PLANNER")
        .where(BuildRunnerExecution.status.in_(("LAUNCHED", "RUNNING")))
    )
    if live is not None:
        return "RUNNING"
    succeeded = session.scalar(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == planner.task_id)
        .where(BuildRunnerExecution.role == "PLANNER")
        .where(BuildRunnerExecution.status == "SUCCEEDED")
    )
    if succeeded is not None and objective_work_tasks(session, objective.objective_id):
        return "APPLIED"
    if planner.state == "FAILED":
        return "FAILED"
    if unavailable is not None:
        return "UNAVAILABLE"
    return "PENDING"


def _apply_plan(
    session: Session,
    objective: BuildObjective,
    planned: list[PlannedChildTask],
    plan: BuildObjectivePlan,
) -> None:
    seen_ids: set[str] = set()
    for item in planned:
        if item.task_id in seen_ids:
            raise StructuredContractError(f"duplicate task_id in plan: {item.task_id}")
        seen_ids.add(item.task_id)
    _check_for_cycles(planned)

    for item in planned:
        _create_planned_task(session, objective, item, plan_id=plan.plan_id)


def check_for_cycles(planned: list[PlannedChildTask]) -> None:
    graph = {item.task_id: set(item.dependencies) for item in planned}

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, chain: list[str]) -> None:
        if node not in graph:
            return  # dependency on a task outside this plan is not our cycle to detect
        if node in visiting:
            raise StructuredContractError(f"dependency cycle detected: {' -> '.join(chain + [node])}")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep, chain + [node])
        visiting.discard(node)
        visited.add(node)

    for task_id in graph:
        visit(task_id, [])


_check_for_cycles = check_for_cycles


def _create_planned_task(
    session: Session, objective: BuildObjective, item: PlannedChildTask, *, plan_id: str
) -> BuildTask:
    task = upsert_task(
        session,
        TaskSpec(
            task_id=item.task_id,
            title=item.title,
            description=item.description,
            acceptance_criteria=list(item.acceptance_criteria),
            dependencies=list(item.dependencies),
            risk_level=item.risk_level,
            permitted_scope=list(item.scope),
            review_policy=item.review_policy,
        ),
    )
    task.objective_id = objective.objective_id
    task.parent_task_id = item.parent_task_id
    task.reason_created = item.reason_created
    task.parallel_safe = item.parallel_safe
    task.requires_integration = item.requires_integration
    _record_event(
        session,
        objective.objective_id,
        "objective.task_created",
        task_id=item.task_id,
        parent_task_id=item.parent_task_id,
        reason_created=item.reason_created,
        plan_id=plan_id,
    )
    return task


def _raise_gate(
    session: Session,
    objective: BuildObjective,
    *,
    gate_type: str,
    reason: str,
    source_task_id: str | None,
) -> BuildObjectiveGate | None:
    """Open a typed human gate, unless an equivalent one is already OPEN
    for this exact (objective, source, gate_type) -- restart-safe:
    reconciling the same finding twice must not open the gate twice.

    A gate that has already been RESOLVED does not count as "existing":
    the same condition recurring after a prior resolution is a new
    occurrence and must raise a fresh gate, or the recurrence goes
    silently unaddressed with the task left stranded against a closed
    gate."""
    existing = session.scalar(
        select(BuildObjectiveGate)
        .where(BuildObjectiveGate.objective_id == objective.objective_id)
        .where(BuildObjectiveGate.gate_type == gate_type)
        .where(BuildObjectiveGate.source_task_id == source_task_id)
        .where(BuildObjectiveGate.status == "OPEN")
    )
    if existing is not None:
        return None
    gate = BuildObjectiveGate(
        objective_id=objective.objective_id,
        source_task_id=source_task_id,
        gate_type=gate_type,
        reason=reason,
    )
    session.add(gate)
    session.flush()
    _capture_resume_state(objective)
    objective.state = "HUMAN_GATE"
    objective.updated_at = utcnow()
    _record_event(
        session,
        objective.objective_id,
        "objective.gate_raised",
        gate_id=gate.gate_id,
        gate_type=gate_type,
        source_task_id=source_task_id,
    )
    return gate


def resolve_gate(
    session: Session, gate_id: str, *, resolved_by: str, resolution_note: str | None = None
) -> BuildObjectiveGate:
    gate = session.get(BuildObjectiveGate, gate_id)
    if gate is None:
        raise ObjectiveError(f"unknown gate: {gate_id}")
    if gate.status != "OPEN":
        raise ObjectiveError(f"gate {gate_id} is not open (status={gate.status})")
    gate.status = "RESOLVED"
    gate.resolved_at = utcnow()
    gate.resolved_by = resolved_by
    gate.resolution_note = resolution_note
    objective = get_objective(session, gate.objective_id)
    _record_event(
        session,
        objective.objective_id,
        "objective.gate_resolved",
        gate_id=gate.gate_id,
        resolved_by=resolved_by,
    )
    if not open_gates(session, objective.objective_id) and objective.state == "HUMAN_GATE":
        # Only auto-resume out of HUMAN_GATE here. If the objective was
        # also explicitly PAUSED, stay PAUSED -- an operator pause is not
        # implicitly lifted by clearing gates; resume_objective() below
        # still knows the original target via resume_state.
        objective.state = _resume_target_state(session, objective)
        objective.resume_state = None
        objective.updated_at = utcnow()
        _record_event(
            session,
            objective.objective_id,
            "objective.resumed_from_gate",
            actor=resolved_by,
            resumed_to=objective.state,
        )
    return gate


def pause_objective(session: Session, objective_id: str, *, actor: str = "cli") -> BuildObjective:
    objective = get_objective(session, objective_id)
    if objective.state in {"COMPLETED", "FAILED"}:
        raise ObjectiveError(f"cannot pause a {objective.state} objective")
    _capture_resume_state(objective)
    objective.state = "PAUSED"
    objective.updated_at = utcnow()
    _record_event(session, objective_id, "objective.paused", actor=actor)
    return objective


def resume_objective(session: Session, objective_id: str, *, actor: str = "cli") -> BuildObjective:
    objective = get_objective(session, objective_id)
    if objective.state != "PAUSED":
        raise ObjectiveError(f"objective is not paused: {objective.state}")
    if open_gates(session, objective_id):
        objective.state = "HUMAN_GATE"
    else:
        objective.state = _resume_target_state(session, objective)
        objective.resume_state = None
    objective.updated_at = utcnow()
    _record_event(session, objective_id, "objective.resumed", actor=actor, resumed_to=objective.state)
    return objective


# ---------------------------------------------------------------------------
# Reconciliation: structured results -> follow-ups / unrelated tasks / gates
# ---------------------------------------------------------------------------


@dataclass
class ObjectiveReconcileSummary:
    objective_id: str
    follow_ups_created: list[str] = field(default_factory=list)
    unrelated_tasks_created: list[str] = field(default_factory=list)
    gates_raised: list[str] = field(default_factory=list)
    gates_reconciled: list[str] = field(default_factory=list)
    completed: bool = False


def run_objective_cycle(session: Session) -> list[ObjectiveReconcileSummary]:
    """Called once per runner cycle. Restart-safe and idempotent: every
    action here is guarded by a check for prior processing before it
    mutates state, so calling this repeatedly (including after a process
    restart) converges rather than duplicating work."""
    summaries: list[ObjectiveReconcileSummary] = []
    for objective in list_objectives(session):
        # HUMAN_GATE still reconciles: later blocked-task reasons (especially
        # REMOTE_PUSH_APPROVAL_REQUIRED) must surface as additional typed
        # gates. Open gates keep the objective stopped via _reassess_completion.
        if objective.state not in {"ACTIVE", "WAITING_ON_TASKS", "RECONCILING", "HUMAN_GATE"}:
            continue
        summaries.append(reconcile_objective(session, objective))
    return summaries


def reconcile_objective(session: Session, objective: BuildObjective) -> ObjectiveReconcileSummary:
    summary = ObjectiveReconcileSummary(objective_id=objective.objective_id)
    tasks = objective_tasks(session, objective.objective_id)
    task_by_id = {task.task_id: task for task in tasks}

    for task in tasks:
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == task.task_id)
            .where(BuildRunnerExecution.status == "SUCCEEDED")
            .order_by(BuildRunnerExecution.completed_at)
        ).all()
        for execution in executions:
            _reconcile_execution(session, objective, task, execution, summary)

    _reconcile_stale_gates(session, objective, task_by_id, summary)
    _reconcile_blocked_tasks(session, objective, tasks, summary)
    _reassess_completion(session, objective, task_by_id, summary)
    return summary


# Gate types raised from a runner-observed BLOCKED task (see
# BLOCKED_REASON_TO_GATE_TYPE above) describe a *transient* task-level
# condition, not an irreversible policy decision -- if the underlying
# task moves out of BLOCKED on its own (e.g. an automatic rebase clears a
# MERGE_CONFLICT before a human gets to the gate), the gate is stale and
# must not keep stranding the objective waiting on a decision nobody
# needs to make anymore.
_RECONCILABLE_GATE_TYPES = frozenset(BLOCKED_REASON_TO_GATE_TYPE.values())

# The exact reason prefix `_reconcile_blocked_tasks` uses below -- shared so
# `_reconcile_stale_gates` can tell a gate that exists *because* a task was
# BLOCKED apart from a gate of the same type raised directly from a
# structured `human_gate` field (which carries no such transient,
# runner-observed condition to reconcile against).
_BLOCKED_TASK_GATE_REASON_MARKER = "blocked by runner:"


def _reconcile_stale_gates(
    session: Session,
    objective: BuildObjective,
    task_by_id: dict[str, BuildTask],
    summary: ObjectiveReconcileSummary,
) -> None:
    for gate in open_gates(session, objective.objective_id):
        if gate.gate_type not in _RECONCILABLE_GATE_TYPES:
            continue
        if gate.source_task_id is None:
            continue
        if _BLOCKED_TASK_GATE_REASON_MARKER not in gate.reason:
            continue  # not raised from a BLOCKED-task condition -- nothing to reconcile
        task = task_by_id.get(gate.source_task_id)
        if task is None or task.state == "BLOCKED":
            continue  # still blocked (or task gone) -- condition has not cleared
        gate.status = "RESOLVED"
        gate.resolved_at = utcnow()
        gate.resolved_by = "system:auto-reconciled"
        gate.resolution_note = (
            f"source task {task.task_id} left BLOCKED (now {task.state}) before the "
            "gate was actioned; underlying condition self-resolved"
        )
        _record_event(
            session,
            objective.objective_id,
            "objective.gate_reconciled",
            gate_id=gate.gate_id,
            gate_type=gate.gate_type,
            source_task_id=task.task_id,
            resolved_task_state=task.state,
        )
        summary.gates_reconciled.append(gate.gate_id)

    if summary.gates_reconciled and not open_gates(session, objective.objective_id) and objective.state == "HUMAN_GATE":
        objective.state = _resume_target_state(session, objective)
        objective.resume_state = None
        objective.updated_at = utcnow()
        _record_event(
            session,
            objective.objective_id,
            "objective.resumed_from_gate",
            actor="system:auto-reconciled",
            resumed_to=objective.state,
        )


def _reconcile_blocked_tasks(
    session: Session, objective: BuildObjective, tasks: list[BuildTask], summary: ObjectiveReconcileSummary
) -> None:
    for task in tasks:
        if task.state != "BLOCKED":
            continue
        reason = _latest_block_reason(session, task.task_id)
        if reason is None:
            continue
        gate_type = BLOCKED_REASON_TO_GATE_TYPE.get(reason)
        if gate_type is None:
            continue
        gate = _raise_gate(
            session,
            objective,
            gate_type=gate_type,
            reason=f"task {task.task_id} {_BLOCKED_TASK_GATE_REASON_MARKER} {reason}",
            source_task_id=task.task_id,
        )
        if gate is not None:
            summary.gates_raised.append(gate.gate_id)


def _latest_block_reason(session: Session, task_id: str) -> str | None:
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


def _already_processed(session: Session, objective_id: str, execution_id: str) -> bool:
    # Queried in Python rather than a JSON-path SQL predicate to avoid
    # depending on SQLite's json1 extension being compiled in -- this table
    # stays small (one row per processed execution per objective), so a
    # full scan here is not a performance concern.
    rows = session.scalars(
        select(BuildObjectiveEvent.event_data)
        .where(BuildObjectiveEvent.objective_id == objective_id)
        .where(BuildObjectiveEvent.event_type == "objective.execution_processed")
    ).all()
    return any(row.get("execution_id") == execution_id for row in rows)


def _reconcile_execution(
    session: Session,
    objective: BuildObjective,
    task: BuildTask,
    execution: BuildRunnerExecution,
    summary: ObjectiveReconcileSummary,
) -> None:
    if _already_processed(session, objective.objective_id, execution.execution_id):
        return

    try:
        # `execution.result_data` is the fail-closed contract persisted by
        # `execution/results.py::parse_executor_result` -- any role's
        # result may optionally carry an `objective_signal` block. This is
        # NOT the raw executor payload; it already passed that contract's
        # own validation once, on the way into the DB.
        structured = StructuredTaskResult.from_mapping(execution.result_data.get("objective_signal"))
    except StructuredContractError as exc:
        _raise_gate(
            session,
            objective,
            gate_type="UNRESOLVABLE_CONFLICT",
            reason=f"execution {execution.execution_id} produced an invalid structured result: {exc}",
            source_task_id=task.task_id,
        )
        _mark_processed(session, objective, execution)
        return

    if structured.human_gate is not None:
        gate = _raise_gate(
            session,
            objective,
            gate_type=structured.human_gate,
            reason=f"task {task.task_id} ({execution.role}) requested a human gate",
            source_task_id=task.task_id,
        )
        if gate is not None:
            summary.gates_raised.append(gate.gate_id)
        _mark_processed(session, objective, execution)
        return

    for index, finding in enumerate(structured.follow_up_tasks):
        created = _auto_create_finding_task(
            session, objective, task, finding, index, kind="FOLLOW_UP", summary=summary
        )
        if created:
            summary.follow_ups_created.append(created)

    for index, finding in enumerate(structured.unrelated_findings):
        created = _auto_create_finding_task(
            session, objective, task, finding, index, kind="UNRELATED_FINDING", summary=summary
        )
        if created:
            summary.unrelated_tasks_created.append(created)

    _mark_processed(session, objective, execution)


def _mark_processed(session: Session, objective: BuildObjective, execution: BuildRunnerExecution) -> None:
    _record_event(
        session,
        objective.objective_id,
        "objective.execution_processed",
        execution_id=execution.execution_id,
        task_id=execution.task_id,
        role=execution.role,
    )


def _task_depth(task_by_id: dict[str, BuildTask], task_id: str | None, *, limit: int = 64) -> int:
    depth = 0
    current = task_id
    seen: set[str] = set()
    while current is not None and depth <= limit:
        if current in seen:
            return limit + 1  # cycle in persisted data; treat as over any real limit
        seen.add(current)
        task = task_by_id.get(current)
        if task is None or task.parent_task_id is None:
            return depth
        current = task.parent_task_id
        depth += 1
    return depth


def _auto_create_finding_task(
    session: Session,
    objective: BuildObjective,
    source_task: BuildTask,
    finding: FindingSpec,
    index: int,
    *,
    kind: str,
    summary: ObjectiveReconcileSummary,
) -> str | None:
    dedup_key = _dedup_key(objective.objective_id, source_task.task_id, kind, finding.title, finding.description)
    existing = session.scalar(
        select(BuildTask)
        .where(BuildTask.objective_id == objective.objective_id)
        .where(BuildTask.dedup_key == dedup_key)
    )
    if existing is not None:
        return None  # duplicate finding: already handled, restart-safe no-op

    if finding.risk_level not in ("LOW",):
        _raise_gate(
            session,
            objective,
            gate_type="MAJOR_SCOPE_EXPANSION_REQUIRED",
            reason=(
                f"{kind} finding {finding.title!r} from {source_task.task_id} has risk "
                f"level {finding.risk_level}, above the safe auto-creation threshold"
            ),
            source_task_id=source_task.task_id,
        )
        return None

    if objective.auto_created_task_count >= objective.max_auto_created_tasks:
        _raise_gate(
            session,
            objective,
            gate_type="MAJOR_SCOPE_EXPANSION_REQUIRED",
            reason=(
                f"objective has reached its max_auto_created_tasks limit "
                f"({objective.max_auto_created_tasks}); {kind} finding {finding.title!r} "
                "requires human approval to proceed"
            ),
            source_task_id=source_task.task_id,
        )
        return None

    parent_task_id = source_task.task_id if kind == "FOLLOW_UP" else None
    projected_depth = _task_depth(
        {t.task_id: t for t in objective_tasks(session, objective.objective_id)}, parent_task_id
    ) + 1
    if projected_depth > objective.max_child_depth:
        _raise_gate(
            session,
            objective,
            gate_type="MAJOR_SCOPE_EXPANSION_REQUIRED",
            reason=(
                f"{kind} finding {finding.title!r} from {source_task.task_id} would exceed "
                f"max_child_depth ({objective.max_child_depth})"
            ),
            source_task_id=source_task.task_id,
        )
        return None

    task_id = finding.task_id or f"{objective.objective_id}-{kind}-{dedup_key[:10]}"
    if session.get(BuildTask, task_id) is not None:
        # id collision against a task outside this objective/dedup scheme: make it unique deterministically
        task_id = f"{task_id}-{dedup_key[10:16]}"

    planned = PlannedChildTask(
        task_id=task_id,
        title=finding.title,
        description=finding.description or finding.reason,
        parent_task_id=parent_task_id,
        reason_created=kind,
        scope=finding.scope,
        risk_level=finding.risk_level,
    )
    task = _create_planned_task(session, objective, planned, plan_id="RECONCILIATION")
    task.dedup_key = dedup_key
    objective.auto_created_task_count += 1
    objective.updated_at = utcnow()
    _record_event(
        session,
        objective.objective_id,
        f"objective.{kind.lower()}_task_created",
        task_id=task_id,
        source_task_id=source_task.task_id,
        reason=finding.reason,
    )
    return task_id


def _reassess_completion(
    session: Session,
    objective: BuildObjective,
    task_by_id: dict[str, BuildTask],
    summary: ObjectiveReconcileSummary,
) -> None:
    if objective.state == "HUMAN_GATE" or open_gates(session, objective.objective_id):
        objective.state = "HUMAN_GATE"
        return

    tasks = [task for task in task_by_id.values() if not is_planner_task(task)]
    if not tasks:
        return  # PLANNING with no applied plan yet -- nothing to reassess

    if any(task.state in UNRECOVERABLE_TASK_STATES for task in tasks):
        objective.state = "FAILED"
        objective.updated_at = utcnow()
        _record_event(session, objective.objective_id, "objective.failed", failed_tasks=[
            t.task_id for t in tasks if t.state in UNRECOVERABLE_TASK_STATES
        ])
        return

    all_done = all(task.state in TERMINAL_TASK_STATES for task in tasks)
    if not all_done:
        objective.state = "WAITING_ON_TASKS" if objective.state == "ACTIVE" else objective.state
        return

    missing_criteria = [c for c in objective.completion_criteria if c not in task_by_id or task_by_id[c].state != "DONE"]
    if missing_criteria:
        objective.state = "WAITING_ON_TASKS"
        return

    objective.state = "COMPLETED"
    objective.updated_at = utcnow()
    summary.completed = True
    _record_event(
        session,
        objective.objective_id,
        "objective.completed",
        task_count=len(tasks),
    )
