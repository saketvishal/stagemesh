from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .domain import ExecutionKind, ExecutionStatus
from .git import GitWorkspace
from .persistence import Store
from .process_identity import popen_identity
from .attribution import attribution_for_worker


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    candidate_sha: str | None = None
    durable_handoff: bool = False
    capacity_failure: bool = False


class Executor:
    name = "executor"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        raise NotImplementedError


class FakeExecutor(Executor):
    name = "fake"

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        execution_id = store.start_execution(task_id=task_id, claim_id=claim_id, kind=ExecutionKind.IMPLEMENTATION)
        workspace = GitWorkspace(project)
        workspace.init_if_needed()
        task_file = project / f"stagemesh-task-{task_id}.txt"
        task_file.write_text(f"implemented {task_id}\n", encoding="utf-8")
        sha = workspace.commit_all(
            f"StageMesh implementation for {task_id}",
            attribution=attribution_for_worker("local-worker", self.name),
        )
        store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, ExecutionStatus.SUCCEEDED, sha)
        return ExecutionResult(ExecutionStatus.SUCCEEDED, sha, durable_handoff=True)


class SubprocessExecutor(Executor):
    name = "subprocess"

    def __init__(self, command: list[str]):
        self.command = command

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        proc = subprocess.Popen(self.command, cwd=project, text=True)
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
        code = proc.wait()
        status = ExecutionStatus.SUCCEEDED if code == 0 else ExecutionStatus.FAILED
        sha = GitWorkspace(project).head_or_synthetic() if code == 0 else None
        if sha:
            store.add_candidate(task_id, sha, self.name, durable_handoff=True)
        store.finish_execution(execution_id, status, sha)
        return ExecutionResult(status, sha, durable_handoff=bool(sha))
