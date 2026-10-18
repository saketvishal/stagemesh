"""The one supported scheduling mechanism for delivery follow-ups.

A follow-up is woken by exactly one OS schedule, owned by StageMesh and named after the follow-up key. On Windows that is a Task
Scheduler task created with `schtasks` (periodic, at the follow-up's check interval). There is no second mechanism: on other
platforms `default_scheduler` returns None, the window records that no follow-up could be scheduled, and the operator is given the
one command to run instead.

The adapter is deliberately small and idempotent:

* `ensure`  creates (or replaces) the schedule for a key and returns its handle; calling it again changes nothing.
* `remove`  deletes the schedule and its launcher; deleting something already gone is success.
* `exists`  asks the OS, so completion can *verify* cleanup instead of trusting an internal flag.

The scheduled command is a tiny launcher script inside `.stagemesh/followups/`, because `schtasks /TR` is limited to 261 characters
and a runtime path plus a project path easily exceed it. The launcher holds the exact `stagemesh follow-up --key K` command.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

TASK_FOLDER = "StageMesh"


class SchedulerError(RuntimeError):
    pass


class FollowUpScheduler(Protocol):
    name: str

    def ensure(self, key: str, argv: Sequence[str], interval_seconds: int) -> str: ...

    def remove(self, handle: str) -> None: ...

    def exists(self, handle: str) -> bool: ...


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


class WindowsTaskScheduler:
    name = "windows-task-scheduler"

    def __init__(self, project: Path, *, runner: Runner = _run):
        self.project = Path(project)
        self.runner = runner
        self.directory = self.project / ".stagemesh" / "followups"

    def handle_for(self, key: str) -> str:
        return f"{TASK_FOLDER}\\followup-{key}"

    def launcher_for(self, key: str) -> Path:
        return self.directory / f"{key}.cmd"

    def ensure(self, key: str, argv: Sequence[str], interval_seconds: int) -> str:
        handle = self.handle_for(key)
        self.directory.mkdir(parents=True, exist_ok=True)
        launcher = self.launcher_for(key)
        log = self.directory / f"{key}.log"
        command = subprocess.list2cmdline(list(argv))
        launcher.write_text(f'@echo off\r\n{command} >> "{log}" 2>&1\r\n', encoding="utf-8")
        minutes = max(1, round(interval_seconds / 60))
        created = self.runner(["schtasks", "/Create", "/TN", handle, "/SC", "MINUTE", "/MO", str(minutes), "/TR", f'"{launcher}"', "/F"])
        if created.returncode != 0:
            raise SchedulerError(f"could not schedule the follow-up: {(created.stderr or created.stdout).strip()[-300:]}")
        return handle

    def remove(self, handle: str) -> None:
        deleted = self.runner(["schtasks", "/Delete", "/TN", handle, "/F"])
        if deleted.returncode != 0 and self.exists(handle):
            raise SchedulerError(f"could not remove the scheduled follow-up {handle}: {(deleted.stderr or deleted.stdout).strip()[-300:]}")
        key = handle.rsplit("followup-", 1)[-1]
        try:
            self.launcher_for(key).unlink(missing_ok=True)
        except OSError:
            pass  # the schedule is gone; a leftover launcher is inert

    def exists(self, handle: str) -> bool:
        return self.runner(["schtasks", "/Query", "/TN", handle]).returncode == 0


def default_scheduler(project: Path) -> FollowUpScheduler | None:
    """The single supported mechanism on this platform, or None when there is none."""
    return WindowsTaskScheduler(project) if sys.platform == "win32" else None


def follow_up_command(project: Path, key: str) -> list[str]:
    """The exact command a schedule runs. Uses the interpreter that registered it, so the follow-up runs the same installed runtime."""
    return [sys.executable, "-m", "stagemesh.cli", "--project", str(project), "follow-up", "--key", key]


def describe(argv: Sequence[str]) -> str:
    return shlex.join(list(argv))
