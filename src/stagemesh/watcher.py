from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Any

from .process_identity import is_pid_alive, boot_id as current_boot_id
from .persistence import Store
from .coordinator import Coordinator


class WatcherLockError(Exception):
    """Raised when daemon lock acquisition fails."""
    pass


class WatcherLock:
    """Single-instance locking primitive for StageMesh watcher daemon."""

    def __init__(self, lock_file: Path):
        self.lock_file = Path(lock_file)
        self.pid = os.getpid()
        self.boot_id = current_boot_id()
        self.hostname = socket.gethostname()
        self.metadata: dict[str, Any] = {}

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_file.exists():
            try:
                content = json.loads(self.lock_file.read_text(encoding="utf-8"))
                held_pid = content.get("pid")
                held_boot = content.get("boot_id")
                if held_pid and is_pid_alive(held_pid, held_boot):
                    raise WatcherLockError(
                        f"Daemon lock already held by live PID {held_pid} (boot_id={held_boot})"
                    )
            except (json.JSONDecodeError, OSError) as exc:
                if isinstance(exc, WatcherLockError):
                    raise
                # Corrupted lockfile - overwrite safely

        now = time.time()
        self.metadata = {
            "pid": self.pid,
            "boot_id": self.boot_id,
            "hostname": self.hostname,
            "created_at": now,
            "heartbeat_at": now,
        }
        self.lock_file.write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")

    def heartbeat(self) -> None:
        if not self.lock_file.exists():
            return
        self.metadata["heartbeat_at"] = time.time()
        self.lock_file.write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")

    def release(self) -> None:
        if self.lock_file.exists():
            try:
                content = json.loads(self.lock_file.read_text(encoding="utf-8"))
                if content.get("pid") == self.pid:
                    self.lock_file.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                self.lock_file.unlink(missing_ok=True)


class WatcherLoop:
    """Operational background/foreground watcher loop for StageMesh."""

    def __init__(
        self,
        store: Store,
        project: Path | None = None,
        interval_seconds: float = 1.0,
        tick_fn: Callable[[], Any] | None = None,
        lock: WatcherLock | None = None,
    ):
        self.store = store
        self.project = project or store.db_path.parent.parent
        self.interval_seconds = interval_seconds
        self.lock = lock
        self.running = False
        self._coordinator = Coordinator(store=self.store, project=self.project)
        self.tick_fn = tick_fn or self._coordinator.tick

    def run(self, max_ticks: int | None = None) -> int:
        if self.lock:
            self.lock.acquire()

        self.running = True
        ticks_executed = 0

        try:
            while self.running:
                self.tick_fn()
                ticks_executed += 1

                if self.lock:
                    self.lock.heartbeat()

                if max_ticks and ticks_executed >= max_ticks:
                    break

                if self.running:
                    time.sleep(self.interval_seconds)
        finally:
            self.running = False
            if self.lock:
                self.lock.release()

        return ticks_executed

    def stop(self) -> None:
        self.running = False
