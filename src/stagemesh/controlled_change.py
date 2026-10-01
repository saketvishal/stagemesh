from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .validation import AffectedTestDiscovery, DEFAULT_SOURCE_TEST_MAPPING


class ValidationLevel(str, Enum):
    FAST = "FAST"
    TASK = "TASK"
    FULL = "FULL"


class RiskLevel(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


HIGH_RISK_PATTERNS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "build_backend.py",
    "scripts/invariants.py",
    "src/stagemesh/migrations.py",
    ".github/*",
    ".stagemesh/*",
)


@dataclass(frozen=True)
class ChangeContract:
    task_id: str
    baseline_sha: str
    allowed_paths: tuple[str, ...] = ()
    expected_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    validation_level: str = "TASK"  # FAST | TASK | FULL
    required_tests: tuple[str, ...] = ()
    risk_level: str = "NORMAL"
    expanded: bool = False
    expansion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "baseline_sha": self.baseline_sha,
            "allowed_paths": list(self.allowed_paths),
            "expected_paths": list(self.expected_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "validation_level": self.validation_level,
            "required_tests": list(self.required_tests),
            "risk_level": self.risk_level,
            "expanded": self.expanded,
            "expansion_reason": self.expansion_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeContract:
        return cls(
            task_id=data["task_id"],
            baseline_sha=data["baseline_sha"],
            allowed_paths=tuple(data.get("allowed_paths", ())),
            expected_paths=tuple(data.get("expected_paths", ())),
            forbidden_paths=tuple(data.get("forbidden_paths", ())),
            validation_level=data.get("validation_level", "TASK"),
            required_tests=tuple(data.get("required_tests", ())),
            risk_level=data.get("risk_level", "NORMAL"),
            expanded=bool(data.get("expanded", False)),
            expansion_reason=data.get("expansion_reason"),
        )


@dataclass(frozen=True)
class ChangeSet:
    task_id: str
    baseline_sha: str
    result_tree_sha: str
    added_paths: tuple[str, ...] = ()
    modified_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    renamed_paths: tuple[tuple[str, str], ...] = ()

    @property
    def all_changed_paths(self) -> tuple[str, ...]:
        paths: set[str] = set(self.added_paths) | set(self.modified_paths) | set(self.deleted_paths)
        for old_p, new_p in self.renamed_paths:
            paths.add(old_p)
            paths.add(new_p)
        return tuple(sorted(paths))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "baseline_sha": self.baseline_sha,
            "result_tree_sha": self.result_tree_sha,
            "added_paths": list(self.added_paths),
            "modified_paths": list(self.modified_paths),
            "deleted_paths": list(self.deleted_paths),
            "renamed_paths": [list(pair) for pair in self.renamed_paths],
            "all_changed_paths": list(self.all_changed_paths),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeSet:
        return cls(
            task_id=data["task_id"],
            baseline_sha=data["baseline_sha"],
            result_tree_sha=data["result_tree_sha"],
            added_paths=tuple(data.get("added_paths", ())),
            modified_paths=tuple(data.get("modified_paths", ())),
            deleted_paths=tuple(data.get("deleted_paths", ())),
            renamed_paths=tuple(tuple(pair) for pair in data.get("renamed_paths", ())),
        )


@dataclass(frozen=True)
class ScopeValidationResult:
    is_authorized: bool
    expected_paths: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    actual_paths: tuple[str, ...]
    unexpected_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_authorized": self.is_authorized,
            "expected_paths": list(self.expected_paths),
            "allowed_paths": list(self.allowed_paths),
            "actual_paths": list(self.actual_paths),
            "unexpected_paths": list(self.unexpected_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "error_message": self.error_message,
        }


class ScopeViolationError(ValueError):
    def __init__(self, result: ScopeValidationResult):
        super().__init__(result.error_message or "Change scope violation")
        self.result = result


@dataclass(frozen=True)
class ValidationPlan:
    task_id: str
    validation_level: str
    selected_commands: tuple[str, ...]
    reasons: dict[str, str]
    requires_full: bool
    escalation_reason: str | None
    plan_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "validation_level": self.validation_level,
            "selected_commands": list(self.selected_commands),
            "reasons": dict(self.reasons),
            "requires_full": self.requires_full,
            "escalation_reason": self.escalation_reason,
            "plan_hash": self.plan_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationPlan:
        return cls(
            task_id=data["task_id"],
            validation_level=data["validation_level"],
            selected_commands=tuple(data.get("selected_commands", ())),
            reasons=dict(data.get("reasons", {})),
            requires_full=bool(data.get("requires_full", False)),
            escalation_reason=data.get("escalation_reason"),
            plan_hash=data.get("plan_hash", ""),
        )


def match_path_pattern(pattern: str, file_path: str) -> bool:
    """Matches a glob pattern or path against file_path using normalized forward slashes."""
    norm_pattern = pattern.replace("\\", "/").strip().lstrip("./")
    norm_path = file_path.replace("\\", "/").strip().lstrip("./")
    if norm_pattern == norm_path:
        return True
    if fnmatch.fnmatch(norm_path, norm_pattern):
        return True
    if norm_pattern.endswith("/"):
        return norm_path.startswith(norm_pattern)
    # Check directory prefix
    if norm_path.startswith(norm_pattern + "/"):
        return True
    return False


def derive_git_changeset(
    repo_path: Path,
    task_id: str,
    baseline_sha: str,
    result_tree_or_commit: str,
) -> ChangeSet:
    """Derives exact, authoritative ChangeSet directly from Git object trees."""
    # First resolve the exact tree SHA for result_tree_or_commit
    tree_res = subprocess.run(
        ["git", "rev-parse", f"{result_tree_or_commit}^{{tree}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if tree_res.returncode == 0 and tree_res.stdout.strip():
        result_tree_sha = tree_res.stdout.strip()
    else:
        result_tree_sha = result_tree_or_commit

    # Run git diff-tree to get authoritative additions, modifications, deletions, and renames
    cmd = [
        "git",
        "diff-tree",
        "-r",
        "--name-status",
        "--no-commit-id",
        baseline_sha,
        result_tree_sha,
    ]
    res = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git diff-tree failed ({res.returncode}): {res.stderr}")

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []

    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            renamed.append((parts[1].replace("\\", "/"), parts[2].replace("\\", "/")))
        elif status == "A" and len(parts) >= 2:
            added.append(parts[1].replace("\\", "/"))
        elif status == "M" and len(parts) >= 2:
            modified.append(parts[1].replace("\\", "/"))
        elif status == "D" and len(parts) >= 2:
            deleted.append(parts[1].replace("\\", "/"))
        elif len(parts) >= 2:
            modified.append(parts[1].replace("\\", "/"))

    return ChangeSet(
        task_id=task_id,
        baseline_sha=baseline_sha,
        result_tree_sha=result_tree_sha,
        added_paths=tuple(sorted(added)),
        modified_paths=tuple(sorted(modified)),
        deleted_paths=tuple(sorted(deleted)),
        renamed_paths=tuple(sorted(renamed)),
    )


def enforce_change_scope(
    contract: ChangeContract,
    changeset: ChangeSet,
) -> ScopeValidationResult:
    """
    Validates actual ChangeSet against ChangeContract before testing/canonicalization.
    If scope exceeded or forbidden file modified, returns ScopeValidationResult with is_authorized=False.
    """
    effective_allowed = set(contract.allowed_paths) | set(contract.expected_paths)
    actual_paths = changeset.all_changed_paths

    forbidden_found: list[str] = []
    for path in actual_paths:
        for f_pat in contract.forbidden_paths:
            if match_path_pattern(f_pat, path):
                forbidden_found.append(path)
                break

    unexpected_found: list[str] = []
    if effective_allowed:
        for path in actual_paths:
            allowed = False
            for a_pat in effective_allowed:
                if match_path_pattern(a_pat, path):
                    allowed = True
                    break
            if not allowed:
                unexpected_found.append(path)

    is_authorized = (not forbidden_found) and (not unexpected_found)
    error_message: str | None = None
    if not is_authorized:
        parts = []
        if forbidden_found:
            parts.append(f"forbidden paths modified: {sorted(set(forbidden_found))}")
        if unexpected_found:
            parts.append(f"unauthorized unexpected paths modified: {sorted(set(unexpected_found))}")
        error_message = "; ".join(parts)

    return ScopeValidationResult(
        is_authorized=is_authorized,
        expected_paths=contract.expected_paths,
        allowed_paths=contract.allowed_paths,
        actual_paths=actual_paths,
        unexpected_paths=tuple(sorted(set(unexpected_found))),
        forbidden_paths=tuple(sorted(set(forbidden_found))),
        error_message=error_message,
    )


def expand_contract_scope(
    contract: ChangeContract,
    additional_allowed_paths: Sequence[str],
    reason: str,
) -> ChangeContract:
    """Explicit scope-expansion operation. An agent cannot silently self-expand scope."""
    if not reason or not reason.strip():
        raise ValueError("Scope expansion requires an explicit reason")
    new_allowed = sorted(set(contract.allowed_paths) | set(additional_allowed_paths))
    return ChangeContract(
        task_id=contract.task_id,
        baseline_sha=contract.baseline_sha,
        allowed_paths=tuple(new_allowed),
        expected_paths=contract.expected_paths,
        forbidden_paths=contract.forbidden_paths,
        validation_level=contract.validation_level,
        required_tests=contract.required_tests,
        risk_level=contract.risk_level,
        expanded=True,
        expansion_reason=reason.strip(),
    )


def compute_plan_hash(
    task_id: str,
    validation_level: str,
    selected_commands: Sequence[str],
    version: str = "v1",
) -> str:
    serialized = f"{version}:{task_id}:{validation_level}:{sorted(selected_commands)}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def current_platform_key() -> str:
    return f"{sys.platform}-{platform.machine()}"


class ValidationPlanner:
    """
    Deterministic ValidationPlanner.
    Priority:
    1. Explicit task-required validation
    2. Deterministic component -> test mapping
    3. Known direct dependency mappings
    4. Risk escalation
    5. FULL only when required
    """

    def __init__(
        self,
        discovery: AffectedTestDiscovery | None = None,
        high_risk_patterns: Sequence[str] = HIGH_RISK_PATTERNS,
    ):
        self.discovery = discovery or AffectedTestDiscovery(use_defaults=True)
        self.high_risk_patterns = tuple(high_risk_patterns)

    def plan(
        self,
        contract: ChangeContract,
        changeset: ChangeSet,
    ) -> ValidationPlan:
        selected_commands: set[str] = set()
        reasons: dict[str, str] = {}
        actual_paths = changeset.all_changed_paths

        # 1. Explicit task-required validation
        for cmd in contract.required_tests:
            selected_commands.add(cmd)
            reasons[cmd] = "explicit task-required validation"

        # Check risk escalation to FULL
        escalation_reason: str | None = None
        requires_full = False

        if contract.validation_level == ValidationLevel.FULL:
            requires_full = True
            escalation_reason = "task contract explicitly specified FULL validation"

        # Check high-risk file modifications
        if not requires_full:
            for path in actual_paths:
                for pattern in self.high_risk_patterns:
                    if match_path_pattern(pattern, path):
                        requires_full = True
                        escalation_reason = f"high-risk critical file modified: {path}"
                        break
                if requires_full:
                    break

        # 2. Deterministic component -> test mapping
        mapped_commands: set[str] = set()
        unmapped_source: list[str] = []

        for path in actual_paths:
            # If the file itself is a test file, select it directly
            if path.startswith("tests/") and path.endswith(".py"):
                cmd = f"pytest {path} -q"
                selected_commands.add(cmd)
                reasons[cmd] = f"directly modified test file: {path}"
                continue

            # Check discovery mappings
            cmds = self.discovery.discover([path], baseline_commands=[])
            if cmds:
                for c in cmds:
                    selected_commands.add(c)
                    if c not in reasons:
                        reasons[c] = f"direct component mapping from {path}"
                mapped_commands.update(cmds)
            else:
                # Source file with no mapping (excluding doc/markdown/text files)
                if path.startswith("src/") and path.endswith(".py"):
                    unmapped_source.append(path)

        # If an unmapped source file was changed and validation is not already FULL:
        if unmapped_source and not requires_full:
            requires_full = True
            escalation_reason = f"unmapped source file modified with unknown impact: {unmapped_source[0]}"

        # Determine final validation level
        if requires_full:
            validation_level = ValidationLevel.FULL.value
            full_cmd = "pytest tests/ -q"
            selected_commands.add(full_cmd)
            reasons[full_cmd] = escalation_reason or "complete repository validation escalated"
        elif contract.validation_level == ValidationLevel.FAST.value:
            validation_level = ValidationLevel.FAST.value
        else:
            validation_level = ValidationLevel.TASK.value

        sorted_cmds = tuple(sorted(selected_commands))
        plan_hash = compute_plan_hash(contract.task_id, validation_level, sorted_cmds)

        return ValidationPlan(
            task_id=contract.task_id,
            validation_level=validation_level,
            selected_commands=sorted_cmds,
            reasons=reasons,
            requires_full=requires_full,
            escalation_reason=escalation_reason,
            plan_hash=plan_hash,
        )


def format_validation_plan_explain(
    plan: ValidationPlan,
    changeset: ChangeSet,
    scope_result: ScopeValidationResult | None = None,
) -> str:
    """Human-readable explainability output for stagemesh validation-plan."""
    lines: list[str] = []
    lines.append("Changed:")
    if changeset.all_changed_paths:
        for p in changeset.all_changed_paths:
            lines.append(f"  {p}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Authorized:")
    if scope_result is not None:
        lines.append(f"  {'yes' if scope_result.is_authorized else 'no'}")
        if not scope_result.is_authorized and scope_result.error_message:
            lines.append(f"  reason: {scope_result.error_message}")
    else:
        lines.append("  yes")

    lines.append("")
    lines.append("Selected:")
    if plan.selected_commands:
        for cmd in plan.selected_commands:
            reason = plan.reasons.get(cmd, "selected by validation planner")
            lines.append(f"  {cmd}")
            lines.append(f"    reason: {reason}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Skipped:")
    if not plan.requires_full:
        lines.append("  full repository suite")
        lines.append("    reason: impact bounded")
    else:
        lines.append("  (none - full validation required)")

    lines.append("")
    lines.append(f"Validation:\n  {plan.validation_level}")
    return "\n".join(lines)
