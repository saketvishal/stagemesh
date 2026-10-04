"""Primitives for running several tasks at once: provider slots/cooldown, the integration lock and contract conflict checks."""

from __future__ import annotations

import fnmatch
import os
import sys
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contracts import ChangeContract


class ProviderLimiter:
    """Shared by every task thread: caps simultaneous runs per provider and remembers provider-wide cooldowns.

    The per-task cooldown in ProviderPool only hides a provider from the task that failed on it; under parallel execution a
    rate-limited provider must also stop receiving work from the other tasks, which is what `cool_down` records.
    """

    def __init__(self, default_limit: int | None = None, limits: dict[str, int] | None = None):
        self.default_limit = default_limit
        self.limits = dict(limits or {})
        self._active: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._reasons: dict[str, str] = {}
        self._condition = threading.Condition()

    def limit(self, provider: str) -> int | None:
        return self.limits.get(provider, self.default_limit)

    def active(self, provider: str) -> int:
        with self._condition:
            return self._active.get(provider, 0)

    def cooling(self, provider: str) -> str | None:
        with self._condition:
            until = self._cooldown_until.get(provider, 0.0)
            if until > time.time():
                return f"provider_cooldown: {self._reasons.get(provider, 'failure')} ({int(until - time.time())}s remaining, all tasks)"
            return None

    def cool_down(self, provider: str, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        with self._condition:
            self._cooldown_until[provider] = time.time() + seconds
            self._reasons[provider] = reason
            self._condition.notify_all()

    def _has_room(self, provider: str) -> bool:
        limit = self.limit(provider)
        return limit is None or self._active.get(provider, 0) < limit

    def has_room(self, provider: str) -> bool:
        with self._condition:
            return self._has_room(provider)

    def acquire(self, candidates: list[str], timeout: float | None = None) -> str | None:
        """Take a slot on the first candidate (in the given order) that has room, waiting until one does.

        Returns None only on timeout or when every candidate is cooling down, so callers never wait on a provider that
        cannot become usable without outside action.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                usable = [name for name in candidates if self._cooldown_until.get(name, 0.0) <= time.time()]
                if not usable:
                    return None
                ordered = sorted(usable, key=lambda name: (self._active.get(name, 0), candidates.index(name)))
                for name in ordered:
                    if self._has_room(name):
                        self._active[name] = self._active.get(name, 0) + 1
                        return name
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(timeout=1.0 if remaining is None else min(1.0, remaining))

    def release(self, provider: str) -> None:
        with self._condition:
            self._active[provider] = max(0, self._active.get(provider, 0) - 1)
            self._condition.notify_all()

    @contextmanager
    def slot(self, candidates: list[str]):
        name = self.acquire(candidates)
        try:
            yield name
        finally:
            if name is not None:
                self.release(name)

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._condition:
            names = sorted(set(self._active) | set(self._cooldown_until) | set(self.limits))
            return {
                name: {
                    "active": self._active.get(name, 0),
                    "limit": self.limit(name),
                    "cooling_down": self._cooldown_until.get(name, 0.0) > time.time(),
                }
                for name in names
            }


class IntegrationLockTimeout(RuntimeError):
    pass


class IntegrationLock:
    """Serializes updates of the integration ref across task threads AND across StageMesh processes.

    A thread lock orders the threads of this process; an OS file lock (released by the kernel if the holder dies, so a crash
    or Ctrl+C can never leave a stale lock behind) orders processes.
    """

    def __init__(self, path: Path, timeout_seconds: float = 600.0):
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self._thread_lock = threading.Lock()
        self.held_by: str | None = None

    @contextmanager
    def hold(self, owner: str):
        deadline = time.monotonic() + self.timeout_seconds
        if not self._thread_lock.acquire(timeout=self.timeout_seconds):
            raise IntegrationLockTimeout(f"timed out waiting for the integration lock held by {self.held_by or 'another task'}")
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+b")
            _lock_file(handle, deadline, self)
            self.held_by = owner
            try:
                yield
            finally:
                self.held_by = None
                _unlock_file(handle)
        finally:
            if handle is not None:
                handle.close()
            self._thread_lock.release()


def _lock_file(handle, deadline: float, lock: IntegrationLock) -> None:
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise IntegrationLockTimeout("timed out waiting for the integration lock held by another StageMesh process") from None
            time.sleep(0.05)


def _unlock_file(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _literal_prefix(pattern: str) -> str:
    for index, char in enumerate(pattern):
        if char in "*?[":
            return pattern[:index]
    return pattern


def _literal_suffix(pattern: str) -> str:
    for index in range(len(pattern) - 1, -1, -1):
        if pattern[index] in "*?]":
            return pattern[index + 1 :]
    return pattern


def patterns_overlap(first: str, second: str) -> bool:
    """Conservative: could some path match both glob patterns? False only when provably disjoint."""
    a, b = first.replace("\\", "/").casefold(), second.replace("\\", "/").casefold()
    if a == b or fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    prefix_a, prefix_b = _literal_prefix(a), _literal_prefix(b)
    if not (prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)):
        return False
    suffix_a, suffix_b = _literal_suffix(a), _literal_suffix(b)
    return suffix_a.endswith(suffix_b) or suffix_b.endswith(suffix_a)


def _any_overlap(left: Iterable[str], right: Iterable[str]) -> str | None:
    right = tuple(right)
    for a in left:
        for b in right:
            if patterns_overlap(a, b):
                return f"{a} / {b}" if a != b else a
    return None


@dataclass(frozen=True)
class ConflictReason:
    kind: str  # protected_files | exclusive_resource
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def contract_conflict(first: ChangeContract, second: ChangeContract) -> ConflictReason | None:
    """Why two tasks must not run at the same time, or None when their contracts leave them independent."""
    shared = sorted(set(first.exclusive_resources) & set(second.exclusive_resources), key=str.casefold)
    folded = {r.casefold() for r in first.exclusive_resources} & {r.casefold() for r in second.exclusive_resources}
    if shared or folded:
        return ConflictReason("exclusive_resource", ", ".join(shared or sorted(folded)))
    for protected, other in ((first.protected_files, second), (second.protected_files, first)):
        if not protected:
            continue
        hit = _any_overlap(protected, other.protected_files + other.allowed_files)
        if hit:
            return ConflictReason("protected_files", hit)
    return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
