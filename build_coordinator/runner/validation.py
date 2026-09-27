"""Deterministic execution of a task's `validation:` commands.

The commands come from the project's version-controlled task definition (via
synchronization), never from executor output. StageMesh runs them itself in the
task workspace and the result, not an agent's claim, gates the lifecycle.
"""

from __future__ import annotations

import os
import hashlib
import json
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from build_coordinator.execution.base import (
    ExecutionHandle,
    ExecutionLaunch,
    ExecutionObservation,
)

OUTPUT_TAIL_CHARS = 2000


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def validation_environment_fingerprint(env: dict[str, str] | None = None) -> str:
    """Stable, non-secret-ish fingerprint of the effective validation environment."""
    effective = {**os.environ, **(env or {})}
    material = {
        key: effective[key]
        for key in sorted(effective)
        if key in {"PATH", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        or key.startswith(("STAGEMESH_", "PYTEST_", "TOX_", "NOX_"))
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass
class ValidationOutcome:
    passed: bool
    results: list[dict[str, Any]] = field(default_factory=list)

    def failure_summary(self) -> list[str]:
        return [
            f"{item['command']} -> exit {item['exit_code']}: {item['output_tail'][-500:]}"
            for item in self.results
            if item["exit_code"] != 0
        ]


def split_command(command: str) -> list[str]:
    parts = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        parts = [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part for part in parts]
    if not parts:
        raise ValueError("empty validation command")
    return parts


def run_validation(
    commands: list[str],
    cwd: str | Path,
    *,
    timeout_seconds: float = 900,
    env: dict[str, str] | None = None,
) -> ValidationOutcome:
    """Run every command (stopping at the first failure) without a shell."""
    outcome = ValidationOutcome(passed=True)
    for command in commands:
        started = time.monotonic()
        started_at = _utc_now_iso()
        try:
            argv = split_command(command)
            resolved = shutil.which(argv[0], path=(env or os.environ).get("PATH")) or argv[0]
            proc = subprocess.run(
                [resolved, *argv[1:]],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env={**os.environ, **(env or {})},
                stdin=subprocess.DEVNULL,
            )
            exit_code = proc.returncode
            output = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            output = f"timed out after {timeout_seconds}s\n" + str(exc.stdout or "")[-500:]
            failure_type = "TIMEOUT"
        except (OSError, ValueError) as exc:
            exit_code = 127
            output = f"could not run command: {exc}"
            failure_type = "COMMAND_UNAVAILABLE"
        else:
            failure_type = "EXIT_CODE" if exit_code != 0 else None
        outcome.results.append(
            {
                "command": command,
                "exit_code": exit_code,
                "failure_type": failure_type,
                "started_at": started_at,
                "completed_at": _utc_now_iso(),
                "duration_seconds": round(time.monotonic() - started, 2),
                "output_tail": output[-OUTPUT_TAIL_CHARS:],
            }
        )
        if exit_code != 0:
            outcome.passed = False
            break
    return outcome


class ValidationExecutor:
    """Pollable executor for runner-owned validation commands."""

    adapter_name = "validation"

    def __init__(self, *, timeout_seconds: float = 900) -> None:
        self._timeout_seconds = timeout_seconds
        self._runs: dict[str, dict[str, Any]] = {}

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        execution_id = launch.execution_id or str(uuid4())
        commands = list(launch.metadata.get("commands") or [])
        if not commands:
            self._runs[execution_id] = {"completed": ExecutionObservation(status="SUCCEEDED", result_data={"passed": True, "results": []})}
            return ExecutionHandle(execution_id=execution_id)
        self._runs[execution_id] = {
            "commands": commands,
            "cwd": launch.worktree_path,
            "env": dict(launch.extra_env or {}),
            "environment_fingerprint": validation_environment_fingerprint(launch.extra_env),
            "index": 0,
            "results": [],
            "process": None,
            "started": None,
            "started_at": None,
        }
        self._start_next(execution_id)
        process = self._runs[execution_id].get("process")
        return ExecutionHandle(
            execution_id=execution_id,
            process_id=str(process.pid) if process is not None else None,
        )

    def poll(self, execution_id: str) -> ExecutionObservation:
        run = self._runs.get(execution_id)
        if run is None:
            return ExecutionObservation(
                status="LOST",
                result_data={
                    "reconciliation_state": "LOST",
                    "validation_lost": True,
                    "validation_terminal_type": "LOST",
                    "ended_at": _utc_now_iso(),
                },
            )
        completed = run.pop("completed", None)
        if completed is not None:
            self._runs.pop(execution_id, None)
            return completed
        process = run.get("process")
        if process is None:
            return ExecutionObservation(status="RUNNING")
        started = float(run.get("started") or time.monotonic())
        if time.monotonic() - started > self._timeout_seconds and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            stdout, stderr = process.communicate()
            return self._finish_command(
                execution_id,
                124,
                f"timed out after {self._timeout_seconds}s\n{stdout or ''}{stderr or ''}",
                started,
                failure_type="TIMEOUT",
            )
        exit_code = process.poll()
        if exit_code is None:
            return ExecutionObservation(status="RUNNING")
        stdout, stderr = process.communicate()
        return self._finish_command(execution_id, int(exit_code), (stdout or "") + (stderr or ""), started)

    def terminate(self, execution_id: str) -> ExecutionObservation:
        run = self._runs.pop(execution_id, None)
        process = run.get("process") if run else None
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return ExecutionObservation(
            status="TERMINATED",
            result_data={"validation_terminal_type": "CANCELLED", "ended_at": _utc_now_iso()},
        )

    def _start_next(self, execution_id: str) -> None:
        run = self._runs[execution_id]
        commands = run["commands"]
        index = int(run["index"])
        if index >= len(commands):
            run["completed"] = ExecutionObservation(
                status="SUCCEEDED",
                result_data={
                    "passed": True,
                    "results": run["results"],
                    "validation_terminal_type": "PASSED",
                    "ended_at": _utc_now_iso(),
                    "environment_fingerprint": run.get("environment_fingerprint"),
                },
            )
            return
        command = commands[index]
        started = time.monotonic()
        run["started_at"] = _utc_now_iso()
        try:
            argv = split_command(command)
            resolved = shutil.which(argv[0], path=(run["env"] or os.environ).get("PATH")) or argv[0]
            run["process"] = subprocess.Popen(
                [resolved, *argv[1:]],
                cwd=str(run["cwd"]) if run["cwd"] else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**os.environ, **run["env"]},
                stdin=subprocess.DEVNULL,
            )
            run["started"] = started
        except (OSError, ValueError) as exc:
            command = commands[index]
            run["results"].append(
                {
                    "command": command,
                    "exit_code": 127,
                    "failure_type": "COMMAND_UNAVAILABLE",
                    "started_at": run.get("started_at") or _utc_now_iso(),
                    "completed_at": _utc_now_iso(),
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "output_tail": f"could not run command: {exc}"[-OUTPUT_TAIL_CHARS:],
                }
            )
            run["completed"] = ExecutionObservation(
                status="FAILED",
                exit_code=127,
                result_data={
                    "passed": False,
                    "results": run["results"],
                    "validation_terminal_type": "COMMAND_UNAVAILABLE",
                    "ended_at": _utc_now_iso(),
                },
            )

    def _finish_command(
        self,
        execution_id: str,
        exit_code: int,
        output: str,
        started: float,
        *,
        failure_type: str | None = None,
    ) -> ExecutionObservation:
        run = self._runs[execution_id]
        command = run["commands"][int(run["index"])]
        typed_failure = failure_type or ("EXIT_CODE" if exit_code != 0 else None)
        run["results"].append(
            {
                "command": command,
                "exit_code": exit_code,
                "failure_type": typed_failure,
                "started_at": run.get("started_at") or _utc_now_iso(),
                "completed_at": _utc_now_iso(),
                "duration_seconds": round(time.monotonic() - started, 2),
                "output_tail": output[-OUTPUT_TAIL_CHARS:],
            }
        )
        run["process"] = None
        run["started"] = None
        run["started_at"] = None
        if exit_code != 0:
            self._runs.pop(execution_id, None)
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={
                    "passed": False,
                    "results": run["results"],
                    "validation_terminal_type": typed_failure,
                    "ended_at": _utc_now_iso(),
                },
            )
        run["index"] = int(run["index"]) + 1
        self._start_next(execution_id)
        completed = run.pop("completed", None)
        if completed is not None:
            self._runs.pop(execution_id, None)
            return completed
        return ExecutionObservation(status="RUNNING")
