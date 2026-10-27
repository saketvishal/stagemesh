"""Terminate only the processes a validation gate actually owns."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass

from .domain import ProcessIdentity
from .process_identity import popen_identity, process_identity
from .queue_control import OperatorStopRequested


@dataclass(frozen=True)
class GateTermination:
    """What happened to the gate process. Never reports success for a timeout."""

    terminated: bool
    note: str
    returncode: int | None
    cleanup_confirmed: bool = False


class GateCleanupUnconfirmed(OperatorStopRequested):
    """Uncertain gate ownership is control flow, never a failed validation verdict."""

    def __init__(self, note: str, identity: ProcessIdentity, *, cwd: str | None = None):
        super().__init__("validation gate cleanup unconfirmed: " + note, cleanup_complete=False)
        self.gate_identity = identity
        self.gate_cwd = cwd
        self.retained_gate_temp: str | None = None


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
    """Reap the owned root and require successful cleanup of its observed tree.

    Root exit alone is not tree proof. Unavailable snapshots, identities, failed
    signals or waits remain unconfirmed. No reused process is signalled.
    """
    if proc.poll() is not None:
        return GateTermination(False, "owned root exited; process-tree cleanup unconfirmed", proc.returncode)
    observed = process_identity(proc.pid)
    if _known_identity_mismatch(saved, observed):
        return GateTermination(False, "process identity no longer matches; not terminated; no exit code", proc.returncode)
    if sys.platform == "win32":
        tree_confirmed = _terminate_windows_descendants(proc.pid, saved.create_time)
        try:
            proc.kill()  # the Popen handle remains bound to the original root
        except OSError:
            pass
    else:
        tree_confirmed = _terminate_posix_group(proc, saved)
    reaped = _reap_if_exited(proc)
    confirmed = reaped and tree_confirmed
    note = "owned process tree terminated; no exit code" if confirmed else "owned process-tree cleanup unconfirmed; no exit code"
    return GateTermination(reaped, note, proc.returncode, cleanup_confirmed=confirmed)


def _known_identity_mismatch(saved: ProcessIdentity, observed: ProcessIdentity | None) -> bool:
    return saved.is_known and observed is not None and observed.is_known and not saved.matches(observed)


def _reap_if_exited(proc: subprocess.Popen[str]) -> bool:
    try:
        return proc.wait(timeout=5) is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


def _terminate_windows_descendants(root_pid: int, root_create: float | None) -> bool:
    children: dict[int, list[int]] = {}
    snapshot = _windows_parent_snapshot()
    if snapshot is None:
        return False
    confirmed = True
    for pid, parent in snapshot:
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
        if root_create is None:
            confirmed = False
            continue
        if created is not None and created < root_create:
            # A process born before this root belongs to a prior PID incarnation.
            # Ignore that whole unrelated branch, including its descendants.
            continue
        if identity is None:
            if children.get(pid):
                confirmed = False  # an exited intermediary cannot prove its observed descendants gone
            continue  # already gone; never signal a PID without identity
        if not identity.is_known or created is None:
            confirmed = False
            continue
        owned.append((pid, created))
        stack.extend(children.get(pid, []))
    deadline = time.monotonic() + 5.0
    for pid, created in reversed(owned):
        wait_ms = max(0, int((deadline - time.monotonic()) * 1000))
        confirmed = _terminate_windows_pid(pid, created, wait_ms=wait_ms) and confirmed
    return confirmed


def _terminate_windows_pid(pid: int, created: float | None, *, wait_ms: int = 5000) -> bool:
    current = process_identity(pid)
    if current is None:
        return True
    if not current.is_known or created is None:
        return False
    if current.create_time != created:
        return True  # the saved process is gone; do not signal the replacement
    import ctypes
    from ctypes import wintypes

    from .process_identity import _windows_process_identity

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    rights = 0x0001 | 0x1000 | 0x00100000  # terminate, query identity, synchronize
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(rights, False, pid)
    if not handle:
        return process_identity(pid) is None
    try:
        bound = _windows_process_identity(pid, owned_handle=int(handle))
        if bound is None:
            return True
        if not bound.is_known:
            return False
        if bound.create_time != created:
            return True
        # Verify and signal through the same handle; a PID race cannot redirect it.
        kernel32.TerminateProcess(handle, 1)
        # A failed signal is not proof, but the same owned handle may already be
        # exiting (for example, a venv launcher after its child). Wait within the
        # shared cleanup deadline; only the signalled process object proves exit.
        return kernel32.WaitForSingleObject(handle, wait_ms) == 0
    finally:
        kernel32.CloseHandle(handle)


def _windows_parent_snapshot() -> list[tuple[int, int]] | None:
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
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    rows: list[tuple[int, int]] = []
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return [] if ctypes.get_last_error() == 18 else None
        while True:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES is successful exhaustion
                    return None
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return rows


def _terminate_posix_group(proc: subprocess.Popen[str], saved: ProcessIdentity) -> bool:
    import os
    import signal

    try:
        group = os.getpgid(proc.pid)
    except OSError:
        group = None
    current = process_identity(proc.pid)
    if _known_identity_mismatch(saved, current):
        return False
    confirmed = False
    if group == proc.pid:
        try:
            os.killpg(group, signal.SIGKILL)
            confirmed = True
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    return confirmed
