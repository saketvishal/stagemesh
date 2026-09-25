"""Value objects for Build Coordinator service operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from build_coordinator.config import get_settings


def _normalize_items(items: tuple[str, ...], replace_slash: bool = False) -> tuple[str, ...]:
    cleaned_items: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned:
            cleaned_items.append(cleaned.replace("\\", "/") if replace_slash else cleaned)
    return tuple(cleaned_items)


@dataclass(frozen=True)
class TaskOwnershipScope:
    """Structured project/module task ownership envelope."""

    project: str
    primary_module: str
    allowed_modules: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    public_dependencies: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    primary_tests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.project or not self.project.strip():
            raise ValueError("project must be non-empty")
        if not self.primary_module or not self.primary_module.strip():
            raise ValueError("primary_module must be non-empty")
        object.__setattr__(self, "project", self.project.strip())
        object.__setattr__(self, "primary_module", self.primary_module.strip())
        object.__setattr__(self, "allowed_modules", _normalize_items(self.allowed_modules))
        object.__setattr__(self, "allowed_paths", _normalize_items(self.allowed_paths, replace_slash=True))
        object.__setattr__(self, "public_dependencies", _normalize_items(self.public_dependencies))
        object.__setattr__(self, "forbidden_paths", _normalize_items(self.forbidden_paths, replace_slash=True))
        object.__setattr__(self, "primary_tests", _normalize_items(self.primary_tests, replace_slash=True))
        self._validate_project_paths()

    def _validate_project_paths(self) -> None:
        project_roots = get_settings().project_roots or {}
        configured_root = project_roots.get(self.project)
        if not configured_root:
            return
        root = configured_root.strip().replace("\\", "/").strip("/")
        allowed_roots = (f"{root}/",)
        for path in self.allowed_paths:
            literal = path.rstrip("*")
            if literal and not literal.startswith(allowed_roots):
                raise ValueError(
                    f"allowed path {path!r} is outside project {self.project!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "primary_module": self.primary_module,
            "allowed_modules": list(self.allowed_modules),
            "allowed_paths": list(self.allowed_paths),
            "public_dependencies": list(self.public_dependencies),
            "forbidden_paths": list(self.forbidden_paths),
            "primary_tests": list(self.primary_tests),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TaskOwnershipScope | None:
        if not data or not isinstance(data, dict):
            return None
        primary = data.get("primary_module")
        project = data.get("project")
        if not primary or not project:
            return None
        return cls(
            project=project,
            primary_module=primary,
            allowed_modules=tuple(data.get("allowed_modules") or ()),
            allowed_paths=tuple(data.get("allowed_paths") or ()),
            public_dependencies=tuple(data.get("public_dependencies") or ()),
            forbidden_paths=tuple(data.get("forbidden_paths") or ()),
            primary_tests=tuple(data.get("primary_tests") or ()),
        )


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    dependencies: list[str] = field(default_factory=list)
    risk_level: str = "MEDIUM"
    review_policy: str = "SELF"
    permitted_scope: list[str] = field(default_factory=list)
    required_validation: list[str] = field(default_factory=list)
    implementation_notes: str | None = None
    program_key: str | None = None
    base_sha: str | None = None
    migration_allowed: bool = False
    ownership_scope: TaskOwnershipScope | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ownership_scope, dict):
            object.__setattr__(
                self, "ownership_scope", TaskOwnershipScope.from_dict(self.ownership_scope)
            )


@dataclass(frozen=True)
class ClaimRequest:
    task_id: str
    worker_id: str
    provider: str | None = None
    worker_metadata: dict[str, Any] = field(default_factory=dict)
    branch_name: str | None = None
    worktree_path: str | None = None
    lease_seconds: int = 1800


@dataclass(frozen=True)
class CheckpointInput:
    current_step: str
    current_head_sha: str | None = None
    completed_work: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    commits_created: list[str] = field(default_factory=list)
    last_successful_tests: list[str] = field(default_factory=list)
    known_failures: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EventInput:
    task_id: str | None
    event_type: str
    actor: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    claim_id: UUID | str | None = None
    event_data: dict[str, Any] = field(default_factory=dict)


OBJECTIVE_GATE_TYPES = (
    "ARCHITECTURE_DECISION_REQUIRED",
    "SECURITY_DECISION_REQUIRED",
    "PRIVACY_DECISION_REQUIRED",
    "EXTERNAL_COST_APPROVAL_REQUIRED",
    "CREDENTIAL_REQUIRED",
    "DESTRUCTIVE_ACTION_APPROVAL_REQUIRED",
    "MAJOR_SCOPE_EXPANSION_REQUIRED",
    "REMOTE_MAIN_PUSH_APPROVAL_REQUIRED",
    "UNRESOLVABLE_CONFLICT",
)
KNOWN_OBJECTIVE_GATE_TYPES = frozenset(OBJECTIVE_GATE_TYPES)

TASK_OUTCOME_VALUES = ("SUCCESS", "PARTIAL", "FAILURE")
KNOWN_TASK_OUTCOMES = frozenset(TASK_OUTCOME_VALUES)

FINDING_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
AUTO_CREATABLE_RISK_LEVELS = frozenset({"LOW"})
PLANNER_TASK_REASON = "OBJECTIVE_PLANNER"
OBJECTIVE_ROOT_COMPAT_REASON = "OBJECTIVE_ROOT_COMPAT"
PLANNER_ALLOWED_REVIEW_POLICIES = frozenset({"INDEPENDENT", "TWO_REVIEWERS"})

PLANNED_CHILD_TASK_FIELDS = frozenset(
    {
        "task_id",
        "title",
        "goal",
        "description",
        "acceptance_criteria",
        "dependencies",
        "parent_task_id",
        "reason_created",
        "scope",
        "prohibited_scope",
        "parallel_safe",
        "risk_level",
        "requires_integration",
        "review_policy",
    }
)
OBJECTIVE_PLAN_FIELDS = frozenset({"tasks", "child_tasks", "requested_human_gates"})


def reject_unknown_fields(data: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    extra = sorted(str(key) for key in data if key not in allowed)
    if extra:
        raise StructuredContractError(f"unknown {where} field(s): {extra}")


class StructuredContractError(ValueError):
    """Raised when a structured field from an executor result (a planner's
    plan, or a builder/reviewer's structured result) fails validation. Fail
    closed: an invalid or unknown value must never silently mutate
    coordinator state."""


@dataclass(frozen=True)
class FindingSpec:
    """One follow-up or unrelated-finding entry inside a structured task
    result. Deliberately narrow -- a title/description/reason/risk/scope,
    never free-form prose that could carry hidden instructions."""

    title: str
    description: str
    reason: str
    risk_level: str = "MEDIUM"
    scope: tuple[str, ...] = ()
    task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.title or not self.title.strip():
            raise StructuredContractError("finding title must be non-empty")
        if self.risk_level not in FINDING_RISK_LEVELS:
            raise StructuredContractError(f"unknown finding risk_level: {self.risk_level!r}")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "FindingSpec":
        if not isinstance(data, dict):
            raise StructuredContractError(f"finding must be an object, got {type(data).__name__}")
        return cls(
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            reason=str(data.get("reason", "")),
            risk_level=str(data.get("risk_level", "MEDIUM")).upper(),
            scope=tuple(data.get("scope") or ()),
            task_id=data.get("task_id"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "scope": list(self.scope),
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class StructuredTaskResult:
    """Validated view of `BuildRunnerExecution.result_data`. Unknown enum
    values fail closed (raise) rather than being coerced or ignored, per
    the coordinator's fail-closed structured-input policy."""

    task_outcome: str = "SUCCESS"
    follow_up_required: bool = False
    follow_up_tasks: tuple[FindingSpec, ...] = ()
    unrelated_findings: tuple[FindingSpec, ...] = ()
    human_gate: str | None = None
    scope_change_requested: bool = False
    objective_progress: str | None = None
    integration_eligible: bool = False

    def __post_init__(self) -> None:
        if self.task_outcome not in KNOWN_TASK_OUTCOMES:
            raise StructuredContractError(f"unknown task_outcome: {self.task_outcome!r}")
        if self.human_gate is not None and self.human_gate not in KNOWN_OBJECTIVE_GATE_TYPES:
            raise StructuredContractError(f"unknown human_gate type: {self.human_gate!r}")

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "StructuredTaskResult":
        data = data or {}
        return cls(
            task_outcome=str(data.get("task_outcome", "SUCCESS")).upper(),
            follow_up_required=bool(data.get("follow_up_required", False)),
            follow_up_tasks=tuple(
                FindingSpec.from_mapping(item) for item in (data.get("follow_up_tasks") or ())
            ),
            unrelated_findings=tuple(
                FindingSpec.from_mapping(item) for item in (data.get("unrelated_findings") or ())
            ),
            human_gate=data.get("human_gate"),
            scope_change_requested=bool(data.get("scope_change_requested", False)),
            objective_progress=data.get("objective_progress"),
            integration_eligible=bool(data.get("integration_eligible", False)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_outcome": self.task_outcome,
            "follow_up_required": self.follow_up_required,
            "follow_up_tasks": [item.to_mapping() for item in self.follow_up_tasks],
            "unrelated_findings": [item.to_mapping() for item in self.unrelated_findings],
            "human_gate": self.human_gate,
            "scope_change_requested": self.scope_change_requested,
            "objective_progress": self.objective_progress,
            "integration_eligible": self.integration_eligible,
        }


@dataclass(frozen=True)
class PlannedChildTask:
    """One child task inside a validated ObjectivePlan. Mirrors TaskSpec's
    shape plus the provenance/graph fields an objective-generated task must
    retain. Unknown fields fail closed -- a planner cannot smuggle
    worktree/push/credential authorizations through extra keys."""

    task_id: str
    title: str
    description: str
    goal: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    parent_task_id: str | None = None
    reason_created: str = "OBJECTIVE_PLAN"
    scope: tuple[str, ...] = ()
    prohibited_scope: tuple[str, ...] = ()
    parallel_safe: bool = True
    risk_level: str = "MEDIUM"
    requires_integration: bool = True
    review_policy: str = "INDEPENDENT"

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise StructuredContractError("planned child task_id must be non-empty")
        if not self.title or not self.title.strip():
            raise StructuredContractError("planned child title must be non-empty")
        if self.risk_level not in FINDING_RISK_LEVELS:
            raise StructuredContractError(f"unknown risk_level: {self.risk_level!r}")
        if self.reason_created == PLANNER_TASK_REASON:
            raise StructuredContractError("planner output cannot create the controller planner task")
        if not self.goal:
            object.__setattr__(self, "goal", self.title)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PlannedChildTask":
        if not isinstance(data, dict):
            raise StructuredContractError(f"planned task must be an object, got {type(data).__name__}")
        reject_unknown_fields(data, PLANNED_CHILD_TASK_FIELDS, where="planned task")
        return cls(
            task_id=str(data.get("task_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            goal=str(data.get("goal") or data.get("title") or ""),
            acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
            dependencies=tuple(data.get("dependencies") or ()),
            parent_task_id=data.get("parent_task_id"),
            reason_created=str(data.get("reason_created", "OBJECTIVE_PLAN")),
            scope=tuple(data.get("scope") or ()),
            prohibited_scope=tuple(data.get("prohibited_scope") or ()),
            parallel_safe=bool(data.get("parallel_safe", True)),
            risk_level=str(data.get("risk_level", "MEDIUM")).upper(),
            requires_integration=bool(data.get("requires_integration", True)),
            review_policy=str(data.get("review_policy", "INDEPENDENT")),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "goal": self.goal,
            "description": self.description,
            "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies),
            "parent_task_id": self.parent_task_id,
            "reason_created": self.reason_created,
            "scope": list(self.scope),
            "prohibited_scope": list(self.prohibited_scope),
            "parallel_safe": self.parallel_safe,
            "risk_level": self.risk_level,
            "requires_integration": self.requires_integration,
            "review_policy": self.review_policy,
        }


@dataclass(frozen=True)
class ObjectivePlan:
    """Validated planner/operator decomposition. The only artifact that may
    create objective child tasks. The planner itself never writes tasks."""

    tasks: tuple[PlannedChildTask, ...]
    requested_human_gates: tuple[str, ...] = ()
    source: str = "EXPLICIT_INPUT"

    def __post_init__(self) -> None:
        if not self.tasks:
            raise StructuredContractError("objective plan must contain at least one child task")
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                raise StructuredContractError(f"duplicate task_id in plan: {task.task_id}")
            seen.add(task.task_id)
        for gate_type in self.requested_human_gates:
            if gate_type not in KNOWN_OBJECTIVE_GATE_TYPES:
                raise StructuredContractError(f"unknown requested human_gate type: {gate_type!r}")

    @classmethod
    def from_mapping(cls, data: Any, *, source: str = "EXPLICIT_INPUT") -> "ObjectivePlan":
        if isinstance(data, list):
            tasks = tuple(PlannedChildTask.from_mapping(item) for item in data)
            return cls(tasks=tasks, source=source)
        if not isinstance(data, dict):
            raise StructuredContractError(
                f"objective plan must be an object or list, got {type(data).__name__}"
            )
        reject_unknown_fields(data, OBJECTIVE_PLAN_FIELDS, where="objective plan")
        if "tasks" in data and "child_tasks" in data:
            raise StructuredContractError("objective plan cannot contain both tasks and child_tasks")
        raw_tasks = data.get("tasks", data.get("child_tasks"))
        if not isinstance(raw_tasks, list):
            raise StructuredContractError("objective plan tasks must be a list")
        raw_gates = data.get("requested_human_gates") or ()
        gates: list[str] = []
        for item in raw_gates:
            if isinstance(item, dict):
                raise StructuredContractError(
                    "requested_human_gates must be an array of typed gate strings, not objects"
                )
            gates.append(str(item))
        return cls(
            tasks=tuple(PlannedChildTask.from_mapping(item) for item in raw_tasks),
            requested_human_gates=tuple(gates),
            source=source,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tasks": [task.to_mapping() for task in self.tasks],
            "requested_human_gates": list(self.requested_human_gates),
        }


@dataclass(frozen=True)
class ObjectiveSpec:
    """One user-submitted high-level objective. This -- not free-form
    task-specific flags -- is the only thing a user provides; the
    controller derives everything else (worktrees, builder prompts,
    reviewer prompts, follow-up tasks) from this plus policy."""

    objective_id: str
    goal: str
    constraints: tuple[str, ...] = ()
    allowed_scope: tuple[str, ...] = ()
    prohibited_scope: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    human_gate_policy: dict[str, Any] = field(default_factory=dict)
    parallelism: int = 2
    main_push_policy: str = "HUMAN_GATED"
    child_tasks: tuple[PlannedChildTask, ...] = ()
    requested_human_gates: tuple[str, ...] = ()
    max_auto_created_tasks: int = 20
    max_child_depth: int = 4

    def __post_init__(self) -> None:
        if not self.objective_id or not self.objective_id.strip():
            raise StructuredContractError("objective_id must be non-empty")
        if not self.goal or not self.goal.strip():
            raise StructuredContractError("goal must be non-empty")
        if self.main_push_policy != "HUMAN_GATED":
            raise StructuredContractError(
                f"unsupported main_push_policy: {self.main_push_policy!r} -- "
                "remote main is always human-gated"
            )


@dataclass(frozen=True)
class ResumeContext:
    task_id: str
    title: str
    task_state: str
    project: str | None
    primary_module: str | None
    allowed_modules: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    public_dependencies: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    primary_tests: tuple[str, ...]
    base_sha: str | None
    migration_allowed: bool
    review_policy: str
    independent_review_required: bool
    branch_name: str | None
    worktree_path: str | None
    current_head_sha: str | None
    last_checkpoint_id: str | None
    last_checkpoint_at: datetime | None
    current_step: str | None
    completed_work: tuple[str, ...]
    remaining_work: tuple[str, ...]
    files_changed: tuple[str, ...]
    commits_created: tuple[str, ...]
    last_successful_tests: tuple[str, ...]
    known_failures: tuple[str, ...]
    explicit_decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    previous_worker_id: str | None
    current_claim_id: str | None
    current_claim_worker_id: str | None
    current_claim_type: str | None
    waiting_input: dict[str, Any] | None = None
