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
        assert "owned process tree terminated" in result.stderr
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


def test_timeout_does_not_kill_an_unrelated_process(tmp_path: Path) -> None:
    import subprocess

    from stagemesh.domain import ProcessIdentity
    from stagemesh.gate_process import terminate_owned_gate

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        saved = ProcessIdentity(pid=other.pid, create_time=1.0, boot_id="not-this-process", executable="python")
        result = terminate_owned_gate(other, saved)
        assert result.terminated is False
        assert "not terminated" in result.note
        assert _alive(other.pid)
    finally:
        other.kill()
        other.wait(timeout=5)


def test_validation_slot_serializes_gates_and_releases_after_timeout(tmp_path: Path) -> None:
    import threading

    first = run_gate(tmp_path, GateCommand("slow", [sys.executable, "-c", "import time; time.sleep(0.6)"], timeout_seconds=5))
    # The assertion below uses two overlapping calls; the one above only warms nothing.
    assert first.status == "PASSED"
    started = time.time()
    results: list[object] = []
    errors: list[BaseException] = []

    def run(name: str, sleep_for: float, timeout: int) -> None:
        try:
            results.append(
                run_gate(
                    tmp_path,
                    GateCommand(name, [sys.executable, "-c", f"import time; time.sleep({sleep_for})"], timeout_seconds=timeout),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - the thread must report the failure
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("a", 0.4, 5)),
        threading.Thread(target=run, args=("b", 0.4, 5)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert len(results) == 2
    assert all(getattr(item, "status", None) == "PASSED" for item in results)
    assert time.time() - started >= 0.75

    follow = run_gate(
        tmp_path,
        GateCommand("after-timeout", [sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=1),
    )
    assert follow.failure_kind == "timeout" and follow.returncode is None
    released = run_gate(tmp_path, GateCommand("released", [sys.executable, "-c", "print('ok')"], timeout_seconds=5))
    assert released.status == "PASSED"


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
