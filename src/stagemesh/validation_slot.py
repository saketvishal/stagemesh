"""One validation gate process at a time for this user's default temp resource.

The scope is the OS user, not the whole machine. Every runtime copy that
resolves the same canonical lock path shares it: threads in one process and
processes in other windows. A different STAGEMESH_VALIDATION_SLOT path is a
different resource and is not serialized with the default. Coding work does
not acquire the slot.

The operating system drops the file lock when the holding process exits,
including a crash. Ownership is that lock. Bytes in the file, including a
PID, are never read and are never a reason to signal another process.

Waiting polls. A terminate-stop, a bound cancel callback, or an applicable
deadline returns before a gate is started and is checked again after the
lock is held. Time spent waiting is not part of the gate's execution timeout.
"""

from __future__ import annotations

import os
import sys
import tempfile
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
_window_deadline: float | None = None


def canonical_lock_path(raw: str) -> Path:
    """One identity for spellings that name the same file.

    Case, slashes, and relative segments are folded. A stored PID is not
    part of the identity.
    """
    absolute = os.path.abspath(os.path.expanduser(raw))
    return Path(os.path.normcase(absolute))


def slot_path() -> Path:
    override = os.environ.get("STAGEMESH_VALIDATION_SLOT")
    if override:
        return canonical_lock_path(override)
    # tempfile picks one directory from TMP/TEMP/TMPDIR and normalizes it.
    # Using that directory, not whichever variable happens to be set, keeps
    # two runtime copies on the same user temp from taking two locks.
    return canonical_lock_path(str(Path(tempfile.gettempdir()) / "stagemesh-validation-slot.lock"))


def current_window_deadline() -> float | None:
    return _window_deadline


def set_window_deadline(deadline: float | None) -> None:
    """Publish a wall-clock deadline (the same clock run_wave uses) to every worker."""
    global _window_deadline
    _window_deadline = deadline


def bind_validation_wait(deadline: float | None = None, cancel=None):
    """Return a token pair. Tests and a window deadline set these; run_gate reads them."""
    return _wait_deadline.set(deadline), _cancel_wait.set(cancel)


def reset_validation_wait(tokens) -> None:
    deadline_token, cancel_token = tokens
    _wait_deadline.reset(deadline_token)
    _cancel_wait.reset(cancel_token)


def _published_window_deadline() -> float | None:
    if _window_deadline is not None:
        return _window_deadline
    raw = os.environ.get("STAGEMESH_WINDOW_DEADLINE")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        # A broken deadline must not become permission to wait without a bound.
        return 0.0


def _deadline_exceeded() -> bool:
    deadline = _wait_deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        return True
    window = _published_window_deadline()
    return window is not None and time.time() >= window


def _stop_requested() -> bool:
    cancel = _cancel_wait.get()
    if cancel is not None and bool(cancel()):
        return True
    stop = current_provider_stop()
    if stop is not None:
        stop.poll()
        request = getattr(stop, "request", None)
        if request and request.get("terminate_running") is True:
            return True
    return _deadline_exceeded()


def raise_if_validation_blocked() -> None:
    """Call again after the slot is held and before any gate process is created."""
    if _stop_requested():
        raise OperatorStopRequested("validation cancelled before the gate started")


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
                reason = (
                    "validation slot wait exceeded the task deadline"
                    if _deadline_exceeded()
                    else "validation cancelled while waiting for the slot"
                )
                raise OperatorStopRequested(reason)
            if _mu.acquire(blocking=False):
                try:
                    if _try_file_lock(handle):
                        held = True
                        break
                finally:
                    if not held:
                        _mu.release()
            time.sleep(0.05)
        try:
            if _stop_requested():
                reason = (
                    "validation slot wait exceeded the task deadline"
                    if _deadline_exceeded()
                    else "validation cancelled before the gate started"
                )
                raise OperatorStopRequested(reason)
            waited = time.monotonic() - started
            _depth.value = 1
            yield waited
        finally:
            _depth.value = 0
            _file_unlock(handle)
            held = False
            _mu.release()
    finally:
        handle.close()
