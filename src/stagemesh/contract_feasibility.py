"""Can this contract be satisfied at all? Decided before a provider runs, from requirements the repository itself declares.

A task can be admitted with `allowed_files` that cannot hold a change the project's own rules demand (for example an "Engineering
Impact" record that must live in a design document the contract does not allow). The provider then burns attempts and reviewers
disagree about a requirement nobody can meet. This module reconciles the two before execution and reports one precise conflict.

Requirement sources, strongest first:

* `structured`   `.stagemesh/policy/required-changes.json`, a project-owned list of {id, kind, description, paths, applies_to}.
                 Authoritative: a conflict blocks admission.
* `contract`     the contract's own acceptance criteria, invariants and objective: a sentence that says a named path
                 "must"/"shall"/"required" be updated. A contract that contradicts itself is a conflict.
* `spec-reference` requirements discovered in cited specs (`spec_refs`) are advisory. A cited authority governs the work but does
                 not imply that every repository path mentioned anywhere in that specification must be edited by this task.
* `policy-prose` unconditional "must/required ... update/add/record ... `path`" sentences in the root policy documents
                 (AGENTS.md, CONTRIBUTING.md). A sentence with a condition ("if", "when", "only", "unless", "for any change to")
                 is advisory because whether it applies cannot be decided here; advisories are reported, never blocking.

Nothing here widens a contract, accepts an alternative evidence location or creates a task. The resolution options name the
existing, authorised mechanisms and leave the decision to whoever owns the policy.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import ChangeContract

POLICY_FILE = Path(".stagemesh") / "policy" / "required-changes.json"
POLICY_DOCUMENTS = ("AGENTS.md", "CONTRIBUTING.md")
MAX_DOCUMENT_BYTES = 200_000

_MODAL = re.compile(r"\b(must|shall|required|requires?|mandatory|need(?:s)? to)\b", re.IGNORECASE)
_VERB = re.compile(r"\b(update|updated|add|added|append|record|recorded|document|documented|include|included|write|written|create|created|contain|contains)\b", re.IGNORECASE)
_CONDITION = re.compile(r"\b(if|when|whenever|unless|only|except|optional|may|where|for any change to|for changes to|touching)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(not|never|no|without|neither|nor|avoid|forbidden|don't|doesn't)\b", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"`([^`\s]+)`|\"([^\"\s]+/[^\"\s]*)\"")
_DOC_KIND = re.compile(r"\b(doc|docs|document|documentation|record|adr|design|readme|changelog|impact)\b", re.IGNORECASE)
_TEST_KIND = re.compile(r"\b(test|tests|spec)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str  # documentation | implementation | review
    description: str
    paths: tuple[str, ...]
    source: str
    origin: str  # structured | contract | spec-reference | policy-prose
    applies_to: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "description": self.description, "paths": list(self.paths),
            "source": self.source, "origin": self.origin, **({"applies_to": list(self.applies_to)} if self.applies_to else {}),
        }


@dataclass(frozen=True)
class Conflict:
    requirement: Requirement
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"requirement": self.requirement.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class FeasibilityReport:
    conflicts: tuple[Conflict, ...] = ()
    advisories: tuple[Requirement, ...] = ()

    @property
    def feasible(self) -> bool:
        return not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "advisories": [item.to_dict() for item in self.advisories],
        }

    def message(self, task_id: str, contract: ChangeContract) -> str:
        """One blocker: the conflicting requirements and the legitimate ways out."""
        allowed = ", ".join(contract.allowed_files) or "(none)"
        lines = [f"task {task_id} cannot satisfy its mandatory requirements within its contract (allowed_files: {allowed}):"]
        for number, conflict in enumerate(self.conflicts, 1):
            requirement = conflict.requirement
            lines.append(
                f"  {number}. [{requirement.kind}] {requirement.description} (required by {requirement.source}) must change "
                f"{', '.join(requirement.paths)}; {conflict.reason}"
            )
        lines.append(
            "Resolve by one of: (a) widen allowed_files to include the required path with an audited "
            f"`stagemesh rebind-contract --task {task_id} --validate` approved by the contract owner; (b) schedule the "
            "required change as a narrowly scoped prerequisite task through the existing planning flow (`stagemesh plan`/`queue`) "
            "so it lands first under its own contract; (c) have the policy owner record an explicit exception in "
            ".stagemesh/policy/required-changes.json. StageMesh did not widen the contract, accept an alternative location "
            "or create a task, and no implementation attempt was made."
        )
        return "\n".join(lines)


def _normalise(path: str) -> str:
    path = path.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    return path + "**" if path.endswith("/") else path


def _literal_prefix(pattern: str) -> str:
    return re.split(r"[*?\[]", pattern, maxsplit=1)[0]


def patterns_overlap(left: str, right: str) -> bool:
    """True when some path can match both globs (conservatively: by containment, literal prefix or fnmatch in either direction)."""
    left, right = _normalise(left), _normalise(right)
    if fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left):
        return True
    a, b = _literal_prefix(left), _literal_prefix(right)
    return a.startswith(b) if len(a) >= len(b) else b.startswith(a)


def _covered(requirement_path: str, patterns: tuple[str, ...]) -> bool:
    return any(patterns_overlap(requirement_path, pattern) for pattern in patterns)


def _fully_inside(requirement_path: str, patterns: tuple[str, ...]) -> bool:
    candidate = _normalise(requirement_path)
    return any(fnmatch.fnmatchcase(candidate, _normalise(pattern)) for pattern in patterns)


def _policy_payload(project: Path) -> dict[str, Any]:
    path = project / POLICY_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _structured(payload: dict[str, Any]) -> list[Requirement]:
    items = payload.get("requirements")
    found: list[Requirement] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("paths"), list) or not item["paths"]:
            continue
        if item.get("waived") is True:  # an explicit, owner-recorded exception
            continue
        found.append(
            Requirement(
                str(item.get("id") or f"requirement-{len(found) + 1}"),
                str(item.get("kind") or "documentation"),
                str(item.get("description") or item.get("id") or "required change"),
                tuple(_normalise(str(p)) for p in item["paths"] if str(p).strip()),
                f"{POLICY_FILE.as_posix()}",
                "structured",
                tuple(_normalise(str(p)) for p in item.get("applies_to", []) if str(p).strip()),
            )
        )
    return found


def _kind_of(sentence: str) -> str:
    if _DOC_KIND.search(sentence):
        return "documentation"
    if _TEST_KIND.search(sentence):
        return "implementation"
    return "implementation"


def _sentences(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        for sentence in re.split(r"(?<=[.;])\s+", line.strip()):
            if sentence:
                found.append((number, sentence))
    return found


_KNOWN_SUFFIX = re.compile(r"\.(?:md|rst|txt|json|ya?ml|toml|py|js|ts|tsx|jsx|cfg|ini)$", re.IGNORECASE)


def _path_like(token: str, project: Path | None = None) -> bool:
    """A token that can name a file in this repository: not a URL, route or `a/b` phrase."""
    token = token.strip().replace("\\", "/")
    if not token or token.startswith(("http:", "https:", "-", "/", "~")) or ".." in token.split("/") or re.search(r"\s", token):
        return False
    if token.endswith("/"):
        return project is None or (project / token.rstrip("/")).is_dir() or token.split("/")[0] in {"docs", "doc", "design"}
    if _KNOWN_SUFFIX.search(token):
        return True
    first = token.split("/")[0]
    return "/" in token and project is not None and (project / first).is_dir()


def _from_text(text: str, source: str, origin: str, project: Path | None = None) -> tuple[list[Requirement], list[Requirement]]:
    blocking: list[Requirement] = []
    advisory: list[Requirement] = []
    for number, sentence in _sentences(text):
        if not (_MODAL.search(sentence) and _VERB.search(sentence)) or _NEGATION.search(sentence):
            continue
        paths = tuple(
            dict.fromkeys(_normalise(a or b) for a, b in _PATH_TOKEN.findall(sentence) if _path_like(a or b, project))
        )
        if not paths:
            continue
        requirement = Requirement(
            f"{Path(source).name}:{number}", _kind_of(sentence), sentence.strip()[:200], paths, f"{source}:{number}", origin,
        )
        conditional = bool(_CONDITION.search(sentence))
        # Without a documentation/record cue and without an existing file, "must create X" may describe runtime output.
        speculative = not _DOC_KIND.search(sentence) and not any((project / p).exists() for p in paths if project is not None)
        (advisory if conditional or speculative else blocking).append(requirement)
    return blocking, advisory


def _read(path: Path, root: Path | None = None) -> str:
    try:
        if root is not None and not path.resolve().is_relative_to(root.resolve()):
            return ""  # spec refs and policy documents are only read from inside the project
        if path.is_file() and path.stat().st_size <= MAX_DOCUMENT_BYTES:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def discover_requirements(project: Path, contract: ChangeContract) -> tuple[list[Requirement], list[Requirement]]:
    """(blocking requirements, advisory requirements) discoverable without running anything."""
    payload = _policy_payload(project)
    blocking = _structured(payload)
    advisory: list[Requirement] = []
    own = [("objective", contract.objective), *((f"acceptance_criteria[{i}]", t) for i, t in enumerate(contract.acceptance_criteria)),
           *((f"invariants[{i}]", t) for i, t in enumerate(contract.invariants))]
    for label, text in own:
        found, soft = _from_text(text, f"contract {label}", "contract", project)
        blocking.extend(found)
        advisory.extend(soft)
    for ref in contract.spec_refs:
        found, soft = _from_text(_read(project / ref, project), ref, "spec-reference", project)
        advisory.extend(found)
        advisory.extend(soft)
    for name in POLICY_DOCUMENTS:
        found, soft = _from_text(_read(project / name, project), name, "policy-prose", project)
        blocking.extend(found)
        advisory.extend(soft)
    # The project owner can dismiss a prose-derived requirement that is not really one (a false positive) by its id or source.
    ignored = {str(x) for x in payload.get("ignore", [])} if isinstance(payload.get("ignore"), list) else set()
    keep = lambda item: item.id not in ignored and item.source not in ignored  # noqa: E731
    return [item for item in blocking if keep(item)], [item for item in advisory if keep(item)]


def _applies(requirement: Requirement, contract: ChangeContract) -> bool:
    if not requirement.applies_to:
        return True
    return any(patterns_overlap(scope, allowed) for scope in requirement.applies_to for allowed in contract.allowed_files)


def check_contract_feasibility(project: Path, contract: ChangeContract) -> FeasibilityReport:
    """Reconcile `allowed_files` with the mandatory requirements discoverable in `project`. Pure and deterministic."""
    blocking, advisory = discover_requirements(project, contract)
    forbidden = contract.forbidden_files + contract.exclusions + contract.protected_files
    conflicts: list[Conflict] = []
    for requirement in blocking:
        if not _applies(requirement, contract):
            continue
        reasons: list[str] = []
        reachable = [path for path in requirement.paths if _covered(path, contract.allowed_files)]
        if not reachable:
            reasons.append("none of those paths is within allowed_files")
        elif all(_fully_inside(path, forbidden) for path in reachable):
            reasons.append("every allowed path for it is forbidden, excluded or protected by the contract")
        if reasons:
            conflicts.append(Conflict(requirement, "; ".join(reasons)))
    return FeasibilityReport(tuple(conflicts), tuple(advisory))
