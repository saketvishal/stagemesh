"""Opt-in Windows Task Scheduler adapter for the local continuous driver."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape

TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DEFAULT_EXECUTION_LIMIT_SECONDS = 2 * 60 * 60


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


def current_user_id() -> str:
    """DOMAIN\\user for the account that installs the schedule (the task runs only as that interactive user)."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def build_task_xml(
    launcher: Path,
    interval_seconds: int,
    *,
    user_id: str,
    start: datetime,
    execution_limit_seconds: int = DEFAULT_EXECUTION_LIMIT_SECONDS,
    arguments: str | None = None,
) -> str:
    """Task Scheduler definition with the reliability behavior `schtasks /SC MINUTE` cannot express.

    * A logon trigger restarts the driver after a reboot or sign-in; the repeating time trigger covers the rest.
    * `StartWhenAvailable` runs a tick that was missed while the machine slept or was off.
    * Battery power neither blocks a start nor stops a running tick, so a laptop is not silently idle.
    * `IgnoreNew` plus the driver's own project lock means a slow tick is never overlapped by the next one.
    * `ExecutionTimeLimit` is a last-resort bound: Task Scheduler terminates the whole task process tree at the limit.
    * Least privilege, interactive token: no stored credentials and no elevation.
    """
    if interval_seconds < 60 or interval_seconds % 60:
        raise DriverSchedulerError("Task Scheduler cadence must be an exact whole number of minutes")
    if execution_limit_seconds < 60:
        raise DriverSchedulerError("the execution time limit must be at least one minute")
    user = escape(user_id)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{TASK_NAMESPACE}">\n'
        "  <RegistrationInfo><Description>StageMesh local continuous driver (project-owned, opt-in)</Description></RegistrationInfo>\n"
        "  <Triggers>\n"
        f"    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>\n"
        "    <TimeTrigger>\n"
        f"      <Repetition><Interval>PT{interval_seconds // 60}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>\n"
        f"      <StartBoundary>{start.strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        f'  <Principals><Principal id="Author"><UserId>{user}</UserId>'
        "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <Enabled>true</Enabled>\n"
        f"    <ExecutionTimeLimit>PT{execution_limit_seconds // 60}M</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec>'
        f"<Command>{escape(str(launcher))}</Command>"
        + (f"<Arguments>{escape(arguments)}</Arguments>" if arguments else "")
        + "</Exec></Actions>\n"
        "</Task>\n"
    )


class WindowsDriverScheduler:
    name = "windows-task-scheduler"

    def __init__(
        self,
        project: Path,
        *,
        runner: Runner = _run,
        task_name: str | None = None,
        execution_limit_seconds: int = DEFAULT_EXECUTION_LIMIT_SECONDS,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.project = Path(project).resolve()
        self.runner = runner
        self.directory = self.project / ".stagemesh" / "driver"
        self._task_name = task_name
        self.execution_limit_seconds = execution_limit_seconds
        self.clock = clock

    @property
    def handle(self) -> str:
        if self._task_name:
            return self._task_name
        digest = hashlib.sha256(str(self.project).casefold().encode("utf-8")).hexdigest()[:12]
        return f"StageMesh\\driver-{digest}"

    @property
    def definition(self) -> Path:
        return self.directory / "task.xml"

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
        xml = build_task_xml(
            self.launcher,
            interval_seconds,
            user_id=current_user_id(),
            start=self.clock().replace(microsecond=0),
            execution_limit_seconds=self.execution_limit_seconds,
        )
        temporary_definition = self.definition.with_suffix(".xml.tmp")
        temporary_definition.write_text(xml, encoding="utf-16")  # schtasks /XML expects UTF-16 with a BOM
        temporary_definition.replace(self.definition)
        created = self.runner(["schtasks", "/Create", "/TN", self.handle, "/XML", str(self.definition), "/F"])
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
        for leftover in (self.launcher, self.definition):
            try:
                leftover.unlink(missing_ok=True)
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
