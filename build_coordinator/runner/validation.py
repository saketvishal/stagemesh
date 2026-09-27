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
import sys
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
from build_coordinator.execution.process_tree import (
    capture_process_identity,
    process_identity_status,
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
        self._result_paths: dict[str, str] = {}
        self._process_identities: dict[str, tuple[str, str | None]] = {}

    def remember_result_path(self, execution_id: str, result_path: str | None) -> None:
        if result_path:
            self._result_paths[execution_id] = result_path

    def remember_process_identity(
        self, execution_id: str, process_id: str | None, process_start_key: str | None
    ) -> None:
        """Seed durable process identity for an execution this instance did not launch.

        Mirrors SubprocessExecutor.remember_process_identity. Without this,
        a coordinator/executor restart wipes `self._runs` (process-local
        memory) and poll() would fall through to `_lost_observation()` for
        a validation subprocess that is still genuinely running -- exactly
        the class of bug durable process identity fixed for BUILDER/
        REVIEWER/INTEGRATION executions, but which validation's own
        executor never previously participated in.
        """
        if process_id:
            self._process_identities[execution_id] = (str(process_id), process_start_key)

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        execution_id = launch.execution_id or str(uuid4())
        if launch.result_path:
            self.remember_result_path(execution_id, launch.result_path)
        commands = list(launch.metadata.get("commands") or [])
        if not commands:
            result_data = {"passed": True, "results": []}
            self._write_result_file(execution_id, "SUCCEEDED", result_data)
            self._runs[execution_id] = {"completed": ExecutionObservation(status="SUCCEEDED", result_data=result_data)}
            return ExecutionHandle(execution_id=execution_id)
        result_path = self._result_paths.get(execution_id)
        if result_path:
            spec_path = Path(result_path).with_suffix(Path(result_path).suffix + ".validation-spec.json")
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                json.dumps(
                    {
                        "execution_id": execution_id,
                        "task_id": launch.task_id,
                        "role": launch.role,
                        "commands": commands,
                        "cwd": launch.worktree_path,
                        "timeout_seconds": self._timeout_seconds,
                        "env": dict(launch.extra_env or {}),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            env = {**os.environ, **dict(launch.extra_env or {})}
            env["STAGEMESH_VALIDATION_SPEC_PATH"] = str(spec_path)
            env["STAGEMESH_VALIDATION_RESULT_PATH"] = result_path
            controller_root = str(Path(__file__).resolve().parents[2])
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                controller_root
                if not existing_pythonpath
                else os.pathsep.join([controller_root, existing_pythonpath])
            )
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=launch.worktree_path or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=env,
                **_popen_kwargs(),
            )
            self._runs[execution_id] = {"process": process}
            self._process_identities[execution_id] = (
                str(process.pid),
                capture_process_identity(process.pid),
            )
        else:
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
        identity = self._process_identities.get(execution_id)
        return ExecutionHandle(
            execution_id=execution_id,
            process_id=str(process.pid) if process is not None else None,
            result_path=result_path,
            process_start_key=identity[1] if identity is not None else None,
        )

    def poll(self, execution_id: str) -> ExecutionObservation:
        run = self._runs.get(execution_id)
        if run is None:
            reconciled = self._observation_from_result_file(execution_id)
            if reconciled is not None:
                return reconciled
            # No in-memory run state (e.g. after a coordinator/executor
            # restart -- `self._runs` is process-local) and no result file
            # yet. Before declaring the execution LOST, check whether the
            # original OS process durable identity says it is still
            # genuinely alive: a verified-live validator must not be
            # reported lost -- and consequently terminated/relaunched by
            # the caller -- solely because this executor instance's
            # in-memory bookkeeping did not survive a restart.
            identity = self._process_identities.get(execution_id)
            if identity is not None:
                pid_str, start_key = identity
                try:
                    pid = int(pid_str)
                except (TypeError, ValueError):
                    pid = None
                if pid is not None and pid > 0:
                    status = process_identity_status(pid, start_key)
                    if status in ("MATCH", "ALIVE_UNVERIFIED"):
                        return ExecutionObservation(
                            status="RUNNING",
                            result_data={
                                "reconciliation_state": status,
                                "durable_identity_pid": pid,
                            },
                        )
                    # MISMATCH: pid gone or reused by an unrelated process.
                    # Fall through to the same terminal handling as "no
                    # identity evidence at all".
            return self._lost_observation(execution_id)
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
        if "commands" not in run:
            self._runs.pop(execution_id, None)
            reconciled = self._observation_from_result_file(execution_id, exit_code=int(exit_code))
            if reconciled is not None:
                return reconciled
            return self._lost_observation(execution_id, exit_code=int(exit_code))
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

    def _write_result_file(self, execution_id: str, status: str, result_data: dict[str, Any]) -> None:
        result_path = self._result_paths.get(execution_id)
        if not result_path:
            return
        payload = {"status": status, **result_data}
        path = Path(result_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _observation_from_result_file(
        self,
        execution_id: str,
        *,
        exit_code: int | None = None,
    ) -> ExecutionObservation | None:
        result_path = self._result_paths.get(execution_id)
        if not result_path:
            return None
        path = Path(result_path)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={
                    "passed": False,
                    "results": [],
                    "validation_terminal_type": "INVALID_RESULT",
                    "error": str(exc),
                    "ended_at": _utc_now_iso(),
                },
                result_path=result_path,
            )
        status = str(payload.pop("status", "SUCCEEDED" if exit_code in (None, 0) else "FAILED")).upper()
        if status not in {"SUCCEEDED", "FAILED", "TERMINATED", "LOST"}:
            status = "FAILED"
            payload = {
                **payload,
                "passed": False,
                "validation_terminal_type": "INVALID_RESULT_STATUS",
                "error": "invalid validation result status",
            }
        return ExecutionObservation(status=status, exit_code=exit_code, result_data=payload, result_path=result_path)

    def _lost_observation(self, execution_id: str, *, exit_code: int | None = None) -> ExecutionObservation:
        return ExecutionObservation(
            status="LOST",
            exit_code=exit_code,
            result_data={
                "reconciliation_state": "LOST",
                "validation_lost": True,
                "validation_terminal_type": "LOST",
                "ended_at": _utc_now_iso(),
            },
            result_path=self._result_paths.get(execution_id),
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
            self._write_result_file(execution_id, "SUCCEEDED", run["completed"].result_data)
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
            self._write_result_file(execution_id, "FAILED", run["completed"].result_data)

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
            observation = ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={
                    "passed": False,
                    "results": run["results"],
                    "validation_terminal_type": typed_failure,
                    "ended_at": _utc_now_iso(),
                },
            )
            self._write_result_file(execution_id, "FAILED", observation.result_data)
            return observation
        run["index"] = int(run["index"]) + 1
        self._start_next(execution_id)
        completed = run.pop("completed", None)
        if completed is not None:
            self._runs.pop(execution_id, None)
            return completed
        return ExecutionObservation(status="RUNNING")


def _popen_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": startupinfo}


def _run_worker_from_env() -> int:
    spec_path = os.environ.get("STAGEMESH_VALIDATION_SPEC_PATH")
    result_path = os.environ.get("STAGEMESH_VALIDATION_RESULT_PATH")
    if not spec_path or not result_path:
        return 2
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        commands = [str(command) for command in spec.get("commands") or []]
        cwd = str(spec.get("cwd") or os.getcwd())
        timeout_seconds = float(spec.get("timeout_seconds") or 900)
        env = {str(key): str(value) for key, value in dict(spec.get("env") or {}).items()}
        outcome = run_validation(commands, cwd, timeout_seconds=timeout_seconds, env=env)
        terminal = "PASSED" if outcome.passed else next(
            (
                str(item.get("failure_type") or "EXIT_CODE")
                for item in outcome.results
                if int(item.get("exit_code") or 0) != 0
            ),
            "EXIT_CODE",
        )
        payload = {
            "status": "SUCCEEDED" if outcome.passed else "FAILED",
            "passed": outcome.passed,
            "results": outcome.results,
            "validation_terminal_type": terminal,
            "ended_at": _utc_now_iso(),
            "environment_fingerprint": validation_environment_fingerprint(env),
        }
    except Exception as exc:  # pragma: no cover - last-ditch durable diagnostics
        payload = {
            "status": "FAILED",
            "passed": False,
            "results": [],
            "validation_terminal_type": "VALIDATION_WORKER_ERROR",
            "error": str(exc),
            "ended_at": _utc_now_iso(),
        }
    Path(result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(result_path).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return 0 if payload.get("status") == "SUCCEEDED" else 1


if __name__ == "__main__":
    raise SystemExit(_run_worker_from_env())
