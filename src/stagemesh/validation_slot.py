"""One validation gate process at a time on this machine.

The slot is a file lock in the shared temp directory plus a process-local
mutex. Threads in one runtime and processes in other windows take the same
lock, so they cannot run gate processes side by side. Coding work does not
acquire it.

The operating system drops the file lock when the holding process exits,
including a crash. Ownership is the lock itself. A PID written into the file
is never consulted and is never a reason to signal another process.

Waiting polls. A stop or a supplied deadline returns before a gate is
started. Time spent waiting is not part of the gate's execution timeout.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from .queue_control import OperatorStopRequested, current_provider_stop

_mu = threading.Lock()
_depth = threading.local()
_wait_deadline: ContextVar[float | None] = ContextVar("stagemesh_validation_wait_deadline", default=None)
_cancel_wait: ContextVar[object | None] = ContextVar("stagemesh_validation_cancel", default=None)


def slot_path() -> Path:
    override = os.environ.get("STAGEMESH_VALIDATION_SLOT")
    if override:
        return Path(override)
    return Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp") / "stagemesh-validation-slot.lock"


def bind_validation_wait(deadline: float | None = None, cancel=None):
    """Return a token pair. Tests and a window deadline set these; run_gate reads them."""
    return _wait_deadline.set(deadline), _cancel_wait.set(cancel)


def reset_validation_wait(tokens) -> None:
    deadline_token, cancel_token = tokens
    _wait_deadline.reset(deadline_token)
    _cancel_wait.reset(cancel_token)


def _stop_requested() -> bool:
    cancel = _cancel_wait.get()
    if cancel is not None and bool(cancel()):
        return True
    stop = current_provider_stop()
    request = getattr(stop, "request", None) if stop is not None else None
    return bool(request and request.get("terminate_running") is True)


def _try_file_lock(handle) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _file_unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def validation_slot() -> Iterator[float]:
    """Acquire the slot. Yield how many seconds the caller waited.

    The same thread may re-enter without taking the file lock a second time.
    Another thread or process waits outside the gate timeout.
    """
    depth = getattr(_depth, "value", 0)
    if depth:
        _depth.value = depth + 1
        try:
            yield 0.0
        finally:
            _depth.value -= 1
        return

    started = time.monotonic()
    path = slot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.seek(0, os.SEEK_END) < 1:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    held = False
    try:
        while True:
            if _stop_requested():
                raise OperatorStopRequested("validation cancelled while waiting for the slot")
            deadline = _wait_deadline.get()
            if deadline is not None and time.monotonic() >= deadline:
                raise OperatorStopRequested("validation slot wait exceeded the task deadline")
            if _mu.acquire(blocking=False):
                try:
                    if _try_file_lock(handle):
                        held = True
                        break
                finally:
                    if not held:
                        _mu.release()
            time.sleep(0.05)
        waited = time.monotonic() - started
        _depth.value = 1
        try:
            yield waited
        finally:
            _depth.value = 0
            _file_unlock(handle)
            held = False
            _mu.release()
    finally:
        handle.close()
