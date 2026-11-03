"""Central preserved-worktree boundary for destructive Git cleanup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..persistence import Store
from ..workspaces import _task_key
from .decisions import Action, AutonomyDecision, record_decision
from .gitfacts import GitFacts
from .recovery_policy import GitOperation, GitOperationRequest, decide_git_operation


@dataclass(frozen=True)
class DestructiveCleanupResult:
    reset_executed: bool
    clean_executed: bool
    preserved_ref: str | None

    @property
    def completed(self) -> bool:
        return self.reset_executed and self.clean_executed


def execute_preserved_cleanup(
    store: Store,
    project: Path,
    task_id: str,
    worktree: Path,
    target: str,
    reason: str,
    *,
    snapshot: str | None = None,
    recorder: Callable[[AutonomyDecision], AutonomyDecision] | None = None,
) -> DestructiveCleanupResult:
    """Preserve exact state, apply policy, run reset/clean, and write per-command receipts."""
    work = GitFacts(worktree)
    project_facts = GitFacts(project)
    before = work.resolve("HEAD")
    resolved_target = work.resolve(target) or target
    snapshot = snapshot or work.snapshot_worktree(
        work.path,
        f"StageMesh preservation before destructive cleanup of {task_id}",
    )
    has_unpreserved = snapshot is not None and snapshot != resolved_target
    ref = f"refs/stagemesh/worktrees/{_task_key(task_id)}"
    requests = (
        GitOperationRequest(
            GitOperation.RESET_HARD,
            ref,
            reason,
            snapshot or before,
            has_unpreserved_work=has_unpreserved,
            published=False,
        ),
        GitOperationRequest(
            GitOperation.CLEAN,
            ref,
            reason,
            snapshot or before,
            has_unpreserved_work=has_unpreserved,
            published=False,
        ),
    )
    decisions = [decide_git_operation(request, task_id=task_id) for request in requests]
    write = recorder or (lambda decision: _record(store, decision))
    allowed = {Action.PROCEED, Action.PRESERVE_THEN_PROCEED}
    if any(decision.action not in allowed for decision in decisions):
        for decision in decisions:
            write(decision)
        return DestructiveCleanupResult(False, False, None)
    preserved_ref: str | None = None
    if has_unpreserved and snapshot is not None:
        preserved_ref = f"refs/stagemesh/preserved/{_task_key(task_id)}/{snapshot[:12]}"
        project_facts.ensure_ref(preserved_ref, snapshot)
    reset = work.git.run(
        "reset", "--hard", target, check=False, destructive_reason=reason
    )
    clean = work.git.run("clean", "-fdq", check=False, destructive_reason=reason)
    reset_executed = reset.returncode == 0 and work.resolve("HEAD") == resolved_target
    clean_executed = clean.returncode == 0
    for decision in decisions:
        operation = decision.observed["operation"]
        write(
            replace(
                decision,
                detail={
                    **decision.detail,
                    "destructive_operation_executed": (
                        reset_executed if operation == GitOperation.RESET_HARD.value else clean_executed
                    ),
                    "preserved_ref": preserved_ref,
                    "reset_target": resolved_target,
                },
            )
        )
    return DestructiveCleanupResult(reset_executed, clean_executed, preserved_ref)


def _record(store: Store, decision: AutonomyDecision) -> AutonomyDecision:
    record_decision(store, decision)
    return decision
