from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .security import WorkspaceBoundary


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    status: str
    output: str


class AcceptanceValidationError(ValueError):
    pass


def run_check(name: str, command: list[str], cwd: Path) -> AcceptanceCheck:
    name = _validate_text(name, "acceptance check name")
    command = _validate_command(command)
    cwd = _validate_root(cwd)
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    status = "PASS" if result.returncode == 0 else "FAIL"
    return AcceptanceCheck(name, status, result.stdout + result.stderr)


def local_acceptance_report(root: Path, include_acceptance: bool = True) -> dict[str, object]:
    root = _validate_root(root)
    checks = [
        run_check("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py"], root),
        run_check("invariants", [sys.executable, "scripts/invariants.py"], root),
        run_check("provider_acceptance", [sys.executable, "scripts/provider_acceptance.py"], root),
        run_check("github_acceptance", [sys.executable, "scripts/github_acceptance.py"], root),
        run_check("live_acceptance", [sys.executable, "scripts/live_acceptance.py"], root),
        run_check("clean_acceptance", [sys.executable, "scripts/clean_acceptance.py"], root),
        run_check("install", [sys.executable, "-m", "pip", "install", ".", "--target", ".tmp-install", "--no-cache-dir", "--upgrade"], root),
    ]
    if include_acceptance:
        checks.insert(2, run_check("acceptance", [sys.executable, "scripts/acceptance.py"], root))
    return {
        "generated_at": time.time(),
        "status": "PASS" if all(check.status == "PASS" for check in checks) else "FAIL",
        "checks": [
            {"name": check.name, "status": check.status, "output": check.output[-4000:]}
            for check in checks
        ],
    }


def write_acceptance_report(root: Path, output: Path, include_acceptance: bool = True) -> None:
    root = _validate_root(root)
    output = WorkspaceBoundary(root).require_inside(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(local_acceptance_report(root, include_acceptance), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise AcceptanceValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise AcceptanceValidationError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise AcceptanceValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_command(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise AcceptanceValidationError("acceptance command must be a non-empty list")
    return [_validate_text(arg, "acceptance command argument", 1000) for arg in command]


def _validate_root(root: Path) -> Path:
    resolved = Path(root).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise AcceptanceValidationError("acceptance root must be an existing directory")
    return resolved
