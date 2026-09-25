"""Runner configuration and structured result contracts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build_coordinator.runner.routing import (
    DEFAULT_ROLE_CAPABILITIES,
    DEFAULT_ROLE_PERMISSIONS,
    DEFAULT_ROLE_STAGES,
    ProviderConfig,
    RoutingPolicy,
    RuntimeConfig,
    StageRequirement,
    WorkerModelConfig,
    default_stage_requirements,
)


HUMAN_ESCALATION_TYPES = (
    "SCOPE_EXPANSION_REQUIRED",
    "ARCHITECTURE_DECISION_REQUIRED",
    "REMOTE_PUSH_APPROVAL_REQUIRED",
    "MERGE_CONFLICT",
    "MERGE_CONFLICT_RECOVERY_FAILED",
    "MIGRATION_SCOPE_VIOLATION",
    "SECURITY_POLICY_BLOCK",
    "EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED",
    "REMEDIATION_LIMIT_REACHED",
    "REVIEW_ENVIRONMENT_BLOCKED",
    "TEST_FAILURE_REQUIRES_JUDGMENT",
    "COORDINATOR_INVARIANT_FAILURE",
    "REVIEWED_SHA_CHANGED",
    "UPSTREAM_PUSH_FAILED",
    "WORKING_CHECKOUT_DIRTY",
    "AGENT_AUTHENTICATION_REQUIRED",
    "EXECUTION_RETRY_LIMIT_REACHED",
    "GIT_SAFETY_FAILURE",
    "WORKTREE_INVALID",
    "NO_CHANGES_PRODUCED",
    "BUILDER_BLOCKER",
    "MISSING_REVIEWED_SHA",
    "BRANCH_MOVED_CONCURRENTLY",
)

REVIEW_VERDICT_VALUES = (
    "GREEN",
    "GREEN_WITH_NOTES",
    "REMEDIATION_REQUIRED",
    "REVIEW_ENVIRONMENT_BLOCKED",
)
ELIGIBLE_REVIEW_VERDICTS = frozenset({"GREEN", "GREEN_WITH_NOTES"})
KNOWN_REVIEW_VERDICTS = frozenset(REVIEW_VERDICT_VALUES)
FORBIDDEN_REVIEW_VERDICT_ALIASES = (
    "APPROVE",
    "APPROVED",
    "PASS",
    "REJECT",
)


def review_verdict_instructions() -> str:
    allowed = "\n".join(REVIEW_VERDICT_VALUES)
    forbidden = "\n".join(FORBIDDEN_REVIEW_VERDICT_ALIASES)
    return (
        "For reviewer role:\n\n"
        "verdict MUST be exactly one of:\n\n"
        f"{allowed}\n\n"
        "Do NOT emit:\n\n"
        f"{forbidden}\n\n"
        "Reviewer result must also contain:\n\n"
        "ready_for_integration: boolean\n"
        "required_remediation: array\n\n"
        "Rules:\n\n"
        "GREEN:\n"
        "ready_for_integration = true\n"
        "required_remediation = []\n\n"
        "GREEN_WITH_NOTES:\n"
        "integration allowed only when:\n"
        "ready_for_integration = true\n"
        "required_remediation = []\n\n"
        "REMEDIATION_REQUIRED:\n"
        "ready_for_integration = false\n"
        "Use when implementation violates requirements.\n\n"
        "REVIEW_ENVIRONMENT_BLOCKED:\n"
        "ready_for_integration = false\n"
        "required_remediation = []\n"
        "Use when verification cannot be completed because review infrastructure, environment, or required tooling is unavailable.\n"
        "Do NOT request source-code remediation for review environment or tooling failures.\n\n"
        "finding_dispositions (optional array): when re-reviewing a task, "
        "resume_context.open_findings_from_prior_review (if present) lists prior findings with their durable ids. "
        "Classify each one you were shown as one of RESOLVED, STILL_OPEN, INVALID, or "
        "NOT_APPLICABLE, e.g. [{\"id\": \"<finding id>\", \"status\": \"RESOLVED\", \"reason\": \"...\"}]. "
        "A prior finding you do not restate in `findings` and do not classify here is presumed resolved. "
        "To keep such a finding open anyway, or to reopen one already resolved, include it here with "
        "status STILL_OPEN and a reason.\n"
    )


def review_verdict_contract() -> dict[str, Any]:
    return {
        "must_be_exactly_one_of": list(REVIEW_VERDICT_VALUES),
        "do_not_emit": list(FORBIDDEN_REVIEW_VERDICT_ALIASES),
        "ready_for_integration": "boolean",
        "required_remediation": "array",
        "consistency": {
            "GREEN": {"ready_for_integration": True, "required_remediation": []},
            "GREEN_WITH_NOTES": {"ready_for_integration": True, "required_remediation": []},
            "REMEDIATION_REQUIRED": {"ready_for_integration": False},
            "REVIEW_ENVIRONMENT_BLOCKED": {"ready_for_integration": False, "required_remediation": []},
        },
        "instructions": review_verdict_instructions(),
    }


class ReviewVerdictContradiction(ValueError):
    """Raised when a structured review result is internally contradictory."""


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    role: str
    provider: str = "local"
    adapter: str = "fake"
    command: tuple[str, ...] = ()
    worktree_path: str | None = None
    branch_name: str | None = None
    timeout_seconds: int | None = None
    poll_seconds: float | None = None
    enabled: bool = True
    runtime: str = "local"
    model: str | None = None
    capabilities: tuple[str, ...] = ()
    max_concurrency: int = 1
    stages: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    env: dict[str, Any] = field(default_factory=dict)
    preference: int = 100
    cost: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", _normal_tuple(self.capabilities))
        object.__setattr__(self, "stages", _normal_tuple(self.stages))
        object.__setattr__(
            self,
            "permissions",
            _normal_tuple(self.permissions) or DEFAULT_ROLE_PERMISSIONS.get(self.role, ()),
        )
        object.__setattr__(self, "command", tuple(self.command or ()))
        object.__setattr__(self, "max_concurrency", int(self.max_concurrency or 1))
        object.__setattr__(self, "preference", int(self.preference or 100))

    def stage_names(self) -> tuple[str, ...]:
        return self.stages or DEFAULT_ROLE_STAGES.get(self.role, (self.role.lower(),))

    def capability_names(self) -> tuple[str, ...]:
        return self.capabilities or DEFAULT_ROLE_CAPABILITIES.get(self.role, ())

    def public_summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "enabled": self.enabled,
            "role": self.role,
            "runtime": self.runtime,
            "provider": self.provider,
            "model": self.model,
            "adapter": self.adapter,
            "capabilities": list(self.capability_names()),
            "max_concurrency": self.max_concurrency,
            "stages": list(self.stage_names()),
            "permissions": list(self.permissions),
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "timeout_seconds": self.timeout_seconds,
            "poll_seconds": self.poll_seconds,
            "preference": self.preference,
            "cost": self.cost,
            "env": _public_env_refs(self.env),
        }

    def resolved_env(self) -> dict[str, str]:
        """Resolve operator-approved environment references without persisting values."""
        resolved: dict[str, str] = {}
        for key, value in self.env.items():
            if isinstance(value, str):
                resolved[str(key)] = value
                continue
            if not isinstance(value, dict):
                continue
            source = str(value.get("source") or "").lower()
            if source == "environment":
                variable = str(value.get("variable") or key)
                if variable in os.environ:
                    resolved[str(key)] = os.environ[variable]
            elif source == "literal_path":
                path = value.get("path")
                if path:
                    resolved[str(key)] = str(path)
        return resolved


@dataclass(frozen=True)
class RunnerConfig:
    workers: tuple[WorkerConfig, ...] = field(default_factory=tuple)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    runtimes: dict[str, RuntimeConfig] = field(default_factory=dict)
    models: dict[str, WorkerModelConfig] = field(default_factory=dict)
    stage_requirements: dict[str, StageRequirement] = field(default_factory=default_stage_requirements)
    routing_policy: RoutingPolicy = field(default_factory=RoutingPolicy)
    poll_seconds: float = 5.0
    max_remediation_cycles: int = 2
    max_review_environment_attempts: int = 2
    auto_push_allowed: bool = False
    allowed_workspace_roots: tuple[str, ...] = ()
    result_dir: str | None = None
    main_ref: str = "main"
    remote_name: str | None = "origin"
    upstream_remote: str | None = None
    push_upstream: bool = False
    run_validation: bool = True
    validation_timeout_seconds: float = 900.0
    max_execution_attempts: int = 3
    max_conflict_recovery_attempts: int = 2
    cleanup_branches: bool = False
    # When true every builder task gets its own branch, started from main_ref,
    # in the worker's managed worktree instead of reusing one branch per worker.
    task_branches: bool = False
    # #65: opt-in, work-conserving scheduling while external (e.g. GitHub
    # Actions) CI is pending on an integrated push. Strictly additive:
    # when False (the default), INTEGRATING -> DONE behaves exactly as it
    # did before this was added.
    external_ci_enabled: bool = False
    external_ci_repo: str | None = None
    external_ci_max_consecutive_errors: int = 5
    # GH-56: opt-in, repository-scoped standalone clone pools. When enabled,
    # each worker's workspace for a repository is a full standalone `git
    # clone` under clone_pool_root (keyed by repository identity and worker
    # id) instead of a linked `git worktree add` checkout, avoiding the
    # shared .git/worktrees metadata problems linked worktrees hit on
    # Windows. Strictly additive: when False (the default), worktree
    # provisioning behaves exactly as it did before this was added.
    use_clone_pool: bool = False
    clone_pool_root: str | None = None

    @classmethod
    def default(cls, *, dry_run: bool = False) -> "RunnerConfig":
        path = os.getenv("BUILD_COORDINATOR_RUNNER_CONFIG")
        if path:
            return cls.from_file(Path(path), dry_run=dry_run)
        from build_coordinator.coordinator_config import load_coordinator_config

        coordinator = load_coordinator_config()
        adapter = "fake" if dry_run else "unconfigured"
        default_roles = (
            ("planner-1", "PLANNER"),
            ("builder-a", "BUILDER"),
            ("reviewer-1", "REVIEWER"),
            ("integration-1", "INTEGRATION"),
        )
        allowed_roots = _env_paths("BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS")
        if not allowed_roots and coordinator.worktrees:
            allowed_roots = tuple(coordinator.worktrees.values())
        return cls(
            workers=tuple(
                WorkerConfig(
                    worker_id,
                    role,
                    adapter=adapter,
                    worktree_path=(
                        None if role == "PLANNER" else coordinator.worktrees.get(worker_id)
                    ),
                    capabilities=DEFAULT_ROLE_CAPABILITIES.get(role, ()),
                    stages=DEFAULT_ROLE_STAGES.get(role, (role.lower(),)),
                )
                for worker_id, role in default_roles
            ),
            auto_push_allowed=os.getenv("BUILD_COORDINATOR_AUTO_PUSH_ALLOWED", "false").lower()
            == "true",
            allowed_workspace_roots=allowed_roots,
            result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
        )

    @classmethod
    def from_file(cls, path: Path, *, dry_run: bool = False) -> "RunnerConfig":
        data = _load_mapping(path)
        providers = {
            name: ProviderConfig.from_mapping(name, row)
            for name, row in (data.get("providers") or {}).items()
        }
        runtimes = {
            name: RuntimeConfig.from_mapping(name, row)
            for name, row in (data.get("runtimes") or {}).items()
        }
        models = {
            name: WorkerModelConfig.from_mapping(name, row)
            for name, row in (data.get("models") or {}).items()
        }
        stage_requirements = default_stage_requirements()
        stage_requirements.update(
            {
                name: StageRequirement.from_mapping(name, row)
                for name, row in (data.get("stages") or {}).items()
            }
        )
        routing_policy = RoutingPolicy.from_mapping(data.get("routing") or {})
        workers = []
        for row in data.get("workers", []):
            adapter = "fake" if dry_run else row.get("adapter", row.get("provider", "subprocess"))
            timeout = row.get("timeout_seconds")
            poll = row.get("poll_seconds")
            worker_id = row.get("worker_id") or row["id"]
            role = row.get("role") or _role_for_stages(tuple(row.get("stages") or ()))
            workers.append(
                WorkerConfig(
                    worker_id=worker_id,
                    role=role,
                    provider=row.get("provider", "local"),
                    adapter=adapter,
                    command=tuple(row.get("command") or ()),
                    worktree_path=row.get("worktree_path"),
                    branch_name=row.get("branch_name"),
                    timeout_seconds=int(timeout) if timeout is not None else None,
                    poll_seconds=float(poll) if poll is not None else None,
                    enabled=bool(row.get("enabled", True)),
                    runtime=row.get("runtime", row.get("adapter", "local")),
                    model=row.get("model"),
                    capabilities=tuple(row.get("capabilities") or DEFAULT_ROLE_CAPABILITIES.get(role, ())),
                    max_concurrency=int(row.get("max_concurrency", 1)),
                    stages=tuple(row.get("stages") or DEFAULT_ROLE_STAGES.get(role, (role.lower(),))),
                    permissions=tuple(row.get("permissions") or DEFAULT_ROLE_PERMISSIONS.get(role, ())),
                    env=dict(row.get("env") or row.get("env_refs") or {}),
                    preference=int(row.get("preference", row.get("routing_preference", 100))),
                    cost=dict(row.get("cost") or {}),
                )
            )
        allowed_roots = tuple(data.get("allowed_workspace_roots") or ())
        if not allowed_roots:
            allowed_roots = _env_paths("BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS")
        if not any(worker.role == "PLANNER" for worker in workers):
            donor = next(
                (
                    worker
                    for worker in workers
                    if worker.role == "BUILDER" and worker.adapter != "unconfigured"
                ),
                None,
            )
            if donor is not None:
                workers.append(
                    WorkerConfig(
                        "planner-1",
                        "PLANNER",
                        provider=donor.provider,
                        adapter=donor.adapter,
                        command=donor.command,
                        worktree_path=None,
                        timeout_seconds=donor.timeout_seconds,
                        poll_seconds=donor.poll_seconds,
                        runtime=donor.runtime,
                        model=donor.model,
                        capabilities=DEFAULT_ROLE_CAPABILITIES["PLANNER"],
                        stages=DEFAULT_ROLE_STAGES["PLANNER"],
                        permissions=donor.permissions,
                    )
                )
        return cls(
            workers=tuple(workers),
            providers=providers,
            runtimes=runtimes,
            models=models,
            stage_requirements=stage_requirements,
            routing_policy=routing_policy,
            poll_seconds=float(data.get("poll_seconds", 5.0)),
            max_remediation_cycles=int(data.get("max_remediation_cycles", 2)),
            max_review_environment_attempts=int(data.get("max_review_environment_attempts", 2)),
            auto_push_allowed=bool(data.get("auto_push_allowed", False)),
            allowed_workspace_roots=allowed_roots,
            result_dir=data.get("result_dir") or os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
            main_ref=str(data.get("main_ref") or "main"),
            remote_name=str(data.get("remote_name") or "origin"),
            upstream_remote=data.get("upstream_remote"),
            push_upstream=bool(data.get("push_upstream", False)),
            run_validation=bool(data.get("run_validation", True)),
            validation_timeout_seconds=float(data.get("validation_timeout_seconds", 900.0)),
            max_execution_attempts=int(data.get("max_execution_attempts", 3)),
            max_conflict_recovery_attempts=int(data.get("max_conflict_recovery_attempts", 2)),
            cleanup_branches=bool(data.get("cleanup_branches", False)),
            task_branches=bool(data.get("task_branches", False)),
            external_ci_enabled=bool((data.get("external_ci") or {}).get("enabled", False)),
            external_ci_repo=(data.get("external_ci") or {}).get("repo"),
            external_ci_max_consecutive_errors=int(
                (data.get("external_ci") or {}).get("max_consecutive_errors", 5)
            ),
            use_clone_pool=bool((data.get("clone_pool") or {}).get("enabled", False)),
            clone_pool_root=(data.get("clone_pool") or {}).get("root"),
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "routing_policy": self.routing_policy.to_public_dict(),
            "providers": {key: value.to_public_dict() for key, value in self.providers.items()},
            "runtimes": {key: value.to_public_dict() for key, value in self.runtimes.items()},
            "models": {key: value.to_public_dict() for key, value in self.models.items()},
            "stages": {key: value.to_public_dict() for key, value in self.stage_requirements.items()},
            "workers": [worker.public_summary() for worker in self.workers],
            "external_ci": {
                "enabled": self.external_ci_enabled,
                "repo": self.external_ci_repo,
                "max_consecutive_errors": self.external_ci_max_consecutive_errors,
            },
        }


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    findings: tuple[str, ...] = ()
    required_remediation: tuple[str, ...] = ()
    architecture_notes: tuple[str, ...] = ()
    ready_for_integration: bool = False
    # Optional, additive: explicit per-finding classification against the
    # durable finding registry (see build_coordinator.runner.findings). Each
    # entry is {"id": <finding fingerprint>, "status": RESOLVED|STILL_OPEN|
    # INVALID|NOT_APPLICABLE, "reason": <str>}. When absent, findings not
    # restated in `findings` are presumed resolved automatically.
    finding_dispositions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "ReviewVerdict":
        return cls(
            verdict=str(data.get("verdict", "")).upper(),
            findings=tuple(data.get("findings") or ()),
            required_remediation=tuple(data.get("required_remediation") or ()),
            architecture_notes=tuple(data.get("architecture_notes") or ()),
            ready_for_integration=bool(data.get("ready_for_integration", False)),
            finding_dispositions=tuple(
                item for item in (data.get("finding_dispositions") or ()) if isinstance(item, dict)
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": list(self.findings),
            "required_remediation": list(self.required_remediation),
            "architecture_notes": list(self.architecture_notes),
            "ready_for_integration": self.ready_for_integration,
            "finding_dispositions": [dict(item) for item in self.finding_dispositions],
        }

    def validate_consistency(self) -> None:
        if self.verdict not in KNOWN_REVIEW_VERDICTS:
            raise ReviewVerdictContradiction(
                f"unknown review verdict: {self.verdict or '(empty)'}"
            )
        if self.verdict in ELIGIBLE_REVIEW_VERDICTS:
            if self.required_remediation:
                raise ReviewVerdictContradiction(
                    f"{self.verdict} with required_remediation is contradictory"
                )
            if not self.ready_for_integration:
                raise ReviewVerdictContradiction(
                    f"{self.verdict} with ready_for_integration false is contradictory"
                )
        if self.verdict == "REMEDIATION_REQUIRED" and self.ready_for_integration:
            raise ReviewVerdictContradiction(
                "REMEDIATION_REQUIRED with ready_for_integration true is contradictory"
            )
        if self.verdict == "REVIEW_ENVIRONMENT_BLOCKED":
            if self.ready_for_integration:
                raise ReviewVerdictContradiction(
                    "REVIEW_ENVIRONMENT_BLOCKED with ready_for_integration true is contradictory"
                )
            if self.required_remediation:
                raise ReviewVerdictContradiction(
                    "REVIEW_ENVIRONMENT_BLOCKED with required_remediation is contradictory"
                )

    def integration_eligible(self) -> bool:
        self.validate_consistency()
        return (
            self.verdict in ELIGIBLE_REVIEW_VERDICTS
            and not self.required_remediation
            and self.ready_for_integration is True
        )


def _env_paths(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return ()
    return tuple(item.strip() for item in raw.split(os.pathsep) if item.strip())


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ModuleNotFoundError as exc:
            raise ValueError("YAML runner configs require PyYAML; JSON configs remain supported") from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"runner config must be a mapping: {path}")
    return data


def _normal_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _role_for_stages(stages: tuple[str, ...]) -> str:
    preferred = {
        "planning": "PLANNER",
        "implementation": "BUILDER",
        "remediation": "REMEDIATION",
        "review": "REVIEWER",
        "integration": "INTEGRATION",
    }
    for stage in stages:
        if stage in preferred:
            return preferred[stage]
    return "BUILDER"


def _public_env_refs(env: dict[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key, value in env.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("secret", "token", "password", "key")):
            public[key] = "<redacted-ref>"
        elif isinstance(value, dict):
            public[key] = {
                nested_key: ("<redacted-ref>" if "value" in str(nested_key).lower() else nested_value)
                for nested_key, nested_value in value.items()
            }
        else:
            public[key] = value
    return public
