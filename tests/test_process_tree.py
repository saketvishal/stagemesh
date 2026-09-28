from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.subprocess_executor import SubprocessExecutor


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        # Reap zombie if it happens to be our direct child
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return False
        except OSError:
            pass

        # Check /proc/<pid>/status for zombie on Linux
        proc_status = Path(f"/proc/{pid}/status")
        if proc_status.is_file():
            try:
                for line in proc_status.read_text(encoding="utf-8").splitlines():
                    if line.startswith("State:"):
                        state = line.split(":", 1)[1].strip()
                        if state.startswith("Z"):
                            return False
                        break
            except OSError:
                return False

        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_tree_scripts(pid_dir: Path) -> Path:
    grandchild = pid_dir / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "pid_dir = Path(sys.argv[1])\n"
        "(pid_dir / 'grandchild.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child = pid_dir / "child.py"
    child.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "pid_dir = Path(sys.argv[1])\n"
        "(pid_dir / 'child.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "gc = pid_dir / 'grandchild.py'\n"
        "import subprocess\n"
        "subprocess.Popen([sys.executable, str(gc), str(pid_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    worker = pid_dir / "worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "pid_dir = Path(sys.argv[1])\n"
        "(pid_dir / 'worker.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "child = pid_dir / 'child.py'\n"
        "import subprocess\n"
        "subprocess.Popen([sys.executable, str(child), str(pid_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return worker


def test_terminate_kills_worker_child_and_grandchild(tmp_path: Path):
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    worker = _write_tree_scripts(pid_dir)
    unrelated_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    unrelated = unrelated_proc.pid
    executor = SubprocessExecutor([sys.executable, str(worker), str(pid_dir)])
    handle = executor.launch(
        ExecutionLaunch(
            task_id="PT-1",
            role="BUILDER",
            worker_id="builder-1",
            provider="local",
            worktree_path=None,
            branch_name=None,
            prompt="",
        )
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if all((pid_dir / name).is_file() for name in ("worker.pid", "child.pid", "grandchild.pid")):
            break
        time.sleep(0.05)
    pids = {
        name: int((pid_dir / f"{name}.pid").read_text(encoding="utf-8"))
        for name in ("worker", "child", "grandchild")
    }
    for pid in pids.values():
        assert _alive(pid)
    assert _alive(unrelated)
    executor.terminate(handle.execution_id)
    deadline = time.time() + 15
    while time.time() < deadline and any(_alive(pid) for pid in pids.values()):
        time.sleep(0.1)
    for name, pid in pids.items():
        assert not _alive(pid), f"{name} pid {pid} still alive"
    assert _alive(unrelated)
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_p = kernel32.OpenProcess(0x0001, False, unrelated)
        if handle_p:
            kernel32.TerminateProcess(handle_p, 1)
            kernel32.CloseHandle(handle_p)
    else:
        os.kill(unrelated, 9)
    unrelated_proc.wait(timeout=5)


def test_restart_reaps_owned_tree_from_durable_pid(tmp_path: Path):
    from build_coordinator.execution.process_tree import kill_process_tree

    pid_dir = tmp_path / "restart-pids"
    pid_dir.mkdir()
    worker = _write_tree_scripts(pid_dir)
    executor = SubprocessExecutor([sys.executable, str(worker), str(pid_dir)])
    handle = executor.launch(
        ExecutionLaunch(
            task_id="PT-R",
            role="BUILDER",
            worker_id="builder-1",
            provider="local",
            worktree_path=None,
            branch_name=None,
            prompt="",
        )
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if all((pid_dir / name).is_file() for name in ("worker.pid", "child.pid", "grandchild.pid")):
            break
        time.sleep(0.05)
    pids = {
        name: int((pid_dir / f"{name}.pid").read_text(encoding="utf-8"))
        for name in ("worker", "child", "grandchild")
    }
    durable_pid = int(handle.process_id)
    del executor
    kill_process_tree(durable_pid)
    deadline = time.time() + 15
    while time.time() < deadline and any(_alive(pid) for pid in pids.values()):
        time.sleep(0.1)
    for name, pid in pids.items():
        assert not _alive(pid), f"{name} pid {pid} still alive after restart reap"


def test_dead_pid_with_stale_matching_identity_is_never_reported_alive(monkeypatch):
    """Regression for a Windows-specific false-positive discovered on
    native Windows CI: `test_restart_with_dead_worker_and_no_result_is_lost`
    reported the reconciled execution as RUNNING instead of LOST for a
    process that had genuinely exited.

    Root cause: on Windows, a process's start-identity metadata (creation
    time, read via GetProcessTimes) can remain queryable -- and unchanged
    -- even after the process has exited, as long as some handle (e.g. the
    launching process's own still-open `subprocess.Popen` handle) keeps the
    underlying kernel process object alive past exit. `process_identity_status`
    used to compare identity tokens before checking current liveness, so a
    confirmed-dead pid whose stale identity still matched the remembered one
    was reported MATCH -- and a caller (`SubprocessExecutor.poll`) then
    treated a genuinely-gone worker as still running, exactly the
    "duplicate worker dispatched against a still-claimed task" failure mode
    this whole identity-reconciliation mechanism exists to prevent.

    `_process_exists` (the real exit-code/zombie check, not just
    object/handle existence) must be checked first and is authoritative:
    a pid that is not currently alive is always MISMATCH, never MATCH or
    ALIVE_UNVERIFIED, regardless of what identity capture reports.
    """
    import build_coordinator.execution.process_tree as process_tree

    monkeypatch.setattr(process_tree, "_process_exists", lambda pid: False)
    monkeypatch.setattr(
        process_tree, "capture_process_identity", lambda pid: "stale-but-matching-key"
    )

    assert (
        process_tree.process_identity_status(12345, "stale-but-matching-key")
        == "MISMATCH"
    )
