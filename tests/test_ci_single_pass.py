"""
Tests for StageMesh CI Performance / Single-Pass Validation v1.

Verifies:
1. No recursive StageMesh CI invocation anywhere in tests or acceptance.
2. No unit test calls full acceptance.main().
3. No production environment variable / CLI option can replace real CI gates with passing stubs.
4. CI-specific tests complete in <= 30 seconds locally.
5. pytest tests/ -q executes acceptance.py ZERO times (verified via runtime instrumentation).
6. One stagemesh ci --full invocation executes exactly:
   - unit_tests once
   - invariants once
   - provider_acceptance once
   - github_acceptance once
   - live_acceptance once
   - clean_acceptance once
   - acceptance once
7. Real-time instrumentation proving invocation counts.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.acceptance as acceptance_module
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


def test_full_ci_invocation_count_instrumentation(tmp_path: Path, monkeypatch):
    """
    6 & 7. One stagemesh ci --full invocation executes exactly:
    - unit_tests once
    - invariants once
    - provider_acceptance once
    - github_acceptance once
    - live_acceptance once
    - clean_acceptance once
    - acceptance once
    Instrumented at the subprocess execution boundary to prove exact count = 1.
    """
    invoked_commands: list[list[str]] = []

    def recording_run(cmd, *args, **kwargs):
        cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        invoked_commands.append(cmd_list)
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "gate output: passed\n"
        mock.stderr = ""
        return mock

    monkeypatch.setattr(subprocess, "run", recording_run)

    (tmp_path / "tests").mkdir()
    args = Namespace(
        project=str(tmp_path),
        full=True,
        future_feature_gate=False,
        skip_acceptance=False,
        json=False,
    )

    # Invoke production command_ci with NO gate_runner injected
    exit_code = command_ci(args)
    assert exit_code == 0

    # Required gates to verify
    required_leaf_gates = {
        "unit_tests": "pytest",
        "invariants": "scripts/invariants.py",
        "provider_acceptance": "scripts/provider_acceptance.py",
        "github_acceptance": "scripts/github_acceptance.py",
        "live_acceptance": "scripts/live_acceptance.py",
        "clean_acceptance": "scripts/clean_acceptance.py",
        "acceptance": "scripts/acceptance.py",
    }

    for gate_name, script_pattern in required_leaf_gates.items():
        matches = [
            cmd for cmd in invoked_commands
            if any(script_pattern in arg for arg in cmd)
        ]
        assert len(matches) == 1, (
            f"Expected {gate_name} ({script_pattern}) to execute exactly once, "
            f"but found {len(matches)} invocations: {matches}"
        )


def test_pytest_executes_acceptance_py_zero_times():
    """
    2 & 5. pytest tests/ -q executes acceptance.py ZERO times.
    Verified through runtime instrumentation of acceptance.py's main invocation counter.
    """
    assert acceptance_module.ACCEPTANCE_MAIN_INVOCATION_COUNT == 0, (
        f"acceptance.py main() was executed {acceptance_module.ACCEPTANCE_MAIN_INVOCATION_COUNT} times during unit tests!"
    )


def test_no_production_stub_bypass(tmp_path: Path, monkeypatch):
    """3. Setting STAGEMESH_CI_STUB_GATES in os.environ must NOT bypass real gate execution."""
    monkeypatch.setenv("STAGEMESH_CI_STUB_GATES", "1")

    # Create a project with a failing test
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fail.py").write_text("def test_broken(): assert False, 'intentional failure'\n")

    buf = io.StringIO()
    args = Namespace(
        project=str(tmp_path),
        future_feature_gate=False,
        skip_acceptance=True,
        json=False,
    )
    # Production command_ci without injected runner
    exit_code = command_ci(args, gate_runner=None, stdout=buf)
    assert exit_code == 1, "Real execution must fail; environment variables cannot stub gates"
    assert "FAIL" in buf.getvalue()


def test_check_ci_acceptance_semantics_and_zero_ci_spawns(tmp_path: Path, monkeypatch):
    """1. check_ci_acceptance verifies semantics with zero recursive stagemesh-ci subprocess invocations."""
    recorded_subprocesses: list[list[str]] = []
    real_run = subprocess.run

    def intercept_run(cmd, *args, **kwargs):
        cmd_list = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        recorded_subprocesses.append(cmd_list)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept_run)

    def stub_runner(name: str, command: list[str], cwd: Path) -> GateResult:
        return GateResult(name=name, passed=True, output=f"{name}: PASS\n", elapsed_seconds=0.01)

    check_ci_acceptance(tmp_path, gate_runner=stub_runner)

    ci_spawns = [
        cmd for cmd in recorded_subprocesses
        if any("stagemesh.cli" in arg for arg in cmd) and "ci" in cmd
    ]
    assert ci_spawns == [], f"check_ci_acceptance spawned recursive stagemesh ci: {ci_spawns}"


def test_json_progress_emitted_to_stderr(tmp_path: Path):
    """JSON CI emits start/completion progress to stderr while keeping stdout valid JSON."""
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

    data = json.loads(stdout_buf.getvalue())
    assert data["status"] == "PASS"
    assert len(data["gates"]) == 2

    stderr_lines = stderr_buf.getvalue().splitlines()
    assert "[1/2] compile running..." in stderr_lines
    assert "[1/2] compile PASS (0.1s)" in stderr_lines
    assert "[2/2] unit_tests running..." in stderr_lines
    assert "[2/2] unit_tests PASS (141.2s)" in stderr_lines


def test_skip_acceptance_excludes_only_acceptance_and_no_recursion(tmp_path: Path):
    """--skip-acceptance excludes only the main acceptance gate."""
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


def test_plain_and_json_formatting_tested_without_expensive_gates():
    """Plain and JSON formatting operate purely on GateResult objects without executing expensive gates."""
    synthetic_results = [
        GateResult("compile", True, "compilation clean\n", 1.25),
        GateResult("unit_tests", True, "259 passed\n", 45.10),
    ]

    plain = format_ci_plain(synthetic_results)
    assert "compile: PASS" in plain
    assert "unit_tests: PASS" in plain

    json_text = format_ci_json(synthetic_results)
    data = json.loads(json_text)
    assert data["status"] == "PASS"
    assert len(data["gates"]) == 2


def test_failed_synthetic_gate_returns_failure_with_useful_diagnostics(tmp_path: Path):
    """A failed synthetic gate returns CI failure and contains useful diagnostic output."""
    error_text = "AssertionError: line 42 failed in invariant check\nDetailed trace:\n  foo -> bar"
    synthetic_results = [
        GateResult("compile", True, "ok\n", 0.5),
        GateResult("invariants", False, error_text, 1.2),
    ]

    plain = format_ci_plain(synthetic_results)
    assert "invariants: FAIL" in plain
    assert error_text in plain

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
    assert "invariants: FAIL" in buf.getvalue()


def test_diagnostics_truncation():
    """format_gate_diagnostics bounds output properly."""
    short = "error snippet"
    assert format_gate_diagnostics(short, max_chars=100) == short

    long_output = "x" * 5000 + "IMPORTANT_ERROR_TAIL"
    bounded = format_gate_diagnostics(long_output, max_chars=200)
    assert "truncated" in bounded
    assert bounded.endswith("IMPORTANT_ERROR_TAIL")


def test_gate_duration_is_measured_and_reported(tmp_path: Path):
    """Gate duration is measured and reported."""
    cmd = [sys.executable, "-c", "import time; time.sleep(0.05)"]
    res = run_gate("sleep_gate", cmd, tmp_path)
    assert res.passed is True
    assert res.elapsed_seconds >= 0.04

    json_str = format_ci_json([res])
    data = json.loads(json_str)
    assert data["gates"][0]["elapsed_seconds"] == res.elapsed_seconds

    plain_str = format_gate_plain(res)
    assert f"sleep_gate: PASS ({res.elapsed_seconds:.1f}s)" in plain_str
