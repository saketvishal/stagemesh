"""Process-tree termination for owned worker processes.

Windows: Job Objects + Win32 process descendants + taskkill /F /T /PID.
POSIX: Process group termination (SIGTERM / SIGKILL via killpg).
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any


class ProcessTree:
    """Tracks OS handles needed to terminate an execution's descendant tree."""

    def __init__(self, pid: int, *, job_handle: Any = None) -> None:
        self.pid = pid
        self.job_handle = job_handle

    def terminate(self, *, grace_seconds: float = 0.5) -> None:
        if self.pid <= 1 or self.pid == os.getpid():
            return
        if sys.platform == "win32":
            _terminate_windows(self)
            return
        _terminate_posix(self.pid, grace_seconds=grace_seconds)

    def close(self) -> None:
        if sys.platform == "win32" and self.job_handle:
            _close_handle(self.job_handle)
            self.job_handle = None


def kill_process_tree(pid: int) -> None:
    """Kill an execution tree given a PID."""
    if pid <= 1 or pid == os.getpid():
        return
    ProcessTree(pid).terminate()


def _terminate_posix(pid: int, *, grace_seconds: float) -> None:
    current_pgid = None
    try:
        current_pgid = os.getpgrp()
    except Exception:
        pass
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if pgid is not None and pgid > 1 and pgid != current_pgid:
            try:
                os.killpg(pgid, sig)
            except OSError:
                pass
        try:
            os.kill(pid, sig)
        except OSError:
            return

        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
                if reaped == pid:
                    return
            except OSError:
                pass
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.05)


def _terminate_windows(tree: ProcessTree) -> None:
    if tree.job_handle:
        _terminate_job(tree.job_handle)
        _close_handle(tree.job_handle)
        tree.job_handle = None
    _taskkill_tree(tree.pid)


def _terminate_job(job: Any) -> None:
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject(job, 1)
    except Exception:
        pass


def _taskkill_tree(pid: int) -> None:
    import subprocess

    descendants = _windows_descendants(pid)
    for target in [pid, *descendants]:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(target)],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass


def _windows_descendants(root_pid: int) -> list[int]:
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return []
        children: dict[int, list[int]] = {}
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)

        found: list[int] = []
        stack = list(children.get(root_pid, ()))
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(children.get(current, ()))
        return found
    except Exception:
        return []


def _close_handle(handle: Any) -> None:
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(handle)
    except Exception:
        pass
