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
    if pid <= 1 or pid == os.getpid():
        return
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
            # Reap zombie if it happens to be our child
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
            # On Linux, check if process transitioned to zombie
            try:
                with open(f"/proc/{pid}/stat", "r") as f:
                    if f.read().split()[2] == "Z":
                        return
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
    # Do not set KILL_ON_JOB_CLOSE so child processes survive coordinator cycle restarts and --once.
    # Process trees are explicitly terminated via _terminate_windows / _terminate_job.
    info.BasicLimitInformation.LimitFlags = 0
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


def capture_process_identity(pid: int) -> str | None:
    """Best-effort durable start-identity token for `pid`.

    Combined with the pid itself, this lets a later reconciliation confirm
    "the process I launched is still running" rather than "some process with
    this pid exists" -- pids get reused, a bare pid does not. Returns None
    when no platform-specific identity evidence could be captured; callers
    must treat that as "unknown", never as "same" or "different".
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_start_key(pid)
    return _posix_process_start_key(pid)


def process_identity_status(pid: int, remembered_start_key: str | None) -> str:
    """Classify a remembered (pid, start_key) pair against current OS state.

    Returns one of:
      "MATCH"      -- pid is alive and its current start identity matches
                       remembered_start_key. Safe to treat as the original
                       process, still running.
      "MISMATCH"   -- pid exists but its start identity differs (the pid was
                       reused by a different process) or pid no longer
                       exists at all. The original process is gone.
      "ALIVE_UNVERIFIED" -- pid is alive but current identity could not be
                       captured for comparison (platform probe failed), and
                       there is no positive evidence the original process is
                       gone. Treated as still running, not redispatched, but
                       distinguishable in evidence from a confirmed MATCH.
      "UNKNOWN"    -- remembered_start_key is None (identity was never
                       captured for this execution, e.g. a row that predates
                       durable identity tracking). No liveness claim can be
                       made from identity alone.
    """
    if remembered_start_key is None:
        return "UNKNOWN"
    current_key = capture_process_identity(pid)
    if current_key is not None:
        return "MATCH" if current_key == remembered_start_key else "MISMATCH"
    if _process_exists(pid):
        return "ALIVE_UNVERIFIED"
    return "MISMATCH"


def _process_exists(pid: int) -> bool:
    """True iff `pid` refers to a process that is genuinely still running.

    A zombie (a child that has exited but not yet been reaped by its
    parent) is NOT considered to exist here: `kill(pid, 0)` alone still
    succeeds for a zombie, because the kernel keeps its pid slot allocated
    until reaped, but the worker it represents has already exited -- an
    executor that treated that as "still running" would strand a task
    waiting on a process that will never produce anything more.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    stat = _read_posix_stat_fields(pid)
    if stat is not None:
        state, _starttime = stat
        return state != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists and is not ours to introspect via /proc; best effort.
        return True
    except OSError:
        return False
    return True


def _read_posix_stat_fields(pid: int) -> tuple[str, str] | None:
    """Return (state, starttime) from /proc/<pid>/stat, or None if unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return None
    try:
        # comm (field 2) is parenthesized and may itself contain ')' and
        # spaces, so split on the LAST ')' before reading the remaining
        # space-delimited fields: index0=state(field3), ...,
        # starttime is field22 overall -> index19 after the split.
        after_comm = content.rsplit(")", 1)[1].split()
        state = after_comm[0]
        starttime = after_comm[19]
        if not starttime.isdigit():
            return None
    except (IndexError, ValueError):
        return None
    return state, starttime


def _posix_process_start_key(pid: int) -> str | None:
    stat = _read_posix_stat_fields(pid)
    if stat is None:
        return _posix_ps_start_key(pid)
    state, starttime = stat
    if state == "Z":
        # A zombie has exited; it has no meaningful "still running" identity.
        return None
    return f"proc:{starttime}"


def _posix_ps_start_key(pid: int) -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return f"ps:{value}" if value else None


def _windows_process_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED (5): the process exists but is not queryable.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ctypes.get_last_error() == 5
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_start_key(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if value == 0:
            return None
        return f"win:{value}"
    finally:
        kernel32.CloseHandle(handle)


def _close_handle(handle) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(handle)
