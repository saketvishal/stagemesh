from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import os

from stagemesh.contracts import GateCommand, run_gate


def test_gate_does_not_reuse_provider_temporary_root(tmp_path, monkeypatch):
    shared = tmp_path / "provider-temp"
    shared.mkdir()
    (shared / "pytest-of-user").mkdir()
    for key in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(key, str(shared))
    code = (
        "import json,os; from pathlib import Path; "
        "p=Path(os.environ['TMPDIR']); "
        "assert p.is_dir() and not (p/'pytest-of-user').exists(); "
        "assert os.environ['TEMP']==os.environ['TMP']==str(p); "
        "print(json.dumps(str(p)))"
    )
    first = run_gate(tmp_path, GateCommand("one", [sys.executable, "-c", code]))
    second = run_gate(tmp_path, GateCommand("two", [sys.executable, "-c", code]))
    assert first.status == second.status == "PASSED"
    first_temp, second_temp = json.loads(first.stdout), json.loads(second.stdout)
    assert first_temp != second_temp
    assert not Path(first_temp).exists() and not Path(second_temp).exists()
    assert (shared / "pytest-of-user").is_dir()


def test_explicit_gate_temp_configuration_is_preserved(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    gate = GateCommand(
        "explicit", [sys.executable, "-c", "import os; print(os.environ['TMPDIR'])"],
        env=(("TMPDIR", str(selected)),),
    )
    result = run_gate(tmp_path, gate)
    assert result.status == "PASSED"
    assert result.stdout.strip() == str(selected)
    assert selected.is_dir()


@pytest.mark.parametrize("timeout", [False, True])
def test_gate_temp_is_cleaned_after_failure_or_timeout(tmp_path, timeout):
    code = "import os; print(os.environ['TMPDIR'], flush=True); "
    code += "import time; time.sleep(10)" if timeout else "raise SystemExit(1)"
    gate = GateCommand("failure", [sys.executable, "-c", code], timeout_seconds=2 if timeout else 10)
    result = run_gate(tmp_path, gate)
    assert result.status == "FAILED"
    assert result.failure_kind == ("timeout" if timeout else None)
    assert result.stdout.strip()
    assert not Path(result.stdout.strip()).exists()
    if timeout:
        assert result.returncode is None
        assert "process tree terminated" in result.stderr
        assert "no exit code" in result.stderr


def test_gate_timeout_kills_grandchild_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import pathlib, subprocess, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "path.write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    gate = GateCommand(
        "tree",
        [sys.executable, "-c", code, str(pid_file)],
        timeout_seconds=2,
    )
    result = run_gate(tmp_path, gate)
    assert result.failure_kind == "timeout"
    assert result.returncode is None
    deadline = time.time() + 5
    pid = int(pid_file.read_text(encoding="utf-8"))
    while time.time() < deadline and _alive(pid):
        time.sleep(0.1)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in listed.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
