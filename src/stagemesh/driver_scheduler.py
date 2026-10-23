"""Opt-in Windows Task Scheduler adapter for the local continuous driver."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


class DriverSchedulerError(RuntimeError):
    pass


class DriverScheduler(Protocol):
    name: str

    def ensure(self, argv: Sequence[str], interval_seconds: int) -> str: ...

    def remove(self, handle: str) -> None: ...

    def exists(self, handle: str) -> bool: ...


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


class WindowsDriverScheduler:
    name = "windows-task-scheduler"

    def __init__(self, project: Path, *, runner: Runner = _run):
        self.project = Path(project).resolve()
        self.runner = runner
        self.directory = self.project / ".stagemesh" / "driver"

    @property
    def handle(self) -> str:
        digest = hashlib.sha256(str(self.project).casefold().encode("utf-8")).hexdigest()[:12]
        return f"StageMesh\\driver-{digest}"

    @property
    def launcher(self) -> Path:
        return self.directory / "run.cmd"

    def ensure(self, argv: Sequence[str], interval_seconds: int) -> str:
        if interval_seconds < 60 or interval_seconds % 60:
            raise DriverSchedulerError("Task Scheduler cadence must be an exact whole number of minutes")
        self.directory.mkdir(parents=True, exist_ok=True)
        log = self.directory / "driver.log"
        command = subprocess.list2cmdline(list(argv))
        temporary = self.launcher.with_suffix(".cmd.tmp")
        temporary.write_text(f'@echo off\r\n{command} >> "{log}" 2>&1\r\n', encoding="utf-8")
        temporary.replace(self.launcher)
        minutes = interval_seconds // 60
        created = self.runner(
            [
                "schtasks",
                "/Create",
                "/TN",
                self.handle,
                "/SC",
                "MINUTE",
                "/MO",
                str(minutes),
                "/TR",
                f'"{self.launcher}"',
                "/RL",
                "LIMITED",
                "/F",
            ]
        )
        if created.returncode != 0:
            raise DriverSchedulerError(
                f"could not schedule the local driver: {(created.stderr or created.stdout).strip()[-300:]}"
            )
        return self.handle

    def remove(self, handle: str) -> None:
        if handle != self.handle:
            raise DriverSchedulerError("refusing to remove a scheduler task owned by another project")
        deleted = self.runner(["schtasks", "/Delete", "/TN", handle, "/F"])
        if deleted.returncode != 0 and self.exists(handle):
            raise DriverSchedulerError(
                f"could not remove the local driver schedule: {(deleted.stderr or deleted.stdout).strip()[-300:]}"
            )
        try:
            self.launcher.unlink(missing_ok=True)
        except OSError:
            pass

    def exists(self, handle: str) -> bool:
        if handle != self.handle:
            return False
        queried = self.runner(["schtasks", "/Query", "/TN", handle])
        if queried.returncode == 0:
            return True
        detail = (queried.stderr or queried.stdout).strip()
        lowered = detail.casefold()
        if "cannot find" in lowered or "not found" in lowered or "does not exist" in lowered:
            return False
        raise DriverSchedulerError(f"could not query the local driver schedule: {detail[-300:] or 'unknown error'}")


def default_driver_scheduler(project: Path) -> DriverScheduler | None:
    return WindowsDriverScheduler(project) if sys.platform == "win32" else None


def driver_command(project: Path) -> list[str]:
    return [sys.executable, "-m", "stagemesh.cli", "--project", str(Path(project).resolve()), "driver", "tick"]
