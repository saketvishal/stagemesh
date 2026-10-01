"""
Tests for StageMesh CI Performance / Single-Pass Validation v1.

Verifies:
1. No production stub bypass (STAGEMESH_CI_STUB_GATES in environ does not bypass real gate execution).
2. Acceptance CI helper (check_ci_acceptance) tested with injected stub gate runner without running acceptance.main().
3. Single-pass execution: full CI gate list contains each expected gate once; acceptance.py does not execute during unit_tests.
4. JSON progress: stdout is valid JSON, stderr receives incremental start and completion progress lines.
5. Plain and JSON formatting operate purely on GateResult objects without executing expensive gates.
6. Gate duration is measured and reported.
7. Diagnostics truncation bounds failure output.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.acceptance import check_ci_acceptance
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
    """A. Full CI gate list contains each expected gate once without duplicates."""
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

    # Recording runner proves each gate runs at most once
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
    """B. --skip-acceptance excludes only the main acceptance gate."""
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


def test_no_production_stub_bypass(tmp_path: Path, monkeypatch):
    """1. Setting STAGEMESH_CI_STUB_GATES in os.environ must NOT bypass real gate execution."""
    monkeypatch.setenv("STAGEMESH_CI_STUB_GATES", "1")

    # Create a project with a test that deliberately fails
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fail.py").write_text("def test_broken(): assert False, 'intentional real failure'\n")

    # Run command_ci without an explicit gate_runner.
    # If the stub bypass were active, it would silently return 0 (PASS).
    # Since the stub bypass is eliminated, it must run real pytest and return 1 (FAIL).
    buf = io.StringIO()
    args = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=False,
    )
    exit_code = command_ci(args, gate_runner=None, stdout=buf)
    assert exit_code == 1, "Real execution must run and fail; STAGEMESH_CI_STUB_GATES must have no effect"
    assert "FAIL" in buf.getvalue()


def test_json_progress_emitted_to_stderr(tmp_path: Path):
    """4. JSON CI emits start/completion progress to stderr while keeping stdout valid JSON."""
    synthetic_gates = [
        GateResult("compile", True, "ok\n", 0.12),
        GateResult("unit_tests", True, "ok\n", 141.2),
    ]
    gate_iter = iter(synthetic_gates)

    def stub_runner(name: str, cmd: list[str], cwd: Path) -> GateResult:
        return next(gate_iter)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    args = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=True,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "stagemesh.ci.default_gate_commands",
            lambda include_acceptance, root: [("compile", []), ("unit_tests", [])],
        )
        code = command_ci(args, gate_runner=stub_runner, stdout=stdout_buf, stderr=stderr_buf)

    assert code == 0

    # Verify stdout is 100% valid JSON with no plain-text lines
    data = json.loads(stdout_buf.getvalue())
    assert data["status"] == "PASS"
    assert len(data["gates"]) == 2

    # Verify stderr received incremental progress lines
    stderr_lines = stderr_buf.getvalue().splitlines()
    assert "[1/2] compile running..." in stderr_lines
    assert "[1/2] compile PASS (0.1s)" in stderr_lines
    assert "[2/2] unit_tests running..." in stderr_lines
    assert "[2/2] unit_tests PASS (141.2s)" in stderr_lines


def test_check_ci_acceptance_semantics_and_zero_ci_spawns(tmp_path: Path, monkeypatch):
    """2. check_ci_acceptance verifies plain, JSON, failure, and future-feature semantics with zero ci subprocesses."""
    import subprocess

    recorded_subprocesses: list[list[str]] = []
    real_run = subprocess.run

    def intercept_run(cmd, *args, **kwargs):
        cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        recorded_subprocesses.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept_run)

    # Injected stub runner to keep test fast and isolated
    def stub_runner(name: str, command: list[str], cwd: Path) -> GateResult:
        return GateResult(name=name, passed=True, output=f"{name}: PASS\n", elapsed_seconds=0.01)

    # Run the focused acceptance helper directly
    check_ci_acceptance(tmp_path, gate_runner=stub_runner)

    # Behaviorally assert NO subprocess was spawned targeting "ci"
    ci_spawns = [
        cmd for cmd in recorded_subprocesses
        if any("stagemesh.cli" in arg for arg in cmd) and "ci" in cmd
    ]
    assert ci_spawns == [], f"check_ci_acceptance spawned recursive stagemesh ci: {ci_spawns}"


def test_acceptance_script_does_not_execute_during_unit_tests():
    """3. Prove acceptance.py does not execute as part of unit_tests."""
    # The unit_tests gate command runs pytest on tests/
    commands = default_gate_commands(include_acceptance=True)
    unit_test_cmd = next(cmd for name, cmd in commands if name == "unit_tests")
    acceptance_cmd = next(cmd for name, cmd in commands if name == "acceptance")

    assert unit_test_cmd == [sys.executable, "-m", "pytest", "tests/", "-q"]
    assert acceptance_cmd == [sys.executable, "scripts/acceptance.py"]

    # Verify no test in tests/ calls acceptance.main()
    tests_dir = Path(__file__).resolve().parent
    for test_file in tests_dir.glob("test_*.py"):
        if test_file.name == "test_ci_single_pass.py":
            continue
        content = test_file.read_text(encoding="utf-8")
        assert "acceptance.main()" not in content, f"{test_file} calls acceptance.main()"
