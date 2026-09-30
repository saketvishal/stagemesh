"""
CI GREEN SEMANTICS Regression Tests.

Ensures that:
1. default_gate_commands includes unit_tests when a test directory exists.
2. A failing unit test causes the CI gate to fail.
3. The overall CI command (stagemesh ci) cannot return PASS / exit code 0 when a test fails.
4. An intentional failing gate blocks CI from reporting PASS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from stagemesh.ci import (
    default_gate_commands,
    default_gates,
    broken_future_feature_gate,
    run_gate,
    GateResult,
)
from stagemesh.cli import main


def test_default_gate_commands_includes_unit_tests(tmp_path: Path):
    """default_gate_commands includes unit_tests when tests/ directory exists."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    cmds = default_gate_commands(include_acceptance=True, root=tmp_path)
    gate_names = [name for name, _ in cmds]
    assert "unit_tests" in gate_names
    assert "compile" in gate_names
    assert "invariants" in gate_names

    # Check command structure
    unit_test_cmd = next(cmd for name, cmd in cmds if name == "unit_tests")
    assert unit_test_cmd == [sys.executable, "-m", "pytest", "tests/", "-q"]


def test_default_gate_commands_omits_unit_tests_when_no_tests_dir(tmp_path: Path):
    """default_gate_commands does not invoke pytest if tests/ directory is absent."""
    cmds = default_gate_commands(include_acceptance=False, root=tmp_path)
    gate_names = [name for name, _ in cmds]
    assert "unit_tests" not in gate_names


def test_failing_unit_test_causes_ci_failure(tmp_path: Path, monkeypatch, capsys):
    """
    A failing unit test must cause the unit_tests gate to fail
    and prevent overall CI from reporting PASS (must exit 1).
    """
    # Create minimal project with a failing test
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    failing_test = tests_dir / "test_intentional_failure.py"
    failing_test.write_text("def test_will_fail():\n    assert False, 'intentional failure for CI'\n")

    # Mock default_gates to only run unit_tests to keep test fast and isolated
    with patch("stagemesh.ci.default_gate_commands") as mock_cmds:
        mock_cmds.return_value = [
            ("unit_tests", [sys.executable, "-m", "pytest", "tests/", "-q"])
        ]
        
        # Test default_gates function directly
        results = default_gates(tmp_path, include_acceptance=False)
        assert len(results) == 1
        assert results[0].name == "unit_tests"
        assert results[0].passed is False

        # Test CLI command stagemesh ci --json
        monkeypatch.chdir(tmp_path)
        exit_code = main(["ci", "--json", "--project", str(tmp_path), "--skip-acceptance"])
        assert exit_code == 1, "CI command must return non-zero exit code when unit tests fail"

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "FAIL", "Overall CI status cannot be PASS when unit tests fail"
        assert any(g["name"] == "unit_tests" and not g["passed"] for g in data["gates"])


def test_passing_unit_test_allows_ci_pass(tmp_path: Path, monkeypatch, capsys):
    """When all gates pass, CI reports PASS and exits 0."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    passing_test = tests_dir / "test_success.py"
    passing_test.write_text("def test_ok():\n    assert True\n")

    with patch("stagemesh.ci.default_gate_commands") as mock_cmds:
        mock_cmds.return_value = [
            ("unit_tests", [sys.executable, "-m", "pytest", "tests/", "-q"])
        ]
        monkeypatch.chdir(tmp_path)
        exit_code = main(["ci", "--json", "--project", str(tmp_path), "--skip-acceptance"])
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "PASS"
        assert all(g["passed"] for g in data["gates"])


def test_broken_future_feature_gate_fails_overall_ci(tmp_path: Path, monkeypatch, capsys):
    """broken_future_feature_gate causes CI to fail when marker is present."""
    marker = tmp_path / ".stagemesh-broken-feature"
    marker.write_text("broken")

    res = broken_future_feature_gate(tmp_path)
    assert res.passed is False

    with patch("stagemesh.ci.default_gate_commands") as mock_cmds:
        mock_cmds.return_value = []
        monkeypatch.chdir(tmp_path)
        exit_code = main(["ci", "--future-feature-gate", "--json", "--project", str(tmp_path), "--skip-acceptance"])
        assert exit_code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "FAIL"
