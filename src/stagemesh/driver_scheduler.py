"""Opt-in Windows Task Scheduler adapter for the local continuous driver."""

from __future__ import annotations

import csv
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

    def ensure(self, argv: Sequence[str], interval_seconds: int, execution_limit_seconds: int | None = None) -> str: ...

    def remove(self, handle: str) -> None: ...

    def exists(self, handle: str) -> bool: ...


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


def current_user_id() -> str:
    """DOMAIN\\user for the account that installs the schedule (the task runs only as that interactive user)."""
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        raise DriverSchedulerError("cannot determine the Windows user that should own the schedule (USERNAME is not set)")
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def build_task_xml(
    command: Path,
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
    * `ExecutionTimeLimit` is a watchdog for a hung tick process. The tick is the task's own process (the action runs
      Python directly, not through `cmd.exe`), so the limit really ends it. Real-Windows evidence shows End and the
      limit terminate only that process, never its descendants; the limit must therefore exceed the tick's own hard
      timeout so the driver bounds and kills its `run-window` child tree first (`kill_process_tree`, verified).
      A tick the watchdog ends can leave that child running; the next tick sees its durable owner and does not overlap.
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
        f"    <ExecutionTimeLimit>PT{-(-execution_limit_seconds // 60)}M</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author"><Exec>'
        f"<Command>{escape(str(command))}</Command>"
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

    def ensure(self, argv: Sequence[str], interval_seconds: int, execution_limit_seconds: int | None = None) -> str:
        if interval_seconds < 60 or interval_seconds % 60:
            raise DriverSchedulerError("Task Scheduler cadence must be an exact whole number of minutes")
        if not argv:
            raise DriverSchedulerError("a scheduled command is required")
        self.directory.mkdir(parents=True, exist_ok=True)
        xml = build_task_xml(
            Path(argv[0]),
            interval_seconds,
            user_id=current_user_id(),
            start=self.clock().replace(microsecond=0),
            execution_limit_seconds=execution_limit_seconds or self.execution_limit_seconds,
            arguments=subprocess.list2cmdline(list(argv[1:])) or None,
        )
        temporary_definition = self.definition.with_suffix(".xml.tmp")
        temporary_definition.write_text(xml, encoding="utf-16")  # schtasks /XML expects UTF-16 with a BOM
        temporary_definition.replace(self.definition)
        try:
            created = self.runner(["schtasks", "/Create", "/TN", self.handle, "/XML", str(self.definition), "/F"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._discard_definition()
            raise DriverSchedulerError(f"could not schedule the local driver: {exc}") from exc
        if created.returncode != 0:
            self._discard_definition()
            raise DriverSchedulerError(
                f"could not schedule the local driver: {(created.stderr or created.stdout).strip()[-300:]}"
            )
        return self.handle

    def _discard_definition(self) -> None:
        for leftover in (self.definition, self.definition.with_suffix(".xml.tmp")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass

    def remove(self, handle: str) -> None:
        if handle != self.handle:
            raise DriverSchedulerError("refusing to remove a scheduler task owned by another project")
        deleted = self.runner(["schtasks", "/Delete", "/TN", handle, "/F"])
        if deleted.returncode != 0 and self.exists(handle):
            raise DriverSchedulerError(
                f"could not remove the local driver schedule: {(deleted.stderr or deleted.stdout).strip()[-300:]}"
            )
        self._discard_definition()

    def exists(self, handle: str) -> bool:
        """True when the task is registered. Decided from exit codes and the task list, never from localized message text."""
        if handle != self.handle:
            return False
        try:
            queried = self.runner(["schtasks", "/Query", "/TN", handle])
            if queried.returncode == 0:
                return True
            listed = self.runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DriverSchedulerError(f"could not query the local driver schedule: {exc}") from exc
        if listed.returncode != 0:
            detail = (listed.stderr or listed.stdout or queried.stderr or queried.stdout).strip()
            raise DriverSchedulerError(f"could not query the local driver schedule: {detail[-300:] or 'unknown error'}")
        wanted = ("\\" + handle).casefold()
        return any(row and row[0].strip().casefold() == wanted for row in csv.reader(listed.stdout.splitlines()))


def default_driver_scheduler(project: Path) -> DriverScheduler | None:
    return WindowsDriverScheduler(project) if sys.platform == "win32" else None


def driver_command(project: Path) -> list[str]:
    resolved = Path(project).resolve()
    log = resolved / ".stagemesh" / "driver" / "driver.log"
    return [sys.executable, "-m", "stagemesh.cli", "--project", str(resolved), "driver", "tick", "--log-file", str(log)]
