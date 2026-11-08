from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import GitError, GitWorkspace
from .queue_control import OperatorStopRequested


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
AGENT_POLICY_EXCEPTIONS = frozenset(
    {
        "allow_unrequested_abstraction",
        "allow_speculative_code",
        "allow_filler_comments",
        "allow_documentation_churn",
        "allow_policy_change",
    }
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
    spec_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
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
    # Atomic Scope Mode is opt-in so historical frozen contract digests remain
    # byte-for-byte stable.  The existing allowed/forbidden fields remain the
    # write boundary for legacy contracts.
    scope_mode: str | None = None  # STRICT | BOUNDED | EXPLORATORY
    allowed_read_files: tuple[str, ...] = ()
    allowed_generated_files: tuple[str, ...] = ()
    approved_scope_expansions: tuple[str, ...] = ()
    allowed_symbols: tuple[tuple[str, tuple[str, ...]], ...] = ()
    scope_owner: str | None = None
    scope_revision: int | None = None
    scope_exceptions: tuple[str, ...] = ()
    scope_write_enabled: bool = True
    forbid_new_imports: bool = False
    agent_policy_exceptions: tuple[str, ...] = ()
    agent_policy_owner: str | None = None

    @property
    def gates(self) -> tuple[GateCommand, ...]:
        return self.required_tests + self.lint + self.typecheck + self.dependency_checks


_INVARIANT_RUNNERS = frozenset({
    "py", "pytest", "uv", "ruff", "node", "npm", "npx", "pnpm", "yarn", "bun",
    "bash", "sh", "cmd", "powershell", "pwsh", "make", "cmake", "cargo", "go",
    "dotnet", "java", "mvn", "gradle", "git",
})


def _invariant_gate(text: str) -> GateCommand | None:
    if text.startswith("review:"):
        _required_text(text.removeprefix("review:"), "review invariant")
        return None
    explicit = text.startswith("command:")
    command = text.removeprefix("command:").strip() if explicit else text
    # Text alone cannot distinguish prose from a custom program's positional
    # arguments. Preserve clear legacy commands and reject ambiguous strings.
    first, _, rest = command.partition(" ")
    executable = first.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = executable.removesuffix(".exe").removesuffix(".cmd")
    legacy = (stem in _INVARIANT_RUNNERS or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", stem)
              or "/" in first or "\\" in first or not rest
              or rest.lstrip().startswith("-")
              or executable.endswith((".exe", ".cmd", ".bat", ".py", ".sh", ".ps1")))
    if not explicit and not legacy:
        raise ContractError("ambiguous invariant: use 'review:' for prose or put an executable check in required_tests (or prefix it with 'command:')")
    try:
        return GateCommand(f"invariant:{text}", _split_command(command))
    except ValueError as exc:
        raise ContractError(f"invalid executable invariant: {text}") from exc


def invariant_gates(contract: ChangeContract) -> tuple[GateCommand, ...]:
    return tuple(gate for text in contract.invariants if (gate := _invariant_gate(text)) is not None)


def declarative_invariants(contract: ChangeContract) -> tuple[str, ...]:
    return tuple(text for text in contract.invariants if _invariant_gate(text) is None)


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
    failure_kind: str | None = None
    executed_command: tuple[str, ...] = ()  # the argv actually launched; differs from `command` only in its resolved first element
    resolution: dict[str, Any] | None = None  # declared vs executed executable and how one became the other
    cwd: str | None = None
    duration_seconds: float | None = None
    diagnosis: dict[str, Any] | None = None  # timeout class, attribution, explanation and the last progress lines
    output_truncated: bool = False
    input_fingerprint: str | None = None  # complete-input fingerprint (see validation_reuse); None when it could not be computed
    reused_from: str | None = None  # "evidence:<id>:<gate>" for a prior PASSED result, "attempt:<gate>" for a duplicate in this attempt
    policy_hash: str | None = None  # trusted repository policy digest; never inferred from candidate output

    wait_seconds: float = 0.0
    execution_seconds: float = 0.0

    def provenance(self) -> dict[str, Any]:
        """The bounded, redacted execution record stored with validation evidence."""
        from .gate_execution import bounded_output

        stdout, cut_out = bounded_output(self.stdout, 600)
        stderr, cut_err = bounded_output(self.stderr, 600)
        return {
            "declared_command": list(self.command),
            "executed_command": list(self.executed_command),
            "executable_resolution": self.resolution,
            "cwd": self.cwd,
            "duration_seconds": self.duration_seconds,
            "wait_seconds": self.wait_seconds,
            "execution_seconds": self.execution_seconds,
            "diagnosis": self.diagnosis,
            "stdout_tail": stdout,
            "stderr_tail": stderr,
            "output_truncated": cut_out or cut_err or self.output_truncated,
            "input_fingerprint": self.input_fingerprint,
            "reused_from": self.reused_from,
            "policy_hash": self.policy_hash,
        }


def _redacted_tail(text: str) -> str:
    from .gate_execution import bounded_output

    return bounded_output(text)[0]


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


def contract_admission_problem(contract: ChangeContract) -> str | None:
    """Whitespace hygiene alone is never an executable task's validation contract.

    Check commands, not gate names or generation metadata: legacy canonical bindings
    discard that metadata, and renaming the placeholder must not make it executable.
    Structural parsing stays separate so historical contracts remain readable.
    """
    try:
        invariant_gates(contract)
    except ContractError as exc:
        return f"invalid invariant: {exc}; classify legacy requirements explicitly with rebind-contract --review-invariant INDEX or --command-invariant INDEX"
    # A concrete edit target must not also be immutable. Broad allow patterns
    # may legitimately contain protected exceptions, so do not reject those.
    for path in contract.allowed_files:
        if not any(char in path for char in "*?[") and (
            _matches(path, contract.protected_files) or _matches(path, contract.forbidden_files)
        ):
            return f"contract scope conflict: allowed edit target {path} is protected or forbidden; correct the contract before implementation"

    if contract.scope_mode is not None:
        if contract.scope_owner is None:
            return "atomic scope contract requires scope_owner"
        if contract.scope_revision is None:
            return "atomic scope contract requires a positive scope_revision"
        writable = (*contract.allowed_files, *contract.allowed_generated_files, *contract.approved_scope_expansions)
        for path in (*writable, *contract.allowed_read_files, *(path for path, _ in contract.allowed_symbols)):
            normalized = path.replace("\\", "/")
            if normalized.startswith("/") or ":" in normalized or ".." in normalized.split("/"):
                return f"atomic scope path must stay inside the project: {path}"
        if contract.scope_mode == "STRICT" and (
            contract.allowed_files == ("**",)
            or any(any(char in path for char in "*?[") for path in contract.allowed_files)
        ):
            return "STRICT atomic scope requires explicit writable files, not wildcard paths"
        if contract.scope_mode in {"STRICT", "BOUNDED"} and contract.allowed_files == ("**",):
            return f"{contract.scope_mode} atomic scope cannot authorize the entire repository"
        symbol_paths = {path.replace("\\", "/").casefold() for path, _ in contract.allowed_symbols}
        if any(not _matches(path, contract.allowed_files) for path in symbol_paths):
            return "allowed_symbols may only target files already present in allowed_files"
        if contract.approved_scope_expansions and not contract.scope_exceptions:
            return "approved_scope_expansions require a recorded scope_exceptions justification"
    if contract.agent_policy_exceptions:
        if contract.agent_policy_owner is None:
            return "agent policy exceptions require agent_policy_owner"
        unknown = sorted(set(contract.agent_policy_exceptions) - AGENT_POLICY_EXCEPTIONS)
        if unknown:
            return "unknown agent policy exception(s): " + ", ".join(unknown)

    def whitespace_only(gate: GateCommand) -> bool:
        command = gate.command
        program = Path(command[0].replace("\\", "/")).name.casefold() if command else ""
        return program in {"git", "git.exe"} and command[1:] == ["diff", "--check"]

    if contract.gates and all(whitespace_only(gate) for gate in contract.gates):
        return "contract only checks whitespace (git diff --check); supply a task contract with real validation gates"
    return None


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
    try:
        payload = json.loads(canonical_json)
    except ValueError as exc:
        raise ContractError("bound contract payload is not valid JSON") from exc
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
        spec_refs=_relative_paths(payload.get("spec_refs", ()), "spec_refs"),
        skill_refs=_relative_paths(payload.get("skill_refs", ()), "skill_refs"),
        capability_refs=_texts(payload.get("capability_refs", ()), "capability_refs"),
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
        scope_mode=_scope_mode(payload.get("scope_mode")),
        allowed_read_files=_texts(payload.get("allowed_read_files", ()), "allowed_read_files"),
        allowed_generated_files=_texts(payload.get("allowed_generated_files", ()), "allowed_generated_files"),
        approved_scope_expansions=_texts(payload.get("approved_scope_expansions", ()), "approved_scope_expansions"),
        allowed_symbols=_scope_symbols(payload.get("allowed_symbols")),
        scope_owner=_optional_text(payload.get("scope_owner"), "scope_owner"),
        scope_revision=_optional_positive_int(payload.get("scope_revision"), "scope_revision"),
        scope_exceptions=_texts(payload.get("scope_exceptions", ()), "scope_exceptions"),
        scope_write_enabled=_optional_bool(payload.get("scope_write_enabled"), "scope_write_enabled", default=True),
        forbid_new_imports=_optional_bool(payload.get("forbid_new_imports"), "forbid_new_imports", default=False),
        agent_policy_exceptions=_texts(payload.get("agent_policy_exceptions", ()), "agent_policy_exceptions"),
        agent_policy_owner=_optional_text(payload.get("agent_policy_owner"), "agent_policy_owner"),
    )


def evaluate_contract(
    project: Path,
    candidate_sha: str,
    contract: ChangeContract,
    *,
    baseline_sha: str | None = None,
    run_gates: bool = True,
    gate_reuse: Any = None,
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
    task_contract = contract
    try:
        from .quality_policy import (
            QualityPolicyError,
            apply_quality_policy,
            quality_scope_findings,
        )

        contract, applied_quality = apply_quality_policy(
            project,
            contract,
            changed,
            baseline_sha=baseline_sha,
            candidate_sha=candidate_sha,
        )
    except QualityPolicyError as exc:
        return ContractEvaluation(
            status="FAILED",
            changed_files=changed,
            findings=(
                {
                    "severity": "error",
                    "code": "quality_policy_invalid",
                    "message": str(exc),
                },
            ),
            gates=(),
        )
    findings: list[dict[str, Any]] = []
    if applied_quality is not None:
        findings.extend(
            quality_scope_findings(applied_quality.policy, task_contract, changed)
        )
    gates: list[GateResult] = []

    if contract.scope_mode is not None:
        problem = contract_admission_problem(contract)
        if problem:
            findings.append({"severity": "error", "code": "atomic_scope_invalid", "message": problem})
        if changed and not contract.scope_write_enabled:
            findings.append(
                {
                    "severity": "error",
                    "code": "scope_write_not_authorized",
                    "message": f"{contract.scope_mode} scope is read-only; proposed edits require owner authorization",
                }
            )
        findings.extend(_atomic_scope_findings(project, baseline_sha, candidate_sha, contract, changed))

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
        if not _matches(path, _writable_patterns(contract)):
            findings.append(
                {
                    "severity": "error",
                    "code": "OUT_OF_SCOPE_EDIT" if contract.scope_mode is not None else "outside_allowed_files",
                    "path": path,
                    "message": f"{path} is outside the allowed file scope",
                    **(
                        {"next_action": "SCOPE_EXPANSION_REQUIRED"}
                        if contract.scope_mode is not None
                        else {}
                    ),
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
            gates.extend(_run_planned_gates(project, candidate_sha, contract, gate_reuse))
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
                    "failure_kind": gate.failure_kind,
                    "stdout": _redacted_tail(gate.stdout),
                    "stderr": _redacted_tail(gate.stderr),
                    "execution": gate.provenance(),
                }
            )

    return ContractEvaluation(
        status="PASSED" if not findings else "FAILED",
        changed_files=changed,
        findings=tuple(findings),
        gates=tuple(gates),
    )


def _run_planned_gates(project: Path, candidate_sha: str, contract: ChangeContract, gate_reuse: Any) -> list[GateResult]:
    """Run each distinct gate once, reuse a prior PASSED result only for identical complete inputs, keep the declared order."""
    from dataclasses import replace

    from .validation_reuse import gate_identity

    planned = [*contract.gates, *invariant_gates(contract)]
    slots: list[GateResult | tuple[str, GateCommand] | None] = []
    to_run: dict[tuple[Any, ...], GateCommand] = {}
    fingerprints: dict[tuple[Any, ...], str | None] = {}
    for gate in planned:
        identity = gate_identity(gate)
        if identity in to_run or identity in fingerprints:
            slots.append(("duplicate", gate))
            continue
        fingerprints[identity] = gate_reuse.fingerprint(gate) if gate_reuse is not None else None
        hit = gate_reuse.lookup(gate) if gate_reuse is not None else None
        if hit is not None:
            fingerprint, prior = hit
            source = f"evidence:{prior['evidence_id']}:{prior['gate']}"
            reused = GateResult(
                gate.name, "PASSED", tuple(gate.command), 0, "", "", None, input_fingerprint=fingerprint, reused_from=source,
                cwd=gate.cwd, policy_hash=dict(gate.env).get("STAGEMESH_QUALITY_POLICY_SHA256"),
            )
            gate_reuse.reused.append({"gate": gate.name, "from": source, "candidate_sha": prior["candidate_sha"], "fingerprint": fingerprint})
            slots.append(reused)
            continue
        to_run[identity] = gate
        slots.append(("run", gate))
    if to_run and gate_reuse is not None and gate_reuse.reused:
        # Gates in one checkout can depend on each other's side effects (a build before its tests). Reuse is therefore
        # all-or-nothing: if any distinct gate must run, every distinct gate runs in the fresh checkout.
        gate_reuse.reused.clear()
        to_run, slots = {}, []
        for gate in planned:
            identity = gate_identity(gate)
            if identity in to_run:
                slots.append(("duplicate", gate))
            else:
                to_run[identity] = gate
                slots.append(("run", gate))
    executed: dict[tuple[Any, ...], GateResult] = {}
    if to_run:
        with candidate_workspace(project, candidate_sha) as gate_project:
            for identity, gate in to_run.items():
                result = run_gate(gate_project, gate)
                executed[identity] = replace(result, input_fingerprint=fingerprints.get(identity))
    results: list[GateResult] = []
    shared: dict[tuple[Any, ...], GateResult] = {}
    for slot in slots:
        if isinstance(slot, GateResult):
            results.append(slot)
            shared[gate_identity(planned[len(results) - 1])] = slot
            continue
        kind, gate = slot  # type: ignore[misc]
        identity = gate_identity(gate)
        if kind == "run":
            results.append(executed[identity])
            shared[identity] = executed[identity]
        else:
            first = shared.get(identity) or executed[identity]
            results.append(replace(first, name=gate.name, reused_from=f"attempt:{first.name}"))
    return results


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
    if baseline_sha:
        # `git diff` resolves and validates both commits itself. Separate
        # `cat-file` probes doubled Windows process creation without adding a
        # stronger fail-closed condition.
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
    temp_root = Path(tempfile.mkdtemp(prefix="stagemesh-candidate-"))
    target = temp_root / "checkout"
    retain = False
    try:
        workspace.run("worktree", "add", "--detach", str(target), candidate_sha)
        expected = workspace.run("rev-parse", f"{candidate_sha}^{{commit}}").stdout.strip()
        checkout = GitWorkspace(target)
        if checkout.head() != expected:
            raise GitError(f"candidate checkout is not exactly {expected}")
        from .candidate_environments import prepare_candidate_environments

        try:
            prepare_candidate_environments(project, target)
        except (TypeError, ValueError, subprocess.SubprocessError) as exc:
            raise OSError(f"candidate validation environment unavailable: {exc}") from exc
        yield target
        if checkout.head() != expected:  # a gate moved HEAD: the tree that ran is no longer the candidate
            raise GitError(f"a gate moved the candidate checkout off {expected}")
    except OperatorStopRequested as exc:
        if not exc.cleanup_complete:
            retain = True
            exc.retained_workspace = str(target)
        raise
    finally:
        if not retain:
            try:
                workspace.run(
                    "worktree",
                    "remove",
                    "--force",
                    str(target),
                    check=False,
                    destructive_reason="remove a StageMesh-created ephemeral candidate validation worktree after its owned process ended",
                )
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)


def _run_gate_process(
    command: list[str], cwd: Path, env: dict[str, str] | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    """A failed cleanup cannot become a timeout verdict or release owned work."""
    from .gate_process import GateCleanupUnconfirmed, spawn_gate_process, terminate_owned_gate

    proc, identity = spawn_gate_process(command, cwd, env)

    def unconfirmed(note: str, *causes: BaseException) -> GateCleanupUnconfirmed:
        error = GateCleanupUnconfirmed(note, identity, cwd=str(cwd))
        error.monitor_failed = any(getattr(cause, "monitor_failed", False) for cause in causes)
        return error

    from .validation_slot import _stop_requested

    def check_control() -> None:
        try:
            stopped = _stop_requested()
        except Exception as monitor_error:
            raise OperatorStopRequested(
                "validation stop monitoring failed: " + type(monitor_error).__name__, monitor_failed=True,
            ) from monitor_error
        if stopped:
            raise OperatorStopRequested("active validation gate cancelled by control request or deadline")

    deadline = time.monotonic() + timeout
    completed = False
    try:
        while True:
            check_control()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                continue
            completed = True  # ordinary root and pipe completion, before accepting output
            check_control()
            break
    except BaseException as exc:
        if completed:
            # A stop observed after ordinary completion still suppresses the verdict.
            # No new termination attempt or detached-child guarantee is implied.
            raise
        try:
            termination = terminate_owned_gate(proc, identity)
        except BaseException as cleanup_error:  # even interrupted cleanup must retain uncertain ownership
            raise unconfirmed(type(cleanup_error).__name__, exc, cleanup_error) from cleanup_error
        if not termination.cleanup_confirmed:
            raise unconfirmed(termination.note, exc) from exc
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except BaseException as drain_error:
            raise unconfirmed("owned pipes did not drain after termination", exc, drain_error) from drain_error
        if not isinstance(exc, subprocess.TimeoutExpired):
            raise
        expired = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        expired.stagemesh_note = termination.note  # type: ignore[attr-defined]
        raise expired from exc
    return subprocess.CompletedProcess(command, proc.returncode if proc.returncode is not None else 1, stdout, stderr)


@contextmanager
def _gate_temporary_directory() -> Iterator[str]:
    # Explicit cleanup avoids TemporaryDirectory's GC finalizer deleting retained files.
    path = tempfile.mkdtemp(prefix="stagemesh-gate-")
    retain = False
    try:
        yield path
    except OperatorStopRequested as exc:
        if not exc.cleanup_complete:
            retain = True
            exc.retained_gate_temp = path
        raise
    finally:
        if not retain:
            shutil.rmtree(path, ignore_errors=True)


def run_gate(project: Path, gate: GateCommand) -> GateResult:
    from .gate_execution import bounded_output, classify_timeout, resolve_gate_executable
    from .validation_slot import validation_slot

    run_dir = project / gate.cwd if gate.cwd else project
    declared = tuple(gate.command)
    environment = _gate_environment(run_dir, gate)
    resolution = resolve_gate_executable(list(gate.command), run_dir, search_path=(environment or os.environ).get("PATH"))
    command = list(gate.command)
    if resolution is not None and resolution.executed:
        command[0] = resolution.executed
    provenance = {
        "executed_command": tuple(command),
        "resolution": None if resolution is None else resolution.to_dict(),
        "cwd": str(run_dir),
    }
    policy_hash = dict(gate.env).get("STAGEMESH_QUALITY_POLICY_SHA256")
    if resolution is not None and not resolution.launchable:
        # An explicit executable that is missing is a launch failure. Substituting a same-named PATH program would run something
        # the gate never declared, so the gate fails with the exact path that was looked for.
        return GateResult(
            gate.name, "FAILED", declared, None, "", resolution.detail, "environment",
            **{**provenance, "executed_command": ()},
            diagnosis={"timeout_class": None, "attribution": "environment", "explanation": resolution.detail, "progress": []},
            policy_hash=policy_hash,
        )
    started = time.monotonic()
    waited = 0.0
    ran = 0.0
    try:
        with ExitStack() as resources:
            # Provider sandboxes can create pytest temp roots unreadable by the
            # validator. Each gate needs its own host-owned temporary directory.
            temp_keys = ("TMPDIR", "TEMP", "TMP")
            if not set(temp_keys).intersection(dict(gate.env)):
                temp_root = resources.enter_context(_gate_temporary_directory())
                environment = {**(environment or os.environ), **dict.fromkeys(temp_keys, temp_root)}
            with validation_slot() as waited:
                from .validation_slot import raise_if_validation_blocked

                raise_if_validation_blocked()
                started = time.monotonic()
                try:
                    result = _run_gate_process(command, run_dir, environment, gate.timeout_seconds)
                finally:
                    ran = time.monotonic() - started
    except OSError as exc:
        return GateResult(
            gate.name, "FAILED", declared, None, "", str(exc), "environment",
            duration_seconds=round(time.monotonic() - started, 3), **provenance,
            policy_hash=policy_hash,
        )
    except subprocess.TimeoutExpired as exc:
        out, err = _gate_output_text(exc.stdout), _gate_output_text(exc.stderr)
        diagnosis = classify_timeout(out, err)
        cleanup_note = getattr(exc, "stagemesh_note", "process cleanup not established; no exit code")
        return GateResult(
            gate.name,
            "FAILED",
            declared,
            None,
            bounded_output(out, 4000)[0],
            f"timed out after {gate.timeout_seconds}s; {cleanup_note}\nwaited {waited:.3f}s; ran {ran:.3f}s\n{bounded_output(err, 4000)[0]}",
            "timeout",
            duration_seconds=round(time.monotonic() - started, 3),
            diagnosis=diagnosis,
            output_truncated=len(out) > 4000 or len(err) > 4000,
            wait_seconds=waited, execution_seconds=ran,
            **provenance,
            policy_hash=policy_hash,
        )
    return GateResult(
        gate.name,
        "PASSED" if result.returncode == 0 else "FAILED",
        declared,
        result.returncode,
        _gate_output_text(result.stdout),
        _gate_output_text(result.stderr),
        duration_seconds=round(time.monotonic() - started, 3),
        wait_seconds=waited, execution_seconds=ran,
        **provenance,
        policy_hash=policy_hash,
    )


def _gate_runtime_command(command: list[str], run_dir: Path | None = None) -> list[str]:
    """The command with its executable resolved for `run_dir` (see gate_execution); kept for callers that only need argv."""
    from .gate_execution import resolve_gate_executable

    resolution = resolve_gate_executable(command, run_dir or Path.cwd())
    if resolution is not None and (resolution.executed or resolution.path):
        command[0] = resolution.executed or str(resolution.path)  # an explicit path stays explicit even if nothing exists there
    return command


def _gate_environment(run_dir: Path, gate: GateCommand) -> dict[str, str] | None:
    environment = {**os.environ, **dict(gate.env)}
    # A child that buffers its stdout in a pipe prints nothing before a timeout, which makes every hang look identical.
    environment.setdefault("PYTHONUNBUFFERED", "1")
    src = run_dir / "src"
    if src.is_dir():
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(src) if not existing else str(src) + os.pathsep + existing
    return environment


def scope_policy_payload(contract: ChangeContract) -> dict[str, Any]:
    """Canonical provider-neutral write policy embedded in a bound task contract."""
    return {
        "mode": contract.scope_mode,
        "allowed_write_files": list(contract.allowed_files),
        "allowed_read_files": list(contract.allowed_read_files),
        "allowed_generated_files": list(contract.allowed_generated_files),
        "approved_scope_expansions": list(contract.approved_scope_expansions),
        "allowed_symbols": {path: list(symbols) for path, symbols in contract.allowed_symbols},
        "owner": contract.scope_owner,
        "revision": contract.scope_revision,
        "exceptions": list(contract.scope_exceptions),
        "write_enabled": contract.scope_write_enabled,
        "forbid_new_imports": contract.forbid_new_imports,
    }


def scope_policy_hash(contract: ChangeContract) -> str:
    canonical = json.dumps(scope_policy_payload(contract), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _writable_patterns(contract: ChangeContract) -> tuple[str, ...]:
    return (*contract.allowed_files, *contract.allowed_generated_files, *contract.approved_scope_expansions)


def _atomic_scope_findings(
    project: Path,
    baseline_sha: str | None,
    candidate_sha: str,
    contract: ChangeContract,
    changed: tuple[str, ...],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    symbol_policy = dict(contract.allowed_symbols)
    for path in changed:
        symbols = symbol_policy.get(path.replace("\\", "/"))
        if symbols:
            reason = _python_symbol_scope_problem(project, baseline_sha, candidate_sha, path, symbols)
            if reason:
                findings.append(
                    {
                        "severity": "error",
                        "code": "scope_symbol_verification_required",
                        "path": path,
                        "message": reason,
                    }
                )
    if contract.forbid_new_imports:
        for path in changed:
            if not path.casefold().endswith(".py"):
                continue
            reason = _new_python_import_problem(project, baseline_sha, candidate_sha, path)
            if reason:
                findings.append(
                    {
                        "severity": "error",
                        "code": "scope_new_import_forbidden",
                        "path": path,
                        "message": reason,
                    }
                )
    return findings


def _git_source(project: Path, sha: str | None, path: str) -> str | None:
    if not sha:
        return None
    normalized = path.replace("\\", "/")
    result = GitWorkspace(project).run("show", f"{sha}:{normalized}", check=False)
    return result.stdout if result.returncode == 0 else None


def _symbol_spans(source: str) -> dict[str, tuple[int, int]]:
    tree = ast.parse(source)
    spans: dict[str, tuple[int, int]] = {}

    def visit(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{node.name}" if prefix else node.name
                decorators = getattr(node, "decorator_list", ())
                start = min([node.lineno, *(item.lineno for item in decorators)])
                spans[name] = (start, int(node.end_lineno or node.lineno))
                visit(node.body, name)

    visit(tree.body)
    return spans


def _without_spans(source: str, spans: list[tuple[int, int]]) -> str:
    removed = {line for start, end in spans for line in range(start, end + 1)}
    return "\n".join(text for line, text in enumerate(source.splitlines(), 1) if line not in removed).strip()


def _python_symbol_scope_problem(
    project: Path, baseline_sha: str | None, candidate_sha: str, path: str, symbols: tuple[str, ...]
) -> str | None:
    if not path.casefold().endswith(".py"):
        return "function-level scope has no language-aware verifier for this file; independent approval is required"
    before = _git_source(project, baseline_sha, path)
    after = _git_source(project, candidate_sha, path)
    if before is None or after is None:
        return "function-level scope cannot verify an added, deleted, or baseline-less file"
    try:
        before_spans = _symbol_spans(before)
        after_spans = _symbol_spans(after)
    except (SyntaxError, ValueError):
        return "function-level scope cannot parse both Python revisions"
    missing = [name for name in symbols if name not in before_spans or name not in after_spans]
    if missing:
        return "function-level scope cannot locate the allowed symbol(s) in both revisions: " + ", ".join(missing)
    old_outside = _without_spans(before, [before_spans[name] for name in symbols])
    new_outside = _without_spans(after, [after_spans[name] for name in symbols])
    if old_outside != new_outside:
        return "candidate changes content outside the authorized symbol(s): " + ", ".join(symbols)
    return None


def _new_python_import_problem(project: Path, baseline_sha: str | None, candidate_sha: str, path: str) -> str | None:
    before = _git_source(project, baseline_sha, path)
    after = _git_source(project, candidate_sha, path)
    if before is None or after is None:
        return "new-import policy cannot verify an added, deleted, or baseline-less Python file"
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except (SyntaxError, ValueError):
        return "new-import policy cannot parse both Python revisions"

    def imports(tree: ast.AST) -> set[str]:
        return {ast.dump(node, include_attributes=False) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))}

    added = imports(new_tree) - imports(old_tree)
    return f"candidate introduces {len(added)} new Python import statement(s)" if added else None


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
    if contract.invariants:
        lines.append("- Required invariants (prose constraints must be checked during review, not executed as commands):")
        lines.extend(f"  - {item}" for item in contract.invariants)
    if contract.spec_refs:
        lines.append("- Project specs:")
        lines.extend(f"  - {item}" for item in contract.spec_refs)
    if contract.skill_refs:
        lines.append("- Project skills:")
        lines.extend(f"  - {item}" for item in contract.skill_refs)
    if contract.capability_refs:
        lines.append("- Registered capabilities (must be enabled and provider-compatible):")
        lines.extend(f"  - {item}" for item in contract.capability_refs)
    if contract.max_changed_files is not None:
        lines.append(f"- Max changed files: {contract.max_changed_files}")
    if contract.max_diff_lines is not None:
        lines.append(f"- Max diff lines: {contract.max_diff_lines}")
    if contract.scope_mode is not None:
        lines.extend(
            [
                f"- Atomic scope mode: {contract.scope_mode}",
                f"- Scope owner: {contract.scope_owner}",
                f"- Scope revision: {contract.scope_revision}",
                f"- Scope policy hash: {scope_policy_hash(contract)}",
                f"- Writes authorized: {'yes' if contract.scope_write_enabled else 'no'}",
            ]
        )
        if contract.allowed_read_files:
            lines.append(f"- Allowed read paths: {', '.join(contract.allowed_read_files)}")
        if contract.allowed_generated_files:
            lines.append(f"- Allowed generated files: {', '.join(contract.allowed_generated_files)}")
        if contract.approved_scope_expansions:
            lines.append(f"- Owner-approved scope expansions: {', '.join(contract.approved_scope_expansions)}")
        if contract.allowed_symbols:
            lines.append("- Allowed symbols (all other content in these files is read-only):")
            lines.extend(f"  - {path}: {', '.join(symbols)}" for path, symbols in contract.allowed_symbols)
        if contract.scope_exceptions:
            lines.append("- Recorded scope exceptions:")
            lines.extend(f"  - {item}" for item in contract.scope_exceptions)
        if contract.forbid_new_imports:
            lines.append("- New imports/dependencies are forbidden by this task policy.")
    lines.extend(
        [
            "Scope discipline:",
            "- Allowed files constrain edits, not read-only investigation.",
            "- When Allowed read paths are listed, keep investigation inside that explicit read boundary.",
            "- Read directly relevant project source, tests, configuration, and specifications to understand the task; respect project access rules and never read secrets or unrelated private data.",
            "- Do not edit unrelated files, broad refactors, generated noise, dependency manifests, or configuration outside this contract.",
            "- If the task truly needs to edit another path, stop and request an audited scope expansion instead of editing it.",
            "- Out-of-scope edits are rejected before integration.",
        ]
    )
    return "\n".join(lines)


def scope_classification(contract: ChangeContract, changed: tuple[str, ...] | list[str]) -> dict[str, list[str]]:
    """Stable, operator-visible grouping of a candidate diff against its contract scope."""
    result = {
        "in_scope": [],
        "outside_allowed": [],
        "excluded": [],
        "forbidden": [],
        "protected": [],
        "dependency_manifest_without_gate": [],
        "noise": [],
    }
    if contract.scope_mode is not None:
        result["generated"] = []
        result["approved_expansion"] = []
    for path in changed:
        if _matches(path, contract.allowed_files):
            result["in_scope"].append(path)
        elif _matches(path, contract.allowed_generated_files):
            result["in_scope"].append(path)
            result["generated"].append(path)
        elif _matches(path, contract.approved_scope_expansions):
            result["in_scope"].append(path)
            result["approved_expansion"].append(path)
        else:
            result["outside_allowed"].append(path)
        if _matches(path, contract.exclusions):
            result["excluded"].append(path)
        if _matches(path, contract.forbidden_files):
            result["forbidden"].append(path)
        if _matches(path, contract.protected_files):
            result["protected"].append(path)
        if _matches(path, DEPENDENCY_MANIFESTS) and not contract.dependency_checks:
            result["dependency_manifest_without_gate"].append(path)
        if is_noise_path(path):
            result["noise"].append(path)
    return {key: sorted(values) for key, values in result.items()}


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


def _relative_paths(value: Any, field: str) -> tuple[str, ...]:
    paths = _texts(value, field)
    for path in paths:
        normalized = path.replace("\\", "/")
        parts = normalized.split("/")
        if normalized.startswith("/") or ":" in normalized or ".." in parts:
            raise ContractError(f"{field} entries must be relative paths inside the project")
    return paths


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _scope_mode(value: Any) -> str | None:
    if value is None:
        return None
    mode = _required_text(value, "scope_mode").upper()
    if mode not in {"STRICT", "BOUNDED", "EXPLORATORY"}:
        raise ContractError("scope_mode must be STRICT, BOUNDED, or EXPLORATORY")
    return mode


def _scope_symbols(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ContractError("allowed_symbols must be an object mapping paths to symbol lists")
    rows = []
    for path, symbols in value.items():
        clean_path = _required_text(path, "allowed_symbols path").replace("\\", "/")
        rows.append((clean_path, _texts(symbols, f"allowed_symbols[{clean_path}]")))
    return tuple(sorted(rows, key=lambda row: row[0].casefold()))


def _optional_bool(value: Any, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise ContractError(f"{field} must be a boolean")
    return value


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
    if contract.spec_refs:
        payload["spec_refs"] = list(contract.spec_refs)
    if contract.skill_refs:
        payload["skill_refs"] = list(contract.skill_refs)
    if contract.capability_refs:
        payload["capability_refs"] = list(contract.capability_refs)
    if contract.exclusive_resources:  # only when set, so existing contracts keep their digests
        payload["exclusive_resources"] = list(contract.exclusive_resources)
    if contract.max_changed_files is not None:
        payload["max_changed_files"] = contract.max_changed_files
    if contract.max_diff_lines is not None:
        payload["max_diff_lines"] = contract.max_diff_lines
    if contract.scope_mode is not None:
        payload.update(
            {
                "scope_mode": contract.scope_mode,
                "allowed_read_files": list(contract.allowed_read_files),
                "allowed_generated_files": list(contract.allowed_generated_files),
                "approved_scope_expansions": list(contract.approved_scope_expansions),
                "allowed_symbols": {path: list(symbols) for path, symbols in contract.allowed_symbols},
                "scope_owner": contract.scope_owner,
                "scope_revision": contract.scope_revision,
                "scope_exceptions": list(contract.scope_exceptions),
                "scope_write_enabled": contract.scope_write_enabled,
                "forbid_new_imports": contract.forbid_new_imports,
            }
        )
    if contract.agent_policy_exceptions:
        payload["agent_policy_exceptions"] = list(contract.agent_policy_exceptions)
        payload["agent_policy_owner"] = contract.agent_policy_owner
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
