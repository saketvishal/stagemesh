"""Subprocess-backed provider adapter.

The command is trusted operator configuration. Task text is passed on stdin
instead of interpolated into a shell command. Structured results are read
from a runner-generated result file. In-memory Popen handles are not
required after restart; missing handles never pretend the process is alive.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

from build_coordinator.execution.base import (
    ExecutionHandle,
    ExecutionLaunch,
    ExecutionObservation,
)
from build_coordinator.execution.process_tree import (
    ProcessTree,
    attach_started_process,
    kill_process_tree,
    popen_kwargs,
)
from build_coordinator.execution.results import (
    ExecutorResultError,
    load_result_file,
    result_env,
    validated_result_status,
)


_MAX_LOG_BYTES = 256_000


class SubprocessExecutor:
    adapter_name = "subprocess"

    def __init__(
        self,
        command: list[str],
        *,
        log_dir: str | Path | None = None,
        result_paths: dict[str, str] | None = None,
        temp_dir: str | Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("subprocess executor command must be non-empty")
        self._command = list(command)
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._trees: dict[str, ProcessTree] = {}
        self._result_paths: dict[str, str] = dict(result_paths or {})
        self._log_handles: dict[str, tuple] = {}
        self._log_dir = Path(log_dir) if log_dir else None
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir)
        elif self._log_dir is not None:
            self._temp_dir = self._log_dir.parent / "tmp"
        else:
            self._temp_dir = Path(".stagemesh") / "tmp"

    def _prepare_temp_dir(self, worker_id: str, execution_id: str) -> Path:
        target = self._temp_dir / worker_id / execution_id
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".probe_{execution_id}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Managed execution temp directory is not writable: {target}"
            ) from exc
        return target

    def remember_result_path(self, execution_id: str, result_path: str | None) -> None:
        if result_path:
            self._result_paths[execution_id] = result_path

    def launch(self, launch: ExecutionLaunch) -> ExecutionHandle:
        execution_id = launch.execution_id or str(uuid4())
        result_path = launch.result_path
        if result_path:
            self._result_paths[execution_id] = result_path
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        managed_temp = self._prepare_temp_dir(launch.worker_id, execution_id)
        temp_str = str(managed_temp.resolve())
        env["TEMP"] = temp_str
        env["TMP"] = temp_str
        env["TMPDIR"] = temp_str
        env.update(launch.extra_env)
        if result_path:
            env.update(
                result_env(
                    result_path=result_path,
                    execution_id=execution_id,
                    task_id=launch.task_id,
                    role=launch.role,
                    reviewed_feature_sha=launch.reviewed_feature_sha,
                )
            )
        stdout, stderr = self._open_logs(execution_id)
        process = subprocess.Popen(
            self._command,
            cwd=launch.worktree_path or None,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            shell=False,
            env=env,
            **popen_kwargs(),
        )
        tree = attach_started_process(process.pid)
        self._trees[execution_id] = tree
        if process.stdin is not None:
            process.stdin.write(launch.prompt)
            process.stdin.close()
        self._processes[execution_id] = process
        return ExecutionHandle(
            execution_id=execution_id,
            process_id=str(process.pid),
            result_path=result_path,
        )

    def poll(self, execution_id: str) -> ExecutionObservation:
        process = self._processes.get(execution_id)
        if process is None:
            return self._reconcile_without_handle(execution_id)
        exit_code = process.poll()
        if exit_code is None:
            return ExecutionObservation(status="RUNNING")
        tree = self._trees.pop(execution_id, None)
        if tree is not None:
            tree.close()
        self._close_logs(execution_id)
        return self._observation_after_exit(execution_id, exit_code)

    def terminate(self, execution_id: str) -> ExecutionObservation:
        tree = self._trees.pop(execution_id, None)
        if tree is not None:
            tree.terminate()
            tree.close()
        process = self._processes.pop(execution_id, None)
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        elif tree is None and process is None:
            remembered = self._result_paths.get(execution_id)
            # Restart path: caller uses terminate_pid.
            _ = remembered
        self._close_logs(execution_id)
        return ExecutionObservation(status="TERMINATED", result_path=self._result_paths.get(execution_id))

    def terminate_pid(self, pid: int | str | None) -> None:
        if not pid:
            return
        kill_process_tree(int(pid))

    def terminate_all(self) -> None:
        for execution_id in list(self._trees) + list(self._processes):
            self.terminate(execution_id)

    def _reconcile_without_handle(self, execution_id: str) -> ExecutionObservation:
        result_path = self._result_paths.get(execution_id)
        if result_path and Path(result_path).is_file():
            return self._observation_from_result_file(execution_id)
        return ExecutionObservation(
            status="LOST",
            result_data={"reconciliation_state": "LOST"},
            result_path=result_path,
        )

    def _observation_after_exit(self, execution_id: str, exit_code: int) -> ExecutionObservation:
        result_path = self._result_paths.get(execution_id)
        if result_path and Path(result_path).is_file():
            return self._observation_from_result_file(execution_id, exit_code=exit_code)
        if exit_code == 0:
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={"error": "subprocess exited 0 without a structured result file"},
                human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
                result_path=result_path,
            )
        return ExecutionObservation(status="FAILED", exit_code=exit_code, result_path=result_path)

    def _observation_from_result_file(
        self,
        execution_id: str,
        *,
        exit_code: int | None = None,
    ) -> ExecutionObservation:
        result_path = self._result_paths.get(execution_id)
        try:
            payload = load_result_file(result_path)
        except ExecutorResultError as exc:
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={"error": str(exc)},
                human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
                result_path=result_path,
            )
        try:
            default = "SUCCEEDED" if exit_code in (None, 0) else "FAILED"
            status = validated_result_status(payload.get("status"), default=default)
        except ExecutorResultError as exc:
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={"error": str(exc)},
                human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
                result_path=result_path,
            )
        if exit_code not in (None, 0) and status == "SUCCEEDED":
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data={"error": "process exit code contradicts SUCCEEDED result"},
                human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
                result_path=result_path,
            )
        return ExecutionObservation(
            status=status,
            exit_code=exit_code,
            result_data=payload,
            result_path=result_path,
        )

    def _open_logs(self, execution_id: str):
        if self._log_dir is None:
            return subprocess.DEVNULL, subprocess.DEVNULL
        self._log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self._log_dir / f"{execution_id}.stdout.log"
        stderr_path = self._log_dir / f"{execution_id}.stderr.log"
        stdout = stdout_path.open("w", encoding="utf-8")
        stderr = stderr_path.open("w", encoding="utf-8")
        self._log_handles[execution_id] = (stdout, stderr)
        return stdout, stderr

    def _close_logs(self, execution_id: str) -> None:
        handles = self._log_handles.pop(execution_id, None)
        if not handles:
            return
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        if self._log_dir is None:
            return
        for name in ("stdout", "stderr"):
            path = self._log_dir / f"{execution_id}.{name}.log"
            try:
                if path.is_file() and path.stat().st_size > _MAX_LOG_BYTES:
                    data = path.read_bytes()[-_MAX_LOG_BYTES:]
                    path.write_bytes(b"...truncated...\n" + data)
            except OSError:
                pass
