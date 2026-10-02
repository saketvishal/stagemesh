from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .git import GitError, GitWorkspace


class ChangeControlError(ValueError):
    pass


DEPENDENCY_FILES = (
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
)


@dataclass(frozen=True)
class ChangeContract:
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    required_changed_paths: tuple[str, ...] = ()
    validation_commands: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    excluded_work: tuple[str, ...] = ()
    max_changed_files: int = 50
    max_changed_lines: int = 2000
    allow_dependency_changes: bool = False

    @classmethod
    def from_mapping(cls, data: object) -> "ChangeContract":
        if not isinstance(data, dict):
            raise ChangeControlError("change contract must be an object")
        objective = _required_text(data.get("objective"), "objective")
        max_changed_files = _bounded_int(data.get("max_changed_files", 50), "max_changed_files", 1, 5000)
        max_changed_lines = _bounded_int(data.get("max_changed_lines", 2000), "max_changed_lines", 1, 1_000_000)
        allow_dependency_changes = data.get("allow_dependency_changes", False)
        if not isinstance(allow_dependency_changes, bool):
            raise ChangeControlError("allow_dependency_changes must be a boolean")
        return cls(
            objective=objective,
            acceptance_criteria=_string_tuple(data.get("acceptance_criteria", []), "acceptance_criteria"),
            allowed_paths=_pattern_tuple(data.get("allowed_paths", []), "allowed_paths"),
            forbidden_paths=_pattern_tuple(data.get("forbidden_paths", []), "forbidden_paths"),
            required_changed_paths=_pattern_tuple(data.get("required_changed_paths", []), "required_changed_paths"),
            validation_commands=_string_tuple(data.get("validation_commands", []), "validation_commands"),
            invariants=_string_tuple(data.get("invariants", []), "invariants"),
            excluded_work=_string_tuple(data.get("excluded_work", []), "excluded_work"),
            max_changed_files=max_changed_files,
            max_changed_lines=max_changed_lines,
            allow_dependency_changes=allow_dependency_changes,
        )

    @classmethod
    def load(cls, project: Path, task_id: str) -> "ChangeContract | None":
        path = contract_path(project, task_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChangeControlError(f"invalid change contract for {task_id}") from exc
        return cls.from_mapping(payload)

    def to_mapping(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "required_changed_paths": list(self.required_changed_paths),
            "validation_commands": list(self.validation_commands),
            "invariants": list(self.invariants),
            "excluded_work": list(self.excluded_work),
            "max_changed_files": self.max_changed_files,
            "max_changed_lines": self.max_changed_lines,
            "allow_dependency_changes": self.allow_dependency_changes,
        }

    def render_prompt(self) -> str:
        sections = [f"OBJECTIVE\n{self.objective}"]
        for heading, values in (
            ("ACCEPTANCE CRITERIA", self.acceptance_criteria),
            ("ALLOWED PATHS", self.allowed_paths),
            ("FORBIDDEN PATHS", self.forbidden_paths),
            ("REQUIRED CHANGED PATHS", self.required_changed_paths),
            ("INVARIANTS", self.invariants),
            ("EXCLUDED WORK", self.excluded_work),
            ("VALIDATION COMMANDS", self.validation_commands),
        ):
            if values:
                sections.append(heading + "\n" + "\n".join(f"- {value}" for value in values))
        sections.append(
            "HARD LIMITS\n"
            f"- max changed files: {self.max_changed_files}\n"
            f"- max changed lines: {self.max_changed_lines}\n"
            f"- dependency changes allowed: {self.allow_dependency_changes}"
        )
        return "\n\n".join(sections)


@dataclass(frozen=True)
class DiffSummary:
    changed_files: tuple[str, ...]
    changed_lines: int


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str


def contract_path(project: Path, task_id: str) -> Path:
    safe = _safe_task_id(task_id)
    return Path(project).resolve() / ".stagemesh" / "contracts" / f"{safe}.json"


def write_contract(project: Path, task_id: str, contract: ChangeContract) -> Path:
    path = contract_path(project, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def diff_summary(project: Path, candidate_sha: str) -> DiffSummary:
    workspace = GitWorkspace(project)
    exists = workspace.run(
        "cat-file",
        "-e",
        f"{candidate_sha}^{{commit}}",
        check=False,
    )
    if exists.returncode != 0:
        raise ChangeControlError(
            f"candidate commit does not exist: {candidate_sha}"
        )
    baseline = _candidate_baseline(workspace, candidate_sha)
    names = workspace.run(
        "diff",
        "--name-only",
        baseline,
        candidate_sha,
        "--",
    ).stdout.splitlines()
    numstat = workspace.run(
        "diff",
        "--numstat",
        baseline,
        candidate_sha,
        "--",
    ).stdout.splitlines()
    changed_lines = 0
    for line in numstat:
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        for value in parts[:2]:
            if value.isdigit():
                changed_lines += int(value)
    return DiffSummary(
        tuple(sorted({name.strip() for name in names if name.strip()})),
        changed_lines,
    )

def contract_definition_violations(
    contract: ChangeContract,
) -> list[str]:
    """Return omissions that make a contract unsafe for autonomous execution."""
    violations: list[str] = []
    if not contract.acceptance_criteria:
        violations.append(
            "strict contract requires at least one acceptance criterion"
        )
    if not contract.allowed_paths:
        violations.append(
            "strict contract requires explicit allowed_paths"
        )
    if not contract.validation_commands:
        violations.append(
            "strict contract requires at least one deterministic validation command"
        )
    return violations


def contract_violations(contract: ChangeContract, summary: DiffSummary) -> list[str]:
    violations: list[str] = []
    files = summary.changed_files
    if len(files) > contract.max_changed_files:
        violations.append(
            f"changed file count {len(files)} exceeds maximum {contract.max_changed_files}"
        )
    if summary.changed_lines > contract.max_changed_lines:
        violations.append(
            f"changed line count {summary.changed_lines} exceeds maximum {contract.max_changed_lines}"
        )
    if contract.allowed_paths:
        for path in files:
            if not _matches_any(path, contract.allowed_paths):
                violations.append(f"out-of-scope path changed: {path}")
    for path in files:
        if _matches_any(path, contract.forbidden_paths):
            violations.append(f"forbidden path changed: {path}")
    for pattern in contract.required_changed_paths:
        if not any(_matches_pattern(path, pattern) for path in files):
            violations.append(f"required path was not changed: {pattern}")
    if not contract.allow_dependency_changes:
        for path in files:
            name = Path(path).name
            if name in DEPENDENCY_FILES or name.startswith("requirements") and name.endswith(".txt"):
                violations.append(f"dependency manifest changed without permission: {path}")
    return violations


def run_validation_commands(
    project: Path,
    candidate_sha: str,
    commands: Iterable[str],
    *,
    timeout_seconds: int = 900,
) -> list[CommandResult]:
    commands = tuple(commands)
    if not commands:
        return []
    root = Path(project).resolve()
    workspace = GitWorkspace(root)
    parent = root.parent
    with tempfile.TemporaryDirectory(prefix=".stagemesh-validate-", dir=parent) as tmp:
        worktree = Path(tmp)
        # TemporaryDirectory creates the directory; git worktree requires the path not to exist.
        worktree.rmdir()
        added = workspace.run(
            "worktree", "add", "--detach", str(worktree), candidate_sha, check=False
        )
        if added.returncode != 0:
            raise ChangeControlError(added.stderr.strip() or "unable to create validation worktree")
        results: list[CommandResult] = []
        try:
            for command in commands:
                try:
                    argv = shlex.split(command)
                except ValueError as exc:
                    raise ChangeControlError(f"invalid validation command: {command}") from exc
                if not argv:
                    raise ChangeControlError("validation command cannot be empty")
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=worktree,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    results.append(
                        CommandResult(command, 124, exc.stdout or "", exc.stderr or "validation timeout")
                    )
                    break
                results.append(CommandResult(command, proc.returncode, proc.stdout, proc.stderr))
                if proc.returncode != 0:
                    break
        finally:
            workspace.run("worktree", "remove", "--force", str(worktree), check=False)
            workspace.run("worktree", "prune", check=False)
        return results


def git_diff_check(project: Path, candidate_sha: str) -> CommandResult:
    workspace = GitWorkspace(project)
    baseline = _candidate_baseline(workspace, candidate_sha)
    proc = workspace.run(
        "diff",
        "--check",
        baseline,
        candidate_sha,
        "--",
        check=False,
    )
    return CommandResult(
        command=f"git diff --check {baseline} {candidate_sha} --",
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _candidate_baseline(
    workspace: GitWorkspace,
    candidate_sha: str,
) -> str:
    try:
        head = workspace.head()
    except GitError as exc:
        raise ChangeControlError(
            "target checkout must have a committed HEAD"
        ) from exc
    if head == candidate_sha:
        parent = workspace.run(
            "rev-parse",
            f"{candidate_sha}^",
            check=False,
        )
        if parent.returncode == 0 and parent.stdout.strip():
            return parent.stdout.strip()
        # A root candidate has no parent. This is Git's canonical empty-tree SHA.
        return "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    merge_base = workspace.run(
        "merge-base",
        head,
        candidate_sha,
        check=False,
    )
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        raise ChangeControlError(
            "candidate does not share history with target checkout"
        )
    return merge_base.stdout.strip()


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_matches_pattern(path, pattern) for pattern in patterns)


def _matches_pattern(path: str, pattern: str) -> bool:
    """Match repository-relative POSIX paths without letting * cross directories."""
    normalized_path = PurePosixPath(path.replace("\\", "/"))
    normalized_pattern = pattern.replace("\\", "/")
    return normalized_path.match(normalized_pattern)


def _safe_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip():
        raise ChangeControlError("task id must be a non-empty string")
    value = task_id.strip()
    if len(value) > 200:
        raise ChangeControlError("task id must be 200 characters or fewer")
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChangeControlError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 4000:
        raise ChangeControlError(f"{field} must be 4000 characters or fewer")
    return normalized


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list):
        raise ChangeControlError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        result.append(_required_text(item, field))
    return tuple(result)


def _pattern_tuple(value: object, field: str) -> tuple[str, ...]:
    patterns = _string_tuple(value, field)
    for pattern in patterns:
        if pattern.startswith("/") or ".." in Path(pattern).parts:
            raise ChangeControlError(f"{field} patterns must be repository-relative")
    return patterns


def _bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChangeControlError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ChangeControlError(f"{field} must be between {minimum} and {maximum}")
    return value
