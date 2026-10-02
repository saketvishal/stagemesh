from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from .domain import ProcessIdentity


def boot_id() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return f"{platform.system()}:{platform.node()}"


def current_process_identity() -> ProcessIdentity:
    return ProcessIdentity(
        pid=os.getpid(),
        create_time=None,
        boot_id=boot_id(),
        executable=None,
    )


def popen_identity(proc: subprocess.Popen[str]) -> ProcessIdentity:
    return ProcessIdentity(pid=proc.pid, create_time=None, boot_id=boot_id(), executable=proc.args[0] if proc.args else None)


def process_identity(pid: int | None) -> ProcessIdentity | None:
    if pid is None or pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_identity(pid)
    return _posix_process_identity(pid)


def classify_process(saved: ProcessIdentity, observed: ProcessIdentity | None) -> str:
    if not saved.is_known:
        return "UNKNOWN"
    if observed is None:
        return "DEAD"
    if not observed.is_known:
        return "UNKNOWN"
    return "LIVE" if saved.matches(observed) else "DEAD"


def _posix_process_identity(pid: int) -> ProcessIdentity | None:
    stat = Path(f"/proc/{pid}/stat")
    exe = Path(f"/proc/{pid}/exe")
    try:
        fields = stat.read_text(encoding="utf-8").split()
    except OSError:
        if stat.exists():
            return ProcessIdentity(pid=pid, create_time=None, boot_id=boot_id(), executable=None)
        return None
    if len(fields) < 22:
        return None
    executable: str | None = None
    try:
        executable = str(exe.resolve())
    except OSError:
        pass
    try:
        return ProcessIdentity(pid=pid, create_time=float(fields[21]), boot_id=boot_id(), executable=executable)
    except ValueError:
        return None


def _windows_process_identity(pid: int) -> ProcessIdentity | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() != 87:
            return ProcessIdentity(pid=pid, create_time=None, boot_id=boot_id(), executable=None)
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        raw_creation = (creation.dwHighDateTime << 32) + creation.dwLowDateTime
        executable: str | None = None
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        query = getattr(ctypes.windll.kernel32, "QueryFullProcessImageNameW", None)
        if query and query(handle, 0, buffer, ctypes.byref(size)):
            executable = buffer.value
        return ProcessIdentity(pid=pid, create_time=float(raw_creation), boot_id=boot_id(), executable=executable)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
