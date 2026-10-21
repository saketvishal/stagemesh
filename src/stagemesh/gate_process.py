"""Terminate only the processes a validation gate actually owns."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from .domain import ProcessIdentity
from .process_identity import classify_process, popen_identity, process_identity


@dataclass(frozen=True)
class GateTermination:
    """What happened to the gate process. Never reports success for a timeout."""

    terminated: bool
    note: str
    returncode: int | None


def spawn_gate_process(
    command: list[str], cwd, env: dict[str, str] | None
) -> tuple[subprocess.Popen[str], ProcessIdentity]:
    """Start a gate in its own session on POSIX so its group can be reaped."""
    kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    return proc, popen_identity(proc)


def terminate_owned_gate(proc: subprocess.Popen[str], saved: ProcessIdentity) -> GateTermination:
    """Kill the gate and its descendants only when the saved identity is still live.

    A reused PID is left alone. The direct child is signalled through the
    process handle on Windows, which stays bound to the original process.
    """
    observed = process_identity(proc.pid)
    if classify_process(saved, observed) != "LIVE":
        # The handle still belongs to the process we started, but the identity
        # check failed. Do not signal it, and do not block the slot waiting
        # for a process we refused to classify as ours.
        proc.poll()
        return GateTermination(
            False,
            "process identity no longer matches; not terminated; no exit code",
            proc.returncode,
        )
    if sys.platform == "win32":
        _terminate_windows_descendants(proc.pid, saved.create_time)
        try:
            proc.kill()
        except OSError:
            pass
    else:
        _terminate_posix_group(proc, saved)
    _reap_if_exited(proc)
    return GateTermination(True, "owned process tree terminated; no exit code", proc.returncode)


def _reap_if_exited(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _terminate_windows_descendants(root_pid: int, root_create: float | None) -> None:
    children: dict[int, list[int]] = {}
    for pid, parent in _windows_parent_snapshot():
        children.setdefault(parent, []).append(pid)
    owned: list[tuple[int, float | None]] = []
    stack = list(children.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        identity = process_identity(pid)
        created = identity.create_time if identity is not None else None
        if root_create is not None and created is not None and created < root_create:
            continue
        owned.append((pid, created))
        stack.extend(children.get(pid, []))
    for pid, created in reversed(owned):
        _terminate_windows_pid(pid, created)


def _terminate_windows_pid(pid: int, created: float | None) -> None:
    current = process_identity(pid)
    if current is None or not current.is_known:
        return
    if created is not None and current.create_time != created:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_TERMINATE = 0x0001
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


def _windows_parent_snapshot() -> list[tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    rows: list[tuple[int, int]] = []
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _terminate_posix_group(proc: subprocess.Popen[str], saved: ProcessIdentity) -> None:
    import os
    import signal

    try:
        group = os.getpgid(proc.pid)
    except OSError:
        group = None
    current = process_identity(proc.pid)
    if classify_process(saved, current) != "LIVE" or group != proc.pid:
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass
