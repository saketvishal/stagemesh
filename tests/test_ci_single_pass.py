"""
Tests for StageMesh CI Performance / Single-Pass Validation v1.

Verifies:
A. Full CI gate list contains each expected gate once.
B. --skip-acceptance excludes only the main acceptance gate and does not cause recursion.
C. Plain and JSON formatting can be tested without running expensive real gates.
D. A failed synthetic gate:
   - returns CI failure;
   - contains useful diagnostic output.
E. Gate duration is reported.
F. scripts/acceptance.py does not spawn stagemesh ci recursively (behavioral proof).
"""

from __future__ import annotations

import io
import json
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from stagemesh.ci import (
    GateResult,
    broken_future_feature_gate,
    default_gate_commands,
    default_gates,
    format_ci_json,
    format_ci_plain,
    format_gate_diagnostics,
    format_gate_plain,
    run_ci,
    run_gate,
)
from stagemesh.cli import command_ci


def test_full_ci_gate_list_contains_expected_gates_once(tmp_path: Path):
    """A. Full CI gate list contains each expected gate once."""
    (tmp_path / "tests").mkdir()
    commands = default_gate_commands(include_acceptance=True, root=tmp_path)
    gate_names = [name for name, _ in commands]

    expected = [
        "compile",
        "unit_tests",
        "invariants",
        "provider_acceptance",
        "github_acceptance",
        "live_acceptance",
        "clean_acceptance",
        "acceptance",
    ]
    assert gate_names == expected
    assert len(gate_names) == len(set(gate_names))

    # With future-feature gate in run_ci:
    invocations: list[str] = []

    def stub_runner(name: str, cmd: list[str], cwd: Path) -> GateResult:
        invocations.append(name)
        return GateResult(name, True, f"{name}: ok\n", 0.01)

    results = run_ci(
        root=tmp_path,
        include_acceptance=True,
        future_feature_gate=True,
        gate_runner=stub_runner,
    )
    result_names = [r.name for r in results]
    assert result_names == expected + ["future-feature"]
    assert len(result_names) == len(set(result_names))
    assert invocations == expected


def test_skip_acceptance_excludes_only_acceptance_and_no_recursion(tmp_path: Path):
    """B. --skip-acceptance excludes only the main acceptance gate and does not cause recursion."""
    (tmp_path / "tests").mkdir()
    commands = default_gate_commands(include_acceptance=False, root=tmp_path)
    gate_names = [name for name, _ in commands]

    assert "acceptance" not in gate_names
    expected = [
        "compile",
        "unit_tests",
        "invariants",
        "provider_acceptance",
        "github_acceptance",
        "live_acceptance",
        "clean_acceptance",
    ]
    assert gate_names == expected
    assert len(gate_names) == len(set(gate_names))

    executed: list[str] = []

    def stub_runner(name: str, cmd: list[str], cwd: Path) -> GateResult:
        executed.append(name)
        return GateResult(name, True, "ok\n", 0.02)

    buf = io.StringIO()
    args = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=False,
    )
    code = command_ci(args, gate_runner=stub_runner, stdout=buf)
    assert code == 0
    assert "acceptance" not in executed
    assert executed == expected
    assert len(executed) == len(set(executed))


def test_plain_and_json_formatting_tested_without_expensive_gates():
    """C. Plain and JSON formatting can be tested without running expensive real gates."""
    synthetic_results = [
        GateResult("compile", True, "compilation clean\n", 1.25),
        GateResult("unit_tests", True, "259 passed\n", 45.10),
        GateResult("invariants", True, "invariants: passed\n", 8.30),
    ]

    plain = format_ci_plain(synthetic_results)
    assert "compile: PASS (1.2s)" in plain or "compile: PASS (1.3s)" in plain
    assert "unit_tests: PASS (45.1s)" in plain
    assert "invariants: PASS (8.3s)" in plain

    json_text = format_ci_json(synthetic_results)
    data = json.loads(json_text)
    assert data["status"] == "PASS"
    assert len(data["gates"]) == 3
    gate_map = {g["name"]: g for g in data["gates"]}
    assert gate_map["compile"]["passed"] is True
    assert gate_map["compile"]["elapsed_seconds"] == 1.25
    assert gate_map["compile"]["duration_seconds"] == 1.25
    assert "compilation clean" in gate_map["compile"]["output"]


def test_failed_synthetic_gate_returns_failure_with_useful_diagnostics(tmp_path: Path):
    """D. A failed synthetic gate returns CI failure and contains useful diagnostic output."""
    error_text = "AssertionError: line 42 failed in invariant check\nDetailed trace:\n  foo -> bar"
    synthetic_results = [
        GateResult("compile", True, "ok\n", 0.5),
        GateResult("invariants", False, error_text, 1.2),
    ]

    plain = format_ci_plain(synthetic_results)
    assert "compile: PASS" in plain
    assert "invariants: FAIL (1.2s)" in plain
    assert "--- diagnostics for invariants ---" in plain
    assert error_text in plain
    assert "--- end diagnostics (invariants) ---" in plain

    def failing_runner(name: str, cmd: list[str], cwd: Path) -> GateResult:
        if name == "invariants":
            return GateResult(name, False, error_text, 1.2)
        return GateResult(name, True, "ok\n", 0.5)

    buf = io.StringIO()
    args_plain = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=False,
    )
    code = command_ci(args_plain, gate_runner=failing_runner, stdout=buf)
    assert code == 1
    out_plain = buf.getvalue()
    assert "invariants: FAIL" in out_plain
    assert "AssertionError: line 42 failed in invariant check" in out_plain

    json_buf = io.StringIO()
    args_json = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=True,
    )
    code_json = command_ci(args_json, gate_runner=failing_runner, stdout=json_buf)
    assert code_json == 1
    data = json.loads(json_buf.getvalue())
    assert data["status"] == "FAIL"
    failing_gate = next(g for g in data["gates"] if g["name"] == "invariants")
    assert failing_gate["passed"] is False
    assert error_text in failing_gate["output"]


def test_diagnostics_truncation():
    """Verify format_gate_diagnostics bounds output properly."""
    short = "error snippet"
    assert format_gate_diagnostics(short, max_chars=100) == short

    long_output = "x" * 5000 + "IMPORTANT_ERROR_TAIL"
    bounded = format_gate_diagnostics(long_output, max_chars=200)
    assert "truncated" in bounded
    assert bounded.endswith("IMPORTANT_ERROR_TAIL")
    assert len(bounded) < 300


def test_gate_duration_is_measured_and_reported(tmp_path: Path):
    """E. Gate duration is measured and reported."""
    cmd = [sys.executable, "-c", "import time; time.sleep(0.05)"]
    res = run_gate("sleep_gate", cmd, tmp_path)
    assert res.passed is True
    assert res.elapsed_seconds >= 0.04

    json_str = format_ci_json([res])
    data = json.loads(json_str)
    assert data["gates"][0]["elapsed_seconds"] == res.elapsed_seconds
    assert data["gates"][0]["duration_seconds"] == res.elapsed_seconds

    plain_str = format_gate_plain(res)
    assert f"sleep_gate: PASS ({res.elapsed_seconds:.1f}s)" in plain_str


def test_acceptance_script_does_not_spawn_stagemesh_ci_recursively(monkeypatch):
    """F. scripts/acceptance.py does not spawn stagemesh ci recursively (behavioral proof)."""
    import subprocess
    import scripts.acceptance as acceptance_module

    acceptance_src = Path(acceptance_module.__file__).read_text(encoding="utf-8")
    assert 'stagemesh.cli",\n                "--project",\n                str(ROOT),\n                "ci"' not in acceptance_src
    assert '"ci",\n                "--future-feature-gate"' not in acceptance_src

    recorded_commands: list[list[str]] = []
    real_run = subprocess.run

    def intercepting_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)):
            cmd_list = [str(c) for c in cmd]
        else:
            cmd_list = [str(cmd)]
        recorded_commands.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercepting_run)

    exit_code = acceptance_module.main()
    assert exit_code == 0, "acceptance.main() must pass successfully"

    ci_spawns = [
        cmd for cmd in recorded_commands
        if any("stagemesh.cli" in arg for arg in cmd) and "ci" in cmd
    ]
    assert ci_spawns == [], f"acceptance.py spawned recursive stagemesh ci commands: {ci_spawns}"
