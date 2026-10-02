from __future__ import annotations

import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import GitError, GitWorkspace


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class GateCommand:
    name: str
    command: list[str]
    timeout_seconds: int = 120


@dataclass(frozen=True)
class ChangeContract:
    objective: str
    explicit: bool = False
    acceptance_criteria: tuple[str, ...] = ()
    allowed_files: tuple[str, ...] = ("**",)
    forbidden_files: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    required_tests: tuple[GateCommand, ...] = ()
    lint: tuple[GateCommand, ...] = ()
    typecheck: tuple[GateCommand, ...] = ()
    dependency_checks: tuple[GateCommand, ...] = ()
    public_api: tuple[str, ...] = ()

    @property
    def gates(self) -> tuple[GateCommand, ...]:
        return self.required_tests + self.lint + self.typecheck + self.dependency_checks


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ContractEvaluation:
    status: str
    changed_files: tuple[str, ...]
    findings: tuple[dict[str, Any], ...]
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"


def load_contract(project: Path, task_id: str | None = None) -> ChangeContract:
    candidates = []
    if task_id:
        candidates.extend(
            [
                project / ".stagemesh" / "contracts" / f"{task_id}.json",
                project / ".stagemesh" / "contracts" / f"{task_id}.contract.json",
            ]
        )
    candidates.extend([project / "stagemesh.contract.json", project / ".stagemesh" / "contract.json"])
    for path in candidates:
        if path.exists():
            return parse_contract(json.loads(path.read_text(encoding="utf-8")))
    return ChangeContract(objective="No explicit change contract supplied")


def parse_contract(payload: dict[str, Any]) -> ChangeContract:
    if not isinstance(payload, dict):
        raise ContractError("change contract must be a JSON object")
    objective = _required_text(payload.get("objective"), "objective")
    return ChangeContract(
        objective=objective,
        explicit=True,
        acceptance_criteria=_texts(payload.get("acceptance_criteria", ()), "acceptance_criteria"),
        allowed_files=_texts(payload.get("allowed_files", ("**",)), "allowed_files") or ("**",),
        forbidden_files=_texts(payload.get("forbidden_files", ()), "forbidden_files"),
        exclusions=_texts(payload.get("exclusions", ()), "exclusions"),
        invariants=_texts(payload.get("invariants", ()), "invariants"),
        required_tests=_commands(payload.get("required_tests", ()), "required_tests"),
        lint=_commands(payload.get("lint", ()), "lint"),
        typecheck=_commands(payload.get("typecheck", ()), "typecheck"),
        dependency_checks=_commands(payload.get("dependency_checks", ()), "dependency_checks"),
        public_api=_texts(payload.get("public_api", ()), "public_api"),
    )


def evaluate_contract(
    project: Path,
    candidate_sha: str,
    contract: ChangeContract,
    *,
    run_gates: bool = True,
) -> ContractEvaluation:
    try:
        changed = tuple(changed_files(project, candidate_sha))
    except (GitError, OSError) as exc:
        if not contract.explicit:
            return ContractEvaluation(
                status="PASSED",
                changed_files=(),
                findings=(),
                gates=(),
            )
        return ContractEvaluation(
            status="FAILED",
            changed_files=(),
            findings=(
                {
                    "severity": "error",
                    "code": "candidate_unavailable",
                    "message": f"candidate {candidate_sha} cannot be inspected: {exc}",
                },
            ),
            gates=(),
        )
    findings: list[dict[str, Any]] = []
    gates: list[GateResult] = []

    if contract.explicit and not changed:
        findings.append({"severity": "error", "code": "empty_diff", "message": "candidate has no changed files"})

    for path in changed:
        if _matches(path, contract.exclusions):
            findings.append(
                {
                    "severity": "error",
                    "code": "excluded_file_changed",
                    "path": path,
                    "message": f"{path} is explicitly excluded from this change",
                }
            )
        if not _matches(path, contract.allowed_files):
            findings.append(
                {
                    "severity": "error",
                    "code": "outside_allowed_files",
                    "path": path,
                    "message": f"{path} is outside the allowed file scope",
                }
            )
        if _matches(path, contract.forbidden_files):
            findings.append(
                {
                    "severity": "error",
                    "code": "forbidden_file_changed",
                    "path": path,
                    "message": f"{path} is forbidden by the change contract",
                }
            )
        if _matches(path, contract.public_api) and not _matches(path, contract.allowed_files):
            findings.append(
                {
                    "severity": "error",
                    "code": "public_api_changed",
                    "path": path,
                    "message": f"{path} changes public API outside the allowed scope",
                }
            )

    if run_gates:
        for gate in contract.gates:
            gates.append(run_gate(project, gate))
        for invariant in contract.invariants:
            gates.append(run_gate(project, GateCommand(f"invariant:{invariant}", _split_command(invariant))))

    for gate in gates:
        if gate.status != "PASSED":
            findings.append(
                {
                    "severity": "error",
                    "code": "gate_failed",
                    "message": f"{gate.name} failed",
                    "command": list(gate.command),
                    "returncode": gate.returncode,
                    "stdout": gate.stdout[-2000:],
                    "stderr": gate.stderr[-2000:],
                }
            )

    return ContractEvaluation(
        status="PASSED" if not findings else "FAILED",
        changed_files=changed,
        findings=tuple(findings),
        gates=tuple(gates),
    )


def changed_files(project: Path, candidate_sha: str) -> list[str]:
    workspace = GitWorkspace(project)
    workspace.run("cat-file", "-e", f"{candidate_sha}^{{commit}}")
    parent = workspace.run("rev-list", "--parents", "-n", "1", candidate_sha).stdout.strip().split()
    if len(parent) > 1:
        base = parent[1]
        output = workspace.run("diff", "--name-only", base, candidate_sha).stdout
    else:
        output = workspace.run("show", "--pretty=", "--name-only", candidate_sha).stdout
    return sorted({line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()})


def run_gate(project: Path, gate: GateCommand) -> GateResult:
    try:
        result = subprocess.run(
            gate.command,
            cwd=project,
            text=True,
            capture_output=True,
            check=False,
            timeout=gate.timeout_seconds,
        )
    except FileNotFoundError as exc:
        return GateResult(gate.name, "FAILED", tuple(gate.command), None, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return GateResult(
            gate.name,
            "FAILED",
            tuple(gate.command),
            None,
            exc.stdout or "",
            f"timed out after {gate.timeout_seconds}s\n{exc.stderr or ''}",
        )
    return GateResult(
        gate.name,
        "PASSED" if result.returncode == 0 else "FAILED",
        tuple(gate.command),
        result.returncode,
        result.stdout,
        result.stderr,
    )


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _texts(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{field} must be a list of strings")
    return tuple(_required_text(item, field) for item in value)


def _commands(value: Any, field: str) -> tuple[GateCommand, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{field} must be a list")
    return tuple(_command(item, f"{field} entry") for item in value)


def _command(value: Any, field: str) -> GateCommand:
    if isinstance(value, str):
        return GateCommand(value, _split_command(value))
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be a string or object")
    name = _required_text(value.get("name") or value.get("command"), f"{field} name")
    raw_command = value.get("command")
    if isinstance(raw_command, str):
        command = _split_command(raw_command)
    elif isinstance(raw_command, list):
        command = [_required_text(part, f"{field} command") for part in raw_command]
    else:
        raise ContractError(f"{field} command must be a string or list")
    timeout = value.get("timeout_seconds", 120)
    if not isinstance(timeout, int) or timeout < 1:
        raise ContractError(f"{field} timeout_seconds must be a positive integer")
    return GateCommand(name, command, timeout)


def _split_command(command: str) -> list[str]:
    import shlex

    return shlex.split(_required_text(command, "command"), posix=False)
