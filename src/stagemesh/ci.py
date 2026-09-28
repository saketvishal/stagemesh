from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    output: str


def run_gate(name: str, command: list[str], cwd: Path) -> GateResult:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return GateResult(name, result.returncode == 0, result.stdout + result.stderr)


def default_gates(root: Path, include_acceptance: bool = True) -> list[GateResult]:
    gates = [
        run_gate("compile", [sys.executable, "-m", "compileall", "-q", "src", "scripts", "build_backend.py"], root),
        run_gate("invariants", [sys.executable, "scripts/invariants.py"], root),
        run_gate("clean_acceptance", [sys.executable, "scripts/clean_acceptance.py"], root),
    ]
    if include_acceptance:
        gates.append(run_gate("acceptance", [sys.executable, "scripts/acceptance.py"], root))
    return gates


def broken_future_feature_gate(root: Path) -> GateResult:
    marker = root / ".stagemesh-broken-feature"
    if marker.exists():
        return GateResult("future-feature", False, "broken future feature marker present")
    return GateResult("future-feature", True, "future feature gate passed")
