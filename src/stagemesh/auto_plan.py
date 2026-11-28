"""Deterministic change-contract generation for tasks that have none.

No model is involved: the objective is the task title plus its source description, scope is "everything except protected
paths", and validation gates are detected from the project's own tooling. If no gate can be detected, or the result does not
round-trip through the contract parser, generation fails closed and the operator is told what to do next.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .capabilities import capability_policy_snapshot
from .contracts import (
    CONTRACT_VERSION,
    ChangeContract,
    ContractError,
    _matches,
    canonical_contract_json,
    contract_admission_problem,
    parse_contract,
    task_contract_path,
)
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .profile import ProfileError, load_profile, resolve_type
from .profile import build_contract as build_profile_contract

GENERATED_BY = "stagemesh-auto-plan"
LEGACY_PYTEST_GATE = {
    "name": "project-acceptance-pytest",
    "command": ["python", "-m", "pytest", "-q"],
    "timeout_seconds": 1800,
}
MAX_OBJECTIVE_CHARS = 4000
SOURCE_OBJECTIVE_LIMIT = 6000
_PATH_TOKEN_RE = re.compile(r"`([^`\r\n]+)`")
_FILE_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.+)$")
_EXPLICIT_SCOPE_HEADINGS = {"allowed files", "allowed paths"}
_SCOPE_HEADINGS = (
    "allowed files",
    "allowed paths",
    "implementation scope",
    "narrow implementation scope",
    "suggested narrow implementation scope",
)
FORBIDDEN_FILES = (
    ".stagemesh/**",
    ".git/**",
    ".github/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
)


class AutoPlanError(Exception):
    def __init__(self, reason: str, message: str, next_action: str):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.next_action = next_action


@dataclass(frozen=True)
class AutoPlanResult:
    path: Path
    gates: tuple[str, ...]
    digest_chars: int
    profile: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": str(self.path), "gates": list(self.gates), "canonical_chars": self.digest_chars}
        if self.profile:
            data["profile"] = self.profile
        return data


def _shell(command: str) -> list[str]:
    return ["cmd", "/d", "/s", "/c", command] if sys.platform == "win32" else ["sh", "-c", command]


# Gate names contain "acceptance" on purpose: validation planning treats that as a broad gate, which the CORE tier below
# requires (it always plans a broad check, and an unscoped contract must really run the project test suite).

def _project_name(pyproject: Path) -> str | None:
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_project = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[project]":
            in_project = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = False
        if in_project and line.startswith("name") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"\'') or None
    return None


def _stagemesh_smoke_gate(project: Path) -> dict[str, Any] | None:
    required = [
        project / "tests" / "test_run_ready.py",
        project / "tests" / "test_provider_pool.py",
        project / "tests" / "test_canary_regression.py",
        project / "tests" / "test_zero_config_github.py",
    ]
    if not all(path.is_file() for path in required):
        return None
    return {
        "name": "stagemesh-lifecycle-smoke",
        "command": [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_run_ready.py",
            "tests/test_provider_pool.py",
            "tests/test_canary_regression.py",
            "tests/test_zero_config_github.py",
        ],
        "timeout_seconds": 360,
    }

def detect_gates(project: Path) -> list[dict[str, Any]]:
    """Validation gates inferred from conventional project files; empty when nothing conventional is found."""
    gates: list[dict[str, Any]] = []
    package = project / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        test = scripts.get("test") if isinstance(scripts, dict) else None
        check = scripts.get("check") if isinstance(scripts, dict) else None
        if isinstance(check, str) and check.strip():
            # A repository-owned `check` script is normally its aggregate acceptance command.
            # Prefer it to the narrower `test` script so init does not claim partial readiness.
            gates.append(
                {"name": "project-acceptance-npm", "command": _shell("npm run check"), "timeout_seconds": 1800}
            )
        elif isinstance(test, str) and test.strip() and "no test specified" not in test:
            gates.append({"name": "project-acceptance-npm", "command": _shell("npm test"), "timeout_seconds": 1800})
    has_pytest_config = any((project / name).is_file() for name in ("pytest.ini", "tox.ini", "setup.cfg"))
    pyproject = project / "pyproject.toml"
    pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    if pyproject.is_file() and "pytest" in pyproject_text:
        has_pytest_config = True
    if has_pytest_config or (pyproject.is_file() and (project / "tests").is_dir()):
        smoke = _stagemesh_smoke_gate(project) if _project_name(pyproject) == "stagemesh" else None
        gates.append(
            smoke
            or {"name": "project-acceptance-pytest", "command": ["python", "-m", "pytest", "-q"], "timeout_seconds": 1800}
        )
    if (project / "Cargo.toml").is_file():
        gates.append({"name": "project-acceptance-cargo", "command": ["cargo", "test"], "timeout_seconds": 1800})
    if (project / "go.mod").is_file():
        gates.append({"name": "project-acceptance-go", "command": ["go", "test", "./..."], "timeout_seconds": 1800})
    return gates


def cached_state(store: Store, task: Any) -> dict[str, Any]:
    """What the task source last told us about the task (labels, created_at, description)."""
    row = store.conn.execute(
        "SELECT state FROM source_cache WHERE source=? AND source_id=?", (task["source"], task["source_id"])
    ).fetchone()
    if row is None:
        return {}
    try:
        state = json.loads(row["state"])
    except (TypeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def task_labels(store: Store, task: Any) -> tuple[str, ...]:
    labels = cached_state(store, task).get("labels")
    return tuple(labels) if isinstance(labels, list) else ()


def outside_label_selector(store: Store, task: Any, required_label: str | None) -> bool:
    """True when an operator --label selector is active and the task does not carry exactly that label (case-insensitive)."""
    from .selection_scope import drains_candidate, task_problem

    scope = getattr(store, "selection_scope", None)
    if task is None:
        return bool(required_label or scope)
    if scope is None and task is not None and cached_state(store, task).get("selection_scope") is not None:
        return True  # generated scoped children cannot escape via a fresh unfiltered runner
    if scope is not None:
        problem = task_problem(store, task)
        if problem:
            return True
        if drains_candidate(store, task, scope):
            return False
    if not required_label:
        return False
    return required_label.strip().casefold() not in {str(label).casefold() for label in task_labels(store, task)}


def task_text(store: Store, task_id: str) -> tuple[str, str]:
    task = store.get_task(task_id)
    if task is None:
        raise AutoPlanError("task_not_found", f"task does not exist: {task_id}", "check the task id")
    body = cached_state(store, task).get("objective")
    return str(task["title"]).strip(), body.strip() if isinstance(body, str) else ""


def task_objective(store: Store, task_id: str) -> str:
    title, body = task_text(store, task_id)
    objective = f"{title}\n\n{body}" if body else title
    if len(objective) > MAX_OBJECTIVE_CHARS:
        objective = objective[: MAX_OBJECTIVE_CHARS - 15].rstrip() + " [truncated]"
    return objective


def _heading_text(line: str) -> str:
    text = line.strip().lstrip("#").strip().rstrip(":").strip()
    return re.sub(r"\s+", " ", text).lower()


def _scope_heading(line: str) -> bool:
    heading = _heading_text(line)
    return heading == "scope" or any(part in heading for part in _SCOPE_HEADINGS)


def _looks_like_file_scope(token: str) -> bool:
    if not token or token == "**" or any(ch.isspace() for ch in token):
        return False
    if token.startswith("-"):
        return False
    lowered = token.lower()
    if lowered.startswith(("stagemesh ", "python ", "git ", "uv ", "npm ")):
        return False
    return "/" in token or "\\" in token or "*" in token or bool(re.search(r"\.[A-Za-z0-9][A-Za-z0-9_-]*(?:$|\*)", token))


def _safe_task_scope_token(token: str) -> str:
    normalized = token.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        (normalized and not normalized.strip("*?"))
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in normalized.split("/")
    ):
        raise AutoPlanError(
            "invalid_task_scope",
            f"task allowed path must be narrower than the repository and stay inside it: {token}",
            "replace the task Allowed files entry with a project-relative file or bounded glob",
        )
    return normalized


def _scope_within_profile(source: str, profile_scope: tuple[str, ...]) -> bool:
    """Conservative subset check: exact files may match globs; source globs need a provably containing profile prefix."""
    if "**" in profile_scope:
        return True
    if not any(char in source for char in "*?["):
        return _matches(source, profile_scope)
    source_prefix = source.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
    for profile_pattern in profile_scope:
        normalized = profile_pattern.replace("\\", "/")
        if source == normalized:
            return True
        if normalized.endswith("/**"):
            profile_prefix = normalized[:-3].rstrip("/")
            if source_prefix.startswith(profile_prefix + "/"):
                return True
    return False


def _suggested_file_scope(objective: str) -> list[str]:
    """Read the legacy scope hints that can accompany a task without an explicit GitHub allowlist."""
    allowed: list[str] = []
    seen: set[str] = set()
    in_scope = False
    for raw in objective.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if _scope_heading(stripped):
                in_scope = True
                continue
            if in_scope:
                break
        if not in_scope:
            continue
        for match in _PATH_TOKEN_RE.findall(stripped):
            token = match.strip()
            if token == "**":
                _safe_task_scope_token(token)  # raises: a source issue may narrow a profile, never widen it to the repository
            if _looks_like_file_scope(token):
                token = _safe_task_scope_token(token)
                if token not in seen:
                    allowed.append(token)
                    seen.add(token)
    return allowed


def explicit_github_file_scope(body: str) -> list[str] | None:
    """An explicit section has one repository-relative path per Markdown bullet."""
    paths: list[str] = []
    found = False
    in_scope = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            in_scope = _heading_text(line) in _EXPLICIT_SCOPE_HEADINGS
            if in_scope and found:
                raise _invalid_scope_list()
            found = found or in_scope
            continue
        bullet = _FILE_BULLET_RE.fullmatch(line) if in_scope else None
        if bullet is None:
            continue
        entry = bullet[1].strip()
        quoted = _PATH_TOKEN_RE.fullmatch(entry)
        if quoted:
            entry = quoted[1]
        elif "`" in entry or any(char.isspace() for char in entry):
            raise _invalid_scope_list()
        path = _safe_task_scope_token(entry)
        if not path or path == "." or path.endswith("/"):
            raise _invalid_scope_list()
        paths.append(path)
    if found and not paths:
        raise _invalid_scope_list()
    return list(dict.fromkeys(paths)) if found else None


def _invalid_scope_list() -> AutoPlanError:
    return AutoPlanError(
        "invalid_task_scope",
        "the GitHub issue needs one Allowed files section with one repository-relative file or bounded glob per bullet",
        "list each approved path in its own Markdown bullet; use inline code for paths containing spaces",
    )


def _task_suggested_allowed_files(objective: str) -> list[str]:
    """Read GitHub scope, retaining legacy hints when the issue has no explicit list."""
    scope = explicit_github_file_scope(objective)
    return scope if scope is not None else _suggested_file_scope(objective)


def _github_scope(store: Store, task: Any) -> list[str] | None:
    cached = cached_state(store, task)
    if "github_file_scope_error" in cached:
        raise AutoPlanError("invalid_task_scope", str(cached["github_file_scope_error"]), "correct the GitHub issue and refresh it")
    if "github_file_scope" in cached:
        scope = cached["github_file_scope"]
        if scope is None or (isinstance(scope, list) and scope and all(isinstance(path, str) and path for path in scope)):
            return scope
        raise AutoPlanError("incomplete_task_scope", "the cached GitHub file scope is invalid; refresh its source", "refresh GitHub tasks")
    body = str(cached.get("objective") or "")
    if len(body) >= SOURCE_OBJECTIVE_LIMIT:
        raise AutoPlanError(
            "incomplete_task_scope", "the cached GitHub issue may be truncated; refresh its source before planning", "refresh GitHub tasks",
        )
    return explicit_github_file_scope(body)


def _scope_for_task(store: Store, task_id: str, body: str) -> list[str]:
    task = store.get_task(task_id)
    if task is not None and task["source"] == "github":
        scope = _github_scope(store, task)
        return scope if scope is not None else _task_suggested_allowed_files(body)
    return _suggested_file_scope(body)


def source_file_scope_problem(store: Store, task_id: str, contract: ChangeContract) -> str | None:
    """An explicit GitHub allowlist constrains the writable paths of an existing contract."""
    task = store.get_task(task_id)
    if task is None or task["source"] != "github":
        return None
    try:
        source_files = _github_scope(store, task)
    except AutoPlanError as exc:
        return exc.message
    if source_files is None:
        return None
    patterns = tuple(path for path in source_files if any(char in path for char in "*?["))
    writable = {
        path.replace("\\", "/")
        for path in (*contract.allowed_files, *contract.allowed_generated_files, *contract.approved_scope_expansions)
    }
    outside = sorted(path for path in writable if path not in source_files and not _scope_within_profile(path, patterns))
    if outside:
        return (
            f"contract paths outside the issue's explicit file scope: {', '.join(outside)}; "
            f"expected paths from: {', '.join(source_files)}; correct the contract and run `stagemesh rebind-contract --task {task_id}`"
        )
    return None


def build_contract(store: Store, project: Path, task_id: str, gates: list[dict[str, Any]]) -> dict[str, Any]:
    objective = task_objective(store, task_id)
    _, body = task_text(store, task_id)
    allowed_files = _scope_for_task(store, task_id, body) or ["**"]
    no_scope = allowed_files == ["**"]
    return {
        "objective": objective,
        "explicit": True,
        # Scope is unknown, so use the highest validation tier: broad gates are allowed to run.
        "validation_classification": "CORE_LIFECYCLE_OR_SCHEMA_SECURITY",
        "validation_escalation_reasons": [
            "auto-generated contract has no file scope; bounded project smoke required before merge-readiness checks"
            if no_scope
            else "auto-generated contract uses task-provided file scope; bounded project smoke required before merge-readiness checks"
        ],
        "acceptance_criteria": [
            "The change fulfils the task objective and nothing else.",
            "Every generated local smoke gate passes; full-suite/hosted CI remains a separate merge-readiness check.",
        ],
        "allowed_files": allowed_files,
        "forbidden_files": list(FORBIDDEN_FILES),
        "required_tests": gates,
        "max_changed_files": _literal_file_cap(allowed_files, 60),
        "max_diff_lines": 6000,
        "generated_by": GENERATED_BY,
        "source_task": task_id,
    }


def _literal_file_cap(paths: list[str], cap: int) -> int:
    if paths and all(not any(char in path for char in "*?[") for path in paths):
        return min(len(paths), cap)
    return cap


def validate_generated(payload: dict[str, Any]) -> int:
    """Fail closed: the contract must parse, be explicit, carry a real gate and fit the store limit. Returns its size."""
    try:
        contract = parse_contract(payload)
        size = len(canonical_contract_json(contract))
    except (ContractError, ValueError, TypeError) as exc:
        raise AutoPlanError(
            "invalid_generated_contract",
            f"generated contract is invalid: {exc}",
            "write .stagemesh/contracts/<task>.json by hand",
        ) from exc
    gates = payload.get("required_tests") or []
    if any(not isinstance(g, dict) or not g.get("name") or not g.get("command") for g in gates):
        raise AutoPlanError(
            "invalid_generated_contract",
            "generated contract has a validation gate without a name or command",
            "write .stagemesh/contracts/<task>.json by hand",
        )
    if not contract.explicit or not contract.gates:
        raise AutoPlanError(
            "no_validation_gates",
            "generated contract has no executable validation gate",
            "write .stagemesh/contracts/<task>.json by hand",
        )
    if size > MAX_CANONICAL_CONTRACT_CHARS:
        raise AutoPlanError(
            "contract_too_large",
            f"generated contract is {size} characters; the limit is {MAX_CANONICAL_CONTRACT_CHARS}",
            "shorten the task description or write the contract by hand",
        )
    return size


def _gate_signature(gate: Any) -> dict[str, Any]:
    return {
        "name": gate.get("name"),
        "command": gate.get("command"),
        "timeout_seconds": gate.get("timeout_seconds"),
    } if isinstance(gate, dict) else {}


def _is_refreshable_generated_contract(payload: dict[str, Any], replacement_gates: list[dict[str, Any]]) -> bool:
    if payload.get("generated_by") != GENERATED_BY:
        return False
    if [_gate_signature(gate) for gate in payload.get("required_tests", [])] != [LEGACY_PYTEST_GATE]:
        return False
    return any(gate.get("name") == "stagemesh-lifecycle-smoke" for gate in replacement_gates)


def _task_has_passed_evidence(store: Store, task_id: str) -> bool:
    return store.conn.execute("SELECT 1 FROM evidence WHERE task_id=? AND status='PASSED' LIMIT 1", (task_id,)).fetchone() is not None


def refresh_stale_generated_contract(store: Store, project: Path, task_id: str) -> AutoPlanResult | None:
    """Replace old generated broad-pytest contracts with the current project-owned smoke gate.

    This is intentionally narrow. Handwritten contracts are never touched, and generated contracts with recorded evidence are
    left alone so historical proof remains tied to the exact contract it satisfied.
    """
    gates = detect_gates(project)
    if not any(gate.get("name") == "stagemesh-lifecycle-smoke" for gate in gates):
        return None
    path = task_contract_path(project, task_id)
    frozen = store.task_contract(task_id)
    if path is None and frozen is None:
        return None
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path is not None else json.loads(str(frozen["canonical_json"]))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(existing, dict) or not _is_refreshable_generated_contract(existing, gates):
        return None
    if frozen is not None and _task_has_passed_evidence(store, task_id):
        return None

    try:
        if source_file_scope_problem(store, task_id, parse_contract(existing)):
            return None
        if frozen is not None:
            frozen_contract = parse_contract(json.loads(str(frozen["canonical_json"])))
            if frozen_contract.scope_manifest_ref or source_file_scope_problem(store, task_id, frozen_contract):
                return None
        payload = build_contract(store, project, task_id, gates)
        size = validate_generated(payload)
        contract = parse_contract(payload)
    except (ValueError, AutoPlanError):
        return None
    if contract_admission_problem(contract):
        return None
    canonical_json = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    names = tuple(str(gate["name"]) for gate in gates)

    if path is not None:
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, path)
    if frozen is not None:
        candidate = store.latest_candidate(task_id)
        candidate_sha = str(candidate["sha"]) if candidate is not None else None
        baseline_sha = str(frozen["baseline_sha"]) if frozen["baseline_sha"] is not None else None
        capability_policy = capability_policy_snapshot(project, contract.capability_refs)
        store.rebind_task_contract(
            task_id,
            CONTRACT_VERSION,
            digest,
            canonical_json,
            candidate_sha,
            capability_digest=str(capability_policy["digest"]),
            capability_json=str(capability_policy["canonical_json"]),
        )
        if candidate_sha is not None and store.contract_binding(task_id, candidate_sha) is None:
            store.bind_contract(task_id, candidate_sha, baseline_sha, CONTRACT_VERSION, digest, canonical_json)
    record_audit(
        store,
        "contract.auto_refreshed",
        {
            "task_id": task_id,
            "path": str(path) if path is not None else None,
            "from_gates": [LEGACY_PYTEST_GATE["name"]],
            "gates": list(names),
            "canonical_chars": size,
            "frozen": frozen is not None,
        },
    )
    return AutoPlanResult(path or project / ".stagemesh" / "contracts" / f"{task_id}.json", names, size)


def profile_payload(store: Store, project: Path, task_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The profile-built contract and its selection record, or None when the project has no profile."""
    try:
        profile = load_profile(project)
        if profile is None:
            return None
        if profile.contract_generation == "explicit_only":
            raise AutoPlanError(
                "explicit_contract_required",
                "project requires an explicit task contract; automatic contract generation is disabled",
                f"prepare .stagemesh/contracts/{task_id}.json with approved scope and validation gates",
            )
        title, body = task_text(store, task_id)
        task = store.get_task(task_id)
        decision = resolve_type(profile, task_labels(store, task), title, body)
        objective = task_objective(store, task_id)
        payload = build_profile_contract(profile, decision, objective, task_id, GENERATED_BY)
        source_scope = _scope_for_task(store, task_id, body)
        if source_scope:
            profile_scope = tuple(str(path) for path in payload["allowed_files"])
            narrowed = [path for path in source_scope if _scope_within_profile(path, profile_scope)]
            if len(narrowed) != len(source_scope):
                outside = sorted(set(source_scope) - set(narrowed))
                raise AutoPlanError(
                    "task_scope_outside_profile",
                    "the task's explicit allowed files are outside the selected profile task type: " + ", ".join(outside),
                    "correct the issue scope or select the matching project task type",
                )
            payload["allowed_files"] = narrowed
            payload["max_changed_files"] = _literal_file_cap(narrowed, payload["max_changed_files"])
            payload["validation_escalation_reasons"].append(
                "source task provides a narrower explicit file scope than the project profile"
            )
    except ProfileError as exc:
        raise AutoPlanError(
            "invalid_profile", f"project profile is unusable: {exc}", "fix .stagemesh/profile.json (see `stagemesh profile`)"
        ) from exc
    problem = contract_admission_problem(parse_contract(payload))
    if problem:
        raise AutoPlanError("placeholder_validation_gates", problem, "configure real gates or supply an explicit task contract")
    _fit_objective(payload)
    return payload, payload["profile"]


def _fit_objective(payload: dict[str, Any]) -> None:
    """Trim the objective so the canonical contract fits the store limit; gates and scope are never trimmed."""
    for _ in range(3):
        try:
            size = len(canonical_contract_json(parse_contract(payload)))
        except (ContractError, ValueError, TypeError):
            return  # validate_generated reports it
        excess = size - (MAX_CANONICAL_CONTRACT_CHARS - 200)
        if excess <= 0 or len(payload["objective"]) <= 600:
            return
        keep = max(600, len(payload["objective"]) - excess - 15)
        payload["objective"] = payload["objective"][:keep].rstrip() + " [truncated]"


def plannable(store: Store, project: Path, task_id: str) -> str | None:
    """None when a contract can be generated for the task, else the reason it cannot."""
    try:
        planned = profile_payload(store, project, task_id)
        if planned is not None:
            validate_generated(planned[0])
            return None
        gates = detect_gates(project)
        if not gates:
            return "no validation gate can be generated"
        return contract_admission_problem(parse_contract(build_contract(store, project, task_id, gates)))
    except AutoPlanError as exc:
        return exc.message


def create_contract(store: Store, project: Path, task_id: str, *, gate_detector=None) -> AutoPlanResult:
    path = project / ".stagemesh" / "contracts" / f"{task_id}.json"
    if path.exists():
        raise AutoPlanError("contract_exists", f"refusing to overwrite existing contract {path}", "reuse the existing contract")
    profile_info: dict[str, Any] | None = None
    planned = profile_payload(store, project, task_id) if gate_detector is None else None
    if planned is not None:
        payload, profile_info = planned
        gates = payload["required_tests"]
    else:
        gates = (gate_detector or detect_gates)(project)
        if not gates:
            raise AutoPlanError(
                "no_validation_gates",
                "no validation gate could be detected (looked for npm test, pytest, cargo test, go test at the project root)",
                f"write {path} by hand with explicit required_tests, add a root-level test configuration, or add .stagemesh/profile.json",
            )
        payload = build_contract(store, project, task_id, gates)
    size = validate_generated(payload)
    problem = contract_admission_problem(parse_contract(payload))
    if problem:
        raise AutoPlanError("placeholder_validation_gates", problem, "configure real gates or supply an explicit task contract")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    names = tuple(str(g["name"]) for g in gates)
    record_audit(store, "contract.auto_generated", {"task_id": task_id, "path": str(path), "gates": list(names), "canonical_chars": size})
    return AutoPlanResult(path, names, size, profile_info)
