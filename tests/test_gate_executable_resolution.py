"""Behaviour of the earlier gate-executable repair (explicit relative executables run from the gate directory), kept as regressions.

The earlier repair lived only in the installed shared runtime; these tests pin the same behaviour in the reviewed source:
relative paths resolve against the gate cwd, a venv launcher is never dereferenced, bare names keep their PATH lookup, and a real
candidate virtual environment launches from a different parent directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from stagemesh.contracts import GateCommand, _gate_runtime_command, run_gate


@pytest.mark.parametrize("cwd", [None, "apps/api"])
@pytest.mark.parametrize("program", [".venv/Scripts/python.exe", "./tools/check.cmd"])
def test_explicit_gate_executable_uses_gate_directory(tmp_path, monkeypatch, cwd, program):
    project = tmp_path / "candidate checkout"
    run_dir = project / cwd if cwd else project
    target = run_dir / program
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    expected = Path(os.path.normpath(target))
    monkeypatch.setattr("stagemesh.contracts.shutil.which", lambda command: None)

    def launch(command, cwd, env, timeout):
        assert Path(command[0]) == expected and Path(command[0]).is_absolute()
        assert command[1:] == ["-m", "pytest"]
        assert cwd == run_dir
        assert timeout > 0
        return CompletedProcess(command, 0, stdout="candidate-only", stderr="")

    monkeypatch.setattr("stagemesh.contracts._run_gate_process", launch)

    result = run_gate(project, GateCommand("relative", [program, "-m", "pytest"], cwd=cwd))

    assert result.status == "PASSED"
    assert result.command == (program, "-m", "pytest")  # the declared command is preserved
    assert Path(result.executed_command[0]) == expected  # and so is what actually ran


def test_missing_explicit_executable_is_not_launched_and_does_not_fall_back_to_path(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr("stagemesh.contracts.shutil.which", lambda command: "C:/elsewhere/python.exe")
    monkeypatch.setattr("stagemesh.contracts.subprocess.run", lambda *a, **k: launched.append(a) or CompletedProcess(a, 0, "", ""))

    result = run_gate(tmp_path, GateCommand("missing", ["missing/python.exe", "-m", "pytest"]))

    assert launched == [] and result.status == "FAILED" and result.failure_kind == "environment"
    # the path-only helper still reports where the explicit executable would be, never a PATH program
    command = _gate_runtime_command(["missing/python.exe", "-m", "pytest"], tmp_path)
    assert command == [str(Path(os.path.normpath(tmp_path / "missing/python.exe"))), "-m", "pytest"]


def test_bare_command_retains_path_lookup(tmp_path, monkeypatch):
    selected = str(tmp_path / "from-path" / "npm.cmd")
    monkeypatch.setattr("stagemesh.contracts.shutil.which", lambda command: selected if command == "npm" else None)
    assert _gate_runtime_command(["npm", "test"], tmp_path) == [selected, "test"]


def test_relative_candidate_python_really_launches_from_gate_directory(tmp_path):
    project = tmp_path / "candidate checkout"
    run_dir = project / "apps/api"
    run_dir.mkdir(parents=True)
    venv = run_dir / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    program = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"

    result = run_gate(project, GateCommand("candidate-python", [program, "-c", "import sys; print(sys.prefix)"], cwd="apps/api"))

    assert result.status == "PASSED", result.stderr
    assert Path(result.stdout.strip()).resolve() == venv.resolve()


def test_explicit_executable_does_not_dereference_venv_symlink(tmp_path, monkeypatch):
    expected = str(Path(os.path.normpath(tmp_path / ".venv/bin/python")))
    monkeypatch.setattr("stagemesh.contracts.shutil.which", lambda command: None)

    def no_resolve(self, *args, **kwargs):
        raise AssertionError("Dereferencing a venv executable can select the system interpreter")

    with monkeypatch.context() as local:
        local.setattr(Path, "resolve", no_resolve)
        assert _gate_runtime_command([".venv/bin/python", "-m", "pytest"], tmp_path)[0] == expected
