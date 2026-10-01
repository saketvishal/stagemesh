from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    output: str
    elapsed_seconds: float = 0.0


GateRunner = Callable[[str, list[str], Path], GateResult]


class CIValidationError(ValueError):
    pass


def run_gate(name: str, command: list[str], cwd: Path) -> GateResult:
    name = _validate_text(name, "ci gate name")
    command = _validate_command(command)
    cwd = _validate_root(cwd)
    start = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    elapsed = time.perf_counter() - start
    return GateResult(name, result.returncode == 0, result.stdout + result.stderr, round(elapsed, 2))


def default_gates(root: Path, include_acceptance: bool = True, gate_runner: GateRunner | None = None) -> list[GateResult]:
    root = _validate_root(root)
    runner = gate_runner or run_gate
    return [runner(name, command, root) for name, command in default_gate_commands(include_acceptance, root=root)]


def default_gate_commands(include_acceptance: bool = True, root: Path | None = None) -> list[tuple[str, list[str]]]:
    gates: list[tuple[str, list[str]]] = [
        ("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py"]),
    ]
    check_root = root if root is not None else Path.cwd()
    if (check_root / "tests").is_dir():
        gates.append(("unit_tests", [sys.executable, "-m", "pytest", "tests/", "-q"]))
    gates.extend([
        ("invariants", [sys.executable, "scripts/invariants.py"]),
        ("provider_acceptance", [sys.executable, "scripts/provider_acceptance.py"]),
        ("github_acceptance", [sys.executable, "scripts/github_acceptance.py"]),
        ("live_acceptance", [sys.executable, "scripts/live_acceptance.py"]),
        ("clean_acceptance", [sys.executable, "scripts/clean_acceptance.py"]),
    ])
    if include_acceptance:
        gates.append(("acceptance", [sys.executable, "scripts/acceptance.py"]))
    return gates


def broken_future_feature_gate(root: Path) -> GateResult:
    root = _validate_root(root)
    start = time.perf_counter()
    marker = root / ".stagemesh-broken-feature"
    exists = marker.exists()
    elapsed = time.perf_counter() - start
    if exists:
        return GateResult("future-feature", False, "broken future feature marker present", round(elapsed, 4))
    return GateResult("future-feature", True, "future feature gate passed", round(elapsed, 4))


def run_ci(
    root: Path,
    include_acceptance: bool = True,
    future_feature_gate: bool = False,
    gate_runner: GateRunner | None = None,
    on_gate_complete: Callable[[GateResult], None] | None = None,
) -> list[GateResult]:
    root = _validate_root(root)
    runner = gate_runner or run_gate
    commands = default_gate_commands(include_acceptance=include_acceptance, root=root)
    results: list[GateResult] = []
    for name, command in commands:
        res = runner(name, command, root)
        results.append(res)
        if on_gate_complete:
            on_gate_complete(res)
    if future_feature_gate:
        res = broken_future_feature_gate(root)
        results.append(res)
        if on_gate_complete:
            on_gate_complete(res)
    return results


def format_gate_diagnostics(output: str, max_chars: int = 2000) -> str:
    cleaned = output.strip()
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    return f"... [truncated {len(cleaned) - max_chars} chars] ...\n" + cleaned[-max_chars:]


def format_gate_plain(result: GateResult, include_diagnostics: bool = True) -> str:
    status_str = "PASS" if result.passed else "FAIL"
    line = f"{result.name}: {status_str} ({result.elapsed_seconds:.1f}s)"
    if not result.passed and include_diagnostics:
        diag = format_gate_diagnostics(result.output, max_chars=2000)
        if diag:
            return f"{line}\n--- diagnostics for {result.name} ---\n{diag}\n--- end diagnostics ({result.name}) ---"
    return line


def format_ci_plain(results: list[GateResult]) -> str:
    return "\n".join(format_gate_plain(r) for r in results)


def format_ci_json(results: list[GateResult]) -> str:
    overall_pass = all(r.passed for r in results)
    data = {
        "gates": [
            {
                "duration_seconds": round(r.elapsed_seconds, 2),
                "elapsed_seconds": round(r.elapsed_seconds, 2),
                "name": r.name,
                "output": r.output[-4000:],
                "passed": r.passed,
            }
            for r in results
        ],
        "status": "PASS" if overall_pass else "FAIL",
    }
    return json.dumps(data, indent=2, sort_keys=True)


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
