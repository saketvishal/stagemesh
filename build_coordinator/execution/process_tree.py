"""Owned process-tree termination for SubprocessExecutor.

Windows: Job Objects with KILL_ON_JOB_CLOSE so descendants die with the job.
POSIX: a new session/process group, then SIGTERM/SIGKILL to the group.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any


CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
PROCESS_SUSPEND_RESUME = 0x0800


class ProcessTree:
    """Tracks OS handles needed to kill an execution's descendant tree."""

    def __init__(self, pid: int, *, job_handle: Any = None) -> None:
        self.pid = pid
        self.job_handle = job_handle

    def terminate(self, *, grace_seconds: float = 0.5) -> None:
        if sys.platform == "win32":
            _terminate_windows(self)
            return
        _terminate_posix(self.pid, grace_seconds=grace_seconds)

    def close(self) -> None:
        if sys.platform == "win32" and self.job_handle:
            _close_handle(self.job_handle)
            self.job_handle = None


def popen_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    """Kill an execution tree from a durable PID after restart."""
    ProcessTree(pid).terminate()


def attach_started_process(pid: int) -> ProcessTree:
    if sys.platform == "win32":
        job = _create_job_object()
        assigned = False
        try:
            _assign_pid_to_job(job, pid)
            assigned = True
        except OSError:
            assigned = False
        _resume_process(pid, required=True)
        if not assigned:
            _close_handle(job)
            return ProcessTree(pid)
        return ProcessTree(pid, job_handle=job)
    return ProcessTree(pid)


def _terminate_posix(pid: int, *, grace_seconds: float) -> None:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
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


def _create_job_object():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed")

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        raise OSError("SetInformationJobObject failed")
    return job


def _assign_pid_to_job(job, pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_SUSPEND_RESUME = 0x0800
    access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    process = kernel32.OpenProcess(access, False, pid)
    if not process:
        raise OSError(f"OpenProcess failed for pid {pid}")
    try:
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(job, process):
            raise OSError("AssignProcessToJobObject failed")
    finally:
        kernel32.CloseHandle(process)


def _resume_process(pid: int, *, required: bool = False) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        if required:
            raise OSError(f"OpenProcess(PROCESS_SUSPEND_RESUME) failed for pid {pid}")
        return
    try:
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        status = ntdll.NtResumeProcess(handle)
        if required and status not in (0, None):
            # STATUS_SUCCESS is 0; some Windows versions return None via ctypes.
            if isinstance(status, int) and status < 0:
                raise OSError(f"NtResumeProcess failed for pid {pid}: {status}")
    finally:
        kernel32.CloseHandle(handle)


def _terminate_job(job) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject(job, 1)


def _taskkill_tree(pid: int) -> None:
    import subprocess

    descendants = _windows_descendants(pid)
    for target in [pid, *descendants]:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(target)],
            capture_output=True,
            check=False,
        )
    for target in [*reversed(descendants), pid]:
        _terminate_process_windows(target)


def _terminate_process_windows(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)


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


def _close_handle(handle) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(handle)
