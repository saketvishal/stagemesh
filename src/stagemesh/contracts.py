from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import GitError, GitWorkspace


class ContractError(ValueError):
    pass


DEPENDENCY_MANIFESTS = (
    "pyproject.toml",
    "poetry.lock",
    "requirements*.txt",
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
class GateCommand:
    name: str
    command: list[str]
    timeout_seconds: int = 120
    cwd: str | None = None  # relative to the gate checkout; never outside it
    env: tuple[tuple[str, str], ...] = ()  # extra environment for this gate only, so test databases are explicit


@dataclass(frozen=True)
class ChangeContract:
    objective: str
    explicit: bool = False
    validation_classification: str | None = None
    validation_risk: str | None = None
    validation_escalation_reasons: tuple[str, ...] = ()
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
    protected_files: tuple[str, ...] = ()
    exclusive_resources: tuple[str, ...] = ()  # named resources (a database, a port) two tasks must never use at once
    max_changed_files: int | None = None
    max_diff_lines: int | None = None

    @property
    def gates(self) -> tuple[GateCommand, ...]:
        return self.required_tests + self.lint + self.typecheck + self.dependency_checks


@dataclass(frozen=True)
class BoundChangeContract:
    contract: ChangeContract
    version: int
    digest: str
    canonical_json: str
    baseline_sha: str | None = None
    candidate_sha: str | None = None

    def evidence_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": self.version,
            "contract_hash": self.digest,
        }
        if self.baseline_sha:
            payload["baseline_sha"] = self.baseline_sha
        if self.candidate_sha:
            payload["candidate_sha"] = self.candidate_sha
        return payload


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    command: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


def _gate_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass(frozen=True)
class ContractEvaluation:
    status: str
    changed_files: tuple[str, ...]
    findings: tuple[dict[str, Any], ...]
    gates: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"


CONTRACT_VERSION = 1  # the only bound-contract schema version this build understands; never derived from stored rows or operator edits


def task_contract_path(project: Path, task_id: str) -> Path | None:
    for path in (
        project / ".stagemesh" / "contracts" / f"{task_id}.json",
        project / ".stagemesh" / "contracts" / f"{task_id}.contract.json",
    ):
        if path.exists():
            return path
    return None


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


def bind_contract(
    project: Path,
    task_id: str | None = None,
    *,
    baseline_sha: str | None = None,
    candidate_sha: str | None = None,
) -> BoundChangeContract:
    contract = load_contract(project, task_id)
    canonical_json = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return BoundChangeContract(contract, CONTRACT_VERSION, digest, canonical_json, baseline_sha, candidate_sha)


def bound_contract_from_record(record: dict[str, Any]) -> BoundChangeContract:
    version = record.get("version")
    digest = record.get("digest")
    canonical_json = record.get("canonical_json")
    if version != CONTRACT_VERSION:
        raise ContractError(f"unsupported bound contract version: {version}")
    if not isinstance(digest, str) or not digest:
        raise ContractError("bound contract hash is missing")
    if not isinstance(canonical_json, str) or not canonical_json:
        raise ContractError("bound contract payload is missing")
    actual = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if actual != digest:
        raise ContractError("bound contract hash does not match payload")
    payload = json.loads(canonical_json)
    contract = parse_contract(payload)
    return BoundChangeContract(
        contract=contract,
        version=version,
        digest=digest,
        canonical_json=canonical_json,
        baseline_sha=record.get("baseline_sha"),
        candidate_sha=record.get("candidate_sha"),
    )


def canonical_contract_json(contract: ChangeContract) -> str:
    return json.dumps(_contract_payload(contract), sort_keys=True, separators=(",", ":"))


def parse_contract(payload: dict[str, Any]) -> ChangeContract:
    if not isinstance(payload, dict):
        raise ContractError("change contract must be a JSON object")
    objective = _required_text(payload.get("objective"), "objective")
    return ChangeContract(
        objective=objective,
        explicit=bool(payload.get("explicit", True)),
        validation_classification=_optional_text(payload.get("validation_classification"), "validation_classification"),
        validation_risk=_optional_text(payload.get("validation_risk"), "validation_risk"),
        validation_escalation_reasons=_texts(
            payload.get("validation_escalation_reasons", ()), "validation_escalation_reasons"
        ),
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
        protected_files=_texts(payload.get("protected_files", ()), "protected_files"),
        exclusive_resources=_texts(payload.get("exclusive_resources", ()), "exclusive_resources"),
        max_changed_files=_optional_positive_int(payload.get("max_changed_files"), "max_changed_files"),
        max_diff_lines=_optional_positive_int(payload.get("max_diff_lines"), "max_diff_lines"),
    )


def evaluate_contract(
    project: Path,
    candidate_sha: str,
    contract: ChangeContract,
    *,
    baseline_sha: str | None = None,
    run_gates: bool = True,
) -> ContractEvaluation:
    try:
        changed = tuple(changed_files(project, candidate_sha, baseline_sha))
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

    if contract.max_changed_files is not None and len(changed) > contract.max_changed_files:
        findings.append(
            {
                "severity": "error",
                "code": "change_size_files_exceeded",
                "message": f"candidate changes {len(changed)} files; limit is {contract.max_changed_files}",
            }
        )

    diff_lines = changed_line_count(project, candidate_sha, baseline_sha) if changed else 0
    if contract.max_diff_lines is not None and diff_lines > contract.max_diff_lines:
        findings.append(
            {
                "severity": "error",
                "code": "change_size_lines_exceeded",
                "message": f"candidate changes {diff_lines} diff lines; limit is {contract.max_diff_lines}",
            }
        )

    findings.extend(candidate_hygiene_findings(project, candidate_sha, list(changed)))

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
        if _matches(path, contract.protected_files):
            findings.append(
                {
                    "severity": "error",
                    "code": "protected_file_changed",
                    "path": path,
                    "message": f"{path} is protected by the change contract",
                }
            )
        if _matches(path, DEPENDENCY_MANIFESTS) and not contract.dependency_checks:
            findings.append(
                {
                    "severity": "error",
                    "code": "dependency_manifest_changed_without_gate",
                    "path": path,
                    "message": f"{path} changes dependencies without a dependency validation gate",
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
        try:
            with candidate_workspace(project, candidate_sha) as gate_project:
                for gate in contract.gates:
                    gates.append(run_gate(gate_project, gate))
                for invariant in contract.invariants:
                    gates.append(run_gate(gate_project, GateCommand(f"invariant:{invariant}", _split_command(invariant))))
        except (GitError, OSError) as exc:
            findings.append(
                {
                    "severity": "error",
                    "code": "candidate_workspace_unavailable",
                    "message": f"candidate {candidate_sha} cannot be checked out for gates: {exc}",
                }
            )

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


NOISE_SEGMENTS = frozenset(
    {".npm-cache", ".npm", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".parcel-cache"}
)
NOISE_FILENAMES = frozenset({".DS_Store", "Thumbs.db", "_update-notifier-last-checked"})
NOISE_SUFFIXES = (".pyc", ".pyo")


def is_noise_path(path: str) -> bool:
    """True for tool cache/noise files that must never be part of a product candidate."""
    parts = path.replace("\\", "/").split("/")
    return (
        any(part in NOISE_SEGMENTS for part in parts[:-1])
        or parts[-1] in NOISE_FILENAMES
        or parts[-1].endswith(NOISE_SUFFIXES)
    )


def candidate_hygiene_findings(project: Path, candidate_sha: str, changed: list[str]) -> list[dict[str, object]]:
    """Findings for cache/noise paths the candidate adds or modifies (pure deletions are allowed)."""
    noisy = [path for path in changed if is_noise_path(path)]
    if not noisy:
        return []
    try:
        present = set(GitWorkspace(project).run("ls-tree", "-r", "--name-only", candidate_sha).stdout.splitlines())
    except GitError:
        present = set(noisy)
    return [
        {
            "severity": "error",
            "code": "candidate_noise_file",
            "path": path,
            "message": f"{path} is a cache/noise file and must not be part of a candidate",
        }
        for path in noisy
        if path in present
    ]


def changed_files(project: Path, candidate_sha: str, baseline_sha: str | None = None) -> list[str]:
    workspace = GitWorkspace(project)
    workspace.run("cat-file", "-e", f"{candidate_sha}^{{commit}}")
    if baseline_sha:
        workspace.run("cat-file", "-e", f"{baseline_sha}^{{commit}}")
        output = workspace.run("diff", "--name-status", "-M", baseline_sha, candidate_sha).stdout
    else:
        parent = workspace.run("rev-list", "--parents", "-n", "1", candidate_sha).stdout.strip().split()
        if len(parent) > 1:
            base = parent[1]
            output = workspace.run("diff", "--name-status", "-M", base, candidate_sha).stdout
        else:
            output = workspace.run("show", "--pretty=", "--name-status", "-M", candidate_sha).stdout
    return sorted(_paths_from_name_status(output))


def changed_line_count(project: Path, candidate_sha: str, baseline_sha: str | None = None) -> int:
    workspace = GitWorkspace(project)
    if baseline_sha:
        workspace.run("cat-file", "-e", f"{baseline_sha}^{{commit}}")
        output = workspace.run("diff", "--numstat", baseline_sha, candidate_sha).stdout
    else:
        parent = workspace.run("rev-list", "--parents", "-n", "1", candidate_sha).stdout.strip().split()
        if len(parent) > 1:
            base = parent[1]
            output = workspace.run("diff", "--numstat", base, candidate_sha).stdout
        else:
            output = workspace.run("show", "--pretty=", "--numstat", candidate_sha).stdout
    total = 0
    for line in output.splitlines():
        parts = line.split("\t")
        for value in parts[:2]:
            if value.isdigit():
                total += int(value)
    return total


@contextmanager
def candidate_workspace(project: Path, candidate_sha: str) -> Iterator[Path]:
    workspace = GitWorkspace(project)
    workspace.run("cat-file", "-e", f"{candidate_sha}^{{commit}}")
    temp_root = Path(tempfile.mkdtemp(prefix="stagemesh-candidate-"))
    target = temp_root / "checkout"
    try:
        workspace.run("worktree", "add", "--detach", str(target), candidate_sha)
        expected = workspace.run("rev-parse", f"{candidate_sha}^{{commit}}").stdout.strip()
        checkout = GitWorkspace(target)
        if checkout.head() != expected:
            raise GitError(f"candidate checkout is not exactly {expected}")
        yield target
        if checkout.head() != expected:  # a gate moved HEAD: the tree that ran is no longer the candidate
            raise GitError(f"a gate moved the candidate checkout off {expected}")
    finally:
        try:
            workspace.run("worktree", "remove", "--force", str(target), check=False)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


def run_gate(project: Path, gate: GateCommand) -> GateResult:
    run_dir = project / gate.cwd if gate.cwd else project
    command = _gate_runtime_command(list(gate.command))
    environment = _gate_environment(run_dir, gate)
    try:
        result = subprocess.run(
            command,
            cwd=run_dir,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            _gate_output_text(exc.stdout),
            f"timed out after {gate.timeout_seconds}s\n{_gate_output_text(exc.stderr)}",
        )
    return GateResult(
        gate.name,
        "PASSED" if result.returncode == 0 else "FAILED",
        tuple(gate.command),
        result.returncode,
        _gate_output_text(result.stdout),
        _gate_output_text(result.stderr),
    )


def _gate_runtime_command(command: list[str]) -> list[str]:
    if not command:
        return command
    resolved = shutil.which(command[0])  # lets "npm" find npm.cmd on Windows without a shell
    if resolved:
        command[0] = resolved
    return command


def _gate_environment(run_dir: Path, gate: GateCommand) -> dict[str, str] | None:
    environment = {**os.environ, **dict(gate.env)}
    src = run_dir / "src"
    if src.is_dir():
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(src) if not existing else str(src) + os.pathsep + existing
    return environment if gate.env or src.is_dir() else None


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    folded = normalized.casefold()
    return any(
        fnmatch.fnmatchcase(normalized, pattern.replace("\\", "/"))
        or fnmatch.fnmatchcase(folded, pattern.replace("\\", "/").casefold())
        for pattern in patterns
    )


def _paths_from_name_status(output: str) -> set[str]:
    paths: set[str] = set()
    for raw in output.splitlines():
        parts = raw.strip().split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        changed_paths = parts[1:]
        if status.startswith(("R", "C")):
            paths.update(path.replace("\\", "/") for path in changed_paths[:2] if path)
        else:
            paths.add(changed_paths[-1].replace("\\", "/"))
    return paths


def contract_prompt(contract: ChangeContract) -> str:
    lines = [
        "Change contract:",
        f"- Objective: {contract.objective}",
        f"- Allowed files: {', '.join(contract.allowed_files)}",
    ]
    if contract.forbidden_files:
        lines.append(f"- Forbidden files: {', '.join(contract.forbidden_files)}")
    if contract.exclusions:
        lines.append(f"- Exclusions: {', '.join(contract.exclusions)}")
    if contract.protected_files:
        lines.append(f"- Protected files: {', '.join(contract.protected_files)}")
    if contract.acceptance_criteria:
        lines.append("- Acceptance criteria:")
        lines.extend(f"  - {item}" for item in contract.acceptance_criteria)
    if contract.max_changed_files is not None:
        lines.append(f"- Max changed files: {contract.max_changed_files}")
    if contract.max_diff_lines is not None:
        lines.append(f"- Max diff lines: {contract.max_diff_lines}")
    lines.append("Stay strictly inside this contract; unrelated edits will be rejected.")
    return "\n".join(lines)


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


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


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
    return GateCommand(name, command, timeout, _gate_cwd(value.get("cwd"), field), _gate_env(value.get("env"), field))


def _gate_cwd(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = _required_text(value, f"{field} cwd").replace("\\", "/")
    parts = text.split("/")
    if text.startswith("/") or ":" in text or ".." in parts:
        raise ContractError(f"{field} cwd must be a relative path inside the checkout")
    return text


def _gate_env(value: Any, field: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()):
        raise ContractError(f"{field} env must be an object of string values")
    return tuple(sorted(value.items()))


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value < 1:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _split_command(command: str) -> list[str]:
    import shlex

    return shlex.split(_required_text(command, "command"), posix=False)


def _contract_payload(contract: ChangeContract) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "objective": contract.objective,
        "explicit": contract.explicit,
        "validation_classification": contract.validation_classification,
        "validation_risk": contract.validation_risk,
        "validation_escalation_reasons": list(contract.validation_escalation_reasons),
        "acceptance_criteria": list(contract.acceptance_criteria),
        "allowed_files": list(contract.allowed_files),
        "forbidden_files": list(contract.forbidden_files),
        "exclusions": list(contract.exclusions),
        "invariants": list(contract.invariants),
        "required_tests": [_gate_payload(gate) for gate in contract.required_tests],
        "lint": [_gate_payload(gate) for gate in contract.lint],
        "typecheck": [_gate_payload(gate) for gate in contract.typecheck],
        "dependency_checks": [_gate_payload(gate) for gate in contract.dependency_checks],
        "public_api": list(contract.public_api),
        "protected_files": list(contract.protected_files),
    }
    if contract.exclusive_resources:  # only when set, so existing contracts keep their digests
        payload["exclusive_resources"] = list(contract.exclusive_resources)
    if contract.max_changed_files is not None:
        payload["max_changed_files"] = contract.max_changed_files
    if contract.max_diff_lines is not None:
        payload["max_diff_lines"] = contract.max_diff_lines
    return payload


def _gate_payload(gate: GateCommand) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": gate.name,
        "command": list(gate.command),
        "timeout_seconds": gate.timeout_seconds,
    }
    if gate.cwd:  # only when set, so existing contracts keep their digests
        payload["cwd"] = gate.cwd
    if gate.env:
        payload["env"] = dict(gate.env)
    return payload
