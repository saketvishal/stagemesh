"""Bounded planning artifact: validate a structured implementation spec and freeze its identity.

The spec cites project requirement owners through ``spec_refs`` instead of copying them. Freezing only computes an
identity for consumers; it never approves anything and never touches production files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .concurrency import IntegrationLockTimeout
from .contracts import ContractError, _relative_paths
from .stage_dispatch import connected_dispatch_database, stage_dispatch_lock

if TYPE_CHECKING:
    from .persistence import Store

MAX_ITEMS = 20
MAX_TEXT_CHARS = 500
MAX_CANONICAL_CHARS = 8000
TRUNCATED_TEXT_SUFFIX = " ... [truncated by StageMesh]"
PLANNING_SPEC_VERSION = 1
PLANNING_CHALLENGE_VERSION = 1
PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION = 2
RESERVED_PATH_ROOTS = (".git", ".stagemesh")
CHALLENGE_VERDICTS = ("accept", "revise", "needs-human-decision")
CHALLENGE_FINDING_KINDS = (
    "cheaper-alternative",
    "human-decision",
    "missing-test",
    "objective-drift",
    "omitted-requirement",
    "scope-risk",
    "unsupported-assumption",
)
CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH = 1_795_030_433  # merge 82130322, 2026-11-18T13:33:53-06:00

_LIST_FIELDS = ("behavior", "non_goals", "affected_paths", "acceptance_checks")
_REQUIRED_LISTS = ("behavior", "affected_paths", "acceptance_checks")  # non_goals may be legitimately empty
_FIELDS = frozenset(_LIST_FIELDS + ("decisions", "spec_refs", "contract_hash", "baseline_sha"))
_CONTRACT_HASH = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class PlanningSpecError(ValueError):
    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class PlanningBudgetError(PlanningSpecError):
    """A structurally valid provider artifact cannot fit without semantic loss."""


@dataclass(frozen=True)
class PlanningDecision:
    question: str
    resolution: str


@dataclass(frozen=True)
class PlanningSpec:
    behavior: tuple[str, ...]
    non_goals: tuple[str, ...]
    affected_paths: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    decisions: tuple[PlanningDecision, ...]
    spec_refs: tuple[str, ...]
    contract_hash: str
    baseline_sha: str


@dataclass(frozen=True)
class FrozenPlanningSpec:
    spec: PlanningSpec
    version: int
    digest: str
    canonical_json: str

    def evidence_payload(self) -> dict[str, object]:
        return {
            "planning_spec_version": self.version,
            "planning_spec_hash": self.digest,
            "contract_hash": self.spec.contract_hash,
            "baseline_sha": self.spec.baseline_sha,
        }


@dataclass(frozen=True)
class PlanningChallengeFinding:
    kind: str
    summary: str


@dataclass(frozen=True)
class PlanningChallenge:
    planning_spec_hash: str
    planner_provider: str
    challenger_provider: str
    verdict: str
    findings: tuple[PlanningChallengeFinding, ...]
    required_decisions: tuple[str, ...]


@dataclass(frozen=True)
class FrozenPlanningChallenge:
    challenge: PlanningChallenge
    version: int
    digest: str
    canonical_json: str


def _parse_planning_spec(payload: Any) -> tuple[PlanningSpec | None, tuple[str, ...]]:
    if not isinstance(payload, dict):
        return None, ("planning spec must be a JSON object",)
    problems: list[str] = []
    problems.extend(f"unknown field: {key}" for key in sorted(map(str, set(payload) - _FIELDS)))

    lists = {name: _text_list(payload.get(name), name, problems, required=name in _REQUIRED_LISTS) for name in _LIST_FIELDS}
    _check_paths(lists["affected_paths"], problems)
    spec_refs = _spec_refs(payload.get("spec_refs"), problems)
    decisions = _decisions(payload.get("decisions"), problems)
    contract_hash = _identity(payload.get("contract_hash"), "contract_hash", _CONTRACT_HASH, problems)
    baseline_sha = _identity(payload.get("baseline_sha"), "baseline_sha", _SHA, problems)
    if problems:
        return None, tuple(problems)

    spec = PlanningSpec(
        behavior=lists["behavior"],
        non_goals=lists["non_goals"],
        affected_paths=lists["affected_paths"],
        acceptance_checks=lists["acceptance_checks"],
        decisions=decisions,
        spec_refs=spec_refs,
        contract_hash=contract_hash,
        baseline_sha=baseline_sha,
    )
    return spec, ()


def validate_planning_spec(payload: Any) -> tuple[PlanningSpec | None, tuple[str, ...]]:
    """Return the parsed spec and no problems, or None and every problem found."""
    spec, problems = _parse_planning_spec(payload)
    if spec is None:
        return None, problems
    size = len(_canonical_json(spec))
    if size > MAX_CANONICAL_CHARS:
        return None, (f"planning spec is {size} characters canonicalized; the limit is {MAX_CANONICAL_CHARS}",)
    return spec, ()


def freeze_planning_spec(payload: Any) -> FrozenPlanningSpec:
    """Validate and compute the identity consumers pin; raises PlanningSpecError listing every problem."""
    spec, problems = validate_planning_spec(payload)
    if spec is None:
        raise PlanningSpecError(problems)
    canonical = _canonical_json(spec)
    return FrozenPlanningSpec(spec, PLANNING_SPEC_VERSION, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), canonical)


def fit_provider_planning_spec(payload: Any) -> FrozenPlanningSpec:
    """Fit provider-authored descriptive fields without changing paths or decisions.

    Manual artifacts remain strict through ``freeze_planning_spec``. This
    provider boundary preserves every item and marks every shortened value.
    """
    spec, problems = _parse_planning_spec(payload)
    if spec is None:
        raise PlanningSpecError(problems)
    canonical = _canonical_json(spec)
    if len(canonical) <= MAX_CANONICAL_CHARS:
        return _freeze_parsed_spec(spec)

    def fitted(limit: int) -> PlanningSpec:
        return PlanningSpec(
            behavior=_truncate_distinct(spec.behavior, limit),
            non_goals=_truncate_distinct(spec.non_goals, limit),
            affected_paths=spec.affected_paths,
            acceptance_checks=_truncate_distinct(spec.acceptance_checks, limit),
            decisions=spec.decisions,
            spec_refs=spec.spec_refs,
            contract_hash=spec.contract_hash,
            baseline_sha=spec.baseline_sha,
        )

    compact = _largest_fitting(fitted, _canonical_json)
    if compact is None:
        raise PlanningBudgetError(
            (
                (
                    "planning spec cannot fit the canonical limit without changing paths, "
                    "decisions, or required structure"
                ),
            )
        )
    normalized = json.loads(_canonical_json(compact))
    normalized.pop("version", None)
    return freeze_planning_spec(normalized)


def _freeze_parsed_spec(spec: PlanningSpec) -> FrozenPlanningSpec:
    canonical = _canonical_json(spec)
    return FrozenPlanningSpec(
        spec,
        PLANNING_SPEC_VERSION,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        canonical,
    )


def _parse_planning_challenge(
    payload: Any,
) -> tuple[PlanningChallenge | None, tuple[str, ...]]:
    if not isinstance(payload, dict):
        return None, ("planning challenge must be a JSON object",)
    allowed = {
        "version",
        "planning_spec_hash",
        "planner_provider",
        "challenger_provider",
        "verdict",
        "findings",
        "required_decisions",
    }
    problems = [f"unknown challenge field: {key}" for key in sorted(map(str, set(payload) - allowed))]
    if payload.get("version") != PLANNING_CHALLENGE_VERSION:
        problems.append(f"planning challenge version must be {PLANNING_CHALLENGE_VERSION}")
    spec_hash = _identity(payload.get("planning_spec_hash"), "planning_spec_hash", _CONTRACT_HASH, problems)
    planner = _provider_name(payload.get("planner_provider"), "planner_provider", problems)
    challenger = _provider_name(payload.get("challenger_provider"), "challenger_provider", problems)
    if planner and challenger and planner.casefold() == challenger.casefold():
        problems.append("planner_provider and challenger_provider must be different")
    verdict = payload.get("verdict")
    if verdict not in CHALLENGE_VERDICTS:
        problems.append(f"challenge verdict must be one of {', '.join(CHALLENGE_VERDICTS)}")
        verdict = ""
    required_decisions = _text_list(
        payload.get("required_decisions", []),
        "required_decisions",
        problems,
        required=False,
    )
    raw_findings = payload.get("findings", [])
    findings: list[PlanningChallengeFinding] = []
    if not isinstance(raw_findings, list):
        problems.append("findings must be a list of objects")
    else:
        if len(raw_findings) > MAX_ITEMS:
            problems.append(f"findings has {len(raw_findings)} entries; the limit is {MAX_ITEMS}")
        for item in raw_findings:
            if not isinstance(item, dict) or set(item) != {"kind", "summary"}:
                problems.append("findings entries must contain only kind and summary")
                continue
            kind = item.get("kind")
            summary = _text(item.get("summary"))
            if kind not in CHALLENGE_FINDING_KINDS:
                problems.append(
                    f"finding kind must be one of {', '.join(CHALLENGE_FINDING_KINDS)}"
                )
            elif summary is None:
                problems.append("finding summary must be a non-empty string")
            elif len(summary) > MAX_TEXT_CHARS:
                problems.append(
                    f"finding summary is {len(summary)} characters; the limit is {MAX_TEXT_CHARS}"
                )
            else:
                findings.append(PlanningChallengeFinding(str(kind), summary))
    if verdict == "revise" and not findings:
        problems.append("revise challenge verdict requires at least one finding")
    if verdict == "needs-human-decision" and not required_decisions:
        problems.append("needs-human-decision verdict requires required_decisions")
    if verdict == "accept" and required_decisions:
        problems.append("accept challenge verdict cannot retain required decisions")
    if problems:
        return None, tuple(problems)
    challenge = PlanningChallenge(
        spec_hash,
        planner,
        challenger,
        str(verdict),
        tuple(findings),
        required_decisions,
    )
    return challenge, ()


def validate_planning_challenge(
    payload: Any,
) -> tuple[PlanningChallenge | None, tuple[str, ...]]:
    """Validate a bounded challenge without treating it as executed-agent proof."""
    challenge, problems = _parse_planning_challenge(payload)
    if challenge is None:
        return None, problems
    canonical = _challenge_canonical_json(challenge)
    if len(canonical) > MAX_CANONICAL_CHARS:
        return None, (
            f"planning challenge is {len(canonical)} characters canonicalized; the limit is {MAX_CANONICAL_CHARS}",
        )
    return challenge, ()


def freeze_planning_challenge(payload: Any) -> FrozenPlanningChallenge:
    challenge, problems = validate_planning_challenge(payload)
    if challenge is None:
        raise PlanningSpecError(problems)
    canonical = _challenge_canonical_json(challenge)
    return FrozenPlanningChallenge(
        challenge,
        PLANNING_CHALLENGE_VERSION,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        canonical,
    )


def fit_provider_planning_challenge(payload: Any) -> FrozenPlanningChallenge:
    """Fit all provider finding summaries to the exact stored JSON budget.

    Findings, required decisions, and the verdict are never dropped or changed.
    If fixed structure and required decisions cannot fit, the response remains a
    typed refusal instead of being silently weakened.
    """
    challenge, problems = _parse_planning_challenge(payload)
    if challenge is None:
        raise PlanningSpecError(problems)
    canonical = _challenge_canonical_json(challenge)
    if len(canonical) <= MAX_CANONICAL_CHARS:
        return _freeze_parsed_challenge(challenge)

    def fitted(limit: int) -> PlanningChallenge:
        summaries = _truncate_distinct(
            tuple(item.summary for item in challenge.findings), limit
        )
        return PlanningChallenge(
            planning_spec_hash=challenge.planning_spec_hash,
            planner_provider=challenge.planner_provider,
            challenger_provider=challenge.challenger_provider,
            verdict=challenge.verdict,
            findings=tuple(
                PlanningChallengeFinding(item.kind, summary)
                for item, summary in zip(challenge.findings, summaries, strict=True)
            ),
            required_decisions=challenge.required_decisions,
        )

    compact = _largest_fitting(fitted, _challenge_canonical_json)
    if compact is None:
        raise PlanningBudgetError(
            (
                (
                    "planning challenge cannot fit the canonical limit without dropping "
                    "findings or changing required decisions"
                ),
            )
        )
    return _freeze_parsed_challenge(compact)


def _freeze_parsed_challenge(challenge: PlanningChallenge) -> FrozenPlanningChallenge:
    canonical = _challenge_canonical_json(challenge)
    return FrozenPlanningChallenge(
        challenge,
        PLANNING_CHALLENGE_VERSION,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        canonical,
    )


def _largest_fitting(factory: Any, canonicalizer: Any) -> Any | None:
    minimum = len(TRUNCATED_TEXT_SUFFIX) + 4
    smallest = factory(minimum)
    if len(canonicalizer(smallest)) > MAX_CANONICAL_CHARS:
        return None
    low, high = minimum, MAX_TEXT_CHARS
    best = smallest
    while low <= high:
        middle = (low + high) // 2
        candidate = factory(middle)
        if len(canonicalizer(candidate)) <= MAX_CANONICAL_CHARS:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _truncate_visible(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - len(TRUNCATED_TEXT_SUFFIX)
    return text[:keep].rstrip() + TRUNCATED_TEXT_SUFFIX


def _truncate_distinct(items: tuple[str, ...], limit: int) -> tuple[str, ...]:
    shortened = [_truncate_visible(item, limit) for item in items]
    duplicates = {value for value in shortened if shortened.count(value) > 1}
    if not duplicates:
        return tuple(shortened)
    result: list[str] = []
    for index, (original, value) in enumerate(zip(items, shortened, strict=True), start=1):
        if value not in duplicates:
            result.append(value)
            continue
        marker = f" #{index:02d}"
        suffix = marker + TRUNCATED_TEXT_SUFFIX
        keep = limit - len(suffix)
        result.append(original[:keep].rstrip() + suffix)
    return tuple(result)


def _challenge_canonical_json(challenge: PlanningChallenge) -> str:
    return json.dumps(
        {
            "version": PLANNING_CHALLENGE_VERSION,
            "planning_spec_hash": challenge.planning_spec_hash,
            "planner_provider": challenge.planner_provider,
            "challenger_provider": challenge.challenger_provider,
            "verdict": challenge.verdict,
            "findings": [
                {"kind": item.kind, "summary": item.summary}
                for item in challenge.findings
            ],
            "required_decisions": list(challenge.required_decisions),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_json(spec: PlanningSpec) -> str:
    payload = {
        "version": PLANNING_SPEC_VERSION,
        "behavior": list(spec.behavior),
        "non_goals": list(spec.non_goals),
        "affected_paths": list(spec.affected_paths),
        "acceptance_checks": list(spec.acceptance_checks),
        "decisions": [{"question": item.question, "resolution": item.resolution} for item in spec.decisions],
        "spec_refs": list(spec.spec_refs),
        "contract_hash": spec.contract_hash,
        "baseline_sha": spec.baseline_sha,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _provider_name(value: Any, field: str, problems: list[str]) -> str:
    text = _text(value)
    if (
        text is None
        or len(text) > 100
        or any(char.isspace() or ord(char) < 32 for char in text)
    ):
        problems.append(f"{field} must be a non-empty provider name without whitespace")
        return ""
    return text


def _text_list(value: Any, field: str, problems: list[str], *, required: bool) -> tuple[str, ...]:
    if value is None:
        if required:
            problems.append(f"{field} is required")
        return ()
    if not isinstance(value, list):
        problems.append(f"{field} must be a list of strings")
        return ()
    if required and not value:
        problems.append(f"{field} must not be empty")
    if len(value) > MAX_ITEMS:
        problems.append(f"{field} has {len(value)} entries; the limit is {MAX_ITEMS}")
    items: list[str] = []
    for item in value:
        text = _text(item)
        if text is None:
            problems.append(f"{field} entries must be non-empty strings")
        elif len(text) > MAX_TEXT_CHARS:
            problems.append(f"{field} entry is {len(text)} characters; the limit is {MAX_TEXT_CHARS}")
        else:
            items.append(text)
    if len(set(items)) != len(items):
        problems.append(f"{field} contains duplicate entries")
    return tuple(items)


def _check_paths(paths: tuple[str, ...], problems: list[str]) -> None:
    try:
        _relative_paths(list(paths), "affected_paths")
    except ContractError as exc:
        problems.append(str(exc))
    for path in paths:
        # Glob wildcards remain valid; reject characters invalid in filesystem paths.
        if any(ord(char) < 32 or char in '"<>|' for char in path):
            problems.append(f"affected_paths entry contains invalid path characters: {path!r}")
        parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
        if parts and parts[0].casefold() in RESERVED_PATH_ROOTS:
            problems.append(f"affected_paths entry targets reserved location: {path}")


def _spec_refs(value: Any, problems: list[str]) -> tuple[str, ...]:
    refs = _text_list(value, "spec_refs", problems, required=False)
    try:
        return _relative_paths(list(refs), "spec_refs")
    except ContractError as exc:
        problems.append(str(exc))
        return ()


def _decisions(value: Any, problems: list[str]) -> tuple[PlanningDecision, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append("decisions must be a list of objects")
        return ()
    if len(value) > MAX_ITEMS:
        problems.append(f"decisions has {len(value)} entries; the limit is {MAX_ITEMS}")
    decisions: list[PlanningDecision] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"question", "resolution"}:
            problems.append("decisions entries must be objects with question and resolution")
            continue
        question = _text(item.get("question"))
        resolution = _text(item.get("resolution"))
        if question is None:
            problems.append("decisions entry needs a question")
        elif resolution is None:
            problems.append(f"unresolved decision: {question[:80]}")
        elif max(len(question), len(resolution)) > MAX_TEXT_CHARS:
            problems.append(f"decisions entry exceeds {MAX_TEXT_CHARS} characters: {question[:80]}")
        else:
            decisions.append(PlanningDecision(question, resolution))
    return tuple(decisions)


def _identity(value: Any, field: str, pattern: re.Pattern[str], problems: list[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        problems.append(f"{field} must be a lowercase hex digest")
        return ""
    return value


# --- Opt-in pre-implementation gate -------------------------------------------------------------------------------
PLANNING_ARTIFACT_DIR = ".stagemesh/planning"
NEXT_WRITE_SPEC = "write_planning_spec"
NEXT_RESOLVE_DECISIONS = "resolve_planning_decisions"
NEXT_REFRESH_SPEC = "refresh_planning_spec"
NEXT_APPROVE_SPEC = "approve_planning_spec"
NEXT_CHALLENGE_SPEC = "challenge_planning_spec"
NEXT_REFRESH_CHALLENGE = "refresh_planning_challenge"
NEXT_REVISE_SPEC = "revise_planning_spec"
NEXT_RESOLVE_CHALLENGE_DECISIONS = "resolve_planning_challenge_decisions"


@dataclass(frozen=True)
class PlanningGateDecision:
    allowed: bool
    next_action: str | None = None
    reason: str = ""
    approved_scope: tuple[str, ...] = ()


def planning_artifact_paths(project: Path, task_id: str) -> tuple[Path, Path]:
    """The spec and approval files for a task; approval is a separate file only an approver writes."""
    base = project / PLANNING_ARTIFACT_DIR
    return base / f"{task_id}.spec.json", base / f"{task_id}.approval.json"


def planning_challenge_path(project: Path, task_id: str) -> Path:
    return project / PLANNING_ARTIFACT_DIR / f"{task_id}.challenge.json"


def load_planning_artifacts(project: Path, task_id: str) -> tuple[Any, Any]:
    """Return (spec payload, approval payload); a missing or unreadable file is None."""
    loaded: list[Any] = []
    for path in planning_artifact_paths(project, task_id):
        try:
            loaded.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            loaded.append(None)
    return loaded[0], loaded[1]


def load_planning_challenge(project: Path, task_id: str) -> Any:
    try:
        return json.loads(planning_challenge_path(project, task_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def evaluate_planning_gate(
    *,
    enabled: bool,
    spec_payload: Any,
    approval: Any,
    contract_hash: str,
    baseline_sha: str,
    require_challenge: bool = False,
    challenge_payload: Any = None,
) -> PlanningGateDecision:
    """Decide whether implementation may start. Disabled (legacy) policy always allows; otherwise fail closed."""
    if not enabled:
        return PlanningGateDecision(True)
    if spec_payload is None:
        return PlanningGateDecision(False, NEXT_WRITE_SPEC, "no planning spec has been written for this task")
    spec, problems = validate_planning_spec(spec_payload)
    if spec is None:
        unresolved = any(problem.startswith("unresolved decision") for problem in problems)
        return PlanningGateDecision(
            False, NEXT_RESOLVE_DECISIONS if unresolved else NEXT_WRITE_SPEC, "; ".join(problems)
        )
    if spec.contract_hash != contract_hash or spec.baseline_sha != baseline_sha:
        return PlanningGateDecision(
            False, NEXT_REFRESH_SPEC, "planning spec identity is stale: the contract or baseline changed since it was written"
        )
    digest = freeze_planning_spec(spec_payload).digest
    challenge_digest = None
    if require_challenge:
        if challenge_payload is None:
            return PlanningGateDecision(
                False,
                NEXT_CHALLENGE_SPEC,
                f"planning spec {digest[:12]} has no independent challenge",
            )
        challenge, challenge_problems = validate_planning_challenge(challenge_payload)
        if challenge is None:
            return PlanningGateDecision(
                False, NEXT_CHALLENGE_SPEC, "; ".join(challenge_problems)
            )
        if challenge.planning_spec_hash != digest:
            return PlanningGateDecision(
                False,
                NEXT_REFRESH_CHALLENGE,
                "planning challenge is stale: the planning spec changed",
            )
        if challenge.verdict == "needs-human-decision" or challenge.required_decisions:
            return PlanningGateDecision(
                False,
                NEXT_RESOLVE_CHALLENGE_DECISIONS,
                "independent planning challenge requires a human decision",
            )
        if challenge.verdict == "revise":
            return PlanningGateDecision(
                False,
                NEXT_REVISE_SPEC,
                "independent planning challenge requires the spec to be revised",
            )
        challenge_digest = freeze_planning_challenge(challenge_payload).digest
    approved = (
        isinstance(approval, dict)
        and approval.get("approved") is True
        and approval.get("planning_spec_hash") == digest
        and (
            not require_challenge
            or approval.get("planning_challenge_hash") == challenge_digest
        )
    )
    if not approved:
        return PlanningGateDecision(False, NEXT_APPROVE_SPEC, f"planning spec {digest[:12]} has no matching approval")
    return PlanningGateDecision(True, approved_scope=spec.affected_paths)


def approved_scope(project: Path, task_id: str, contract_hash: str) -> tuple[str, ...]:
    """Affected paths of an approved, contract-current spec; empty when there is no valid approved evidence."""
    spec_payload, approval = load_planning_artifacts(project, task_id)
    challenge_payload = load_planning_challenge(project, task_id)
    spec, _ = validate_planning_spec(spec_payload)
    if spec is None:
        return ()
    decision = evaluate_planning_gate(
        enabled=True,
        spec_payload=spec_payload,
        approval=approval,
        contract_hash=contract_hash,
        baseline_sha=spec.baseline_sha,
        require_challenge=isinstance(approval, dict)
        and "planning_challenge_hash" in approval,
        challenge_payload=challenge_payload,
    )
    return decision.approved_scope


def accepted_planning_context(
    project: Path,
    task_id: str,
    contract_hash: str,
    baseline_sha: str,
    *,
    require_challenge: bool = False,
) -> tuple[str, str] | None:
    """Return bounded accepted planning JSON plus its digest, or no context.

    The JSON is advisory input for a provider. It is not an instruction channel or
    evidence that a planner/challenger process actually ran.
    """
    spec_payload, approval = load_planning_artifacts(project, task_id)
    challenge_payload = load_planning_challenge(project, task_id)
    decision = evaluate_planning_gate(
        enabled=True,
        spec_payload=spec_payload,
        approval=approval,
        contract_hash=contract_hash,
        baseline_sha=baseline_sha,
        require_challenge=require_challenge,
        challenge_payload=challenge_payload,
    )
    if not decision.allowed:
        return None
    frozen_spec = freeze_planning_spec(spec_payload)
    frozen_challenge = (
        freeze_planning_challenge(challenge_payload) if require_challenge else None
    )
    canonical = json.dumps(
        {
            "planning_spec": json.loads(frozen_spec.canonical_json),
            "planning_spec_hash": frozen_spec.digest,
            "planning_challenge": json.loads(frozen_challenge.canonical_json)
            if frozen_challenge is not None
            else None,
            "planning_challenge_hash": frozen_challenge.digest
            if frozen_challenge is not None
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Accountable approval (opt-in via approval_authority) ---------------------------------------------------------
APPROVAL_AUTHORITY_VERSION = 1
APPROVAL_ARTIFACT_VERSION = 2
APPROVAL_RECEIPT_EVENT = "planning_approval_recorded"

APPROVAL_MISSING = "approval_missing"
APPROVAL_VERSION_UNSUPPORTED = "approval_version_unsupported"
APPROVAL_MALFORMED = "approval_malformed"
APPROVAL_RECEIPT_MISSING = "approval_receipt_missing"
APPROVAL_RECEIPT_MISMATCH = "approval_receipt_mismatch"
APPROVAL_AUTHORITY_MISSING = "approval_authority_missing"
APPROVAL_AUTHORITY_STALE = "approval_authority_stale"
APPROVAL_BINDING_STALE = "approval_binding_stale"
APPROVAL_ACTOR_MISSING = "approval_actor_missing"
APPROVAL_ACTOR_UNAUTHORIZED = "approval_actor_unauthorized"
APPROVAL_ROLE_CONFLICT = "approval_role_conflict"
APPROVAL_EXECUTION_IDENTITY_MISSING = "approval_execution_identity_missing"
PLANNING_INPUTS_INVALID = "planning_inputs_invalid"

NEXT_REPAIR_APPROVAL_AUTHORITY = "repair_approval_authority"
NEXT_OBTAIN_APPROVAL = "obtain_planning_approval"
NEXT_CHOOSE_ACTOR = "choose_non_conflicting_actor"

_IDENTITY = re.compile(r"[a-z0-9][a-z0-9._:@/-]{0,99}")
_AUTHORITY_FIELDS = frozenset({"version", "authority_id", "approver_id"})
_APPROVAL_FIELDS = frozenset(
    {
        "version",
        "decision",
        "task_id",
        "planning_spec_hash",
        "planning_challenge_hash",
        "contract_hash",
        "baseline_sha",
        "authority_id",
        "authority_hash",
        "approver_id",
        "receipt_id",
    }
)
# Receipt fields that must equal the artifact; the receipt additionally carries its event name and creation time.
_RECEIPT_BINDINGS = tuple(sorted(_APPROVAL_FIELDS))


@dataclass(frozen=True)
class ApprovalAuthority:
    authority_id: str
    approver_id: str

    @property
    def digest(self) -> str:
        return approval_authority_digest(self)


@dataclass(frozen=True)
class ExecutionIdentities:
    """Identities taken from successful StageMesh execution receipts, never from model output."""

    planner: str | None = None
    challenger: str | None = None
    implementers: tuple[str, ...] = ()
    reviewer: str | None = None


@dataclass(frozen=True)
class AccountableApprovalDecision:
    allowed: bool
    reason: str = ""
    detail: str = ""
    next_action: str | None = None
    receipt_id: str = ""
    approved_scope: tuple[str, ...] = ()


def approval_authority_digest(authority: ApprovalAuthority) -> str:
    canonical = json.dumps(
        {
            "version": APPROVAL_AUTHORITY_VERSION,
            "authority_id": authority.authority_id,
            "approver_id": authority.approver_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_approval_authority(payload: Any) -> tuple[ApprovalAuthority | None, tuple[str, ...]]:
    """Strictly validate an approval_authority object; identifiers are never rewritten."""
    if not isinstance(payload, dict):
        return None, ("approval_authority must be a JSON object",)
    problems = [f"unknown approval_authority field: {key}" for key in sorted(map(str, set(payload) - _AUTHORITY_FIELDS))]
    problems.extend(f"approval_authority is missing {key}" for key in sorted(_AUTHORITY_FIELDS - set(payload)))
    version = payload.get("version")
    if "version" in payload and (type(version) is not int or version != APPROVAL_AUTHORITY_VERSION):
        problems.append(f"approval_authority version must be {APPROVAL_AUTHORITY_VERSION}")
    for key in ("authority_id", "approver_id"):
        if key in payload and not _canonical_identity(payload[key]):
            problems.append(f"approval_authority {key} must be a canonical identity")
    if problems:
        return None, tuple(problems)
    return ApprovalAuthority(payload["authority_id"], payload["approver_id"]), ()


def approval_authority_from_gate_config(gate_config: Any) -> tuple[ApprovalAuthority | None, tuple[str, ...]]:
    """No approval_authority key means legacy behavior (None, no problems); a present key must validate."""
    if not isinstance(gate_config, dict) or "approval_authority" not in gate_config:
        return None, ()
    return validate_approval_authority(gate_config["approval_authority"])


def validate_approval_artifact(payload: Any, *, require_challenge: bool) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Strictly validate a version-2 approval artifact and return it as a plain dict."""
    if not isinstance(payload, dict):
        return None, ("approval artifact must be a JSON object",)
    problems = [f"unknown approval field: {key}" for key in sorted(map(str, set(payload) - _APPROVAL_FIELDS))]
    problems.extend(f"approval is missing {key}" for key in sorted(_APPROVAL_FIELDS - set(payload)))
    if payload.get("decision") != "approve":
        problems.append("approval decision must be approve")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        problems.append("approval task_id must be a non-empty string")
    for key, pattern in (
        ("planning_spec_hash", _CONTRACT_HASH),
        ("contract_hash", _CONTRACT_HASH),
        ("authority_hash", _CONTRACT_HASH),
        ("baseline_sha", _SHA),
    ):
        value = payload.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            problems.append(f"approval {key} must be a lowercase hex digest")
    challenge_hash = payload.get("planning_challenge_hash")
    if require_challenge:
        if not isinstance(challenge_hash, str) or not _CONTRACT_HASH.fullmatch(challenge_hash):
            problems.append("approval planning_challenge_hash must be a lowercase hex digest")
    elif challenge_hash is not None:
        problems.append("approval planning_challenge_hash must be null when no challenge is required")
    for key in ("authority_id", "approver_id"):
        if not _canonical_identity(payload.get(key)):
            problems.append(f"approval {key} must be a canonical identity")
    receipt_id = payload.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        problems.append("approval receipt_id must be a non-empty string")
    if problems:
        return None, tuple(problems)
    return dict(payload), ()


def evaluate_accountable_approval(
    *,
    task_id: str,
    authority: ApprovalAuthority | None,
    spec_payload: Any,
    approval: Any,
    receipts: Iterable[Any],
    contract_hash: str,
    baseline_sha: str,
    require_challenge: bool = False,
    challenge_payload: Any = None,
    identities: ExecutionIdentities | None = None,
    required_roles: tuple[str, ...] = (),
) -> AccountableApprovalDecision:
    """Pure fail-closed decision for one enforcement boundary when approval_authority is configured.

    ``required_roles`` names the execution roles ("implementer", "reviewer") that must be identified at this boundary;
    the planner, and the challenger when a challenge is required, are always required.
    """
    if authority is None:
        return _approval_block(APPROVAL_AUTHORITY_MISSING, "no valid approval_authority is configured", NEXT_REPAIR_APPROVAL_AUTHORITY)
    if approval is None:
        return _approval_block(APPROVAL_MISSING, "no approval artifact exists for this task", NEXT_OBTAIN_APPROVAL)
    if not isinstance(approval, dict):
        return _approval_block(APPROVAL_MALFORMED, "approval artifact must be a JSON object", NEXT_OBTAIN_APPROVAL)
    version = approval.get("version")
    if type(version) is not int or version != APPROVAL_ARTIFACT_VERSION:
        return _approval_block(
            APPROVAL_VERSION_UNSUPPORTED,
            f"accountable approval requires a version-{APPROVAL_ARTIFACT_VERSION} artifact",
            NEXT_OBTAIN_APPROVAL,
        )
    if "approver_id" not in approval or approval["approver_id"] in (None, ""):
        return _approval_block(APPROVAL_ACTOR_MISSING, "approval names no approver", NEXT_OBTAIN_APPROVAL)
    artifact, problems = validate_approval_artifact(approval, require_challenge=require_challenge)
    if artifact is None:
        return _approval_block(APPROVAL_MALFORMED, "; ".join(problems), NEXT_OBTAIN_APPROVAL)
    if artifact["authority_id"] != authority.authority_id or artifact["authority_hash"] != authority.digest:
        return _approval_block(
            APPROVAL_AUTHORITY_STALE, "approval authority changed since the approval was recorded", NEXT_OBTAIN_APPROVAL
        )
    if artifact["approver_id"] != authority.approver_id:
        return _approval_block(
            APPROVAL_ACTOR_UNAUTHORIZED, "approver is not the configured approver", NEXT_OBTAIN_APPROVAL
        )

    spec, spec_problems = validate_planning_spec(spec_payload)
    if spec is None:
        return _approval_block(PLANNING_INPUTS_INVALID, "; ".join(spec_problems) or "no planning spec", NEXT_WRITE_SPEC)
    spec_digest = freeze_planning_spec(spec_payload).digest
    challenge_digest: str | None = None
    if require_challenge:
        challenge, challenge_problems = validate_planning_challenge(challenge_payload)
        if challenge is None:
            return _approval_block(
                PLANNING_INPUTS_INVALID, "; ".join(challenge_problems), NEXT_CHALLENGE_SPEC
            )
        if challenge.planning_spec_hash != spec_digest:
            return _approval_block(APPROVAL_BINDING_STALE, "planning_challenge_hash: challenge is stale", NEXT_OBTAIN_APPROVAL)
        if challenge.verdict != "accept":
            return _approval_block(PLANNING_INPUTS_INVALID, "planning challenge is not accepted", NEXT_CHALLENGE_SPEC)
        challenge_digest = freeze_planning_challenge(challenge_payload).digest
    current = {
        "task_id": task_id,
        "planning_spec_hash": spec_digest,
        "planning_challenge_hash": challenge_digest,
        "contract_hash": contract_hash,
        "baseline_sha": baseline_sha,
    }
    if spec.contract_hash != contract_hash or spec.baseline_sha != baseline_sha:
        return _approval_block(APPROVAL_BINDING_STALE, "planning spec contract or baseline is stale", NEXT_REFRESH_SPEC)
    for key, value in current.items():
        if artifact[key] != value:
            return _approval_block(APPROVAL_BINDING_STALE, f"{key} no longer matches the current input", NEXT_OBTAIN_APPROVAL)

    receipt_id = artifact["receipt_id"]
    matching = [item for item in receipts if isinstance(item, dict) and item.get("receipt_id") == receipt_id]
    if not matching:
        return _approval_block(APPROVAL_RECEIPT_MISSING, "no durable approval receipt matches the artifact", NEXT_OBTAIN_APPROVAL)
    receipt = matching[0]
    created_at = receipt.get("created_at")
    if (
        len(matching) != 1
        or receipt.get("event") != APPROVAL_RECEIPT_EVENT
        or not isinstance(created_at, str)
        or not created_at
    ):
        return _approval_block(APPROVAL_RECEIPT_MISMATCH, "approval receipt is not a unique recorded approval event", NEXT_OBTAIN_APPROVAL)
    for key in _RECEIPT_BINDINGS:
        if key not in receipt or receipt[key] != artifact[key]:
            return _approval_block(APPROVAL_RECEIPT_MISMATCH, f"approval receipt {key} does not match the artifact", NEXT_OBTAIN_APPROVAL)

    return _check_role_separation(authority.approver_id, identities or ExecutionIdentities(), require_challenge, required_roles, receipt_id, spec.affected_paths)


def _check_role_separation(
    approver: str,
    identities: ExecutionIdentities,
    require_challenge: bool,
    required_roles: tuple[str, ...],
    receipt_id: str,
    scope: tuple[str, ...],
) -> AccountableApprovalDecision:
    roles: dict[str, tuple[str, ...]] = {
        "planner": (identities.planner,) if identities.planner is not None else (),
        "challenger": (identities.challenger,) if identities.challenger is not None else (),
        "implementer": tuple(identities.implementers),
        "reviewer": (identities.reviewer,) if identities.reviewer is not None else (),
    }
    required = {"planner"} | ({"challenger"} if require_challenge else set()) | set(required_roles)
    for role in sorted(required):
        if not roles.get(role):
            return _approval_block(APPROVAL_EXECUTION_IDENTITY_MISSING, f"no execution identity for {role}", NEXT_CHOOSE_ACTOR)
    for role, values in roles.items():
        if any(not _canonical_identity(value) for value in values):
            return _approval_block(
                APPROVAL_EXECUTION_IDENTITY_MISSING, f"{role} execution identity is not canonical", NEXT_CHOOSE_ACTOR
            )
    conflicts = sorted(role for role, values in roles.items() if approver in values)
    if conflicts:
        return _approval_block(APPROVAL_ROLE_CONFLICT, f"approver also holds role: {', '.join(conflicts)}", NEXT_CHOOSE_ACTOR)
    return AccountableApprovalDecision(True, receipt_id=receipt_id, approved_scope=scope)


def _approval_block(reason: str, detail: str, next_action: str) -> AccountableApprovalDecision:
    return AccountableApprovalDecision(False, reason, detail, next_action)


def _canonical_identity(value: Any) -> bool:
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


# --- Explicit operator approval action ----------------------------------------------------------------------------
PROVIDER_PROCESS_ENV = "STAGEMESH_PROVIDER_PROCESS"
_APPROVAL_BLOCKING_EXECUTIONS = frozenset({"IMPLEMENTATION", "REVIEW", "INTEGRATION"})
_RECEIPT_KEYS = _APPROVAL_FIELDS | {"event", "created_at"}
PLANNING_CHALLENGE_RETIRED_EVENT = "planning.challenge_retired"
PLANNING_CHALLENGE_RETIRE_INTENT_EVENT = "planning.challenge_retire_intent"
PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT = "planning.challenge_retire_aborted"
PLANNING_CHALLENGER_RECEIPT_EVENT = "planning.challenge_produced"
PLANNING_REVISION_INTENT_EVENT = "planning.spec_revision_intent"
PLANNING_REVISION_COMPLETED_EVENT = "planning.spec_revision_completed"
PLANNING_REVISION_ABORTED_EVENT = "planning.spec_revision_aborted"
MAX_AUTOMATIC_PLANNING_REVISIONS = 2


class PlanningApprovalRefusal(Exception):
    """The operator approval action was refused; ``code`` is stable for scripts."""

    def __init__(self, code: str, message: str, next_action: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action


class PlanningChallengeRefreshRefusal(Exception):
    """An explicit challenge refresh was refused; ``code`` is stable for scripts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlanningRevisionRefusal(Exception):
    """A generated plan could not be revised without weakening its evidence boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def automatic_planning_revision_count(
    store: Store, task_id: str, current_spec_hash: str
) -> int:
    """Count the completed automatic revision lineage ending at the current spec."""
    parents: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in store.task_audit_events(PLANNING_REVISION_COMPLETED_EVENT, task_id):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        from_hash = payload.get("from_spec_hash")
        to_hash = payload.get("to_spec_hash")
        if not (
            isinstance(from_hash, str)
            and _CONTRACT_HASH.fullmatch(from_hash)
            and isinstance(to_hash, str)
            and _CONTRACT_HASH.fullmatch(to_hash)
        ):
            continue
        previous = parents.setdefault(to_hash, from_hash)
        if previous != from_hash:
            ambiguous.add(to_hash)

    count = 0
    cursor = current_spec_hash
    seen: set[str] = set()
    while cursor in parents:
        if cursor in seen or cursor in ambiguous:
            return MAX_AUTOMATIC_PLANNING_REVISIONS
        seen.add(cursor)
        cursor = parents[cursor]
        count += 1
    return count


def publish_automatic_planning_revision(
    store: Store,
    project: Path,
    task_id: str,
    *,
    current_spec_payload: Any,
    challenge_payload: Any,
    revised_spec_payload: Any,
) -> dict[str, Any]:
    """Replace one unapproved generated plan while preserving exact old artifacts and an audit trail."""
    if store.get_task(task_id) is None:
        raise PlanningRevisionRefusal("task_missing", f"task {task_id} is not present in the StageMesh ledger")
    if store.has_active_claim(task_id):
        raise PlanningRevisionRefusal("claim_active", f"task {task_id} has an active claim")
    active = sorted(store.active_execution_kinds(task_id))
    if active:
        raise PlanningRevisionRefusal(
            "execution_active",
            f"task {task_id} has active {', '.join(kind.lower() for kind in active)} execution",
        )

    try:
        current = freeze_planning_spec(current_spec_payload)
        challenge = freeze_planning_challenge(challenge_payload)
        revised = freeze_planning_spec(revised_spec_payload)
    except PlanningSpecError as exc:
        raise PlanningRevisionRefusal("planning_revision_invalid", str(exc)) from exc
    if challenge.challenge.planning_spec_hash != current.digest:
        raise PlanningRevisionRefusal(
            "planning_revision_stale", "the challenge does not belong to the current planning spec"
        )
    if challenge.challenge.verdict != "revise" or challenge.challenge.required_decisions:
        raise PlanningRevisionRefusal(
            "planning_revision_not_allowed",
            "automatic revision requires a revise verdict with no human decisions",
        )
    if revised.digest == current.digest:
        raise PlanningRevisionRefusal(
            "planning_revision_no_change", "the planner returned the same planning spec after review"
        )
    revision_count = automatic_planning_revision_count(store, task_id, current.digest)
    if revision_count >= MAX_AUTOMATIC_PLANNING_REVISIONS:
        raise PlanningRevisionRefusal(
            "planning_revision_limit",
            "the bounded automatic planning revision limit has been reached for the current spec lineage",
        )

    spec_path, approval_path = planning_artifact_paths(project, task_id)
    challenge_path = planning_challenge_path(project, task_id)
    if approval_path.exists() or any(
        receipt.get("planning_spec_hash") == current.digest
        or receipt.get("planning_challenge_hash") == challenge.digest
        for receipt in store.planning_approval_receipts(task_id)
    ):
        raise PlanningRevisionRefusal(
            "approval_exists", "an approved planning artifact cannot be revised automatically"
        )
    if any(
        path.exists() and _is_link_or_junction(path)
        for path in (spec_path, challenge_path, approval_path)
    ):
        raise PlanningRevisionRefusal(
            "unsafe_planning_path", "planning artifacts must not be symlinks or junctions"
        )
    if _spec_file_digest(spec_path) != current.digest or _challenge_file_digest(challenge_path) != challenge.digest:
        raise PlanningRevisionRefusal(
            "planning_revision_race", "the planning artifacts changed before revision publication"
        )

    try:
        history_dir = _validated_planning_history(project, challenge_path, create=True)
    except PlanningChallengeRefreshRefusal as exc:
        raise PlanningRevisionRefusal(exc.code, str(exc)) from exc
    revision_id = uuid.uuid4().hex
    spec_archive = history_dir / f"{spec_path.stem}.{current.digest}.{revision_id}.json"
    challenge_archive = history_dir / f"{challenge_path.stem}.{challenge.digest}.{revision_id}.json"
    pending_spec = history_dir / f"{spec_path.stem}.{revised.digest}.{revision_id}.pending.json"
    intent_payload = {
        "task_id": task_id,
        "from_spec_hash": current.digest,
        "challenge_hash": challenge.digest,
        "to_spec_hash": revised.digest,
        "spec_archive_path": spec_archive.relative_to(project).as_posix(),
        "challenge_archive_path": challenge_archive.relative_to(project).as_posix(),
        "pending_spec_path": pending_spec.relative_to(project).as_posix(),
        "revision_round": revision_count + 1,
        "revision_limit": MAX_AUTOMATIC_PLANNING_REVISIONS,
    }
    intent_event_id = store.add_audit_event(PLANNING_REVISION_INTENT_EVENT, intent_payload)
    try:
        _write_exclusive(
            pending_spec,
            json.dumps(revised_spec_payload, sort_keys=True, separators=(",", ":")),
        )
        os.replace(spec_path, spec_archive)
        os.replace(challenge_path, challenge_archive)
        os.replace(pending_spec, spec_path)
        if (
            _spec_file_digest(spec_path) != revised.digest
            or _spec_file_digest(spec_archive) != current.digest
            or _challenge_file_digest(challenge_archive) != challenge.digest
        ):
            raise PlanningRevisionRefusal(
                "planning_revision_mismatch", "published planning revision does not match its audit intent"
            )
        event_id = store.add_audit_event(
            PLANNING_REVISION_COMPLETED_EVENT,
            {**intent_payload, "intent_event_id": intent_event_id},
        )
    except BaseException:
        if _intent_has_terminal_event(store, task_id, intent_event_id, PLANNING_REVISION_COMPLETED_EVENT):
            raise
        restored = _rollback_planning_revision(
            spec_path=spec_path,
            challenge_path=challenge_path,
            spec_archive=spec_archive,
            challenge_archive=challenge_archive,
            pending_spec=pending_spec,
            from_spec_hash=current.digest,
            challenge_hash=challenge.digest,
            to_spec_hash=revised.digest,
        )
        with suppress(Exception):
            store.add_audit_event(
                PLANNING_REVISION_ABORTED_EVENT,
                {**intent_payload, "intent_event_id": intent_event_id, "restored": restored},
            )
        raise
    return {
        **intent_payload,
        "intent_event_id": intent_event_id,
        "audit_event_id": event_id,
    }


def _write_exclusive(path: Path, text: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PlanningRevisionRefusal(
            "planning_revision_publish_failed",
            f"planning revision could not be staged: {type(exc).__name__}",
        ) from exc


def _rollback_planning_revision(
    *,
    spec_path: Path,
    challenge_path: Path,
    spec_archive: Path,
    challenge_archive: Path,
    pending_spec: Path,
    from_spec_hash: str,
    challenge_hash: str,
    to_spec_hash: str,
) -> bool:
    try:
        if _spec_file_digest(spec_path) == to_spec_hash:
            if pending_spec.exists():
                return False
            os.replace(spec_path, pending_spec)
        if not spec_path.exists() and _spec_file_digest(spec_archive) == from_spec_hash:
            os.replace(spec_archive, spec_path)
        if not challenge_path.exists() and _challenge_file_digest(challenge_archive) == challenge_hash:
            os.replace(challenge_archive, challenge_path)
    except OSError:
        return False
    return (
        _spec_file_digest(spec_path) == from_spec_hash
        and _challenge_file_digest(challenge_path) == challenge_hash
    )


def retire_planning_challenge(
    store: Store,
    project: Path,
    task_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """Retire one non-accepting challenge so the normal runner can challenge the current plan again.

    This is deliberately narrower than deleting a planning artifact. The old bytes are moved to project-local history,
    accepted or approved planning is immutable, active work blocks the action, and a durable audit event names the exact
    retired digest. The next ordinary run creates a new challenge through the configured independent provider.
    """
    if os.environ.get(PROVIDER_PROCESS_ENV):
        raise PlanningChallengeRefreshRefusal(
            "operator_decision",
            "refreshing a planning challenge is an operator decision; it cannot run inside a provider process",
        )
    if not isinstance(task_id, str) or task_id in ("", ".", "..") or Path(task_id).name != task_id or "\\" in task_id:
        raise PlanningChallengeRefreshRefusal("invalid_task", "task id must be a plain identifier")
    reason = " ".join(str(reason).split())
    if not reason:
        raise PlanningChallengeRefreshRefusal("reason_required", "a non-empty refresh reason is required")
    if len(reason) > MAX_TEXT_CHARS:
        raise PlanningChallengeRefreshRefusal(
            "reason_too_long", f"refresh reason is {len(reason)} characters; the limit is {MAX_TEXT_CHARS}"
        )
    entered = False
    try:
        admission = nullcontext()
        database = connected_dispatch_database(store.conn)
        if database is not None:
            admission = stage_dispatch_lock(database, task_id).hold(f"refresh-planning-challenge:{task_id}")
        with admission:
            entered = True
            return _retire_planning_challenge_owned(store, project, task_id, reason)
    except (IntegrationLockTimeout, OSError) as exc:
        if entered:
            raise
        raise PlanningChallengeRefreshRefusal(
            "stage_dispatch_active",
            f"task {task_id} is entering or running a stage; refresh after the current stage finishes",
        ) from exc


def _retire_planning_challenge_owned(
    store: Store,
    project: Path,
    task_id: str,
    reason: str,
) -> dict[str, Any]:
    """Retire the challenge while holding the same task lock used by stage dispatch."""
    reconciled = reconcile_planning_challenge_retirement(store, project, task_id)
    challenge_path = planning_challenge_path(project, task_id)
    if reconciled and not challenge_path.exists():
        recovered = reconciled[-1]
        return {
            "task_id": task_id,
            "retired_challenge_hash": recovered["artifact_digest"],
            "retired_verdict": recovered.get("verdict", "unknown"),
            "reason": "completed a previously authorized interrupted refresh",
            "archive_path": recovered["archive_path"],
            "audit_event_id": recovered["event_id"],
            "next_action": "stagemesh run-window",
        }
    if store.get_task(task_id) is None:
        raise PlanningChallengeRefreshRefusal("task_missing", f"task {task_id} is not present in the StageMesh ledger")
    if store.has_active_claim(task_id):
        raise PlanningChallengeRefreshRefusal(
            "claim_active", f"task {task_id} has an active claim; refresh after it finishes"
        )
    active = sorted(store.active_execution_kinds(task_id))
    if active:
        raise PlanningChallengeRefreshRefusal(
            "execution_active",
            f"task {task_id} has active {', '.join(kind.lower() for kind in active)} execution; refresh after it finishes",
        )

    _, approval_path = planning_artifact_paths(project, task_id)
    if approval_path.exists():
        raise PlanningChallengeRefreshRefusal(
            "approval_exists",
            "the current planning challenge is already bound by an approval artifact; refresh is not allowed",
        )
    challenge_payload = load_planning_challenge(project, task_id)
    if challenge_payload is None:
        code = "challenge_invalid" if challenge_path.exists() else "challenge_missing"
        message = "the current planning challenge is unreadable or malformed" if challenge_path.exists() else "no planning challenge exists"
        raise PlanningChallengeRefreshRefusal(code, message)
    challenge, problems = validate_planning_challenge(challenge_payload)
    if challenge is None:
        raise PlanningChallengeRefreshRefusal("challenge_invalid", "; ".join(problems))
    if challenge.verdict == "accept":
        raise PlanningChallengeRefreshRefusal(
            "challenge_accepted",
            "the current planning challenge is accepted; approve it or revise the planning inputs instead of refreshing it",
        )

    frozen = freeze_planning_challenge(challenge_payload)
    if any(
        approval.get("planning_challenge_hash") == frozen.digest
        for approval in store.planning_approval_receipts(task_id)
    ):
        raise PlanningChallengeRefreshRefusal(
            "approval_exists",
            "the current planning challenge is already bound by a durable approval receipt; refresh is not allowed",
        )
    receipts = _challenge_receipts_for_refresh(store, task_id, frozen.digest)
    if not receipts:
        raise PlanningChallengeRefreshRefusal(
            "challenger_execution_unverified",
            "the current planning challenge is not bound to a successful StageMesh challenger execution",
        )
    review_protocols: list[int | None] = []
    receipt_times: list[float] = []
    for receipt in receipts:
        review_protocol = receipt.get("review_protocol_version")
        if review_protocol is not None and (
            isinstance(review_protocol, bool) or not isinstance(review_protocol, int) or review_protocol < 1
        ):
            raise PlanningChallengeRefreshRefusal(
                "challenger_execution_unverified",
                "the current planning challenge has an invalid review protocol receipt",
            )
        review_protocols.append(review_protocol)
        created_at = receipt.get("_created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)) or created_at <= 0:
            raise PlanningChallengeRefreshRefusal(
                "challenger_execution_unverified",
                "the current planning challenge receipt has no trustworthy creation time",
            )
        receipt_times.append(float(created_at))
    spec_payload, _ = load_planning_artifacts(project, task_id)
    try:
        current_spec_hash = freeze_planning_spec(spec_payload).digest
    except PlanningSpecError as exc:
        raise PlanningChallengeRefreshRefusal(
            "planning_spec_invalid", "the current planning spec must be valid before its challenge can be refreshed"
        ) from exc
    planning_inputs_changed = current_spec_hash != challenge.planning_spec_hash
    if (
        not planning_inputs_changed
        and (
            any(
                protocol is not None and protocol >= PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION
                for protocol in review_protocols
            )
            or any(created_at >= CONTRACT_AWARE_CHALLENGE_CUTOVER_EPOCH for created_at in receipt_times)
        )
    ):
        raise PlanningChallengeRefreshRefusal(
            "challenge_context_unchanged",
            "the current challenge already used the contract-aware review protocol; resolve its decisions or revise the planning inputs",
        )
    history_dir = _validated_planning_history(project, challenge_path, create=True)
    archive_path = history_dir / f"{challenge_path.stem}.{frozen.digest}.{uuid.uuid4().hex}.json"
    intent_payload = {
        "task_id": task_id,
        "artifact_digest": frozen.digest,
        "verdict": challenge.verdict,
        "reason": reason,
        "archive_path": archive_path.relative_to(project).as_posix(),
        "operator_boundary": "operator_cli",
        "from_review_protocol_versions": review_protocols,
        "from_receipt_created_at": receipt_times,
        "to_review_protocol_version": PLANNING_CHALLENGE_REVIEW_PROTOCOL_VERSION,
    }
    intent_event_id = store.add_audit_event(PLANNING_CHALLENGE_RETIRE_INTENT_EVENT, intent_payload)
    try:
        os.replace(challenge_path, archive_path)
        archived_payload = json.loads(archive_path.read_text(encoding="utf-8"))
        if freeze_planning_challenge(archived_payload).digest != frozen.digest:
            raise PlanningChallengeRefreshRefusal(
                "archive_mismatch", "the archived challenge differs from the challenge that was authorized for refresh"
            )
        event_id = store.add_audit_event(
            PLANNING_CHALLENGE_RETIRED_EVENT,
            {
                **intent_payload,
                "intent_event_id": intent_event_id,
            },
        )
    except BaseException:  # Ctrl+C must restore the authoritative artifact too
        if _intent_has_terminal_event(store, task_id, intent_event_id, PLANNING_CHALLENGE_RETIRED_EVENT):
            raise
        restored = False
        if archive_path.exists() and not challenge_path.exists():
            try:
                os.replace(archive_path, challenge_path)
                restored = True
            except OSError:
                pass
        with suppress(Exception):  # the durable intent still exposes an interrupted action
            store.add_audit_event(
                PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT,
                {
                    **intent_payload,
                    "intent_event_id": intent_event_id,
                    "restored": restored,
                },
            )
        raise
    return {
        "task_id": task_id,
        "retired_challenge_hash": frozen.digest,
        "retired_verdict": challenge.verdict,
        "reason": reason,
        "archive_path": archive_path.relative_to(project).as_posix(),
        "audit_event_id": event_id,
        "next_action": "stagemesh run-window",
    }


def _challenge_receipts_for_refresh(
    store: Store, task_id: str, artifact_digest: str
) -> tuple[dict[str, Any], ...]:
    """Return every matching challenger receipt backed by a successful review execution, newest first."""
    receipts: list[dict[str, Any]] = []
    for row in reversed(list(store.task_audit_events(PLANNING_CHALLENGER_RECEIPT_EVENT, task_id))):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("artifact_digest") != artifact_digest
            or payload.get("provider_invoked") is not True
        ):
            continue
        execution = store.conn.execute(
            "SELECT kind, status, task_id, actor FROM executions WHERE id=?", (payload.get("execution_id"),)
        ).fetchone()
        if (
            execution is not None
            and execution["kind"] == "REVIEW"
            and execution["status"] == "SUCCEEDED"
            and execution["task_id"] == task_id
            and execution["actor"] == payload.get("provider")
        ):
            receipts.append(payload)
            receipts[-1]["_created_at"] = row["created_at"]
    return tuple(receipts)


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _validated_planning_history(project: Path, challenge_path: Path, *, create: bool) -> Path:
    runtime_dir = project / Path(PLANNING_ARTIFACT_DIR).parts[0]
    planning_dir = challenge_path.parent
    history_dir = planning_dir / "history"
    if any(_is_link_or_junction(path) for path in (runtime_dir, planning_dir, history_dir)):
        raise PlanningChallengeRefreshRefusal(
            "unsafe_history_path", "planning challenge history must not traverse a symlink or junction"
        )
    if create:
        history_dir.mkdir(parents=True, exist_ok=True)
    if not history_dir.is_dir():
        raise PlanningChallengeRefreshRefusal("unsafe_history_path", "planning challenge history directory is unavailable")
    if (
        runtime_dir.resolve().parent != project.resolve()
        or planning_dir.resolve().parent != runtime_dir.resolve()
        or history_dir.resolve().parent != planning_dir.resolve()
    ):
        raise PlanningChallengeRefreshRefusal(
            "unsafe_history_path", "planning challenge history escaped the project planning directory"
        )
    return history_dir


def reconcile_planning_challenge_retirement(
    store: Store, project: Path, task_id: str
) -> tuple[dict[str, Any], ...]:
    """Finish or abort durable refresh intents left by an interrupted operator process.

    The task's stage-dispatch lock must already be held by the caller. No provider is invoked here.
    """
    terminal_intents: set[str] = set()
    for event_type in (PLANNING_CHALLENGE_RETIRED_EVENT, PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT):
        for row in store.task_audit_events(event_type, task_id):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            intent_id = payload.get("intent_event_id") if isinstance(payload, dict) else None
            if isinstance(intent_id, str):
                terminal_intents.add(intent_id)

    reconciled: list[dict[str, Any]] = []
    challenge_path = planning_challenge_path(project, task_id)
    for row in store.task_audit_events(PLANNING_CHALLENGE_RETIRE_INTENT_EVENT, task_id):
        intent_event_id = str(row["id"])
        if intent_event_id in terminal_intents:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlanningChallengeRefreshRefusal(
                "challenge_refresh_incomplete", "a planning challenge refresh intent is malformed"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("artifact_digest"), str):
            raise PlanningChallengeRefreshRefusal(
                "challenge_refresh_incomplete", "a planning challenge refresh intent is incomplete"
            )
        history_dir = _validated_planning_history(project, challenge_path, create=False)
        raw_archive = payload.get("archive_path")
        if not isinstance(raw_archive, str) or Path(raw_archive).is_absolute():
            raise PlanningChallengeRefreshRefusal(
                "challenge_refresh_incomplete", "a planning challenge refresh archive path is invalid"
            )
        archive_path = project / raw_archive
        if archive_path.parent.resolve() != history_dir.resolve():
            raise PlanningChallengeRefreshRefusal(
                "challenge_refresh_incomplete", "a planning challenge refresh archive escaped its history directory"
            )
        expected_digest = payload["artifact_digest"]
        current_digest = _challenge_file_digest(challenge_path)
        archive_digest = _challenge_file_digest(archive_path)
        terminal_payload = {**payload, "intent_event_id": intent_event_id, "reconciled": True}
        if current_digest == expected_digest and archive_digest is None:
            event_id = store.add_audit_event(
                PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT,
                {**terminal_payload, "restored": True},
            )
            event_type = PLANNING_CHALLENGE_RETIRE_ABORTED_EVENT
        elif current_digest is None and archive_digest == expected_digest:
            event_id = store.add_audit_event(PLANNING_CHALLENGE_RETIRED_EVENT, terminal_payload)
            event_type = PLANNING_CHALLENGE_RETIRED_EVENT
        else:
            raise PlanningChallengeRefreshRefusal(
                "challenge_refresh_incomplete",
                "planning challenge refresh state is ambiguous; the current and archived artifacts do not match its intent",
            )
        reconciled.append({**terminal_payload, "event_id": event_id, "event_type": event_type})
    return tuple(reconciled)


def _challenge_file_digest(path: Path) -> str | None:
    try:
        return freeze_planning_challenge(json.loads(path.read_text(encoding="utf-8"))).digest
    except (OSError, ValueError, PlanningSpecError):
        return None


def _spec_file_digest(path: Path) -> str | None:
    try:
        return freeze_planning_spec(json.loads(path.read_text(encoding="utf-8"))).digest
    except (OSError, ValueError, PlanningSpecError):
        return None


def reconcile_automatic_planning_revision(
    store: Store, project: Path, task_id: str
) -> tuple[dict[str, Any], ...]:
    """Finish or roll back an interrupted generated-plan revision without invoking a provider."""
    terminal_intents: set[str] = set()
    for event_type in (PLANNING_REVISION_COMPLETED_EVENT, PLANNING_REVISION_ABORTED_EVENT):
        for row in store.task_audit_events(event_type, task_id):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            intent_id = payload.get("intent_event_id") if isinstance(payload, dict) else None
            if isinstance(intent_id, str):
                terminal_intents.add(intent_id)

    reconciled: list[dict[str, Any]] = []
    spec_path, _approval_path = planning_artifact_paths(project, task_id)
    challenge_path = planning_challenge_path(project, task_id)
    for row in store.task_audit_events(PLANNING_REVISION_INTENT_EVENT, task_id):
        intent_event_id = str(row["id"])
        if intent_event_id in terminal_intents:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlanningRevisionRefusal(
                "planning_revision_incomplete", "a planning revision intent is malformed"
            ) from exc
        required = {
            "from_spec_hash",
            "challenge_hash",
            "to_spec_hash",
            "spec_archive_path",
            "challenge_archive_path",
            "pending_spec_path",
        }
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
            raise PlanningRevisionRefusal(
                "planning_revision_incomplete", "a planning revision intent is incomplete"
            )
        try:
            history_dir = _validated_planning_history(project, challenge_path, create=False)
        except PlanningChallengeRefreshRefusal as exc:
            raise PlanningRevisionRefusal(exc.code, str(exc)) from exc
        artifact_paths: dict[str, Path] = {}
        for key in ("spec_archive_path", "challenge_archive_path", "pending_spec_path"):
            raw = payload[key]
            if Path(raw).is_absolute():
                raise PlanningRevisionRefusal(
                    "planning_revision_incomplete", "a planning revision history path is invalid"
                )
            artifact = project / raw
            if artifact.parent.resolve() != history_dir.resolve() or (
                artifact.exists() and _is_link_or_junction(artifact)
            ):
                raise PlanningRevisionRefusal(
                    "planning_revision_incomplete", "a planning revision artifact escaped its history directory"
                )
            artifact_paths[key] = artifact

        spec_archive = artifact_paths["spec_archive_path"]
        challenge_archive = artifact_paths["challenge_archive_path"]
        pending_spec = artifact_paths["pending_spec_path"]
        terminal_payload = {**payload, "intent_event_id": intent_event_id, "reconciled": True}
        completed = (
            _spec_file_digest(spec_path) == payload["to_spec_hash"]
            and _spec_file_digest(spec_archive) == payload["from_spec_hash"]
            and _challenge_file_digest(challenge_archive) == payload["challenge_hash"]
            and not challenge_path.exists()
        )
        if completed:
            event_type = PLANNING_REVISION_COMPLETED_EVENT
            event_id = store.add_audit_event(event_type, terminal_payload)
        else:
            restored = _rollback_planning_revision(
                spec_path=spec_path,
                challenge_path=challenge_path,
                spec_archive=spec_archive,
                challenge_archive=challenge_archive,
                pending_spec=pending_spec,
                from_spec_hash=payload["from_spec_hash"],
                challenge_hash=payload["challenge_hash"],
                to_spec_hash=payload["to_spec_hash"],
            )
            if not restored:
                raise PlanningRevisionRefusal(
                    "planning_revision_incomplete",
                    "planning revision state is ambiguous; the current and archived artifacts do not match its intent",
                )
            event_type = PLANNING_REVISION_ABORTED_EVENT
            event_id = store.add_audit_event(
                event_type, {**terminal_payload, "restored": True}
            )
        reconciled.append({**terminal_payload, "event_id": event_id, "event_type": event_type})
    return tuple(reconciled)


def _intent_has_terminal_event(store: Store, task_id: str, intent_event_id: str, event_type: str) -> bool:
    for row in store.task_audit_events(event_type, task_id):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("intent_event_id") == intent_event_id:
            return True
    return False


def record_planning_approval(
    store: Store,
    project: Path,
    task_id: str,
    *,
    authority: ApprovalAuthority | None,
    require_challenge: bool,
) -> dict[str, Any]:
    """Record an accountable approval for the task's current frozen plan, then project it as the version-2 artifact.

    ``authority`` and ``require_challenge`` must come from the operator's protected project configuration; the approver
    is never an argument. The durable receipt is appended first, the artifact is written atomically afterwards, and a
    repeat with identical inputs reuses the existing receipt. Nothing the plan is built from is modified.
    """
    if os.environ.get(PROVIDER_PROCESS_ENV):
        raise PlanningApprovalRefusal(
            "operator_decision", "planning approval is an operator decision; it cannot run inside a provider process"
        )
    if not isinstance(task_id, str) or task_id in ("", ".", "..") or Path(task_id).name != task_id or "\\" in task_id:
        raise PlanningApprovalRefusal("invalid_task", "task id must be a plain identifier")
    if authority is None:
        raise PlanningApprovalRefusal(
            APPROVAL_AUTHORITY_MISSING,
            "no approval_authority is configured under planning_gate; accountable approval is not enabled",
            NEXT_REPAIR_APPROVAL_AUTHORITY,
        )
    contract = store.task_contract(task_id)
    baseline = store.task_baseline(task_id)
    if contract is None or baseline is None:
        raise PlanningApprovalRefusal(
            "task_identity_unbound", f"task {task_id} has no bound contract and baseline to approve against"
        )
    contract_hash = str(contract["digest"])
    active = sorted(store.active_execution_kinds(task_id) & _APPROVAL_BLOCKING_EXECUTIONS)
    if active:
        raise PlanningApprovalRefusal(
            "execution_active",
            f"task {task_id} has active {', '.join(kind.lower() for kind in active)} execution; approve after it finishes",
        )

    spec_payload, _ = load_planning_artifacts(project, task_id)
    challenge_payload = load_planning_challenge(project, task_id)
    gate = evaluate_planning_gate(
        enabled=True,
        spec_payload=spec_payload,
        approval=None,
        contract_hash=contract_hash,
        baseline_sha=baseline,
        require_challenge=require_challenge,
        challenge_payload=challenge_payload,
    )
    if gate.next_action != NEXT_APPROVE_SPEC:  # only "everything valid, approval missing" may proceed
        stale = gate.next_action in (NEXT_REFRESH_SPEC, NEXT_REFRESH_CHALLENGE)
        raise PlanningApprovalRefusal(
            APPROVAL_BINDING_STALE if stale else PLANNING_INPUTS_INVALID, gate.reason, gate.next_action
        )

    bindings: dict[str, Any] = {
        "version": APPROVAL_ARTIFACT_VERSION,
        "decision": "approve",
        "task_id": task_id,
        "planning_spec_hash": freeze_planning_spec(spec_payload).digest,
        "planning_challenge_hash": freeze_planning_challenge(challenge_payload).digest if require_challenge else None,
        "contract_hash": contract_hash,
        "baseline_sha": baseline,
        "authority_id": authority.authority_id,
        "authority_hash": authority.digest,
        "approver_id": authority.approver_id,
    }
    receipt = next(
        (
            item
            for item in store.planning_approval_receipts(task_id)
            if set(item) == _RECEIPT_KEYS
            and item["event"] == APPROVAL_RECEIPT_EVENT
            and isinstance(item["receipt_id"], str)
            and all(item[key] == value for key, value in bindings.items())
        ),
        None,
    )
    recorded = receipt is None
    if receipt is None:
        record = {**bindings, "receipt_id": f"approval-{uuid.uuid4().hex}"}
        _, problems = validate_approval_artifact(record, require_challenge=require_challenge)
        if problems:
            raise PlanningApprovalRefusal(APPROVAL_MALFORMED, "; ".join(problems), NEXT_REPAIR_APPROVAL_AUTHORITY)
        store.record_planning_approval(record)
        receipt = next(
            (item for item in store.planning_approval_receipts(task_id) if item.get("receipt_id") == record["receipt_id"]),
            None,
        )
        if receipt is None:
            raise PlanningApprovalRefusal("receipt_not_durable", "approval receipt could not be read back; no artifact written")
    artifact = {**bindings, "receipt_id": receipt["receipt_id"]}
    artifact_path = planning_artifact_paths(project, task_id)[1]
    written = _write_artifact_atomic(artifact_path, artifact)
    return {
        **artifact,
        "created_at": receipt["created_at"],
        "approval_path": artifact_path.relative_to(project).as_posix(),
        "receipt_recorded": recorded,
        "artifact_written": written,
    }


def _write_artifact_atomic(path: Path, artifact: dict[str, Any]) -> bool:
    """Write the artifact via a same-directory temp file and replace; an identical existing file is left untouched."""
    try:
        if json.loads(path.read_text(encoding="utf-8")) == artifact:
            return False
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


# --- Repeated enforcement at lifecycle boundaries -----------------------------------------------------------------
BOUNDARY_PICKUP = "task_pickup"
BOUNDARY_IMPLEMENTATION = "implementation_dispatch"
BOUNDARY_RECOVERY = "implementation_recovery"
BOUNDARY_REVIEW = "review_dispatch"
BOUNDARY_INTEGRATION = "integration"
BOUNDARY_PROMOTION = "promotion"
APPROVAL_REFUSED_EVENT = "planning_approval.refused"
APPROVAL_PROMOTION_EVENT = "planning_approval.promotion_evidence"

_PLANNER_RECEIPT_EVENT = "planning.spec_produced"
_CHALLENGER_RECEIPT_EVENT = PLANNING_CHALLENGER_RECEIPT_EVENT
_DERIVATION_EVENT = "candidate.implementation_identity_derived"
_DERIVATION_KINDS = frozenset({"rebase", "reattribution", "reconstruction"})
_INTEGRATION_DERIVATION_EVENTS = {
    "integration.rebased": "rebase",
    "integration.reattributed": "reattribution",
    "integration.reconstructed": "reconstruction",
}
_MAX_LINEAGE = 32
_DEFAULT_ROLES = {
    BOUNDARY_PICKUP: (),
    BOUNDARY_IMPLEMENTATION: ("implementer",),
    BOUNDARY_RECOVERY: ("implementer",),
    BOUNDARY_REVIEW: ("implementer", "reviewer"),
    BOUNDARY_INTEGRATION: ("implementer", "reviewer"),
    BOUNDARY_PROMOTION: ("implementer", "reviewer"),
}


def protected_approval_policy(project: Path) -> tuple[ApprovalAuthority | None, bool, AccountableApprovalDecision | None]:
    """Re-read the operator's project-root configuration (never a candidate worktree) on every call.

    Returns (authority, require_challenge, refusal). No authority and no refusal means the legacy path applies; an
    unreadable configuration that names ``approval_authority`` fails closed instead of silently downgrading.
    """
    from .config import ConfigValidationError, load_config

    try:
        config = load_config(Path(project))
    except ConfigValidationError:
        try:
            names_authority = "approval_authority" in (Path(project) / ".stagemesh" / "config.json").read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            names_authority = False
        if names_authority:
            return None, False, _approval_block(
                APPROVAL_AUTHORITY_MISSING, "project configuration naming approval_authority is invalid", NEXT_REPAIR_APPROVAL_AUTHORITY
            )
        return None, False, None
    return config.approval_authority, config.require_plan_challenge, None


def check_accountable_approval(
    store: Store,
    project: Path,
    task_id: str,
    boundary: str,
    *,
    candidate_sha: str | None = None,
    implementers: Iterable[str] = (),
    reviewers: Iterable[str] = (),
    required_roles: tuple[str, ...] | None = None,
    contract_hash: str | None = None,
    baseline_sha: str | None = None,
    record_refusal: bool = True,
) -> AccountableApprovalDecision | None:
    """Evaluate accountable approval for one lifecycle boundary; ``None`` means legacy (no approval_authority).

    Every call re-reads the protected project-root configuration, planning artifacts, and approval receipts. Identities
    come only from successful StageMesh execution receipts plus the actors the caller is about to select. A refusal is
    recorded as durable evidence and changes nothing else: no task, candidate, execution, or worktree state.
    """
    authority, require_challenge, refusal = protected_approval_policy(project)
    if refusal is None and authority is None:
        return None
    decision = refusal or _boundary_decision(
        store, Path(project), task_id, boundary, authority, require_challenge,
        candidate_sha, tuple(implementers), tuple(reviewers), required_roles, contract_hash, baseline_sha,
    )
    if not decision.allowed and record_refusal:
        _record_refusal(store, task_id, boundary, candidate_sha, decision)
    return decision


def _boundary_decision(
    store: Store,
    project: Path,
    task_id: str,
    boundary: str,
    authority: ApprovalAuthority | None,
    require_challenge: bool,
    candidate_sha: str | None,
    implementers: tuple[str, ...],
    reviewers: tuple[str, ...],
    required_roles: tuple[str, ...] | None,
    contract_hash: str | None,
    baseline_sha: str | None,
) -> AccountableApprovalDecision:
    if contract_hash is None:
        contract = store.task_contract(task_id)
        contract_hash = str(contract["digest"]) if contract is not None else None
    if baseline_sha is None:
        baseline_sha = store.task_baseline(task_id)
    if contract_hash is None or baseline_sha is None:
        return _approval_block(APPROVAL_BINDING_STALE, "task contract or baseline is not bound", NEXT_OBTAIN_APPROVAL)
    if candidate_sha is not None:
        binding = store.contract_binding(task_id, candidate_sha)
        if (
            binding is None
            or str(binding["digest"]) != contract_hash
            or (binding["baseline_sha"] is not None and str(binding["baseline_sha"]) != baseline_sha)
        ):
            return _approval_block(
                APPROVAL_BINDING_STALE, "candidate contract binding differs from the approved contract or baseline", NEXT_OBTAIN_APPROVAL
            )
    spec_payload, approval = load_planning_artifacts(project, task_id)
    challenge_payload = load_planning_challenge(project, task_id)
    spec_digest = challenge_digest = None
    spec, _ = validate_planning_spec(spec_payload)
    if spec is not None:
        spec_digest = freeze_planning_spec(spec_payload).digest
        if require_challenge and validate_planning_challenge(challenge_payload)[0] is not None:
            challenge_digest = freeze_planning_challenge(challenge_payload).digest
    planner = _trusted_planning_actor(store, _PLANNER_RECEIPT_EVENT, task_id, spec_digest)
    challenger = _trusted_planning_actor(store, _CHALLENGER_RECEIPT_EVENT, task_id, challenge_digest)
    lineage = _candidate_lineage(store, task_id, candidate_sha) if candidate_sha is not None else None
    if candidate_sha is not None and lineage is None:
        return _approval_block(
            APPROVAL_EXECUTION_IDENTITY_MISSING,
            "candidate implementation lineage is ambiguous, cyclic, or exceeds the supported depth",
            NEXT_CHOOSE_ACTOR,
        )
    actors = {*implementers, *_successful_actors(store, task_id, "IMPLEMENTATION", lineage)}
    reviewer_actors = {*reviewers}
    if candidate_sha is not None:
        reviewer_actors.update(_successful_actors(store, task_id, "REVIEW", {candidate_sha}))
    roles = _DEFAULT_ROLES[boundary] if required_roles is None else required_roles
    receipts = store.planning_approval_receipts(task_id)
    decision = AccountableApprovalDecision(False, APPROVAL_EXECUTION_IDENTITY_MISSING, "no reviewer identity", NEXT_CHOOSE_ACTOR)
    reviewer_choices: tuple[str | None, ...] = tuple(sorted(reviewer_actors)) or (None,)
    for reviewer in reviewer_choices:  # the approver must differ from every successful reviewer
        decision = evaluate_accountable_approval(
            task_id=task_id,
            authority=authority,
            spec_payload=spec_payload,
            approval=approval,
            receipts=receipts,
            contract_hash=contract_hash,
            baseline_sha=baseline_sha,
            require_challenge=require_challenge,
            challenge_payload=challenge_payload,
            identities=ExecutionIdentities(planner, challenger, tuple(sorted(actors)), reviewer),
            required_roles=roles,
        )
        if not decision.allowed:
            return decision
    return decision


def _trusted_planning_actor(store: Store, event_type: str, task_id: str, artifact_digest: str | None) -> str | None:
    """The provider of the newest planner/challenger receipt whose successful StageMesh execution row agrees."""
    if artifact_digest is None:
        return None
    for row in reversed(list(store.task_audit_events(event_type, task_id))):
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("artifact_digest") != artifact_digest or payload.get("provider_invoked") is not True:
            continue
        execution = store.conn.execute(
            "SELECT kind, status, task_id, actor FROM executions WHERE id=?", (payload.get("execution_id"),)
        ).fetchone()
        if (
            execution is not None
            and execution["kind"] == "REVIEW"
            and execution["status"] == "SUCCEEDED"
            and execution["task_id"] == task_id
            and execution["actor"] == payload.get("provider")
        ):
            return str(execution["actor"])
    return None


def _candidate_lineage(store: Store, task_id: str, candidate_sha: str) -> set[str] | None:
    """The candidate and its unambiguous ancestors through trusted StageMesh derivation events."""
    derived: dict[str, set[str]] = {}
    event_kinds = {_DERIVATION_EVENT: None, **_INTEGRATION_DERIVATION_EVENTS}
    for event_type, expected_kind in event_kinds.items():
        for row in store.task_audit_events(event_type, task_id):
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("kind") if expected_kind is None else expected_kind
            child, parent = payload.get("candidate_sha"), payload.get("from_candidate")
            if (
                kind not in _DERIVATION_KINDS
                or not isinstance(child, str)
                or not isinstance(parent, str)
                or child == parent
                or _SHA.fullmatch(child) is None
                or _SHA.fullmatch(parent) is None
            ):
                continue
            derived.setdefault(child, set()).add(parent)

    lineage: set[str] = set()
    resolving: set[str] = set()
    invalid = False

    def visit(sha: str) -> None:
        nonlocal invalid
        if invalid or sha in lineage:
            return
        if sha in resolving or len(lineage) + len(resolving) >= _MAX_LINEAGE:
            invalid = True
            return
        parents = derived.get(sha, set())
        if len(parents) > 1:
            invalid = True
            return
        resolving.add(sha)
        for parent in parents:
            visit(parent)
        resolving.remove(sha)
        lineage.add(sha)

    visit(candidate_sha)
    return None if invalid else lineage


def _successful_actors(store: Store, task_id: str, kind: str, shas: set[str] | None) -> set[str]:
    """Actors of this task's succeeded executions of ``kind``; ``shas`` limits them to those candidates (None: any)."""
    rows = store.conn.execute(
        "SELECT actor, candidate_sha FROM executions WHERE task_id=? AND kind=? AND status='SUCCEEDED' AND actor IS NOT NULL",
        (task_id, kind),
    )
    return {str(row["actor"]) for row in rows if shas is None or row["candidate_sha"] in shas}


def _record_refusal(
    store: Store, task_id: str, boundary: str, candidate_sha: str | None, decision: AccountableApprovalDecision
) -> None:
    payload = {
        "task_id": task_id,
        "boundary": boundary,
        "candidate_sha": candidate_sha,
        "reason": decision.reason,
        "detail": decision.detail[:300],
        "next_action": decision.next_action,
    }
    rows = list(store.task_audit_events(APPROVAL_REFUSED_EVENT, task_id))
    if rows:
        try:
            if json.loads(rows[-1]["payload"]) == payload:
                return  # an identical standing refusal is already durable
        except ValueError:
            pass
    store.add_audit_event(APPROVAL_REFUSED_EVENT, payload)


def refusal_detail(decision: AccountableApprovalDecision, boundary: str, candidate_sha: str | None = None) -> dict[str, Any]:
    """Stable machine-readable refusal for run summaries; no prompts, credentials, or provider output."""
    return {
        "boundary": boundary,
        "reason": decision.reason,
        "detail": decision.detail,
        "next_action": decision.next_action,
        "candidate_sha": candidate_sha,
    }


def record_promotion_evidence(
    store: Store, task_id: str, candidate_sha: str, decision: AccountableApprovalDecision, boundary: str = BOUNDARY_PROMOTION
) -> None:
    """Bind the accepted approval receipt to the exact candidate SHA being promoted; identical evidence is not duplicated."""
    payload = {
        "task_id": task_id,
        "candidate_sha": candidate_sha,
        "receipt_id": decision.receipt_id,
        "boundary": boundary,
    }
    for row in store.task_audit_events(APPROVAL_PROMOTION_EVENT, task_id):
        try:
            if json.loads(row["payload"]) == payload:
                return
        except ValueError:
            continue
    store.add_audit_event(APPROVAL_PROMOTION_EVENT, payload)


def planner_scope_violations(changed_paths: list[str] | tuple[str, ...], task_id: str) -> tuple[str, ...]:
    """A planner may only write its own spec artifact; anything else (production sources, approval) is a violation."""
    allowed = f"{PLANNING_ARTIFACT_DIR}/{task_id}.spec.json"
    return tuple(path for path in changed_paths if path.replace("\\", "/").removeprefix("./") != allowed)


def plan_challenger_scope_violations(
    changed_paths: list[str] | tuple[str, ...], task_id: str
) -> tuple[str, ...]:
    """A plan challenger may write only its challenge, never spec, approval, or source."""
    allowed = f"{PLANNING_ARTIFACT_DIR}/{task_id}.challenge.json"
    return tuple(
        path
        for path in changed_paths
        if path.replace("\\", "/").removeprefix("./") != allowed
    )
