"""Bounded, provider-neutral governance for objective plans and outcomes.

These artifacts are policy evidence.  They do not prove that a provider ran, that
an objective was executed, or that an evaluator is substantively correct.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .persistence import Store
from .security import SecurityBoundaryError, WorkspaceBoundary

OBJECTIVE_GOVERNANCE_VERSION = 1
MAX_ITEMS = 100
MAX_TEXT_CHARS = 1_000
MAX_CANONICAL_CHARS = 64_000
ARTIFACT_ROOT = ".stagemesh/objectives"

PLAN_RECORDED = "objective.plan_recorded"
CHALLENGE_RECORDED = "objective.challenge_recorded"
EVALUATION_RECORDED = "objective.evaluation_recorded"

CHALLENGE_VERDICTS = ("accept", "revise", "needs-human-decision")
CHALLENGE_FINDING_KINDS = (
    "cheaper-alternative",
    "dependency-risk",
    "falsification-gap",
    "human-decision",
    "missing-criterion",
    "objective-drift",
    "scope-risk",
    "unsupported-assumption",
)
RESPONSE_DISPOSITIONS = ("adopted", "rejected-with-rationale")
CRITERION_STATUSES = ("PASS", "FAIL", "UNKNOWN")
EVALUATION_VERDICTS = ("ACHIEVED", "NOT_ACHIEVED", "INCONCLUSIVE")

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_GIT_OBJECT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class ObjectiveGovernanceError(ValueError):
    def __init__(self, problems: str | tuple[str, ...] | list[str]) -> None:
        if isinstance(problems, str):
            problems = (problems,)
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class SuccessCriterion:
    id: str
    description: str


@dataclass(frozen=True)
class ObjectiveTask:
    id: str
    title: str
    dependencies: tuple[str, ...]
    criterion_ids: tuple[str, ...]


@dataclass(frozen=True)
class FindingResponse:
    finding_id: str
    disposition: str
    rationale: str


@dataclass(frozen=True)
class ObjectivePlan:
    objective_id: str
    revision: int
    objective: str
    success_criteria: tuple[SuccessCriterion, ...]
    tasks: tuple[ObjectiveTask, ...]
    non_goals: tuple[str, ...]
    previous_plan_hash: str | None
    responds_to_challenge_hash: str | None
    change_reason: str | None
    finding_responses: tuple[FindingResponse, ...]


@dataclass(frozen=True)
class FrozenObjectivePlan:
    plan: ObjectivePlan
    digest: str
    canonical_json: str


@dataclass(frozen=True)
class ChallengeFinding:
    id: str
    kind: str
    summary: str
    criterion_id: str | None


@dataclass(frozen=True)
class ObjectiveChallenge:
    objective_id: str
    plan_hash: str
    plan_revision: int
    planner_identity: str
    challenger_identity: str
    verdict: str
    findings: tuple[ChallengeFinding, ...]
    required_decisions: tuple[str, ...]


@dataclass(frozen=True)
class FrozenObjectiveChallenge:
    challenge: ObjectiveChallenge
    digest: str
    canonical_json: str


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    status: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ObjectiveEvaluation:
    objective_id: str
    plan_hash: str
    plan_revision: int
    accepted_challenge_hash: str
    candidate_sha: str
    evaluator_identity: str
    implementation_identities: tuple[str, ...]
    runner_identity: str
    criteria: tuple[CriterionResult, ...]
    negative_findings: tuple[str, ...]
    verdict: str


@dataclass(frozen=True)
class FrozenObjectiveEvaluation:
    evaluation: ObjectiveEvaluation
    digest: str
    canonical_json: str


FrozenArtifact = FrozenObjectivePlan | FrozenObjectiveChallenge | FrozenObjectiveEvaluation


def validate_objective_plan(payload: Any) -> tuple[ObjectivePlan | None, tuple[str, ...]]:
    if not isinstance(payload, dict):
        return None, ("objective plan must be a JSON object",)
    allowed = {
        "version",
        "objective_id",
        "revision",
        "objective",
        "success_criteria",
        "tasks",
        "non_goals",
        "previous_plan_hash",
        "responds_to_challenge_hash",
        "change_reason",
        "finding_responses",
    }
    problems = _unknown_fields(payload, allowed, "objective plan")
    if payload.get("version") != OBJECTIVE_GOVERNANCE_VERSION:
        problems.append(f"objective plan version must be {OBJECTIVE_GOVERNANCE_VERSION}")
    objective_id = _identifier(payload.get("objective_id"), "objective_id", problems)
    revision = _positive_int(payload.get("revision"), "revision", problems)
    objective = _bounded_text(payload.get("objective"), "objective", problems)
    criteria = _criteria(payload.get("success_criteria"), problems)
    tasks = _tasks(payload.get("tasks"), criteria, problems)
    non_goals = _text_list(payload.get("non_goals", []), "non_goals", problems)
    previous_plan_hash = _optional_digest(
        payload.get("previous_plan_hash"), "previous_plan_hash", problems
    )
    challenge_hash = _optional_digest(
        payload.get("responds_to_challenge_hash"),
        "responds_to_challenge_hash",
        problems,
    )
    change_reason = _optional_bounded_text(payload.get("change_reason"), "change_reason", problems)
    responses = _finding_responses(payload.get("finding_responses", []), problems)
    if revision == 1:
        if any(value is not None for value in (previous_plan_hash, challenge_hash, change_reason)):
            problems.append("revision 1 cannot name a predecessor, challenge, or change reason")
        if responses:
            problems.append("revision 1 cannot contain finding responses")
    elif revision > 1:
        if previous_plan_hash is None:
            problems.append("later revisions require previous_plan_hash")
        if challenge_hash is None:
            problems.append("later revisions require responds_to_challenge_hash")
        if change_reason is None:
            problems.append("later revisions require change_reason")
        if not responses:
            problems.append("later revisions require finding_responses")
    if problems:
        return None, tuple(problems)
    plan = ObjectivePlan(
        objective_id,
        revision,
        objective,
        criteria,
        tasks,
        non_goals,
        previous_plan_hash,
        challenge_hash,
        change_reason,
        responses,
    )
    canonical = _plan_json(plan)
    if len(canonical) > MAX_CANONICAL_CHARS:
        return None, (f"objective plan exceeds {MAX_CANONICAL_CHARS} canonical characters",)
    return plan, ()


def freeze_objective_plan(payload: Any) -> FrozenObjectivePlan:
    plan, problems = validate_objective_plan(payload)
    if plan is None:
        raise ObjectiveGovernanceError(problems)
    canonical = _plan_json(plan)
    return FrozenObjectivePlan(plan, _hash(canonical), canonical)


def validate_objective_challenge(
    payload: Any, plan: FrozenObjectivePlan
) -> tuple[ObjectiveChallenge | None, tuple[str, ...]]:
    if not isinstance(payload, dict):
        return None, ("objective challenge must be a JSON object",)
    allowed = {
        "version",
        "objective_id",
        "plan_hash",
        "plan_revision",
        "planner_identity",
        "challenger_identity",
        "verdict",
        "findings",
        "required_decisions",
    }
    problems = _unknown_fields(payload, allowed, "objective challenge")
    if payload.get("version") != OBJECTIVE_GOVERNANCE_VERSION:
        problems.append(f"objective challenge version must be {OBJECTIVE_GOVERNANCE_VERSION}")
    objective_id = _identifier(payload.get("objective_id"), "objective_id", problems)
    plan_hash = _required_digest(payload.get("plan_hash"), "plan_hash", problems)
    plan_revision = _positive_int(payload.get("plan_revision"), "plan_revision", problems)
    planner = _identity_text(payload.get("planner_identity"), "planner_identity", problems)
    challenger = _identity_text(payload.get("challenger_identity"), "challenger_identity", problems)
    if planner and challenger and planner.casefold() == challenger.casefold():
        problems.append("planner and challenger identities must be different")
    verdict = payload.get("verdict")
    if verdict not in CHALLENGE_VERDICTS:
        problems.append(f"challenge verdict must be one of {', '.join(CHALLENGE_VERDICTS)}")
        verdict = ""
    findings = _challenge_findings(
        payload.get("findings", []),
        {criterion.id for criterion in plan.plan.success_criteria},
        problems,
    )
    required_decisions = _text_list(
        payload.get("required_decisions", []), "required_decisions", problems
    )
    if objective_id and objective_id != plan.plan.objective_id:
        problems.append("challenge objective_id does not match the frozen plan")
    if plan_hash and plan_hash != plan.digest:
        problems.append("challenge plan_hash does not match the frozen plan")
    if plan_revision and plan_revision != plan.plan.revision:
        problems.append("challenge plan_revision does not match the frozen plan")
    if verdict == "revise" and not findings:
        problems.append("revise challenge verdict requires at least one finding")
    if verdict == "needs-human-decision" and not required_decisions:
        problems.append("needs-human-decision requires required_decisions")
    if verdict == "accept" and required_decisions:
        problems.append("accept challenge cannot retain required decisions")
    if problems:
        return None, tuple(problems)
    challenge = ObjectiveChallenge(
        objective_id,
        plan_hash,
        plan_revision,
        planner,
        challenger,
        str(verdict),
        findings,
        required_decisions,
    )
    canonical = _challenge_json(challenge)
    if len(canonical) > MAX_CANONICAL_CHARS:
        return None, (f"objective challenge exceeds {MAX_CANONICAL_CHARS} canonical characters",)
    return challenge, ()


def freeze_objective_challenge(payload: Any, plan: FrozenObjectivePlan) -> FrozenObjectiveChallenge:
    challenge, problems = validate_objective_challenge(payload, plan)
    if challenge is None:
        raise ObjectiveGovernanceError(problems)
    canonical = _challenge_json(challenge)
    return FrozenObjectiveChallenge(challenge, _hash(canonical), canonical)


def validate_plan_transition(
    previous: FrozenObjectivePlan,
    challenge: FrozenObjectiveChallenge,
    revised: FrozenObjectivePlan,
) -> tuple[str, ...]:
    problems: list[str] = []
    if challenge.challenge.plan_hash != previous.digest:
        problems.append("revision challenge is not bound to the previous plan")
    if challenge.challenge.verdict != "revise":
        problems.append("only a revise challenge can authorize a plan revision")
    if revised.plan.objective_id != previous.plan.objective_id:
        problems.append("objective_id cannot change across revisions")
    if revised.plan.objective != previous.plan.objective:
        problems.append("the original objective cannot change across revisions")
    if revised.plan.revision != previous.plan.revision + 1:
        problems.append("plan revision must increment by exactly one")
    if revised.plan.previous_plan_hash != previous.digest:
        problems.append("revised plan does not bind the previous plan digest")
    if revised.plan.responds_to_challenge_hash != challenge.digest:
        problems.append("revised plan does not bind the challenge digest")
    previous_criteria = {item.id: item.description for item in previous.plan.success_criteria}
    revised_criteria = {item.id: item.description for item in revised.plan.success_criteria}
    for criterion_id, description in previous_criteria.items():
        if revised_criteria.get(criterion_id) != description:
            problems.append(f"original success criterion changed or disappeared: {criterion_id}")
    finding_ids = {item.id for item in challenge.challenge.findings}
    response_ids = {item.finding_id for item in revised.plan.finding_responses}
    for missing in sorted(finding_ids - response_ids):
        problems.append(f"challenge finding has no revision response: {missing}")
    for unknown in sorted(response_ids - finding_ids):
        problems.append(f"revision responds to an unknown challenge finding: {unknown}")
    return tuple(problems)


def ensure_plan_transition(
    previous: FrozenObjectivePlan,
    challenge: FrozenObjectiveChallenge,
    revised: FrozenObjectivePlan,
) -> None:
    problems = validate_plan_transition(previous, challenge, revised)
    if problems:
        raise ObjectiveGovernanceError(problems)


def validate_objective_evaluation(
    payload: Any,
    plan: FrozenObjectivePlan,
    accepted_challenge: FrozenObjectiveChallenge,
) -> tuple[ObjectiveEvaluation | None, tuple[str, ...]]:
    if not isinstance(payload, dict):
        return None, ("objective evaluation must be a JSON object",)
    allowed = {
        "version",
        "objective_id",
        "plan_hash",
        "plan_revision",
        "accepted_challenge_hash",
        "candidate_sha",
        "evaluator_identity",
        "implementation_identities",
        "runner_identity",
        "criteria",
        "negative_findings",
    }
    problems = _unknown_fields(payload, allowed, "objective evaluation")
    if payload.get("version") != OBJECTIVE_GOVERNANCE_VERSION:
        problems.append(f"objective evaluation version must be {OBJECTIVE_GOVERNANCE_VERSION}")
    objective_id = _identifier(payload.get("objective_id"), "objective_id", problems)
    plan_hash = _required_digest(payload.get("plan_hash"), "plan_hash", problems)
    plan_revision = _positive_int(payload.get("plan_revision"), "plan_revision", problems)
    accepted_challenge_hash = _required_digest(
        payload.get("accepted_challenge_hash"),
        "accepted_challenge_hash",
        problems,
    )
    candidate_sha = _required_git_sha(payload.get("candidate_sha"), "candidate_sha", problems)
    evaluator = _identity_text(payload.get("evaluator_identity"), "evaluator_identity", problems)
    implementers = _identity_list(
        payload.get("implementation_identities"),
        "implementation_identities",
        problems,
        required=True,
    )
    if evaluator and evaluator.casefold() in {item.casefold() for item in implementers}:
        problems.append("evaluator identity must differ from implementation identities")
    runner = _identity_text(payload.get("runner_identity"), "runner_identity", problems)
    criteria = _criterion_results(payload.get("criteria"), plan, problems)
    negative_findings = _text_list(
        payload.get("negative_findings", []), "negative_findings", problems
    )
    if objective_id and objective_id != plan.plan.objective_id:
        problems.append("evaluation objective_id does not match the frozen plan")
    if plan_hash and plan_hash != plan.digest:
        problems.append("evaluation plan_hash does not match the frozen plan")
    if plan_revision and plan_revision != plan.plan.revision:
        problems.append("evaluation plan_revision does not match the frozen plan")
    if accepted_challenge.challenge.plan_hash != plan.digest:
        problems.append("accepted challenge does not bind the evaluated plan")
    if accepted_challenge.challenge.verdict != "accept":
        problems.append("objective evaluation requires an accepted plan challenge")
    if accepted_challenge_hash and accepted_challenge_hash != accepted_challenge.digest:
        problems.append("evaluation accepted_challenge_hash does not match the accepted challenge")
    if problems:
        return None, tuple(problems)
    statuses = {item.status for item in criteria}
    if "FAIL" in statuses:
        verdict = "NOT_ACHIEVED"
    elif "UNKNOWN" in statuses:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "ACHIEVED"
    evaluation = ObjectiveEvaluation(
        objective_id,
        plan_hash,
        plan_revision,
        accepted_challenge_hash,
        candidate_sha,
        evaluator,
        implementers,
        runner,
        criteria,
        negative_findings,
        verdict,
    )
    canonical = _evaluation_json(evaluation)
    if len(canonical) > MAX_CANONICAL_CHARS:
        return None, (f"objective evaluation exceeds {MAX_CANONICAL_CHARS} canonical characters",)
    return evaluation, ()


def freeze_objective_evaluation(
    payload: Any,
    plan: FrozenObjectivePlan,
    accepted_challenge: FrozenObjectiveChallenge,
) -> FrozenObjectiveEvaluation:
    evaluation, problems = validate_objective_evaluation(payload, plan, accepted_challenge)
    if evaluation is None:
        raise ObjectiveGovernanceError(problems)
    canonical = _evaluation_json(evaluation)
    return FrozenObjectiveEvaluation(evaluation, _hash(canonical), canonical)


def save_objective_plan(
    store: Store,
    project: Path,
    payload: Any,
    *,
    previous: FrozenObjectivePlan | None = None,
    challenge: FrozenObjectiveChallenge | None = None,
) -> tuple[FrozenObjectivePlan, Path]:
    frozen = freeze_objective_plan(payload)
    if frozen.plan.revision == 1:
        if previous is not None or challenge is not None:
            raise ObjectiveGovernanceError("revision 1 cannot be saved with lineage")
    else:
        if previous is None or challenge is None:
            raise ObjectiveGovernanceError(
                "later revisions require the frozen previous plan and challenge"
            )
        ensure_plan_transition(previous, challenge, frozen)
        _require_persisted(project, previous)
        _require_persisted(project, challenge)
    path = _artifact_path(project, frozen)
    _refuse_duplicate_revision(path, frozen.plan.revision)
    _persist(store, project, frozen, path, PLAN_RECORDED, "plan")
    return frozen, path


def save_objective_challenge(
    store: Store, project: Path, payload: Any, plan: FrozenObjectivePlan
) -> tuple[FrozenObjectiveChallenge, Path]:
    frozen = freeze_objective_challenge(payload, plan)
    _require_persisted(project, plan)
    path = _artifact_path(project, frozen)
    _persist(store, project, frozen, path, CHALLENGE_RECORDED, "challenge")
    return frozen, path


def save_objective_evaluation(
    store: Store,
    project: Path,
    payload: Any,
    plan: FrozenObjectivePlan,
    accepted_challenge: FrozenObjectiveChallenge,
) -> tuple[FrozenObjectiveEvaluation, Path]:
    frozen = freeze_objective_evaluation(payload, plan, accepted_challenge)
    _require_persisted(project, plan)
    _require_persisted(project, accepted_challenge)
    path = _artifact_path(project, frozen)
    _persist(store, project, frozen, path, EVALUATION_RECORDED, "evaluation")
    return frozen, path


def load_objective_plan(path: Path) -> FrozenObjectivePlan:
    frozen = freeze_objective_plan(_read_payload(path))
    _verify_filename(path, frozen.digest)
    return frozen


def load_objective_challenge(path: Path, plan: FrozenObjectivePlan) -> FrozenObjectiveChallenge:
    frozen = freeze_objective_challenge(_read_payload(path), plan)
    _verify_filename(path, frozen.digest)
    return frozen


def load_objective_evaluation(
    path: Path,
    plan: FrozenObjectivePlan,
    accepted_challenge: FrozenObjectiveChallenge,
) -> FrozenObjectiveEvaluation:
    payload = _read_payload(path)
    if not isinstance(payload, dict):
        raise ObjectiveGovernanceError("objective evaluation must be a JSON object")
    recorded_verdict = payload.pop("verdict", None)
    frozen = freeze_objective_evaluation(payload, plan, accepted_challenge)
    if recorded_verdict != frozen.evaluation.verdict:
        raise ObjectiveGovernanceError(
            "objective evaluation recorded verdict does not match its criterion results"
        )
    _verify_filename(path, frozen.digest)
    return frozen


def load_objective_lineage(project: Path, objective_id: str) -> tuple[FrozenObjectivePlan, ...]:
    problems: list[str] = []
    checked_id = _identifier(objective_id, "objective_id", problems)
    if problems:
        raise ObjectiveGovernanceError(problems)
    root = _objective_root(project, checked_id)
    _require_safe_path(project, root)
    plans_dir = root / "plans"
    if not plans_dir.exists():
        return ()
    _require_safe_path(project, plans_dir)
    paths = sorted(plans_dir.glob("*.json"))
    plans = tuple(load_objective_plan(path) for path in paths)
    for expected, plan in enumerate(plans, start=1):
        if plan.plan.objective_id != checked_id:
            raise ObjectiveGovernanceError("objective lineage contains another objective")
        if plan.plan.revision != expected:
            raise ObjectiveGovernanceError("objective plan revisions are not contiguous")
        if expected == 1:
            continue
        previous = plans[expected - 2]
        challenge_path = (
            root
            / "challenges"
            / _artifact_filename(previous.plan.revision, str(plan.plan.responds_to_challenge_hash))
        )
        _require_safe_path(project, challenge_path)
        if not challenge_path.is_file():
            raise ObjectiveGovernanceError(
                f"objective revision {expected} is missing its bound challenge artifact"
            )
        challenge = load_objective_challenge(challenge_path, previous)
        ensure_plan_transition(previous, challenge, plan)
    return plans


def _criteria(value: Any, problems: list[str]) -> tuple[SuccessCriterion, ...]:
    if not isinstance(value, list) or not value:
        problems.append("success_criteria must be a non-empty list")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"success_criteria exceeds {MAX_ITEMS} entries")
    result: list[SuccessCriterion] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "description"}:
            problems.append("success criteria require only id and description")
            continue
        criterion_id = _identifier(item.get("id"), "criterion id", problems)
        description = _bounded_text(item.get("description"), "criterion description", problems)
        if criterion_id and description:
            result.append(SuccessCriterion(criterion_id, description))
    _duplicates((item.id for item in result), "success criterion id", problems)
    return tuple(result)


def _tasks(
    value: Any, criteria: tuple[SuccessCriterion, ...], problems: list[str]
) -> tuple[ObjectiveTask, ...]:
    if not isinstance(value, list) or not value:
        problems.append("tasks must be a non-empty list")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"tasks exceeds {MAX_ITEMS} entries")
    criterion_ids = {item.id for item in criteria}
    result: list[ObjectiveTask] = []
    for item in value:
        expected = {"id", "title", "dependencies", "criterion_ids"}
        if not isinstance(item, dict) or set(item) != expected:
            problems.append("task entries require only id, title, dependencies, and criterion_ids")
            continue
        task_id = _identifier(item.get("id"), "task id", problems)
        title = _bounded_text(item.get("title"), "task title", problems)
        dependencies = _identifier_list(item.get("dependencies"), "task dependencies", problems)
        linked = _identifier_list(
            item.get("criterion_ids"),
            "task criterion_ids",
            problems,
            required=True,
        )
        for criterion_id in linked:
            if criterion_id not in criterion_ids:
                problems.append(f"task references unknown criterion: {criterion_id}")
        if task_id and title:
            result.append(ObjectiveTask(task_id, title, dependencies, linked))
    _duplicates((item.id for item in result), "task id", problems)
    task_ids = {item.id for item in result}
    for task in result:
        for dependency in task.dependencies:
            if dependency not in task_ids:
                problems.append(f"task references unknown dependency: {dependency}")
            if dependency == task.id:
                problems.append(f"task cannot depend on itself: {task.id}")
    if _has_cycle(result):
        problems.append("task dependencies must form an acyclic graph")
    linked_criteria = {criterion for task in result for criterion in task.criterion_ids}
    for criterion_id in sorted(criterion_ids - linked_criteria):
        problems.append(f"success criterion is not linked to a task: {criterion_id}")
    return tuple(result)


def _finding_responses(value: Any, problems: list[str]) -> tuple[FindingResponse, ...]:
    if not isinstance(value, list):
        problems.append("finding_responses must be a list")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"finding_responses exceeds {MAX_ITEMS} entries")
    result: list[FindingResponse] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "finding_id",
            "disposition",
            "rationale",
        }:
            problems.append("finding responses require only finding_id, disposition, and rationale")
            continue
        finding_id = _identifier(item.get("finding_id"), "finding_id", problems)
        disposition = item.get("disposition")
        if disposition not in RESPONSE_DISPOSITIONS:
            problems.append(
                f"finding response disposition must be one of {', '.join(RESPONSE_DISPOSITIONS)}"
            )
            disposition = ""
        rationale = _bounded_text(item.get("rationale"), "finding response rationale", problems)
        if finding_id and disposition and rationale:
            result.append(FindingResponse(finding_id, str(disposition), rationale))
    _duplicates((item.finding_id for item in result), "finding response id", problems)
    return tuple(result)


def _challenge_findings(
    value: Any, criterion_ids: set[str], problems: list[str]
) -> tuple[ChallengeFinding, ...]:
    if not isinstance(value, list):
        problems.append("findings must be a list")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"findings exceeds {MAX_ITEMS} entries")
    result: list[ChallengeFinding] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "kind",
            "summary",
            "criterion_id",
        }:
            problems.append("findings require only id, kind, summary, and criterion_id")
            continue
        finding_id = _identifier(item.get("id"), "finding id", problems)
        kind = item.get("kind")
        if kind not in CHALLENGE_FINDING_KINDS:
            problems.append(f"finding kind must be one of {', '.join(CHALLENGE_FINDING_KINDS)}")
            kind = ""
        summary = _bounded_text(item.get("summary"), "finding summary", problems)
        criterion_id = item.get("criterion_id")
        if criterion_id is not None:
            criterion_id = _identifier(criterion_id, "finding criterion_id", problems)
            if criterion_id and criterion_id not in criterion_ids:
                problems.append(f"finding references unknown criterion: {criterion_id}")
        if finding_id and kind and summary:
            result.append(ChallengeFinding(finding_id, str(kind), summary, criterion_id))
    _duplicates((item.id for item in result), "finding id", problems)
    return tuple(result)


def _criterion_results(
    value: Any, plan: FrozenObjectivePlan, problems: list[str]
) -> tuple[CriterionResult, ...]:
    if not isinstance(value, list) or not value:
        problems.append("evaluation criteria must be a non-empty list")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"evaluation criteria exceeds {MAX_ITEMS} entries")
    result: list[CriterionResult] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "criterion_id",
            "status",
            "evidence_refs",
        }:
            problems.append(
                "evaluation criteria require only criterion_id, status, and evidence_refs"
            )
            continue
        criterion_id = _identifier(item.get("criterion_id"), "evaluation criterion_id", problems)
        status = item.get("status")
        if status not in CRITERION_STATUSES:
            problems.append(f"criterion status must be one of {', '.join(CRITERION_STATUSES)}")
            status = ""
        evidence_refs = _text_list(
            item.get("evidence_refs"),
            "criterion evidence_refs",
            problems,
            required=True,
        )
        if criterion_id and status and evidence_refs:
            result.append(CriterionResult(criterion_id, str(status), evidence_refs))
    _duplicates((item.criterion_id for item in result), "evaluation criterion id", problems)
    expected = {item.id for item in plan.plan.success_criteria}
    observed = {item.criterion_id for item in result}
    for missing in sorted(expected - observed):
        problems.append(f"success criterion has no evaluation result: {missing}")
    for extra in sorted(observed - expected):
        problems.append(f"evaluation contains unknown success criterion: {extra}")
    return tuple(result)


def _has_cycle(tasks: list[ObjectiveTask]) -> bool:
    graph = {task.id: task.dependencies for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited or task_id not in graph:
            return False
        visiting.add(task_id)
        if any(visit(dependency) for dependency in graph[task_id]):
            return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    return any(visit(task_id) for task_id in graph)


def _plan_json(plan: ObjectivePlan) -> str:
    return _canonical(
        {
            "version": OBJECTIVE_GOVERNANCE_VERSION,
            "objective_id": plan.objective_id,
            "revision": plan.revision,
            "objective": plan.objective,
            "success_criteria": [
                {"id": item.id, "description": item.description} for item in plan.success_criteria
            ],
            "tasks": [
                {
                    "id": item.id,
                    "title": item.title,
                    "dependencies": list(item.dependencies),
                    "criterion_ids": list(item.criterion_ids),
                }
                for item in plan.tasks
            ],
            "non_goals": list(plan.non_goals),
            "previous_plan_hash": plan.previous_plan_hash,
            "responds_to_challenge_hash": plan.responds_to_challenge_hash,
            "change_reason": plan.change_reason,
            "finding_responses": [
                {
                    "finding_id": item.finding_id,
                    "disposition": item.disposition,
                    "rationale": item.rationale,
                }
                for item in plan.finding_responses
            ],
        }
    )


def _challenge_json(challenge: ObjectiveChallenge) -> str:
    return _canonical(
        {
            "version": OBJECTIVE_GOVERNANCE_VERSION,
            "objective_id": challenge.objective_id,
            "plan_hash": challenge.plan_hash,
            "plan_revision": challenge.plan_revision,
            "planner_identity": challenge.planner_identity,
            "challenger_identity": challenge.challenger_identity,
            "verdict": challenge.verdict,
            "findings": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "summary": item.summary,
                    "criterion_id": item.criterion_id,
                }
                for item in challenge.findings
            ],
            "required_decisions": list(challenge.required_decisions),
        }
    )


def _evaluation_json(evaluation: ObjectiveEvaluation) -> str:
    return _canonical(
        {
            "version": OBJECTIVE_GOVERNANCE_VERSION,
            "objective_id": evaluation.objective_id,
            "plan_hash": evaluation.plan_hash,
            "plan_revision": evaluation.plan_revision,
            "accepted_challenge_hash": evaluation.accepted_challenge_hash,
            "candidate_sha": evaluation.candidate_sha,
            "evaluator_identity": evaluation.evaluator_identity,
            "implementation_identities": list(evaluation.implementation_identities),
            "runner_identity": evaluation.runner_identity,
            "criteria": [
                {
                    "criterion_id": item.criterion_id,
                    "status": item.status,
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in evaluation.criteria
            ],
            "negative_findings": list(evaluation.negative_findings),
            "verdict": evaluation.verdict,
        }
    )


def _artifact_path(project: Path, artifact: FrozenArtifact) -> Path:
    if isinstance(artifact, FrozenObjectivePlan):
        objective_id = artifact.plan.objective_id
        revision = artifact.plan.revision
        directory = "plans"
    elif isinstance(artifact, FrozenObjectiveChallenge):
        objective_id = artifact.challenge.objective_id
        revision = artifact.challenge.plan_revision
        directory = "challenges"
    else:
        objective_id = artifact.evaluation.objective_id
        revision = artifact.evaluation.plan_revision
        directory = "evaluations"
    return (
        _objective_root(project, objective_id)
        / directory
        / _artifact_filename(revision, artifact.digest)
    )


def _persist(
    store: Store,
    project: Path,
    artifact: FrozenArtifact,
    path: Path,
    event_type: str,
    kind: str,
) -> None:
    _require_safe_path(project, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_safe_path(project, path)
    text = artifact.canonical_json + "\n"
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        created = True
    except FileExistsError:
        if path.is_symlink() or path.read_text(encoding="utf-8") != text:
            raise ObjectiveGovernanceError(
                "immutable objective artifact identity collision or tampering"
            ) from None
    objective_id, revision = _artifact_identity(artifact)
    if created or not _receipt_exists(store, event_type, objective_id, artifact.digest):
        record_audit(
            store,
            event_type,
            {
                "objective_id": objective_id,
                "revision": revision,
                "artifact_kind": kind,
                "artifact_digest": artifact.digest,
                "path": path.relative_to(Path(project).resolve()).as_posix(),
            },
        )


def _require_persisted(project: Path, artifact: FrozenArtifact) -> None:
    path = _artifact_path(project, artifact)
    _require_safe_path(project, path)
    if path.is_symlink() or not path.is_file():
        raise ObjectiveGovernanceError(
            "objective artifact dependency is not persisted in this project"
        )
    if path.read_text(encoding="utf-8") != artifact.canonical_json + "\n":
        raise ObjectiveGovernanceError(
            "persisted objective artifact dependency does not match its frozen identity"
        )


def _artifact_identity(artifact: FrozenArtifact) -> tuple[str, int]:
    if isinstance(artifact, FrozenObjectivePlan):
        return artifact.plan.objective_id, artifact.plan.revision
    if isinstance(artifact, FrozenObjectiveChallenge):
        return artifact.challenge.objective_id, artifact.challenge.plan_revision
    return artifact.evaluation.objective_id, artifact.evaluation.plan_revision


def _receipt_exists(store: Store, event_type: str, objective_id: str, digest: str) -> bool:
    return (
        store.conn.execute(
            "SELECT 1 FROM audit_events WHERE event_type=? "
            "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.objective_id')=? "
            "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.artifact_digest')=? LIMIT 1",
            (event_type, objective_id, digest),
        ).fetchone()
        is not None
    )


def _refuse_duplicate_revision(path: Path, revision: int) -> None:
    if not path.parent.exists():
        return
    for existing in path.parent.glob(f"{revision:04d}-*.json"):
        if existing != path:
            raise ObjectiveGovernanceError(
                f"objective plan revision {revision} already has a different immutable artifact"
            )


def _objective_root(project: Path, objective_id: str) -> Path:
    root = Path(project).resolve()
    directory = (
        f"{objective_id[:32]}-{hashlib.sha256(objective_id.encode('utf-8')).hexdigest()[:12]}"
    )
    candidate = root / ARTIFACT_ROOT / directory
    try:
        WorkspaceBoundary(root).require_inside(candidate)
    except SecurityBoundaryError as exc:
        raise ObjectiveGovernanceError(str(exc)) from exc
    return candidate


def _require_safe_path(project: Path, path: Path) -> None:
    root = Path(project).resolve()
    try:
        WorkspaceBoundary(root).require_inside(path)
    except SecurityBoundaryError as exc:
        raise ObjectiveGovernanceError(str(exc)) from exc
    current = root
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError as exc:
        raise ObjectiveGovernanceError("objective artifact path escapes the project") from exc
    for part in parts:
        current /= part
        if current.is_symlink():
            raise ObjectiveGovernanceError(
                f"objective artifact path traverses a symlink: {current}"
            )


def _read_payload(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ObjectiveGovernanceError("objective artifact is missing or is a symlink")
    if path.stat().st_size > MAX_CANONICAL_CHARS + 1:
        raise ObjectiveGovernanceError("objective artifact exceeds the size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObjectiveGovernanceError("objective artifact is unreadable or malformed") from exc


def _verify_filename(path: Path, digest: str) -> None:
    try:
        revision = int(path.name.split("-", 1)[0])
    except (ValueError, IndexError):
        raise ObjectiveGovernanceError("objective artifact filename is malformed") from None
    if path.name != _artifact_filename(revision, digest):
        raise ObjectiveGovernanceError("objective artifact filename does not match its digest")


def _artifact_filename(revision: int, digest: str) -> str:
    """Keep Windows paths bounded; the artifact and receipt retain the full digest."""
    return f"{revision:04d}-{digest[:16]}.json"


def _unknown_fields(payload: dict[str, Any], allowed: set[str], label: str) -> list[str]:
    return [f"unknown {label} field: {key}" for key in sorted(map(str, set(payload) - allowed))]


def _identifier(value: Any, field: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        problems.append(f"{field} must be a portable identifier")
        return ""
    return value


def _positive_int(value: Any, field: str, problems: list[str]) -> int:
    if type(value) is not int or not 1 <= value <= 9_999:
        problems.append(f"{field} must be an integer from 1 to 9999")
        return 0
    return value


def _bounded_text(value: Any, field: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{field} must be a non-empty string")
        return ""
    text = value.strip()
    if len(text) > MAX_TEXT_CHARS or not text.isprintable():
        problems.append(f"{field} must be printable and at most {MAX_TEXT_CHARS} characters")
        return ""
    return text


def _optional_bounded_text(value: Any, field: str, problems: list[str]) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, problems) or None


def _identity_text(value: Any, field: str, problems: list[str]) -> str:
    text = _bounded_text(value, field, problems)
    if len(text) > 160:
        problems.append(f"{field} must be at most 160 characters")
        return ""
    return text


def _text_list(
    value: Any, field: str, problems: list[str], *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        problems.append(f"{field} must be a list of strings")
        return ()
    if required and not value:
        problems.append(f"{field} must not be empty")
    if len(value) > MAX_ITEMS:
        problems.append(f"{field} exceeds {MAX_ITEMS} entries")
    items = tuple(_bounded_text(item, f"{field} entry", problems) for item in value)
    items = tuple(item for item in items if item)
    _duplicates(items, field, problems)
    return items


def _identifier_list(
    value: Any, field: str, problems: list[str], *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        problems.append(f"{field} must be a list of identifiers")
        return ()
    if required and not value:
        problems.append(f"{field} must not be empty")
    if len(value) > MAX_ITEMS:
        problems.append(f"{field} exceeds {MAX_ITEMS} entries")
    items = tuple(_identifier(item, f"{field} entry", problems) for item in value)
    items = tuple(item for item in items if item)
    _duplicates(items, field, problems)
    return items


def _identity_list(
    value: Any, field: str, problems: list[str], *, required: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        problems.append(f"{field} must be a list of identities")
        return ()
    if required and not value:
        problems.append(f"{field} must not be empty")
    if len(value) > MAX_ITEMS:
        problems.append(f"{field} exceeds {MAX_ITEMS} entries")
    items = tuple(_identity_text(item, f"{field} entry", problems) for item in value)
    items = tuple(item for item in items if item)
    if len({item.casefold() for item in items}) != len(items):
        problems.append(f"{field} contains duplicate identities")
    return items


def _duplicates(values: Any, field: str, problems: list[str]) -> None:
    items = list(values)
    if len(set(items)) != len(items):
        problems.append(f"{field} contains duplicates")


def _required_digest(value: Any, field: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        problems.append(f"{field} must be a lowercase SHA-256 digest")
        return ""
    return value


def _optional_digest(value: Any, field: str, problems: list[str]) -> str | None:
    if value is None:
        return None
    return _required_digest(value, field, problems) or None


def _required_git_sha(value: Any, field: str, problems: list[str]) -> str:
    if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
        problems.append(f"{field} must be a lowercase 40- or 64-character Git object id")
        return ""
    return value


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
