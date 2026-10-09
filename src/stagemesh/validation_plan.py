from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import ChangeContract, GateCommand

CONFIG_OR_PROVIDER_COMMAND = "CONFIG_OR_PROVIDER_COMMAND"
DOCS_ONLY = "DOCS_ONLY"
LOCALIZED_CODE = "LOCALIZED_CODE"
CORE_LIFECYCLE_OR_SCHEMA_SECURITY = "CORE_LIFECYCLE_OR_SCHEMA_SECURITY"

_CLASSIFICATIONS = {
    CONFIG_OR_PROVIDER_COMMAND,
    DOCS_ONLY,
    LOCALIZED_CODE,
    CORE_LIFECYCLE_OR_SCHEMA_SECURITY,
}

_BROAD_GATE_TOKENS = ("full pytest", "invariant", "clean acceptance", "acceptance", "full ci", "ci")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_DOC_PATTERNS = ("README*", "docs/**", "*.md", "**/*.md", "*.rst", "**/*.rst")
_DEPENDENCY_PATTERNS = (
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)
_CONFIG_PROVIDER_PATTERNS = (
    "stagemesh.toml",
    ".stagemesh/config.json",
    ".stagemesh/backlog.json",
    "src/stagemesh/config.py",
    "src/stagemesh/providers.py",
    "src/stagemesh/provider_acceptance.py",
    "src/stagemesh/cli.py",
)
_CORE_PATTERNS = (
    "src/stagemesh/coordinator.py",
    "src/stagemesh/domain.py",
    "src/stagemesh/persistence.py",
    "src/stagemesh/postgres_store.py",
    "src/stagemesh/contracts.py",
    "src/stagemesh/security.py",
    "src/stagemesh/validation.py",
    "src/stagemesh/validation_plan.py",
    "scripts/invariants.py",
    "migrations/**",
    "**/schema/**",
    "**/security.py",
)


@dataclass(frozen=True)
class ValidationPlan:
    classification: str
    risk_level: str
    debug_checks: tuple[str, ...]
    release_checks: tuple[str, ...]
    escalation_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "risk_level": self.risk_level,
            "debug_checks": list(self.debug_checks),
            "release_checks": list(self.release_checks),
            "escalation_reasons": list(self.escalation_reasons),
        }

    @property
    def planned_checks(self) -> tuple[str, ...]:
        return self.debug_checks + self.release_checks

    @property
    def allows_broad_validation(self) -> bool:
        return bool(self.escalation_reasons)


def derive_validation_plan(contract: ChangeContract, changed_files: tuple[str, ...] = ()) -> ValidationPlan:
    classification = _classification(contract, changed_files)
    reasons = _escalation_reasons(contract, changed_files, classification)
    if classification == CORE_LIFECYCLE_OR_SCHEMA_SECURITY:
        return ValidationPlan(
            classification,
            "HIGH",
            _gate_names(contract.gates) or ("focused-tests",),
            _broad_gate_names(contract),
            reasons or ("core lifecycle/state-machine, schema, or security boundary",),
        )
    if classification == CONFIG_OR_PROVIDER_COMMAND:
        checks = _matching_gate_names(contract.gates, ("smoke", "provider", "runtime", "focused"))
        return ValidationPlan(classification, "LOW", checks or ("provider-smoke", "focused-runtime"), (), reasons)
    if classification == DOCS_ONLY:
        checks = _matching_gate_names(contract.gates, ("docs", "static", "lint", "spell"))
        return ValidationPlan(classification, "LOW", checks or ("docs-static",), (), reasons)
    return ValidationPlan(LOCALIZED_CODE, "MEDIUM", _non_broad_gate_names(contract) or ("focused-tests",), (), reasons)


def planned_contract(contract: ChangeContract, plan: ValidationPlan) -> ChangeContract:
    allowed = set(plan.planned_checks)
    return ChangeContract(
        objective=contract.objective,
        explicit=contract.explicit,
        validation_classification=plan.classification,
        validation_risk=plan.risk_level,
        validation_escalation_reasons=plan.escalation_reasons,
        acceptance_criteria=contract.acceptance_criteria,
        allowed_files=contract.allowed_files,
        forbidden_files=contract.forbidden_files,
        exclusions=contract.exclusions,
        invariants=contract.invariants if plan.allows_broad_validation else (),
        required_tests=tuple(gate for gate in contract.required_tests if _gate_allowed(gate, allowed, plan)),
        lint=tuple(gate for gate in contract.lint if _gate_allowed(gate, allowed, plan)),
        typecheck=tuple(gate for gate in contract.typecheck if _gate_allowed(gate, allowed, plan)),
        dependency_checks=tuple(gate for gate in contract.dependency_checks if _gate_allowed(gate, allowed, plan)),
        public_api=contract.public_api,
        protected_files=contract.protected_files,
        max_changed_files=contract.max_changed_files,
        max_diff_lines=contract.max_diff_lines,
    )


_RISK_RANK = {
    DOCS_ONLY: 0,
    CONFIG_OR_PROVIDER_COMMAND: 1,
    LOCALIZED_CODE: 2,
    CORE_LIFECYCLE_OR_SCHEMA_SECURITY: 3,
}


def _declared_classification(contract: ChangeContract) -> str | None:
    configured = contract.validation_classification
    if configured and configured.upper() in _CLASSIFICATIONS:
        return configured.upper()
    return None


def _classification(contract: ChangeContract, changed_files: tuple[str, ...]) -> str:
    derived = _derived_classification(contract, changed_files)
    declared = _declared_classification(contract)
    if declared is None:
        return derived
    # A declaration may raise risk but never lower what the changed files imply. The config/provider
    # patterns are repo-specific, so a plain source file gives no basis to override a CONFIG declaration.
    if declared == CONFIG_OR_PROVIDER_COMMAND and derived == LOCALIZED_CODE:
        return declared
    return max(declared, derived, key=_RISK_RANK.__getitem__)


def _derived_classification(contract: ChangeContract, changed_files: tuple[str, ...]) -> str:
    scope = changed_files or contract.allowed_files
    if scope and all(_matches(path, _DOC_PATTERNS) for path in scope):
        return DOCS_ONLY
    if any(_matches(path, _CORE_PATTERNS) for path in scope):
        return CORE_LIFECYCLE_OR_SCHEMA_SECURITY
    if any(_matches(path, _CONFIG_PROVIDER_PATTERNS) for path in scope):
        return CONFIG_OR_PROVIDER_COMMAND
    return LOCALIZED_CODE


def _escalation_reasons(contract: ChangeContract, changed_files: tuple[str, ...], classification: str) -> tuple[str, ...]:
    reasons = list(contract.validation_escalation_reasons)
    scope = changed_files or contract.allowed_files
    if classification == CORE_LIFECYCLE_OR_SCHEMA_SECURITY and not reasons:
        reasons.append("core lifecycle/state-machine, schema, or security boundary")
    if contract.public_api and not reasons:
        reasons.append("public API/contract")
    if any(_matches(path, _DEPENDENCY_PATTERNS) for path in scope) and not reasons:
        reasons.append("dependency manifest")
    declared = _declared_classification(contract)
    if declared is not None and declared != classification:
        reasons.append(f"declared {declared} raised to {classification} by changed files")
    return tuple(dict.fromkeys(reasons))


def _gate_allowed(gate: GateCommand, allowed: set[str], plan: ValidationPlan) -> bool:
    if gate.name in allowed:
        return True
    is_broad = _is_broad_gate(gate.name) or _is_broad_command(gate.command)
    return is_broad and plan.allows_broad_validation


def _matching_gate_names(gates: tuple[GateCommand, ...], tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(gate.name for gate in gates if any(token in gate.name.casefold() for token in tokens) and not _is_broad_gate(gate.name))


def _non_broad_gate_names(contract: ChangeContract) -> tuple[str, ...]:
    return tuple(gate.name for gate in contract.gates if not _is_broad_gate(gate.name) and not _is_broad_command(gate.command))


def _broad_gate_names(contract: ChangeContract) -> tuple[str, ...]:
    names = [gate.name for gate in contract.gates if _is_broad_gate(gate.name) or _is_broad_command(gate.command)]
    names.extend(f"invariant:{item}" for item in contract.invariants)
    return tuple(names)


def _gate_names(gates: tuple[GateCommand, ...]) -> tuple[str, ...]:
    return tuple(gate.name for gate in gates)


def _is_broad_gate(value: str) -> bool:
    folded = value.casefold()
    if any(token != "ci" and token in folded for token in _BROAD_GATE_TOKENS):
        return True
    return "ci" in {part for part in _TOKEN_SPLIT.split(folded) if part}


def _is_broad_command(command: tuple[str, ...]) -> bool:
    if any(_is_broad_gate(part) for part in command):
        return True
    pytest_index = _pytest_index(command)
    if pytest_index is None:
        return False
    targets = [part.replace("\\", "/").rstrip("/") for part in command[pytest_index + 1 :] if part and not part.startswith("-")]
    return not targets or any(target in {".", "tests", "./tests"} for target in targets)


def _pytest_index(command: tuple[str, ...]) -> int | None:
    for index, part in enumerate(command):
        name = Path(part).name.casefold()
        if name in {"pytest", "pytest.exe"}:
            return index
        if part == "-m" and index + 1 < len(command) and command[index + 1].casefold() == "pytest":
            return index + 1
    return None


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    folded = normalized.casefold()
    return any(
        fnmatch.fnmatchcase(normalized, pattern.replace("\\", "/"))
        or fnmatch.fnmatchcase(folded, pattern.replace("\\", "/").casefold())
        for pattern in patterns
    )
