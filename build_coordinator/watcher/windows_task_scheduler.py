"""Windows Task Scheduler adapter for unattended watcher startup
(SDD-001 section 4.2).

A narrow wrapper around `schtasks.exe` so `watcher install/uninstall/start/
stop/status` can be unit tested with a fake adapter instead of touching the
real Windows scheduler. The installed action always calls the checked-in
CLI entry point (`stagemesh watcher run --foreground`) -- never an
inline script -- and never embeds secrets in the command-line arguments:
tokens and provider credentials come from the operator environment or
credential manager, not from Task Scheduler.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from typing import Protocol


def stable_task_name(control_repo_root: str, repository_slug: str) -> str:
    """Deterministic Task Scheduler task name scoped to the authorized
    repository. Derived from a hash of the normalized control repo root and
    GitHub slug so the name never embeds a token, branch name, or secret."""
    normalized = f"{control_repo_root.strip().rstrip('/\\').lower()}|{repository_slug.strip().lower()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"BuildCoordinator-{digest}"


@dataclass(frozen=True)
class TaskDefinition:
    task_name: str
    command: str
    arguments: str
    working_directory: str


@dataclass(frozen=True)
class TaskQueryStatus:
    installed: bool | None
    running: bool | None
    state: str
    error: str | None = None


class TaskSchedulerAdapter(Protocol):
    def task_exists(self, task_name: str) -> bool: ...

    def query_definition(self, task_name: str) -> TaskDefinition | None: ...

    def create_or_update(self, definition: TaskDefinition) -> None: ...

    def delete(self, task_name: str) -> None: ...

    def run(self, task_name: str) -> None: ...

    def is_running(self, task_name: str) -> bool: ...

    def query_status(self, task_name: str) -> TaskQueryStatus: ...


class SchtasksError(RuntimeError):
    """Raised when a `schtasks.exe` invocation fails."""


class SchtasksAdapter:
    """Default adapter backed by `schtasks.exe`, run at user logon under
    the current operator account -- no administrator privileges, no
    machine-wide service, no embedded secrets."""

    def __init__(self, *, schtasks_path: str = "schtasks.exe") -> None:
        self._schtasks_path = schtasks_path

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self._schtasks_path, *args],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise SchtasksError(f"failed to run schtasks {args}: {exc}") from exc

    def task_exists(self, task_name: str) -> bool:
        return self.query_status(task_name).installed is True

    @staticmethod
    def _query_error_state(result: subprocess.CompletedProcess) -> str:
        text = f"{result.stderr}\n{result.stdout}".lower()
        if "access is denied" in text:
            return "ACCESS_DENIED"
        if "system cannot find the path specified" in text:
            return "QUERY_UNAVAILABLE"
        if "cannot find" in text or "does not exist" in text or "not found" in text:
            return "NOT_FOUND"
        return "UNKNOWN"

    def query_status(self, task_name: str) -> TaskQueryStatus:
        result = self._run(["/Query", "/TN", task_name, "/FO", "LIST"])
        if result.returncode != 0:
            state = self._query_error_state(result)
            if state == "NOT_FOUND":
                return TaskQueryStatus(installed=False, running=False, state=state)
            return TaskQueryStatus(
                installed=None,
                running=None,
                state=state,
                error=result.stderr.strip() or result.stdout.strip(),
            )
        running = "Running" in result.stdout
        return TaskQueryStatus(installed=True, running=running, state="RUNNING" if running else "INSTALLED")

    def query_definition(self, task_name: str) -> TaskDefinition | None:
        result = self._run(["/Query", "/TN", task_name, "/FO", "LIST", "/V"])
        if result.returncode != 0:
            return None
        fields = {}
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        task_to_run = fields.get("Task To Run", "")
        command, _, arguments = task_to_run.partition(" ")
        return TaskDefinition(
            task_name=task_name,
            command=command,
            arguments=arguments,
            working_directory=fields.get("Start In", ""),
        )

    def create_or_update(self, definition: TaskDefinition) -> None:
        # /Create with /F updates an existing task definition in place --
        # this is what makes `watcher install` idempotent across changes to
        # the configured Python executable, repo root, or arguments.
        task_run = f'"{definition.command}" {definition.arguments}'.strip()
        result = self._run(
            [
                "/Create",
                "/F",
                "/TN",
                definition.task_name,
                "/TR",
                task_run,
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
            ]
        )
        if result.returncode != 0:
            raise SchtasksError(
                f"failed to create/update task {definition.task_name!r}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    def delete(self, task_name: str) -> None:
        if not self.task_exists(task_name):
            return
        result = self._run(["/Delete", "/TN", task_name, "/F"])
        if result.returncode != 0:
            raise SchtasksError(
                f"failed to delete task {task_name!r}: {result.stderr.strip() or result.stdout.strip()}"
            )

    def run(self, task_name: str) -> None:
        result = self._run(["/Run", "/TN", task_name])
        if result.returncode != 0:
            raise SchtasksError(
                f"failed to start task {task_name!r}: {result.stderr.strip() or result.stdout.strip()}"
            )

    def is_running(self, task_name: str) -> bool:
        return self.query_status(task_name).running is True


class FakeTaskSchedulerAdapter:
    """In-memory fake for tests. Never touches the real Windows scheduler."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {}
        self._running: set[str] = set()

    def task_exists(self, task_name: str) -> bool:
        return task_name in self._tasks

    def query_definition(self, task_name: str) -> TaskDefinition | None:
        return self._tasks.get(task_name)

    def create_or_update(self, definition: TaskDefinition) -> None:
        self._tasks[definition.task_name] = definition

    def delete(self, task_name: str) -> None:
        self._tasks.pop(task_name, None)
        self._running.discard(task_name)

    def run(self, task_name: str) -> None:
        if task_name not in self._tasks:
            raise SchtasksError(f"task {task_name!r} is not installed")
        self._running.add(task_name)

    def is_running(self, task_name: str) -> bool:
        return task_name in self._running

    def query_status(self, task_name: str) -> TaskQueryStatus:
        if task_name not in self._tasks:
            return TaskQueryStatus(installed=False, running=False, state="NOT_FOUND")
        running = task_name in self._running
        return TaskQueryStatus(installed=True, running=running, state="RUNNING" if running else "INSTALLED")
