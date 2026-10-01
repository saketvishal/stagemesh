from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .attribution import attribution_for_worker
from .domain import ExecutionKind, ExecutionStatus
from .git import GitWorkspace
from .persistence import Store
from .process_identity import popen_identity
from .process_tree import kill_process_tree


class StructuredResultValidationError(ValueError):
    """Raised when a worker emits an invalid structured result payload."""


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    candidate_sha: str | None = None
    durable_handoff: bool = False
    capacity_failure: bool = False
    failure_reason: str | None = None
    metadata: dict[str, Any] | None = None


def parse_structured_result(
    payload: str | dict[str, Any],
    *,
    expected_task_id: str | None = None,
) -> ExecutionResult:
    """Parse and validate a structured worker result JSON payload.

    Fails closed (raises StructuredResultValidationError) if malformed or invalid.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception as exc:
            raise StructuredResultValidationError(f"invalid JSON payload: {exc}") from exc
    elif isinstance(payload, dict):
        data = payload
    else:
        raise StructuredResultValidationError(f"expected dict or JSON string, got {type(payload).__name__}")

    if not isinstance(data, dict):
        raise StructuredResultValidationError("structured result payload must be a JSON object")

    raw_status = data.get("status")
    if not raw_status or not isinstance(raw_status, str):
        raise StructuredResultValidationError("missing or invalid 'status' field in result payload")

    status_upper = raw_status.upper()
    try:
        status = ExecutionStatus[status_upper]
    except KeyError:
        raise StructuredResultValidationError(f"unknown status '{raw_status}' in result payload")

    sha = data.get("candidate_sha")
    if sha is not None:
        if not isinstance(sha, str) or not re.match(r"^[0-9a-fA-F]{40}$", sha):
            raise StructuredResultValidationError(f"invalid candidate_sha format: {sha}")

    durable_handoff = bool(data.get("durable_handoff", False))
    capacity_failure = bool(data.get("capacity_failure", False))
    failure_reason = data.get("failure_reason")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise StructuredResultValidationError("invalid failure_reason field")

    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise StructuredResultValidationError("metadata field must be a JSON object")

    return ExecutionResult(
        status=status,
        candidate_sha=sha,
        durable_handoff=durable_handoff,
        capacity_failure=capacity_failure,
        failure_reason=failure_reason,
        metadata=metadata,
    )


def classify_failure(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    exc: Exception | None = None,
) -> tuple[bool, str]:
    """Classify execution failure into capacity/provider failure vs code defect.

    Returns (is_capacity_failure: bool, reason: str).
    """
    if exc is not None and isinstance(exc, FileNotFoundError):
        return True, "provider_unavailable"

    combined = f"{stdout}\n{stderr}".lower()

    if any(m in combined for m in ["not found", "no such file or directory", "command not found", "cannot find"]):
        return True, "provider_unavailable"

    if any(m in combined for m in [
        "unauthorized", "authentication", "not logged in", "login required",
        "invalid api key", "auth failure", "missing credentials", "authenticate",
        "invalid_api_key", "authentication_error", "forbidden", "401", "403"
    ]):
        return True, "authentication_failure"

    if any(m in combined for m in [
        "rate limit", "rate_limit", "quota", "too many requests", "429",
        "exceeded your current quota", "capacity exhausted", "overloaded_error",
        "rate_limit_error", "insufficient_quota"
    ]):
        return True, "quota_rate_limit"

    if any(m in combined for m in [
        "503", "502", "service unavailable", "bad gateway", "connection refused",
        "connection reset", "overloaded", "server_error", "timed out", "timeout"
    ]):
        return True, "transient_provider_failure"

    return False, "implementation_failure"


class Executor:
    name = "executor"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        raise NotImplementedError


def _capture_baseline_sha(project: Path) -> str | None:
    try:
        ws = GitWorkspace(project)
        ws.init_if_needed()
        sha = ws.run("rev-parse", "--verify", "HEAD", check=False).stdout.strip()
        if len(sha) == 40 and all(c in "0123456789abcdefABCDEF" for c in sha):
            return sha
    except Exception:
        pass
    return None


class FakeExecutor(Executor):
    name = "fake"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        from .governance import (
            capture_agent_result_tree,
            canonicalize_and_record_candidate,
            prepare_task_worktree,
            resolve_or_capture_baseline,
        )
        baseline = resolve_or_capture_baseline(store, project, task_id)
        task_project = prepare_task_worktree(project, task_id, base_sha=baseline.commit_sha)
        workspace = GitWorkspace(task_project)
        workspace.init_if_needed()
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        store.record_baseline(
            task_id=task_id,
            execution_id=execution_id,
            commit_sha=baseline.commit_sha,
            tree_sha=baseline.tree_sha,
            branch=baseline.branch,
            repo_path=baseline.repo_path,
        )
        task_file = task_project / f"stagemesh-task-{task_id}.txt"
        task_file.write_text(f"implemented {task_id}\n", encoding="utf-8")
        agent_tree = capture_agent_result_tree(workspace)
        sha = canonicalize_and_record_candidate(
            store=store,
            workspace=workspace,
            task_id=task_id,
            execution_id=execution_id,
            claim_id=claim_id,
            provider=self.name,
            baseline=baseline,
            agent_result_tree=agent_tree,
            durable_handoff=True,
        )
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(self, command: list[str], name: str | None = None):
        self.command = command
        if name is not None:
            self.name = name

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        import shutil
        executable = self.command[0] if self.command else ""
        if not executable or not shutil.which(executable):
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=True,
                failure_reason="provider_unavailable",
            )

        from .governance import (
            capture_agent_result_tree,
            canonicalize_and_record_candidate,
            prepare_task_worktree,
            resolve_or_capture_baseline,
        )

        # Baseline is durably persisted BEFORE any provider process starts
        baseline = resolve_or_capture_baseline(store, project, task_id)
        task_project = prepare_task_worktree(project, task_id, base_sha=baseline.commit_sha)
        workspace = GitWorkspace(task_project)
        workspace.init_if_needed()

        task = store.get_task(task_id)
        from .providers import _build_task_prompt
        task_prompt = _build_task_prompt(task_id, task)

        result_file = task_project / f".stagemesh-result-{task_id}.json"
        extra_env = {"STAGEMESH_RESULT_PATH": str(result_file)}

        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
            executable=executable,
        )
        store.record_baseline(
            task_id=task_id,
            execution_id=execution_id,
            commit_sha=baseline.commit_sha,
            tree_sha=baseline.tree_sha,
            branch=baseline.branch,
            repo_path=baseline.repo_path,
        )

        try:
            env = dict(subprocess.os.environ)
            env.update(extra_env)
            proc = subprocess.Popen(
                self.command,
                cwd=task_project,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            is_cap, reason = classify_failure(1, exc=exc)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=is_cap,
                failure_reason=reason,
            )

        ident = popen_identity(proc)
        store.attach_execution_process(
            execution_id=execution_id,
            pid=ident.pid,
            process_create_time=ident.create_time,
            boot_id=ident.boot_id,
            executable=ident.executable,
        )

        try:
            stdout, stderr = proc.communicate(input=task_prompt)
            code = proc.returncode
        except Exception:
            kill_process_tree(ident.pid)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            raise

        # 1. Ingest structured result if present
        if result_file.exists():
            try:
                res_content = result_file.read_text(encoding="utf-8")
                # Explicitly remove control artifact BEFORE tree capture to prevent contamination
                result_file.unlink(missing_ok=True)
                workspace.run("rm", "--cached", "--ignore-unmatch", str(result_file.name), check=False)
                parsed = parse_structured_result(res_content, expected_task_id=task_id)
                canonical_sha = None
                if parsed.status is ExecutionStatus.SUCCEEDED:
                    agent_tree = capture_agent_result_tree(workspace, agent_candidate_sha=parsed.candidate_sha)
                    canonical_sha = canonicalize_and_record_candidate(
                        store=store,
                        workspace=workspace,
                        task_id=task_id,
                        execution_id=execution_id,
                        claim_id=claim_id,
                        provider=self.name,
                        baseline=baseline,
                        agent_result_tree=agent_tree,
                        durable_handoff=parsed.durable_handoff,
                        agent_candidate_sha=parsed.candidate_sha,
                    )
                store.finish_execution(execution_id, parsed.status, canonical_sha)
                return ExecutionResult(parsed.status, canonical_sha, durable_handoff=parsed.durable_handoff)
            except StructuredResultValidationError as exc:
                store.finish_execution(execution_id, ExecutionStatus.FAILED)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=False,
                    failure_reason=f"structured_result_invalid: {exc}",
                )
            except Exception as exc:
                store.finish_execution(execution_id, ExecutionStatus.FAILED)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=False,
                    failure_reason=f"governance_canonicalization_failed: {exc}",
                )
            finally:
                result_file.unlink(missing_ok=True)

        if code != 0:
            is_cap, reason = classify_failure(code, stdout, stderr)
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=is_cap,
                failure_reason=reason,
            )

        try:
            agent_tree = capture_agent_result_tree(workspace)
            sha = canonicalize_and_record_candidate(
                store=store,
                workspace=workspace,
                task_id=task_id,
                execution_id=execution_id,
                claim_id=claim_id,
                provider=self.name,
                baseline=baseline,
                agent_result_tree=agent_tree,
                durable_handoff=True,
            )
            store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
            return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=bool(sha))
        except Exception as exc:
            store.finish_execution(execution_id, ExecutionStatus.FAILED)
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=False,
                failure_reason=f"governance_canonicalization_failed: {exc}",
            )
