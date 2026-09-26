"""Subprocess-backed provider adapter.

The command is trusted operator configuration. Task text is passed on stdin
instead of interpolated into a shell command. Structured results are read
from a runner-generated result file. In-memory Popen handles are not
required after restart; missing handles never pretend the process is alive.
"""

from __future__ import annotations

import os
import re
import shutil
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
    RESULT_SCHEMA_VERSION,
    load_result_file,
    now_iso,
    result_env,
    sanitize_result_mapping,
    validated_result_status,
)


_MAX_LOG_BYTES = 256_000
_MAX_EVIDENCE_CHARS = 12_000
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|cookie)\b\s*[:=]\s*\S+"
)


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
        self._pending_observations: dict[str, ExecutionObservation] = {}
        self._log_dir = Path(log_dir) if log_dir else None
        if temp_dir is not None:
            self._temp_dir = Path(temp_dir)
        elif self._log_dir is not None:
            self._temp_dir = self._log_dir.parent / "tmp"
        elif "BUILD_COORDINATOR_DATA_DIR" in os.environ:
            self._temp_dir = Path(os.environ["BUILD_COORDINATOR_DATA_DIR"]) / "tmp"
        else:
            self._temp_dir = Path(".build-coordinator") / "tmp"

    def _prepare_temp_dir(self, worker_id: str, execution_id: str) -> Path:
        target = self._temp_dir / worker_id / execution_id
        probe = target / f".probe_{execution_id}.tmp"
        try:
            target.mkdir(parents=True, exist_ok=True)
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
        preflight = self._preflight(launch, execution_id)
        if preflight is not None:
            self._pending_observations[execution_id] = preflight
            return ExecutionHandle(execution_id=execution_id, result_path=result_path)
        env = os.environ.copy()
        try:
            managed_temp = self._prepare_temp_dir(launch.worker_id, execution_id)
        except RuntimeError as exc:
            self._pending_observations[execution_id] = self._preflight_failure(
                launch,
                execution_id,
                failure_kind="SANDBOX_TEMP_NOT_WRITABLE",
                detail=str(exc),
            )
            return ExecutionHandle(execution_id=execution_id, result_path=result_path)
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
        try:
            stdout, stderr = self._open_logs(execution_id)
        except OSError as exc:
            self._pending_observations[execution_id] = self._preflight_failure(
                launch,
                execution_id,
                failure_kind="LOG_PATH_NOT_WRITABLE",
                detail=f"log files cannot be opened: {self._log_dir}: {exc}",
            )
            return ExecutionHandle(execution_id=execution_id, result_path=result_path)
        try:
            process = subprocess.Popen(
                self._command,
                cwd=launch.worktree_path or None,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=env,
                **popen_kwargs(),
            )
        except OSError as exc:
            self._close_logs(execution_id)
            self._pending_observations[execution_id] = self._preflight_failure(
                launch,
                execution_id,
                failure_kind="COMMAND_LAUNCH_FAILED",
                detail=str(exc),
            )
            return ExecutionHandle(execution_id=execution_id, result_path=result_path)
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
        pending = self._pending_observations.pop(execution_id, None)
        if pending is not None:
            return pending
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
        pending = self._pending_observations.pop(execution_id, None)
        if pending is not None:
            return ExecutionObservation(status="TERMINATED", result_path=pending.result_path)
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
        return self._missing_result_observation(execution_id, exit_code)

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
            return self._invalid_result_observation(execution_id, exit_code, str(exc))
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

    def _preflight(self, launch: ExecutionLaunch, execution_id: str) -> ExecutionObservation | None:
        if launch.worktree_path:
            worktree = Path(launch.worktree_path)
            if not worktree.is_dir():
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="WORKDIR_MISSING",
                    detail=f"execution worktree does not exist or is not a directory: {worktree}",
                )
            error = self._probe_writable(worktree, execution_id)
            if error:
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="WORKDIR_NOT_WRITABLE",
                    detail=f"execution worktree is not writable: {worktree}: {error}",
                )
        resolved = self._resolve_command()
        if resolved is None:
            return self._preflight_failure(
                launch,
                execution_id,
                failure_kind="COMMAND_NOT_FOUND",
                detail=f"executor command is not available: {self._command[0]}",
            )
        if launch.result_path:
            result_parent = Path(launch.result_path).parent
            try:
                result_parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="RESULT_PATH_NOT_CREATABLE",
                    detail=f"result directory cannot be created: {result_parent}: {exc}",
                )
            error = self._probe_writable(result_parent, execution_id)
            if error:
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="RESULT_PATH_NOT_WRITABLE",
                    detail=f"result directory is not writable: {result_parent}: {error}",
                )
        if self._log_dir is not None:
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="LOG_PATH_NOT_CREATABLE",
                    detail=f"log directory cannot be created: {self._log_dir}: {exc}",
                )
            error = self._probe_writable(self._log_dir, execution_id)
            if error:
                return self._preflight_failure(
                    launch,
                    execution_id,
                    failure_kind="LOG_PATH_NOT_WRITABLE",
                    detail=f"log directory is not writable: {self._log_dir}: {error}",
                )
        try:
            self._prepare_temp_dir(launch.worker_id, execution_id)
        except RuntimeError as exc:
            return self._preflight_failure(
                launch,
                execution_id,
                failure_kind="SANDBOX_TEMP_NOT_WRITABLE",
                detail=str(exc),
            )
        return None

    def _resolve_command(self) -> str | None:
        executable = self._command[0]
        path = Path(executable)
        if path.parent != Path(".") or path.is_absolute():
            return str(path) if path.is_file() else None
        return shutil.which(executable)

    def _probe_writable(self, directory: Path, execution_id: str) -> str | None:
        probe = directory / f".build_coordinator_probe_{execution_id}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            return str(exc)
        return None

    def _preflight_failure(
        self,
        launch: ExecutionLaunch,
        execution_id: str,
        *,
        failure_kind: str,
        detail: str,
    ) -> ExecutionObservation:
        payload = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "execution_id": execution_id,
            "task_id": launch.task_id,
            "role": launch.role,
            "status": "HUMAN_ACTION_REQUIRED",
            "completed_at": now_iso(),
            "human_escalation_type": "COORDINATOR_INVARIANT_FAILURE",
            "failure_kind": failure_kind,
            "detail": detail,
            "diagnostics": {
                "adapter": self.adapter_name,
                "command": self._redacted_command(),
                "worktree_path": launch.worktree_path,
                "result_path": launch.result_path,
                "temp_dir": str(self._temp_dir),
            },
        }
        return ExecutionObservation(
            status="HUMAN_ACTION_REQUIRED",
            result_data=sanitize_result_mapping(payload),
            human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
            result_path=launch.result_path,
        )

    def _missing_result_observation(self, execution_id: str, exit_code: int) -> ExecutionObservation:
        evidence = self._failure_evidence(execution_id, exit_code)
        detail = "subprocess exited without a structured result file"
        if exit_code == 0:
            evidence["human_escalation_type"] = "COORDINATOR_INVARIANT_FAILURE"
            evidence["error"] = detail
            return ExecutionObservation(
                status="FAILED",
                exit_code=exit_code,
                result_data=sanitize_result_mapping(evidence),
                human_escalation_type="COORDINATOR_INVARIANT_FAILURE",
                result_path=self._result_paths.get(execution_id),
            )
        evidence["provider_failure"] = self._classify_failure(evidence)
        evidence["detail"] = detail
        return ExecutionObservation(
            status="FAILED",
            exit_code=exit_code,
            result_data=sanitize_result_mapping(evidence),
            result_path=self._result_paths.get(execution_id),
        )

    def _invalid_result_observation(
        self,
        execution_id: str,
        exit_code: int | None,
        error: str,
    ) -> ExecutionObservation:
        evidence = self._failure_evidence(execution_id, exit_code)
        evidence.update(
            {
                "error": error,
                "provider_failure": self._classify_failure(evidence),
                "detail": "subprocess wrote an invalid structured result file",
            }
        )
        return ExecutionObservation(
            status="FAILED",
            exit_code=exit_code,
            result_data=sanitize_result_mapping(evidence),
            result_path=self._result_paths.get(execution_id),
        )

    def _failure_evidence(self, execution_id: str, exit_code: int | None) -> dict:
        stdout_tail, stderr_tail = self._read_log_tails(execution_id)
        return {
            "failure_kind": "EXECUTOR_RESULT_INVALID_OR_MISSING",
            "return_code": exit_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }

    def _read_log_tails(self, execution_id: str) -> tuple[str, str]:
        if self._log_dir is None:
            return "", ""
        return (
            self._read_text_tail(self._log_dir / f"{execution_id}.stdout.log"),
            self._read_text_tail(self._log_dir / f"{execution_id}.stderr.log"),
        )

    def _read_text_tail(self, path: Path) -> str:
        try:
            if not path.is_file():
                return ""
            tail = path.read_text(encoding="utf-8", errors="replace")[-_MAX_EVIDENCE_CHARS:]
            return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", tail)
        except OSError:
            return ""

    def _classify_failure(self, evidence: dict) -> str:
        output = "\n".join(
            str(evidence.get(key) or "")
            for key in ("stderr_tail", "stdout_tail", "error", "detail")
        )
        try:
            from build_coordinator.agents.wrapper import classify_failure

            return classify_failure(output)
        except Exception:
            return "EXECUTION_FAILURE"

    def _redacted_command(self) -> list[str]:
        redacted: list[str] = []
        redact_next = False
        for part in self._command:
            lowered = part.lower()
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            if any(marker in lowered for marker in ("token", "secret", "password", "api_key", "apikey")):
                redacted.append("<redacted>")
                if lowered.startswith("--") and "=" not in lowered:
                    redact_next = True
                continue
            redacted.append(part)
        return redacted

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
