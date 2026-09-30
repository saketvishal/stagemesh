from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from .domain import ProcessIdentity


def boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    try:
        import psutil

        return f"boot:{int(psutil.boot_time())}"
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import ctypes
            import time

            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            boot_epoch = int(time.time() - (uptime_ms / 1000.0))
            # Round to 2-second bucket to absorb sub-millisecond clock jitter between calls
            boot_epoch = (boot_epoch // 2) * 2
            return f"win_boot:{boot_epoch}"
        except Exception:
            pass
    return f"{platform.system()}:{platform.node()}"


def get_process_create_time(pid: int) -> float | None:
    try:
        import psutil

        p = psutil.Process(pid)
        return float(p.create_time())
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                try:
                    c, e, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                    if k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                        ft = (c.dwHighDateTime << 32) + c.dwLowDateTime
                        # 100-ns intervals since Jan 1 1601 to unix epoch
                        return (ft - 116444736000000000) / 10000000.0
                finally:
                    k32.CloseHandle(h)
        except Exception:
            pass
    return None


def get_process_executable(pid: int) -> str | None:
    try:
        import psutil

        p = psutil.Process(pid)
        return str(p.exe())
    except Exception:
        return None


def observe_process_identity(pid: int | None) -> ProcessIdentity | None:
    if pid is None or pid <= 0:
        return None
    create_time = get_process_create_time(pid)
    if create_time is None:
        return None
    executable = get_process_executable(pid)
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        boot_id=boot_id(),
        executable=executable,
    )


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    return ProcessIdentity(
        pid=pid,
        create_time=get_process_create_time(pid),
        boot_id=boot_id(),
        executable=sys.executable,
    )


def popen_identity(proc: subprocess.Popen[Any]) -> ProcessIdentity:
    pid = proc.pid
    exe = str(proc.args[0]) if proc.args and isinstance(proc.args, (list, tuple)) else sys.executable
    create_time = get_process_create_time(pid)
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        boot_id=boot_id(),
        executable=exe,
    )


def classify_process(saved: ProcessIdentity, observed: ProcessIdentity | None) -> str:
    if not saved.is_known or observed is None or not observed.is_known:
        return "UNKNOWN"
    return "LIVE" if saved.matches(observed) else "DEAD"


def is_pid_alive(pid: int, expected_boot_id: str | None = None) -> bool:
    if pid <= 0:
        return False
    current_boot = boot_id()
    if expected_boot_id and expected_boot_id != current_boot:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            cmd = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return str(pid) in res.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, Exception):
        return False
