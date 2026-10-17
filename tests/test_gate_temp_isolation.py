from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
