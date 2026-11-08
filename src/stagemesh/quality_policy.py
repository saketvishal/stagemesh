"""Trusted, provider-neutral repository quality gates for managed coding tasks.

The policy is loaded from the integration checkout, never from the candidate
worktree. A candidate can therefore propose a policy change, but cannot use
that proposed change to weaken the checks applied to itself.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .contracts import ChangeContract, GateCommand

POLICY_PATH = Path(".stagemesh/quality-policy.json")
POLICY_SCHEMA_VERSION = 1
GATE_CATEGORIES = frozenset({"lint", "typecheck", "test", "dependency"})
GATE_TIERS = frozenset({"fast", "commit"})


class QualityPolicyError(ValueError):
    """The configured policy is unreadable, ambiguous, or unsafe."""


def _normal(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normal(path)
    for pattern in patterns:
        variants = {_normal(pattern)}
        while any("**/" in variant for variant in variants):
            expanded = {variant.replace("**/", "", 1) for variant in variants if "**/" in variant}
            if expanded.issubset(variants):
                break
            variants.update(expanded)
        if any(fnmatch.fnmatchcase(normalized, variant) for variant in variants):
            return True
    return False


def _strings(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise QualityPolicyError(f"{label} must be a list of non-empty strings")
    if required and not value:
        raise QualityPolicyError(f"{label} must not be empty")
    return tuple(value)


def _relative_directory(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise QualityPolicyError(f"{label} must be a non-empty relative directory")
    candidate = PurePosixPath(_normal(value))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise QualityPolicyError(f"{label} must stay inside the candidate checkout")
    return candidate.as_posix()


@dataclass(frozen=True)
class RepositoryQualityGate:
    name: str
    category: str
    command: tuple[str, ...]
    applies_to: tuple[str, ...]
    tier: str = "commit"
    timeout_seconds: int = 300
    cwd: str | None = None

    def applies(self, changed_files: tuple[str, ...]) -> bool:
        return any(_matches(path, self.applies_to) for path in changed_files)

    def gate_command(
        self,
        baseline_sha: str | None,
        candidate_sha: str,
        policy_digest: str,
    ) -> GateCommand:
        environment: tuple[tuple[str, str], ...] = (
            ("STAGEMESH_QUALITY_HEAD_SHA", candidate_sha),
            ("STAGEMESH_QUALITY_POLICY_SHA256", policy_digest),
        )
        if baseline_sha:
            environment = (("STAGEMESH_QUALITY_BASE_SHA", baseline_sha), *environment)
        return GateCommand(
            self.name,
            list(self.command),
            timeout_seconds=self.timeout_seconds,
            cwd=self.cwd,
            env=environment,
        )


@dataclass(frozen=True)
class RepositoryQualityPolicy:
    policy_id: str
    gates: tuple[RepositoryQualityGate, ...]
    authorization_required: tuple[str, ...]
    strict_modules: tuple[str, ...]
    complexity_max: int
    dead_code_min_confidence: int
    digest: str

    def evidence(self, applied: tuple[RepositoryQualityGate, ...]) -> dict[str, object]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "policy_hash": self.digest,
            "source": POLICY_PATH.as_posix(),
            "applied_gates": [gate.name for gate in applied],
            "complexity_max": self.complexity_max,
            "dead_code_min_confidence": self.dead_code_min_confidence,
            "strict_modules": list(self.strict_modules),
            "trust_boundary": "loaded from integration checkout, not candidate",
        }


@dataclass(frozen=True)
class AppliedQualityPolicy:
    policy: RepositoryQualityPolicy
    gates: tuple[RepositoryQualityGate, ...]

    def evidence(self) -> dict[str, object]:
        return self.policy.evidence(self.gates)


def _parse_gate(value: object, index: int) -> RepositoryQualityGate:
    label = f"gates[{index}]"
    if not isinstance(value, dict):
        raise QualityPolicyError(f"{label} must be an object")
    allowed = {"name", "category", "command", "applies_to", "tier", "timeout_seconds", "cwd"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise QualityPolicyError(f"{label} has unsupported fields: {', '.join(unknown)}")
    name = value.get("name")
    category = value.get("category")
    tier = value.get("tier", "commit")
    timeout = value.get("timeout_seconds", 300)
    if not isinstance(name, str) or not name.strip():
        raise QualityPolicyError(f"{label}.name must be a non-empty string")
    if category not in GATE_CATEGORIES:
        raise QualityPolicyError(f"{label}.category must be one of {sorted(GATE_CATEGORIES)}")
    if tier not in GATE_TIERS:
        raise QualityPolicyError(f"{label}.tier must be one of {sorted(GATE_TIERS)}")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
        raise QualityPolicyError(f"{label}.timeout_seconds must be an integer from 1 to 3600")
    command = _strings(value.get("command"), f"{label}.command", required=True)
    if any("\n" in item or "\r" in item for item in command):
        raise QualityPolicyError(f"{label}.command arguments must not contain newlines")
    return RepositoryQualityGate(
        name=name,
        category=str(category),
        command=command,
        applies_to=_strings(value.get("applies_to"), f"{label}.applies_to", required=True),
        tier=str(tier),
        timeout_seconds=timeout,
        cwd=_relative_directory(value.get("cwd"), f"{label}.cwd"),
    )


def parse_quality_policy(payload: object) -> RepositoryQualityPolicy:
    if not isinstance(payload, dict):
        raise QualityPolicyError("quality policy root must be an object")
    allowed = {
        "schema_version",
        "policy_id",
        "gates",
        "authorization_required",
        "strict_modules",
        "complexity_max",
        "dead_code_min_confidence",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise QualityPolicyError(f"quality policy has unsupported fields: {', '.join(unknown)}")
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise QualityPolicyError(f"schema_version must be {POLICY_SCHEMA_VERSION}")
    policy_id = payload.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise QualityPolicyError("policy_id must be a non-empty string")
    raw_gates = payload.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise QualityPolicyError("gates must be a non-empty list")
    gates = tuple(_parse_gate(value, index) for index, value in enumerate(raw_gates))
    names = [gate.name for gate in gates]
    if len(names) != len(set(names)):
        raise QualityPolicyError("gate names must be unique")
    if not {gate.category for gate in gates}.issuperset({"lint", "typecheck"}):
        raise QualityPolicyError("policy must contain both lint and typecheck gates")
    complexity = payload.get("complexity_max")
    if not isinstance(complexity, int) or isinstance(complexity, bool) or not 1 <= complexity <= 20:
        raise QualityPolicyError("complexity_max must be an integer from 1 to 20")
    confidence = payload.get("dead_code_min_confidence")
    if not isinstance(confidence, int) or isinstance(confidence, bool) or not 60 <= confidence <= 100:
        raise QualityPolicyError("dead_code_min_confidence must be an integer from 60 to 100")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return RepositoryQualityPolicy(
        policy_id=policy_id,
        gates=gates,
        authorization_required=_strings(payload.get("authorization_required"), "authorization_required"),
        strict_modules=_strings(payload.get("strict_modules"), "strict_modules", required=True),
        complexity_max=complexity,
        dead_code_min_confidence=confidence,
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def load_quality_policy(project: Path) -> RepositoryQualityPolicy | None:
    path = project / POLICY_PATH
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise QualityPolicyError(f"{POLICY_PATH.as_posix()} must be a regular file in the integration checkout")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QualityPolicyError(f"cannot read {POLICY_PATH.as_posix()}: {exc}") from exc
    return parse_quality_policy(payload)


def _append_unique(existing: tuple[GateCommand, ...], added: list[GateCommand]) -> tuple[GateCommand, ...]:
    by_name = {gate.name: gate for gate in existing}
    result = list(existing)
    for gate in added:
        previous = by_name.get(gate.name)
        if previous is not None and previous != gate:
            raise QualityPolicyError(f"quality gate {gate.name!r} conflicts with the task contract")
        if previous is None:
            result.append(gate)
            by_name[gate.name] = gate
    return tuple(result)


def apply_quality_policy(
    project: Path,
    contract: ChangeContract,
    changed_files: tuple[str, ...],
    *,
    baseline_sha: str | None,
    candidate_sha: str,
) -> tuple[ChangeContract, AppliedQualityPolicy | None]:
    policy = load_quality_policy(project)
    if policy is None:
        return contract, None
    selected = tuple(gate for gate in policy.gates if gate.tier == "commit" and gate.applies(changed_files))
    commands: dict[str, list[GateCommand]] = {category: [] for category in GATE_CATEGORIES}
    for gate in selected:
        commands[gate.category].append(
            gate.gate_command(baseline_sha, candidate_sha, policy.digest)
        )
    return (
        replace(
            contract,
            lint=_append_unique(contract.lint, commands["lint"]),
            typecheck=_append_unique(contract.typecheck, commands["typecheck"]),
            required_tests=_append_unique(contract.required_tests, commands["test"]),
            dependency_checks=_append_unique(contract.dependency_checks, commands["dependency"]),
        ),
        AppliedQualityPolicy(policy, selected),
    )


def quality_scope_findings(
    policy: RepositoryQualityPolicy,
    contract: ChangeContract,
    changed_files: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """Require exact, explicit task authorization for policy-sensitive paths.

    Broad globs establish an implementation sandbox but do not prove that a
    workflow, gate, dependency, or policy change was part of the authorized
    plan. Independent review remains responsible for semantic scope and
    unnecessary abstraction judgments that static matching cannot establish.
    """
    exact = {_normal(path) for path in contract.allowed_files if not any(char in path for char in "*?[")}
    return tuple(
        {
            "severity": "error",
            "code": "quality_scope_authorization_missing",
            "path": path,
            "message": (
                f"{path} changes a quality, workflow, or dependency boundary but is not an exact allowed_files entry; "
                "record explicit authorization in the task contract and obtain independent review"
            ),
        }
        for path in changed_files
        if _matches(path, policy.authorization_required) and (not contract.explicit or _normal(path) not in exact)
    )
