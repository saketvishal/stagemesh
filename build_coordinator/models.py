"""Build Coordinator persistence owned by tooling, not product runtime."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from build_coordinator.db import Base

COORDINATOR_MODES = ("RUNNING", "PAUSED", "DRAINING")
TASK_STATES = (
    "READY",
    "CLAIMED",
    "IN_PROGRESS",
    "WAITING_FOR_INPUT",
    "VALIDATING",
    "REVIEW_READY",
    "REVIEWING",
    "INTEGRATING",
    "AWAITING_EXTERNAL_CI",
    "REWORK_REQUIRED",
    "BLOCKED",
    "FAILED",
    "STALE",
    "RESUMABLE",
    "DONE",
)
REVIEW_POLICIES = ("NONE", "SELF", "INDEPENDENT", "TWO_REVIEWERS")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CLAIM_TYPES = ("IMPLEMENTATION", "REVIEW", "INTEGRATION")
CLAIM_STATUSES = ("ACTIVE", "RELEASED", "EXPIRED", "COMPLETED")
RUNNER_ROLES = ("BUILDER", "REVIEWER", "REMEDIATION", "INTEGRATION")
EXECUTION_STATUSES = (
    "LAUNCHED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "HUMAN_ACTION_REQUIRED",
    "WAITING_FOR_INPUT",
    "TERMINATED",
    "LOST",
)


def new_uuid() -> str:
    return str(uuid.uuid4())


class BuildCoordinatorState(Base):
    __tablename__ = "build_coordinator_state"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="chk_build_coord_singleton"),
        CheckConstraint(
            "mode IN ('RUNNING','PAUSED','DRAINING')",
            name="chk_build_coord_mode",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class BuildTask(Base):
    __tablename__ = "build_tasks"
    __table_args__ = (
        CheckConstraint(
            "state IN ('READY','CLAIMED','IN_PROGRESS','WAITING_FOR_INPUT','VALIDATING','REVIEW_READY',"
            "'REVIEWING','INTEGRATING','AWAITING_EXTERNAL_CI','REWORK_REQUIRED','BLOCKED','FAILED','STALE',"
            "'RESUMABLE','DONE')",
            name="chk_build_tasks_state",
        ),
        CheckConstraint(
            "review_policy IN ('NONE','SELF','INDEPENDENT','TWO_REVIEWERS')",
            name="chk_build_tasks_review_policy",
        ),
        CheckConstraint(
            "risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')",
            name="chk_build_tasks_risk_level",
        ),
        Index("idx_build_tasks_state", "state"),
        Index("idx_build_tasks_program", "program_key"),
        Index("idx_build_tasks_objective", "objective_id"),
        Index(
            "uq_build_tasks_objective_dedup_key",
            "objective_id",
            "dedup_key",
            unique=True,
            sqlite_where=text("dedup_key IS NOT NULL"),
            postgresql_where=text("dedup_key IS NOT NULL"),
        ),
    )

    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    dependencies: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    objective_id: Mapped[str | None] = mapped_column(
        ForeignKey("build_objectives.objective_id", ondelete="SET NULL"), nullable=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason_created: Mapped[str] = mapped_column(String(40), nullable=False, default="MANUAL")
    parallel_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_integration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    dedup_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="MEDIUM")
    review_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="SELF")
    permitted_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_validation: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    implementation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    program_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    migration_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ownership_scope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="READY")
    waiting_input: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Durable, content-fingerprinted registry of reviewer findings across
    # review/remediation cycles (see build_coordinator.runner.findings). Lets
    # convergence decisions be finding-aware instead of raw-cycle-count-aware.
    finding_registry: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    branch_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuildTaskClaim(Base):
    __tablename__ = "build_task_claims"
    __table_args__ = (
        CheckConstraint(
            "claim_type IN ('IMPLEMENTATION','REVIEW','INTEGRATION')",
            name="chk_build_claims_type",
        ),
        CheckConstraint("status IN ('ACTIVE','RELEASED','EXPIRED','COMPLETED')", name="chk_build_claims_status"),
        Index(
            "uq_build_claims_one_active_per_type",
            "task_id",
            "claim_type",
            unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "uq_build_claims_one_active_migration",
            "claim_type",
            unique=True,
            sqlite_where=text(
                "status = 'ACTIVE' AND claim_type = 'IMPLEMENTATION' AND migration_allowed = 1"
            ),
            postgresql_where=text(
                "status = 'ACTIVE' AND claim_type = 'IMPLEMENTATION' AND migration_allowed = true"
            ),
        ),
        Index(
            "uq_build_claims_one_active_builder_slot",
            "builder_slot",
            unique=True,
            sqlite_where=text(
                "status = 'ACTIVE' AND claim_type = 'IMPLEMENTATION' AND builder_slot IS NOT NULL"
            ),
            postgresql_where=text(
                "status = 'ACTIVE' AND claim_type = 'IMPLEMENTATION' AND builder_slot IS NOT NULL"
            ),
        ),
        Index("idx_build_claims_task", "task_id"),
        Index("idx_build_claims_worker", "worker_id"),
    )

    claim_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("build_tasks.task_id", ondelete="CASCADE"), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    migration_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    builder_slot: Mapped[int | None] = mapped_column(nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ACTIVE")


class BuildTaskCheckpoint(Base):
    __tablename__ = "build_task_checkpoints"
    __table_args__ = (
        Index("idx_build_checkpoints_task", "task_id"),
        Index("idx_build_checkpoints_claim", "claim_id"),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("build_tasks.task_id", ondelete="CASCADE"), nullable=False)
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("build_task_claims.claim_id", ondelete="SET NULL"), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    current_step: Mapped[str] = mapped_column(Text, nullable=False)
    current_head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_work: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    remaining_work: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    files_changed: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    branch_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    commits_created: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_successful_tests: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    known_failures: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    blockers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuildTaskEvent(Base):
    __tablename__ = "build_task_events"
    __table_args__ = (
        Index("idx_build_events_task", "task_id"),
        Index("idx_build_events_type", "event_type"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("build_tasks.task_id", ondelete="CASCADE"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuildRunnerExecution(Base):
    __tablename__ = "build_runner_executions"
    __table_args__ = (
        CheckConstraint(
            "role IN ('BUILDER','REVIEWER','REMEDIATION','INTEGRATION','PLANNER')",
            name="chk_build_runner_execution_role",
        ),
        CheckConstraint(
            "status IN ('LAUNCHED','RUNNING','SUCCEEDED','FAILED',"
            "'HUMAN_ACTION_REQUIRED','WAITING_FOR_INPUT','TERMINATED','LOST')",
            name="chk_build_runner_execution_status",
        ),
        Index("idx_build_runner_exec_task", "task_id"),
        Index("idx_build_runner_exec_claim", "claim_id"),
        Index("idx_build_runner_exec_status", "status"),
        Index(
            "uq_build_runner_one_live_integration",
            "task_id",
            unique=True,
            sqlite_where=text(
                "role = 'INTEGRATION' AND status IN ('LAUNCHED','RUNNING')"
            ),
            postgresql_where=text(
                "role = 'INTEGRATION' AND status IN ('LAUNCHED','RUNNING')"
            ),
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("build_tasks.task_id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    adapter: Mapped[str] = mapped_column(String(80), nullable=False)
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    process_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_feature_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="LAUNCHED")
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    result_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    human_escalation_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    launched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


OBJECTIVE_STATES = (
    "PLANNING",
    "ACTIVE",
    "WAITING_ON_TASKS",
    "RECONCILING",
    "HUMAN_GATE",
    "COMPLETED",
    "FAILED",
    "PAUSED",
)
OBJECTIVE_GATE_STATUSES = ("OPEN", "APPROVED", "RESOLVED")


class BuildObjective(Base):
    """One user-submitted high-level goal. Everything the autonomous
    controller does in service of that goal (planning, child tasks, follow
    ups, gates) is scoped under this row. No hidden reasoning is stored
    here -- only the structured spec, policy, and lifecycle state."""

    __tablename__ = "build_objectives"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PLANNING','ACTIVE','WAITING_ON_TASKS','RECONCILING',"
            "'HUMAN_GATE','COMPLETED','FAILED','PAUSED')",
            name="chk_build_objectives_state",
        ),
        Index("idx_build_objectives_state", "state"),
    )

    objective_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    prohibited_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    completion_criteria: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    human_gate_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    parallelism: Mapped[int] = mapped_column(nullable=False, default=2)
    main_push_policy: Mapped[str] = mapped_column(String(40), nullable=False, default="HUMAN_GATED")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="PLANNING")
    max_auto_created_tasks: Mapped[int] = mapped_column(nullable=False, default=20)
    max_child_depth: Mapped[int] = mapped_column(nullable=False, default=4)
    auto_created_task_count: Mapped[int] = mapped_column(nullable=False, default=0)
    # The lifecycle state to resume to once the current HUMAN_GATE/PAUSED
    # interruption clears -- captured at the moment the objective first
    # left its normal progressing state, so e.g. an objective gated while
    # still PLANNING (no plan applied yet) resumes to PLANNING rather than
    # being forced into ACTIVE with no work to reconcile.
    resume_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuildObjectivePlan(Base):
    """A validated, structured decomposition of an objective into child
    tasks. Immutable once created -- a re-plan creates a new version rather
    than mutating history, so audit trail stays intact."""

    __tablename__ = "build_objective_plans"
    __table_args__ = (
        Index("idx_build_objective_plans_objective", "objective_id"),
    )

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("build_objectives.objective_id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    plan_data: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BuildObjectiveGate(Base):
    """A typed human gate raised against an objective. Only a human
    resolving this row (via `objective approve`) lets the objective's
    automatic reconciliation resume."""

    __tablename__ = "build_objective_gates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','APPROVED','RESOLVED')",
            name="chk_build_objective_gates_status",
        ),
        Index("idx_build_objective_gates_objective", "objective_id"),
        Index("idx_build_objective_gates_status", "status"),
    )

    gate_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("build_objectives.objective_id", ondelete="CASCADE"), nullable=False
    )
    source_task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BuildObjectiveEvent(Base):
    """Append-only, structured-only audit trail for an objective. Never a
    home for model chain-of-thought -- `event_data` carries reason codes,
    ids, and outcomes, not prompts or raw agent output."""

    __tablename__ = "build_objective_events"
    __table_args__ = (
        Index("idx_build_objective_events_objective", "objective_id"),
        Index("idx_build_objective_events_type", "event_type"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    objective_id: Mapped[str] = mapped_column(
        ForeignKey("build_objectives.objective_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    event_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
