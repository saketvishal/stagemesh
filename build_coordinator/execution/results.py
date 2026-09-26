"""Provider-neutral durable executor result contracts.

Executor result JSON is untrusted structured input. Lifecycle logic must
consume only values that pass typed validation. Secrets and hidden
reasoning are stripped and never persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULT_SCHEMA_VERSION = 1
RESULT_ENV_PATH = "BUILD_COORDINATOR_RESULT_PATH"
RESULT_ENV_EXECUTION_ID = "BUILD_COORDINATOR_EXECUTION_ID"
RESULT_ENV_TASK_ID = "BUILD_COORDINATOR_TASK_ID"
RESULT_ENV_ROLE = "BUILD_COORDINATOR_ROLE"
RESULT_ENV_REVIEWED_SHA = "BUILD_COORDINATOR_REVIEWED_FEATURE_SHA"

RESULT_STATUS_VALUES = (
    "SUCCEEDED",
    "FAILED",
    "HUMAN_ACTION_REQUIRED",
    "WAITING_FOR_INPUT",
    "TERMINATED",
    "LOST",
)
RESULT_STATUSES = frozenset(RESULT_STATUS_VALUES)
FORBIDDEN_RESULT_STATUS_ALIASES = (
    "SUCCESS",
    "COMPLETE",
    "OK",
    "APPROVED",
)
BUILDER_ROLES = frozenset({"BUILDER", "REMEDIATION"})
REVIEWER_ROLES = frozenset({"REVIEWER"})
INTEGRATOR_ROLES = frozenset({"INTEGRATION"})
PLANNER_ROLES = frozenset({"PLANNER"})
KNOWN_ROLES = BUILDER_ROLES | REVIEWER_ROLES | INTEGRATOR_ROLES | PLANNER_ROLES

_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "cookie",
    "private_key",
    "credential",
)
_HIDDEN_REASONING_KEYS = frozenset(
    {
        "chain_of_thought",
        "chain-of-thought",
        "hidden_reasoning",
        "hidden-reasoning",
        "reasoning",
        "thinking",
        "scratchpad",
        "internal_monologue",
        "internal-monologue",
    }
)


class ExecutorResultError(ValueError):
    """Raised when executor result JSON is malformed or contradictory."""


@dataclass(frozen=True)
class TestsSummary:
    items: tuple[str, ...] = ()
    passed: int | None = None
    failed: int | None = None
    raw: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuilderResult:
    feature_sha: str | None = None
    files_changed: tuple[str, ...] = ()
    tests: TestsSummary = field(default_factory=TestsSummary)
    scope_expansion_required: bool = False
    blockers: tuple[str, ...] = ()
    commits_created: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewerResult:
    reviewed_feature_sha: str | None = None
    verdict: Any = None


@dataclass(frozen=True)
class IntegratorResult:
    feature_sha: str | None = None
    reviewed_feature_sha: str | None = None
    current_main_sha: str | None = None
    merge_base: str | None = None
    merge_commit_sha: str | None = None
    tests: TestsSummary = field(default_factory=TestsSummary)
    push_status: str | None = None
    final_main_sha: str | None = None


@dataclass(frozen=True)
class ExecutorResult:
    schema_version: int
    execution_id: str
    task_id: str
    role: str
    status: str
    completed_at: str | None = None
    builder: BuilderResult | None = None
    reviewer: ReviewerResult | None = None
    integrator: IntegratorResult | None = None
    plan: dict[str, Any] | None = None
    human_escalation_type: str | None = None
    objective_signal: dict[str, Any] | None = None
    persisted: dict[str, Any] = field(default_factory=dict)


def result_env(
    *,
    result_path: str,
    execution_id: str,
    task_id: str,
    role: str,
    reviewed_feature_sha: str | None = None,
) -> dict[str, str]:
    payload = {
        RESULT_ENV_PATH: result_path,
        RESULT_ENV_EXECUTION_ID: execution_id,
        RESULT_ENV_TASK_ID: task_id,
        RESULT_ENV_ROLE: role,
    }
    if reviewed_feature_sha:
        payload[RESULT_ENV_REVIEWED_SHA] = reviewed_feature_sha
    return payload


def sanitize_result_mapping(data: Any) -> Any:
    """Drop secrets and hidden reasoning from untrusted result payloads."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            lowered = str(key).strip().lower().replace(" ", "_")
            if lowered in _HIDDEN_REASONING_KEYS:
                continue
            if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
                continue
            cleaned[key] = sanitize_result_mapping(value)
        return cleaned
    if isinstance(data, list):
        return [sanitize_result_mapping(item) for item in data]
    return data


def load_result_file(path: str | Path) -> dict[str, Any]:
    result_path = Path(path)
    if not result_path.is_file():
        raise ExecutorResultError(f"executor result file missing: {result_path}")
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutorResultError(f"malformed executor result file: {result_path}") from exc
    if not isinstance(raw, dict):
        raise ExecutorResultError("executor result must be a JSON object")
    return raw


def parse_executor_result(
    raw: dict[str, Any] | None,
    *,
    execution_id: str,
    task_id: str,
    role: str,
    reviewed_feature_sha: str | None = None,
    require_identity: bool = True,
) -> ExecutorResult:
    if not isinstance(raw, dict):
        raise ExecutorResultError("executor result is missing or not an object")
    data = sanitize_result_mapping(raw)
    if not isinstance(data, dict):
        raise ExecutorResultError("executor result is missing or not an object")

    schema_version = data.get("schema_version", RESULT_SCHEMA_VERSION if not require_identity else None)
    if schema_version != RESULT_SCHEMA_VERSION:
        raise ExecutorResultError(
            f"unsupported executor result schema_version: {schema_version!r}"
        )

    reported_execution_id = str(data.get("execution_id") or execution_id)
    reported_task_id = str(data.get("task_id") or task_id)
    reported_role = str(data.get("role") or role).upper()
    if require_identity:
        _require_identity(
            data,
            execution_id=execution_id,
            task_id=task_id,
            role=role,
        )
        reported_execution_id = str(data["execution_id"])
        reported_task_id = str(data["task_id"])
        reported_role = str(data["role"]).upper()
    elif reported_role != role.upper():
        raise ExecutorResultError(
            f"executor result role mismatch: expected {role}, got {reported_role}"
        )

    status = validated_result_status(data.get("status"), default="SUCCEEDED")

    completed_at = data.get("completed_at")
    if completed_at is not None:
        completed_at = str(completed_at)

    builder = None
    reviewer = None
    integrator = None
    plan = None
    if reported_role in BUILDER_ROLES:
        builder = _parse_builder(data)
    elif reported_role in REVIEWER_ROLES:
        reviewer = _parse_reviewer(data, captured_sha=reviewed_feature_sha)
    elif reported_role in INTEGRATOR_ROLES:
        integrator = _parse_integrator(data)
    elif reported_role in PLANNER_ROLES:
        if status == "SUCCEEDED":
            plan = _parse_planner(data)
    else:
        raise ExecutorResultError(f"unknown executor role: {reported_role}")

    human_escalation_type = _parse_human_escalation_type(data.get("human_escalation_type"))
    objective_signal = _parse_objective_signal(data.get("objective_signal"))
    persisted = _persisted_payload(
        schema_version=RESULT_SCHEMA_VERSION,
        execution_id=reported_execution_id,
        task_id=reported_task_id,
        role=reported_role,
        status=status,
        completed_at=completed_at,
        builder=builder,
        reviewer=reviewer,
        integrator=integrator,
        plan=plan,
        human_escalation_type=human_escalation_type,
        objective_signal=objective_signal,
    )
    for key in (
        "provider_failure",
        "failure_kind",
        "detail",
        "diagnostics",
        "return_code",
        "stdout_tail",
        "stderr_tail",
        "error",
    ):
        if key in data:
            persisted[key] = sanitize_result_mapping(data[key])
    return ExecutorResult(
        schema_version=RESULT_SCHEMA_VERSION,
        execution_id=reported_execution_id,
        task_id=reported_task_id,
        role=reported_role,
        status=status,
        completed_at=completed_at,
        builder=builder,
        reviewer=reviewer,
        integrator=integrator,
        plan=plan,
        human_escalation_type=human_escalation_type,
        objective_signal=objective_signal,
        persisted=persisted,
    )


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def validated_result_status(value: Any, *, default: str = "SUCCEEDED") -> str:
    """Validate untrusted executor status. Aliases are never mapped."""
    if value is None or value == "":
        status = str(default).strip().upper()
    else:
        status = str(value).strip().upper()
    if status not in RESULT_STATUSES:
        raise ExecutorResultError(f"invalid executor result status: {status}")
    return status


def _parse_human_escalation_type(value: Any) -> str | None:
    if value is None or value == "":
        return None
    escalation = str(value).strip().upper()
    from build_coordinator.runner.models import HUMAN_ESCALATION_TYPES

    if escalation not in HUMAN_ESCALATION_TYPES:
        raise ExecutorResultError(f"invalid human_escalation_type: {escalation}")
    return escalation


def _parse_objective_signal(value: Any) -> dict[str, Any] | None:
    """Optional, fail-closed structured block any role's result may carry
    for the objective controller (follow-up/unrelated findings, a typed
    human gate, objective progress, integration eligibility). Available
    alongside the existing role-specific contract, not instead of it --
    reusing the exact same "unknown enum value fails closed" discipline via
    `StructuredTaskResult`."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ExecutorResultError("objective_signal must be an object")
    from build_coordinator.types import StructuredContractError, StructuredTaskResult

    try:
        structured = StructuredTaskResult.from_mapping(value)
    except StructuredContractError as exc:
        raise ExecutorResultError(f"invalid objective_signal: {exc}") from exc
    return structured.to_mapping()


def result_status_instructions() -> str:
    allowed = "\n".join(RESULT_STATUS_VALUES)
    forbidden = "\n".join(FORBIDDEN_RESULT_STATUS_ALIASES)
    return (
        "RESULT STATUS:\n\n"
        "status MUST be exactly one of:\n\n"
        f"{allowed}\n\n"
        "Do NOT emit:\n\n"
        f"{forbidden}\n"
    )


def result_file_contract_for_role(role: str) -> dict[str, Any]:
    """Role-specific result contract derived from the same typed constants used by validation."""
    from build_coordinator.runner.models import review_verdict_contract

    instructions = result_status_instructions()
    contract: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "write_json_to_env": RESULT_ENV_PATH,
        "identity_env": [
            RESULT_ENV_EXECUTION_ID,
            RESULT_ENV_TASK_ID,
            RESULT_ENV_ROLE,
        ],
        "do_not_persist": ["secrets", "api_keys", "hidden_reasoning"],
        "status": {
            "must_be_exactly_one_of": list(RESULT_STATUS_VALUES),
            "do_not_emit": list(FORBIDDEN_RESULT_STATUS_ALIASES),
        },
        "instructions": instructions,
    }
    if str(role or "").upper() in REVIEWER_ROLES:
        review_contract = review_verdict_contract()
        contract["verdict"] = review_contract
        contract["instructions"] = instructions + "\n" + review_contract["instructions"]
    if str(role or "").upper() in PLANNER_ROLES:
        from build_coordinator.types import OBJECTIVE_GATE_TYPES

        contract["required_top_level_fields"] = [
            "schema_version",
            "execution_id",
            "task_id",
            "role",
            "status",
            "plan",
        ]
        contract["plan"] = {
            "required": True,
            "fields": {
                "tasks": "list of child tasks (task_id, title, goal, description, scope, prohibited_scope, dependencies, parallel_safe, risk_level, reason_created)",
                "requested_human_gates": (
                    "optional array of strings; each must be exactly one of: "
                    + ", ".join(OBJECTIVE_GATE_TYPES)
                    + ". Requesting a gate does not authorize the action. Do not emit objects."
                ),
            },
            "forbidden": [
                "worktree",
                "worktree_path",
                "worker_id",
                "branch_name",
                "auto_push",
                "auto_push_allowed",
                "main_push_policy",
                "chain_of_thought",
            ],
            "instructions": (
                "Write the full executor-result envelope, not a bare plan. "
                "The top-level JSON MUST contain schema_version, execution_id, "
                "task_id, role, status, and plan; put the structured ObjectivePlan "
                "inside plan. Do not choose worktrees, authorize remote main push, "
                "weaken review policy, or persist hidden reasoning."
            ),
        }
    return contract


def _require_identity(
    data: dict[str, Any],
    *,
    execution_id: str,
    task_id: str,
    role: str,
) -> None:
    missing = [key for key in ("execution_id", "task_id", "role") if not data.get(key)]
    if missing:
        raise ExecutorResultError(f"executor result missing identity fields: {missing}")
    if str(data["execution_id"]) != execution_id:
        raise ExecutorResultError("executor result execution_id mismatch")
    if str(data["task_id"]) != task_id:
        raise ExecutorResultError("executor result task_id mismatch")
    if str(data["role"]).upper() != role.upper():
        raise ExecutorResultError("executor result role mismatch")


def _parse_planner(data: dict[str, Any]) -> dict[str, Any]:
    from build_coordinator.planner import parse_planner_plan
    from build_coordinator.types import StructuredContractError

    payload = data.get("plan")
    if payload is None:
        raise ExecutorResultError("planner result missing plan")
    try:
        plan = parse_planner_plan(payload, source="PLANNER")
    except StructuredContractError as exc:
        raise ExecutorResultError(f"invalid planner plan: {exc}") from exc
    return plan.to_mapping()


def _parse_builder(data: dict[str, Any]) -> BuilderResult:
    tests = _parse_tests(data.get("tests") or data.get("results"))
    return BuilderResult(
        feature_sha=_optional_str(data.get("feature_sha")),
        files_changed=_string_tuple(data.get("files_changed")),
        tests=tests,
        scope_expansion_required=bool(data.get("scope_expansion_required", False)),
        blockers=_string_tuple(data.get("blockers")),
        commits_created=_string_tuple(data.get("commits_created")),
    )


def _parse_reviewer(data: dict[str, Any], *, captured_sha: str | None) -> ReviewerResult:
    from build_coordinator.runner.models import ReviewVerdict, ReviewVerdictContradiction

    payload = data.get("review") if isinstance(data.get("review"), dict) else data
    try:
        verdict = ReviewVerdict.from_mapping(payload)
        verdict.validate_consistency()
    except ReviewVerdictContradiction as exc:
        raise ExecutorResultError(str(exc)) from exc
    reported_sha = _optional_str(
        payload.get("reviewed_feature_sha") or data.get("reviewed_feature_sha")
    )
    if captured_sha and reported_sha and reported_sha != captured_sha:
        raise ExecutorResultError(
            "reviewer result reviewed_feature_sha does not match runner-captured SHA"
        )
    return ReviewerResult(
        reviewed_feature_sha=reported_sha or captured_sha,
        verdict=verdict,
    )


def _parse_integrator(data: dict[str, Any]) -> IntegratorResult:
    tests = _parse_tests(data.get("tests") or data.get("results"))
    return IntegratorResult(
        feature_sha=_optional_str(data.get("feature_sha")),
        reviewed_feature_sha=_optional_str(data.get("reviewed_feature_sha")),
        current_main_sha=_optional_str(data.get("current_main_sha")),
        merge_base=_optional_str(data.get("merge_base")),
        merge_commit_sha=_optional_str(data.get("merge_commit_sha")),
        tests=tests,
        push_status=_optional_str(data.get("push_status")),
        final_main_sha=_optional_str(data.get("final_main_sha")),
    )


def _parse_tests(value: Any) -> TestsSummary:
    if value is None:
        return TestsSummary()
    if isinstance(value, list):
        items = tuple(str(item) for item in value)
        return TestsSummary(items=items, raw=items)
    if isinstance(value, dict):
        items = value.get("items") or value.get("names") or value.get("summary") or []
        if isinstance(items, list):
            item_tuple = tuple(str(item) for item in items)
        else:
            item_tuple = (str(items),) if items else ()
        passed = value.get("passed")
        failed = value.get("failed")
        return TestsSummary(
            items=item_tuple,
            passed=int(passed) if passed is not None else None,
            failed=int(failed) if failed is not None else None,
            raw=item_tuple,
        )
    return TestsSummary(items=(str(value),), raw=(str(value),))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _persisted_payload(
    *,
    schema_version: int,
    execution_id: str,
    task_id: str,
    role: str,
    status: str,
    completed_at: str | None,
    builder: BuilderResult | None,
    reviewer: ReviewerResult | None,
    integrator: IntegratorResult | None,
    plan: dict[str, Any] | None = None,
    human_escalation_type: Any = None,
    objective_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "execution_id": execution_id,
        "task_id": task_id,
        "role": role,
        "status": status,
        "completed_at": completed_at,
    }
    if human_escalation_type:
        payload["human_escalation_type"] = str(human_escalation_type)
    if objective_signal is not None:
        payload["objective_signal"] = objective_signal
    if plan is not None:
        payload["plan"] = plan
    if builder is not None:
        payload.update(
            {
                "feature_sha": builder.feature_sha,
                "files_changed": list(builder.files_changed),
                "tests": list(builder.tests.items),
                "scope_expansion_required": builder.scope_expansion_required,
                "blockers": list(builder.blockers),
                "commits_created": list(builder.commits_created),
            }
        )
    if reviewer is not None:
        payload.update(
            {
                "reviewed_feature_sha": reviewer.reviewed_feature_sha,
                "verdict": reviewer.verdict.verdict,
                "findings": list(reviewer.verdict.findings),
                "required_remediation": list(reviewer.verdict.required_remediation),
                "architecture_notes": list(reviewer.verdict.architecture_notes),
                "ready_for_integration": reviewer.verdict.ready_for_integration,
                "review": reviewer.verdict.to_mapping(),
            }
        )
    if integrator is not None:
        payload.update(
            {
                "feature_sha": integrator.feature_sha,
                "reviewed_feature_sha": integrator.reviewed_feature_sha,
                "current_main_sha": integrator.current_main_sha,
                "merge_base": integrator.merge_base,
                "merge_commit_sha": integrator.merge_commit_sha,
                "tests": list(integrator.tests.items),
                "push_status": integrator.push_status,
                "final_main_sha": integrator.final_main_sha,
            }
        )
    return sanitize_result_mapping(payload)
