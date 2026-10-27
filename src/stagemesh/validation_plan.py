from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .contracts import ChangeContract, GateCommand, declarative_invariants, invariant_gates

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

_BROAD_GATE_TOKENS = (
    "full pytest",
    "invariant",
    "clean acceptance",
    "acceptance",
    "full ci",
    "ci",
    "ui:quality",
    "ui-quality",
    "full-ui",
    "promoted",
)
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
    "src/stagemesh/review.py",  # Independent review and candidate-bound evidence.
    "scripts/invariants.py",
    "migrations/**",
    "**/schema/**",
    "**/security.py",
)
_FRONTEND_PATTERNS = (
    "apps/web/**",
    "frontend/**",
    "web/**",
    "src/**/*.jsx",
    "src/**/*.tsx",
    "**/*.jsx",
    "**/*.tsx",
)
_BROWSER_E2E_SCRIPTS = ("e2e:ui-gate", "test:e2e", "e2e", "playwright")


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
    return ValidationPlan(
        LOCALIZED_CODE,
        "MEDIUM",
        _non_broad_gate_names(contract) or ("focused-tests",),
        (),
        reasons,
    )


def with_required_checks(contract: ChangeContract, plan: ValidationPlan) -> ValidationPlan:
    """Every declared executable gate is mandatory; prose invariants remain review obligations."""
    required = tuple(dict.fromkeys((*_gate_names(contract.gates), *_gate_names(invariant_gates(contract)))))
    if not required:
        return plan  # Keep missing-command diagnostics for an empty contract.
    debug = tuple(name for name in plan.debug_checks if name in required)
    release = tuple(dict.fromkeys((*(name for name in plan.release_checks if name in required),
                                  *(name for name in required if name not in debug))))
    return replace(plan, debug_checks=debug, release_checks=release)


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
        invariants=contract.invariants if plan.allows_broad_validation else declarative_invariants(contract),
        required_tests=tuple(gate for gate in contract.required_tests if _gate_allowed(gate, allowed, plan)),
        lint=tuple(gate for gate in contract.lint if _gate_allowed(gate, allowed, plan)),
        typecheck=tuple(gate for gate in contract.typecheck if _gate_allowed(gate, allowed, plan)),
        dependency_checks=tuple(gate for gate in contract.dependency_checks if _gate_allowed(gate, allowed, plan)),
        public_api=contract.public_api,
        protected_files=contract.protected_files,
        max_changed_files=contract.max_changed_files,
        max_diff_lines=contract.max_diff_lines,
    )


def contract_with_browser_e2e_gates(project: Path, contract: ChangeContract, changed_files: tuple[str, ...]) -> ChangeContract:
    """Add discovered browser/API E2E gates for frontend-like changes.

    The project opts in by having a conventional package script. StageMesh does not invent
    a browser command when the repository has not declared one.
    """
    if not _looks_frontend(contract, changed_files):
        return contract
    discovered = _browser_e2e_gates(project, contract, changed_files)
    if not discovered:
        return contract
    existing = {gate.name for gate in contract.gates}
    additions = tuple(gate for gate in discovered if gate.name not in existing)
    if not additions:
        return contract
    return ChangeContract(
        objective=contract.objective,
        explicit=contract.explicit,
        validation_classification=contract.validation_classification,
        validation_risk=contract.validation_risk,
        validation_escalation_reasons=contract.validation_escalation_reasons,
        acceptance_criteria=contract.acceptance_criteria,
        allowed_files=contract.allowed_files,
        forbidden_files=contract.forbidden_files,
        exclusions=contract.exclusions,
        invariants=contract.invariants,
        required_tests=contract.required_tests + additions,
        lint=contract.lint,
        typecheck=contract.typecheck,
        dependency_checks=contract.dependency_checks,
        public_api=contract.public_api,
        protected_files=contract.protected_files,
        spec_refs=contract.spec_refs,
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


def _looks_frontend(contract: ChangeContract, changed_files: tuple[str, ...]) -> bool:
    scope = changed_files or contract.allowed_files
    text = f"{contract.objective} {' '.join(contract.acceptance_criteria)}".casefold()
    return any(_matches(path, _FRONTEND_PATTERNS) for path in scope) or any(
        token in text for token in ("ui", "browser", "frontend", "playwright", "puppeteer")
    )


def _browser_e2e_gates(project: Path, contract: ChangeContract | None = None, changed_files: tuple[str, ...] = ()) -> tuple[GateCommand, ...]:
    gates: list[GateCommand] = []
    for package in _package_json_files(project, contract, changed_files):
        script = _preferred_e2e_script(package)
        if script is None:
            continue
        rel = package.parent.relative_to(project).as_posix()
        name = "browser-e2e" if rel == "." else f"browser-e2e:{rel}"
        gates.append(GateCommand(name, ("npm", "run", script), cwd=rel))
    return tuple(gates)


def _package_json_files(project: Path, contract: ChangeContract | None = None, changed_files: tuple[str, ...] = ()) -> list[Path]:
    root = project.resolve()
    found: list[Path] = []
    for package in sorted(root.rglob("package.json")):
        try:
            rel_parts = package.relative_to(root).parts
        except ValueError:
            continue
        if any(
            part in {"node_modules", ".stagemesh", ".git"}
            or (part.startswith(".") and part not in {".", ".."})
            for part in rel_parts
        ):
            continue
        found.append(package)
    permitted = [p for p in found if contract is None or _package_allowed_for_browser_e2e(
        p.parent.relative_to(root).as_posix(), p.relative_to(root).as_posix(), contract)]
    if contract is None or "**" in contract.allowed_files:
        return permitted
    # A source belongs to its nearest package, not every ancestor package.
    directories = {p: p.parent.relative_to(root).as_posix() for p in found}
    candidates = {p.replace("\\", "/") for p in contract.allowed_files if not any(c in p for c in "*?[")}
    candidates.update(p for p in changed_files if _matches(p, contract.allowed_files))
    candidates.update(
        p.relative_to(root).as_posix() for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") or part == "node_modules" for part in p.relative_to(root).parts)
        and _matches(p.relative_to(root).as_posix(), contract.allowed_files)
    )
    owners: set[Path] = set()
    for path in candidates:
        if _matches(path, contract.forbidden_files):
            continue
        containing = [p for p, directory in directories.items() if directory == "." or path == directory or path.startswith(directory + "/")]
        if containing:
            owners.add(max(containing, key=lambda p: len(directories[p])))
    return [p for p in permitted if p in owners]


def _package_allowed_for_browser_e2e(parent_rel: str, package_rel: str, contract: ChangeContract) -> bool:
    if contract.forbidden_files:
        test_paths = (
            parent_rel,
            f"{parent_rel}/",
            f"{parent_rel}/**",
            package_rel,
            f"{parent_rel}/index.html",
        )
        if any(_matches(p, contract.forbidden_files) for p in test_paths):
            return False
    return True


def _preferred_e2e_script(package: Path) -> str | None:
    try:
        payload = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    for name in _BROWSER_E2E_SCRIPTS:
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            return name
    return None


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
    names.extend(_gate_names(invariant_gates(contract)))
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
    joined = " ".join(command).casefold()
    if any(token in joined for token in ("ui:quality", "ui-quality", "playwright test", "playwright.config")):
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
