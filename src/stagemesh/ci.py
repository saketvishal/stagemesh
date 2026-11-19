from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .qualification import qualification_commands


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    output: str


class CIValidationError(ValueError):
    pass


def run_gate(name: str, command: list[str], cwd: Path) -> GateResult:
    name = _validate_text(name, "ci gate name")
    command = _validate_command(command)
    cwd = _validate_root(cwd)
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return GateResult(name, result.returncode == 0, result.stdout + result.stderr)


def default_gates(root: Path, include_acceptance: bool = True) -> list[GateResult]:
    root = _validate_root(root)
    return [run_gate(name, command, root) for name, command in default_gate_commands(include_acceptance)]


def default_gate_commands(include_acceptance: bool = True) -> list[tuple[str, list[str]]]:
    return qualification_commands(include_acceptance)


def broken_future_feature_gate(root: Path) -> GateResult:
    root = _validate_root(root)
    marker = root / ".stagemesh-broken-feature"
    if marker.exists():
        return GateResult("future-feature", False, "broken future feature marker present")
    return GateResult("future-feature", True, "future feature gate passed")


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise CIValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise CIValidationError(f"{field} must be a non-empty string")
    if len(normalized) > max_length:
        raise CIValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_command(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise CIValidationError("ci gate command must be a non-empty list")
    return [_validate_text(arg, "ci gate command argument", 1000) for arg in command]


def _validate_root(root: Path) -> Path:
    resolved = Path(root).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise CIValidationError("ci gate root must be an existing directory")
    return resolved
