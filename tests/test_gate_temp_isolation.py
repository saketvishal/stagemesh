from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import os

from stagemesh.contracts import GateCommand, run_gate


@pytest.fixture(autouse=True)
def _isolated_validation_slot(tmp_path, monkeypatch):
    """This file must not take the machine-wide slot used by a live runtime."""
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "validation-slot.lock"))


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


def test_unknown_identity_still_terminates_the_owned_process_handle(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from stagemesh.gate_process import terminate_owned_gate
    from stagemesh.process_identity import popen_identity

    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    saved = popen_identity(owned)
    monkeypatch.setattr("stagemesh.gate_process.process_identity", lambda _pid: None)
    try:
        result = terminate_owned_gate(owned, saved)
        assert result.terminated is True
        assert owned.poll() is not None
    finally:
        if owned.poll() is None:
            owned.kill()
            owned.wait(timeout=5)


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
    assert follow.execution_seconds >= 1.0
    assert follow.wait_seconds < 1.0
    assert "waited" in follow.stderr and "ran" in follow.stderr
    released = run_gate(tmp_path, GateCommand("released", [sys.executable, "-c", "print('ok')"], timeout_seconds=5))
    assert released.status == "PASSED"


def test_separate_process_cannot_bypass_the_slot(tmp_path: Path) -> None:
    import subprocess

    lock = tmp_path / "validation-slot.lock"
    holder = tmp_path / "hold.py"
    holder.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "os.environ['STAGEMESH_VALIDATION_SLOT'] = r'''%s'''\n"
        "from stagemesh.validation_slot import validation_slot\n"
        "with validation_slot():\n"
        "    Path(r'''%s''').write_text('held', encoding='utf-8')\n"
        "    time.sleep(1.2)\n" % (lock, tmp_path / "held.txt"),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["STAGEMESH_VALIDATION_SLOT"] = str(lock)
    proc = subprocess.Popen([sys.executable, str(holder)], env=env)
    try:
        deadline = time.time() + 5
        while not (tmp_path / "held.txt").exists() and time.time() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "held.txt").exists()
        started = time.time()
        result = run_gate(tmp_path, GateCommand("queued", [sys.executable, "-c", "print('ran')"], timeout_seconds=5))
        assert result.status == "PASSED"
        assert time.time() - started >= 0.8
        assert result.wait_seconds >= 0.5
        assert result.execution_seconds < 2.0
    finally:
        proc.wait(timeout=5)


def test_cancel_while_waiting_does_not_start_the_gate(tmp_path: Path) -> None:
    import threading

    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.validation_slot import bind_validation_wait, reset_validation_wait, validation_slot

    started_file = tmp_path / "gate-started.txt"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with validation_slot():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    cancel = threading.Event()
    tokens = bind_validation_wait(cancel=cancel.is_set)
    try:
        cancel.set()
        with pytest.raises(OperatorStopRequested):
            run_gate(
                tmp_path,
                GateCommand(
                    "never",
                    [sys.executable, "-c", "open(r'%s', 'w', encoding='utf-8').write('x')" % started_file],
                    timeout_seconds=5,
                ),
            )
    finally:
        reset_validation_wait(tokens)
        release.set()
        thread.join(5)
    assert not started_file.exists()


def test_interrupt_after_launch_kills_the_owned_tree_and_releases_the_slot(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    pid_file = tmp_path / "child-pid.txt"
    code = (
        "import pathlib, subprocess, sys, time\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "path.write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    original = subprocess.Popen.communicate

    def interrupt(self, input=None, timeout=None):
        deadline = time.time() + 5
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.02)
        raise KeyboardInterrupt("operator")

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_gate(tmp_path, GateCommand("interrupt", [sys.executable, "-c", code, str(pid_file)], timeout_seconds=30))
    finally:
        monkeypatch.setattr(subprocess.Popen, "communicate", original)
    assert pid_file.exists()
    child = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.time() + 5
    while time.time() < deadline and _alive(child):
        time.sleep(0.05)
    assert not _alive(child)
    follow = run_gate(tmp_path, GateCommand("after-interrupt", [sys.executable, "-c", "print('ok')"], timeout_seconds=5))
    assert follow.status == "PASSED"


def test_crashed_holder_does_not_strand_the_slot(tmp_path: Path) -> None:
    import subprocess

    lock = tmp_path / "validation-slot.lock"
    holder = tmp_path / "crash.py"
    holder.write_text(
        "import os\n"
        "os.environ['STAGEMESH_VALIDATION_SLOT'] = r'''%s'''\n"
        "from stagemesh.validation_slot import validation_slot\n"
        "with validation_slot():\n"
        "    open(r'''%s''', 'w', encoding='utf-8').write('in')\n"
        "    os._exit(3)\n" % (lock, tmp_path / "in.txt"),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["STAGEMESH_VALIDATION_SLOT"] = str(lock)
    proc = subprocess.Popen([sys.executable, str(holder)], env=env)
    proc.wait(timeout=10)
    assert proc.returncode == 3
    result = run_gate(tmp_path, GateCommand("after-crash", [sys.executable, "-c", "print('ok')"], timeout_seconds=5))
    assert result.status == "PASSED"


def test_lock_path_folds_case_and_separates_explicit_resources(tmp_path: Path, monkeypatch) -> None:
    from stagemesh.validation_slot import canonical_lock_path, slot_path

    raw = tmp_path / "Case" / "Slot.lock"
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(raw))
    folded = slot_path()
    assert folded == canonical_lock_path(str(raw))
    if os.name == "nt":
        monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(raw).swapcase())
        assert slot_path() == folded
    monkeypatch.setenv("STAGEMESH_VALIDATION_SLOT", str(tmp_path / "other.lock"))
    assert slot_path() != folded


def test_child_window_deadline_is_read_from_the_environment(tmp_path: Path, monkeypatch) -> None:
    import threading

    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.validation_slot import validation_slot

    started_file = tmp_path / "gate-started.txt"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with validation_slot():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    monkeypatch.setenv("STAGEMESH_WINDOW_DEADLINE", "1")
    try:
        with pytest.raises(OperatorStopRequested, match="deadline"):
            run_gate(
                tmp_path,
                GateCommand(
                    "never",
                    [sys.executable, "-c", "open(r'%s', 'w', encoding='utf-8').write('x')" % started_file],
                    timeout_seconds=5,
                ),
            )
    finally:
        release.set()
        thread.join(5)
    assert not started_file.exists()


def test_deadline_while_waiting_starts_no_gate(tmp_path: Path) -> None:
    import threading

    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.validation_slot import bind_validation_wait, reset_validation_wait, validation_slot

    started_file = tmp_path / "gate-started.txt"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with validation_slot():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    tokens = bind_validation_wait(deadline=time.monotonic() + 0.15)
    try:
        with pytest.raises(OperatorStopRequested, match="deadline"):
            run_gate(
                tmp_path,
                GateCommand(
                    "never",
                    [sys.executable, "-c", "open(r'%s', 'w', encoding='utf-8').write('x')" % started_file],
                    timeout_seconds=5,
                ),
            )
    finally:
        reset_validation_wait(tokens)
        release.set()
        thread.join(5)
    assert not started_file.exists()


def test_cancel_after_acquire_and_before_launch_starts_no_gate(tmp_path: Path, monkeypatch) -> None:
    import threading

    from stagemesh.queue_control import OperatorStopRequested
    from stagemesh.validation_slot import bind_validation_wait, reset_validation_wait

    started_file = tmp_path / "gate-started.txt"
    cancel = threading.Event()

    def flip():
        if cancel.is_set():
            return True
        cancel.set()
        return False

    tokens = bind_validation_wait(cancel=flip)
    try:
        # The first observation is the wait loop. flip arms the event and
        # reports clear, so the slot is taken. The recheck then sees it.
        with pytest.raises(OperatorStopRequested):
            run_gate(
                tmp_path,
                GateCommand(
                    "never",
                    [sys.executable, "-c", "open(r'%s', 'w', encoding='utf-8').write('x')" % started_file],
                    timeout_seconds=5,
                ),
            )
    finally:
        reset_validation_wait(tokens)
    assert not started_file.exists()


def test_exception_during_post_acquire_recheck_releases_the_slot() -> None:
    from stagemesh.validation_slot import bind_validation_wait, reset_validation_wait, validation_slot

    observations = 0

    def fail_on_recheck() -> bool:
        nonlocal observations
        observations += 1
        if observations == 2:
            raise RuntimeError("cancel callback failed")
        return False

    tokens = bind_validation_wait(cancel=fail_on_recheck)
    try:
        with pytest.raises(RuntimeError, match="cancel callback failed"):
            with validation_slot():
                pytest.fail("slot body must not run")
    finally:
        reset_validation_wait(tokens)

    with validation_slot():
        pass


def test_slot_file_contents_are_not_used_to_signal_a_live_process(tmp_path: Path) -> None:
    import subprocess

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock = tmp_path / "validation-slot.lock"
        lock.write_text(str(other.pid), encoding="utf-8")
        result = run_gate(tmp_path, GateCommand("ignore-pid", [sys.executable, "-c", "print('ok')"], timeout_seconds=5))
        assert result.status == "PASSED"
        assert _alive(other.pid)
    finally:
        other.kill()
        other.wait(timeout=5)


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
