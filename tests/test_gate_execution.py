"""Gate launch and timeout explanation: scenarios 1 and 2 of the reliable-delivery repair."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from stagemesh.contracts import GateCommand, run_gate
from stagemesh.gate_execution import (
    TIMEOUT_COLLECTION,
    TIMEOUT_NO_OUTPUT,
    TIMEOUT_SERVICE_UNAVAILABLE,
    TIMEOUT_SETUP,
    TIMEOUT_TEST_EXECUTION,
    classify_timeout,
    resolve_gate_executable,
)
from stagemesh.redaction import redact_output


def _make_venv(root: Path) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(root)
    return root / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def _identity_script(path: Path) -> None:
    path.write_text("import sys; print(sys.prefix)\n", encoding="utf-8")


def test_relative_virtualenv_executable_resolves_against_gate_cwd_and_keeps_venv_identity(tmp_path: Path, monkeypatch) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "service").mkdir(parents=True)
    interpreter = _make_venv(candidate / ".venv")
    _identity_script(candidate / "service" / "identity.py")
    declared = os.path.relpath(interpreter, candidate / "service")  # ..\.venv\Scripts\python.exe
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the validator's own directory is unrelated to the gate's

    result = run_gate(candidate, GateCommand("identity", [declared, "identity.py"], cwd="service", timeout_seconds=60))

    assert result.status == "PASSED", result.stderr
    assert Path(result.stdout.strip()) == candidate / ".venv"  # the venv, not the base interpreter the launcher points at
    assert result.command == (declared, "identity.py")  # the declared command is preserved verbatim
    assert result.executed_command[0] == str(Path(os.path.normpath(candidate / "service" / declared)))
    assert result.resolution["method"] == "explicit-relative"
    assert result.cwd == str(candidate / "service")


@pytest.mark.skipif(os.name == "nt", reason="symlinked interpreters need POSIX symlinks")
def test_symlinked_virtualenv_interpreter_is_not_resolved_through_the_link(tmp_path: Path) -> None:
    interpreter = _make_venv(tmp_path / ".venv")
    assert interpreter.is_symlink()
    resolution = resolve_gate_executable([str(interpreter.relative_to(tmp_path))], tmp_path)
    assert resolution is not None and resolution.executed == str(interpreter)  # not the symlink target


def test_missing_explicit_executable_fails_closed_and_never_falls_back_to_path(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir()
    path_dir = tmp_path / "pathdir"
    path_dir.mkdir()
    shim = path_dir / ("python.cmd" if os.name == "nt" else "python")
    shim.write_text("@echo substituted\n" if os.name == "nt" else "#!/bin/sh\necho substituted\n", encoding="utf-8")
    shim.chmod(0o755)
    gate = GateCommand("missing", ["bin/python", "-c", "print(1)"], env=(("PATH", str(path_dir)),))

    result = run_gate(tmp_path, gate)

    assert result.status == "FAILED" and result.failure_kind == "environment"
    assert result.returncode is None and "substituted" not in result.stdout
    assert "does not exist" in result.stderr and "no PATH program was substituted" in result.stderr
    assert result.executed_command == ()  # nothing was launched
    assert result.resolution["method"] == "missing-explicit"
    assert result.command == ("bin/python", "-c", "print(1)")


def test_bare_command_still_uses_a_recorded_path_lookup(tmp_path: Path) -> None:
    resolution = resolve_gate_executable([Path(sys.executable).name, "-V"], tmp_path)
    assert resolution is not None and resolution.method in {"path-lookup", "bare-unresolved"}
    assert resolution.declared == Path(sys.executable).name


def test_gate_output_is_unbuffered_so_a_hang_is_not_an_empty_log(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\nprint('collecting ...')\ntime.sleep(30)\n", encoding="utf-8")
    result = run_gate(tmp_path, GateCommand("hang", [sys.executable, "hang.py"], timeout_seconds=3))
    assert result.failure_kind == "timeout"
    assert "collecting" in result.stdout  # block-buffered pipes would have printed nothing
    assert result.diagnosis["timeout_class"] == TIMEOUT_COLLECTION
    assert result.provenance()["diagnosis"]["progress"][-1] == "collecting ..."


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected", "attribution"),
    [
        ("", "", TIMEOUT_NO_OUTPUT, "undetermined"),
        ("collecting ...", "", TIMEOUT_COLLECTION, "environment_or_import_time"),
        ("collected 12 items\n", "", TIMEOUT_SETUP, "environment_or_fixture_setup"),
        ("collected 12 items\ntests/test_a.py::test_x PASSED [ 8%]\n", "", TIMEOUT_TEST_EXECUTION, "candidate_possible"),
        ("collected 3 items", "psycopg2.OperationalError: connection refused", TIMEOUT_SERVICE_UNAVAILABLE, "infrastructure"),
    ],
)
def test_timeout_classes_claim_only_what_the_output_supports(stdout, stderr, expected, attribution) -> None:
    diagnosis = classify_timeout(stdout, stderr)
    assert diagnosis["timeout_class"] == expected
    assert diagnosis["attribution"] == attribution
    if expected == TIMEOUT_NO_OUTPUT:
        assert "not established" in diagnosis["explanation"]
        assert "postgres" not in diagnosis["explanation"].lower()


def test_captured_output_is_redacted_and_bounded() -> None:
    text = "connecting postgresql://app:hunter2@db/prod password=hunter2 API_KEY: abc123 Authorization: Bearer tok.en"
    cleaned = redact_output(text)
    assert "hunter2" not in cleaned and "abc123" not in cleaned and "tok.en" not in cleaned
    assert "postgresql://***REDACTED***@db/prod" in cleaned

    noisy = subprocess.CompletedProcess([], 1, stdout="x" * 50_000, stderr="")
    result = run_gate(
        Path(os.getcwd()),
        GateCommand("noisy", [sys.executable, "-c", "print('x'*50000); import sys; sys.exit(1)"], timeout_seconds=30),
    )
    assert noisy.returncode == result.returncode == 1
    provenance = result.provenance()
    assert len(provenance["stdout_tail"]) <= 600 and provenance["output_truncated"] is True
