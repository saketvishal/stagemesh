"""Deterministic change-contract generation for tasks that have none.

No model is involved: the objective is the task title plus its source description, scope is "everything except protected
paths", and validation gates are detected from the project's own tooling. If no gate can be detected, or the result does not
round-trip through the contract parser, generation fails closed and the operator is told what to do next.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .contracts import ContractError, canonical_contract_json, parse_contract
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store

GENERATED_BY = "stagemesh-auto-plan"
MAX_OBJECTIVE_CHARS = 4000
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

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "gates": list(self.gates), "canonical_chars": self.digest_chars}


def _shell(command: str) -> list[str]:
    return ["cmd", "/d", "/s", "/c", command] if sys.platform == "win32" else ["sh", "-c", command]


# Gate names contain "acceptance" on purpose: validation planning treats that as a broad gate, which the CORE tier below
# requires (it always plans a broad check, and an unscoped contract must really run the project test suite).
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
        if isinstance(test, str) and test.strip() and "no test specified" not in test:
            gates.append({"name": "project-acceptance-npm", "command": _shell("npm test"), "timeout_seconds": 1800})
    has_pytest_config = any((project / name).is_file() for name in ("pytest.ini", "tox.ini", "setup.cfg"))
    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and "pytest" in pyproject.read_text(encoding="utf-8", errors="replace"):
        has_pytest_config = True
    if has_pytest_config or (pyproject.is_file() and (project / "tests").is_dir()):
        gates.append({"name": "project-acceptance-pytest", "command": ["python", "-m", "pytest", "-q"], "timeout_seconds": 1800})
    if (project / "Cargo.toml").is_file():
        gates.append({"name": "project-acceptance-cargo", "command": ["cargo", "test"], "timeout_seconds": 1800})
    if (project / "go.mod").is_file():
        gates.append({"name": "project-acceptance-go", "command": ["go", "test", "./..."], "timeout_seconds": 1800})
    return gates


def task_objective(store: Store, task_id: str) -> str:
    task = store.get_task(task_id)
    if task is None:
        raise AutoPlanError("task_not_found", f"task does not exist: {task_id}", "check the task id")
    objective = str(task["title"]).strip()
    row = store.conn.execute(
        "SELECT state FROM source_cache WHERE source=? AND source_id=?", (task["source"], task["source_id"])
    ).fetchone()
    if row is not None:
        try:
            body = json.loads(row["state"]).get("objective")
        except (TypeError, ValueError):
            body = None
        if isinstance(body, str) and body.strip():
            objective = f"{objective}\n\n{body.strip()}"
    if len(objective) > MAX_OBJECTIVE_CHARS:
        objective = objective[: MAX_OBJECTIVE_CHARS - 15].rstrip() + " [truncated]"
    return objective


def build_contract(store: Store, project: Path, task_id: str, gates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "objective": task_objective(store, task_id),
        "explicit": True,
        # Scope is unknown, so use the highest validation tier: broad gates are allowed to run.
        "validation_classification": "CORE_LIFECYCLE_OR_SCHEMA_SECURITY",
        "validation_escalation_reasons": ["auto-generated contract has no file scope; full project validation required"],
        "acceptance_criteria": [
            "The change fulfils the task objective and nothing else.",
            "Every detected project test gate passes.",
        ],
        "allowed_files": ["**"],
        "forbidden_files": list(FORBIDDEN_FILES),
        "required_tests": gates,
        "max_changed_files": 60,
        "max_diff_lines": 6000,
        "generated_by": GENERATED_BY,
        "source_task": task_id,
    }


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


def create_contract(store: Store, project: Path, task_id: str, *, gate_detector=None) -> AutoPlanResult:
    path = project / ".stagemesh" / "contracts" / f"{task_id}.json"
    if path.exists():
        raise AutoPlanError("contract_exists", f"refusing to overwrite existing contract {path}", "reuse the existing contract")
    gates = (gate_detector or detect_gates)(project)
    if not gates:
        raise AutoPlanError(
            "no_validation_gates",
            "no validation gate could be detected (looked for npm test, pytest, cargo test, go test at the project root)",
            f"write {path} by hand with explicit required_tests, or add a root-level test configuration",
        )
    payload = build_contract(store, project, task_id, gates)
    size = validate_generated(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    names = tuple(str(g["name"]) for g in gates)
    record_audit(store, "contract.auto_generated", {"task_id": task_id, "path": str(path), "gates": list(names), "canonical_chars": size})
    return AutoPlanResult(path, names, size)
