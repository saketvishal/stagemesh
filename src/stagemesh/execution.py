from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .attribution import attribution_for_worker
from .domain import ExecutionKind, ExecutionStatus
from .git import GitError, GitWorkspace
from .persistence import Store
from .process_identity import popen_identity


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    candidate_sha: str | None = None
    durable_handoff: bool = False
    capacity_failure: bool = False
    failure_reason: str | None = None


def classify_failure(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    exc: Exception | None = None,
) -> tuple[bool, str]:
    """Classify execution failure into provider/capacity failure vs code defect."""
    if exc is not None and isinstance(exc, FileNotFoundError):
        return True, "provider_unavailable"

    combined = f"{stdout}\n{stderr}".lower()

    if any(
        marker in combined
        for marker in [
            "not found",
            "no such file or directory",
            "command not found",
            "cannot find",
        ]
    ):
        return True, "provider_unavailable"

    if any(
        marker in combined
        for marker in [
            "unauthorized",
            "authentication",
            "not logged in",
            "login required",
            "invalid api key",
            "auth failure",
            "missing credentials",
            "authenticate",
            "invalid_api_key",
            "authentication_error",
            "forbidden",
            "401",
            "403",
        ]
    ):
        return True, "authentication_failure"

    if any(
        marker in combined
        for marker in [
            "rate limit",
            "rate_limit",
            "quota",
            "too many requests",
            "429",
            "exceeded your current quota",
            "capacity exhausted",
            "overloaded_error",
            "rate_limit_error",
            "insufficient_quota",
        ]
    ):
        return True, "quota_rate_limit"

    if any(
        marker in combined
        for marker in [
            "503",
            "502",
            "service unavailable",
            "bad gateway",
            "connection refused",
            "connection reset",
            "overloaded",
            "server_error",
            "timed out",
            "timeout",
        ]
    ):
        return True, "transient_provider_failure"

    return False, "implementation_failure"


class Executor:
    name = "executor"

    def run(
        self,
        store: Store,
        task_id: str,
        claim_id: str | None,
        project: Path,
    ) -> ExecutionResult:
        raise NotImplementedError


class FakeExecutor(Executor):
    name = "fake"

    def run(
        self,
        store: Store,
        task_id: str,
        claim_id: str | None,
        project: Path,
    ) -> ExecutionResult:
        execution_id = store.start_execution(
            task_id=task_id,
            claim_id=claim_id,
            kind=ExecutionKind.IMPLEMENTATION,
        )
        workspace = GitWorkspace(project)
        workspace.init_if_needed()
        task_file = project / f"stagemesh-task-{task_id}.txt"
        task_file.write_text(f"implemented {task_id}\n", encoding="utf-8")
        sha = workspace.commit_all(
            f"StageMesh implementation for {task_id}",
            attribution=attribution_for_worker("local-worker", self.name),
        )
        store.add_candidate(
            task_id,
            sha,
            self.name,
            durable_handoff=True,
        )
        store.finish_execution(
            execution_id,
            ExecutionStatus.SUCCEEDED,
            sha,
        )
        return ExecutionResult(
            ExecutionStatus.SUCCEEDED,
            sha,
            durable_handoff=True,
        )


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(
        self,
        command: list[str],
        name: str | None = None,
        *,
        isolate: bool = False,
    ):
        self.command = command
        self.isolate = isolate
        if name is not None:
            self.name = name

    def run(
        self,
        store: Store,
        task_id: str,
        claim_id: str | None,
        project: Path,
    ) -> ExecutionResult:
        executable = self.command[0] if self.command else ""
        if not executable or not shutil.which(executable):
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=True,
                failure_reason="provider_unavailable",
            )

        project = Path(project).resolve()
        root_workspace = GitWorkspace(project)
        run_project = project
        isolated_worktree: Path | None = None
        baseline_sha: str | None = None

        if self.isolate:
            root_workspace.init_if_needed()
            try:
                baseline_sha = root_workspace.head()
            except GitError:
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    failure_reason="isolation_requires_existing_commit",
                )
            isolated_worktree = _create_isolated_worktree(
                root_workspace,
                project,
                task_id,
                baseline_sha,
            )
            run_project = isolated_worktree

        task = store.get_task(task_id)
        from .providers import _build_task_prompt

        task_prompt = _build_task_prompt(
            task_id,
            task,
            store=store,
            project=project,
        )
        execution_id: str | None = None
        try:
            try:
                proc = subprocess.Popen(
                    self.command,
                    cwd=run_project,
                    text=True,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                is_cap, reason = classify_failure(1, exc=exc)
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=is_cap,
                    failure_reason=reason,
                )

            ident = popen_identity(proc)
            execution_id = store.start_execution(
                task_id=task_id,
                claim_id=claim_id,
                kind=ExecutionKind.IMPLEMENTATION,
                pid=ident.pid,
                process_create_time=ident.create_time,
                boot_id=ident.boot_id,
                executable=ident.executable,
            )
            stdout, stderr = proc.communicate(input=task_prompt)
            code = proc.returncode

            if code != 0:
                is_cap, reason = classify_failure(
                    code,
                    stdout,
                    stderr,
                )
                store.finish_execution(
                    execution_id,
                    ExecutionStatus.FAILED,
                )
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    capacity_failure=is_cap,
                    failure_reason=reason,
                )

            workspace = GitWorkspace(run_project)
            workspace.init_if_needed()
            sha = workspace.commit_all(
                f"StageMesh implementation for {task_id}",
                attribution=attribution_for_worker(
                    "local-worker",
                    self.name,
                ),
            )
            if self.isolate and baseline_sha == sha:
                store.finish_execution(
                    execution_id,
                    ExecutionStatus.FAILED,
                )
                return ExecutionResult(
                    ExecutionStatus.FAILED,
                    failure_reason="no_candidate_changes",
                )

            store.add_candidate(
                task_id,
                sha,
                self.name,
                durable_handoff=True,
            )
            store.finish_execution(
                execution_id,
                ExecutionStatus.SUCCEEDED,
                sha,
            )
            return ExecutionResult(
                ExecutionStatus.SUCCEEDED,
                sha,
                durable_handoff=True,
            )
        finally:
            if isolated_worktree is not None:
                root_workspace.run(
                    "worktree",
                    "remove",
                    "--force",
                    str(isolated_worktree),
                    check=False,
                )
                root_workspace.run(
                    "worktree",
                    "prune",
                    check=False,
                )
                if isolated_worktree.exists():
                    shutil.rmtree(
                        isolated_worktree,
                        ignore_errors=True,
                    )


def _create_isolated_worktree(
    workspace: GitWorkspace,
    project: Path,
    task_id: str,
    baseline_sha: str,
) -> Path:
    safe = "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in task_id
    )[:80]
    created = Path(
        tempfile.mkdtemp(
            prefix=f".stagemesh-{safe}-",
            dir=project.parent,
        )
    )
    created.rmdir()
    result = workspace.run(
        "worktree",
        "add",
        "--detach",
        str(created),
        baseline_sha,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(
            result.stderr.strip()
            or result.stdout.strip()
            or "unable to create isolated worktree"
        )
    return created
