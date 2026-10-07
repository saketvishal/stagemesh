"""Lifecycle hooks called from `workspaces` when the project has opted in to the supervisor. No-ops otherwise."""

from __future__ import annotations

from pathlib import Path

from ..persistence import Store
from .wiring import project_of, settings_for_store


def workspace_handed_to_execution(store: Store, task_id: str, run_path: Path) -> None:
    """Called when an execution takes over a task worktree: refuse if it was mutated since last handed over, then record it as owned."""
    settings = settings_for_store(store)
    if not settings.enabled:
        return
    from .supervisor import (  # lazy: supervisor imports workspaces
        ExternalWorkspaceMutation,
        Supervisor,
    )

    supervisor = Supervisor(store, project_of(store), max_reconstructs=settings.max_reconstructs)
    decision = supervisor.check_workspace(task_id, execution_running=False, restore=True)
    if decision is not None:
        raise ExternalWorkspaceMutation(decision)
    supervisor.claim_workspace(task_id, run_path, trusted_committer_emails=settings.trusted_committer_emails)


def before_candidate_commit(store: Store, task_id: str, run_path: Path) -> None:
    """Called after the provider exits and before StageMesh commits the worktree as a candidate: a foreign commit is never adopted."""
    settings = settings_for_store(store)
    if not settings.enabled:
        return
    from .supervisor import ExternalWorkspaceMutation, Supervisor

    supervisor = Supervisor(store, project_of(store), max_reconstructs=settings.max_reconstructs)
    # The owner legitimately edited tracked files, so those are not mutations here; commits, ref moves and rewrites are.
    decision = supervisor.check_workspace(task_id, execution_running=True, restore=True)
    if decision is not None:
        raise ExternalWorkspaceMutation(decision)
