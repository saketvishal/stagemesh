"""Bounded planning artifact: validate a structured implementation spec and freeze its identity.

The spec cites project requirement owners through ``spec_refs`` instead of copying them. Freezing only computes an
identity for consumers; it never approves anything and never touches production files.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ContractError, _relative_paths

MAX_ITEMS = 20
MAX_TEXT_CHARS = 500
MAX_CANONICAL_CHARS = 8000
PLANNING_SPEC_VERSION = 1
RESERVED_PATH_ROOTS = (".git", ".stagemesh")

_LIST_FIELDS = ("behavior", "non_goals", "affected_paths", "acceptance_checks")
_REQUIRED_LISTS = ("behavior", "affected_paths", "acceptance_checks")  # non_goals may be legitimately empty
_FIELDS = frozenset(_LIST_FIELDS + ("decisions", "spec_refs", "contract_hash", "baseline_sha"))
_CONTRACT_HASH = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class PlanningSpecError(ValueError):
    def __init__(self, problems: tuple[str, ...]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


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


def validate_planning_spec(payload: Any) -> tuple[PlanningSpec | None, tuple[str, ...]]:
    """Return the parsed spec and no problems, or None and every problem found."""
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


def load_planning_artifacts(project: Path, task_id: str) -> tuple[Any, Any]:
    """Return (spec payload, approval payload); a missing or unreadable file is None."""
    loaded: list[Any] = []
    for path in planning_artifact_paths(project, task_id):
        try:
            loaded.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            loaded.append(None)
    return loaded[0], loaded[1]


def evaluate_planning_gate(
    *, enabled: bool, spec_payload: Any, approval: Any, contract_hash: str, baseline_sha: str
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
    if not (isinstance(approval, dict) and approval.get("approved") is True and approval.get("planning_spec_hash") == digest):
        return PlanningGateDecision(False, NEXT_APPROVE_SPEC, f"planning spec {digest[:12]} has no matching approval")
    return PlanningGateDecision(True, approved_scope=spec.affected_paths)


def approved_scope(project: Path, task_id: str, contract_hash: str) -> tuple[str, ...]:
    """Affected paths of an approved, contract-current spec; empty when there is no valid approved evidence."""
    spec_payload, approval = load_planning_artifacts(project, task_id)
    spec, _ = validate_planning_spec(spec_payload)
    if spec is None:
        return ()
    decision = evaluate_planning_gate(
        enabled=True, spec_payload=spec_payload, approval=approval, contract_hash=contract_hash, baseline_sha=spec.baseline_sha
    )
    return decision.approved_scope


def planner_scope_violations(changed_paths: list[str] | tuple[str, ...], task_id: str) -> tuple[str, ...]:
    """A planner may only write its own spec artifact; anything else (production sources, approval) is a violation."""
    allowed = f"{PLANNING_ARTIFACT_DIR}/{task_id}.spec.json"
    return tuple(path for path in changed_paths if path.replace("\\", "/").removeprefix("./") != allowed)
