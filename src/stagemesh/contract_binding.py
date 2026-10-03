from __future__ import annotations

from pathlib import Path

from .contracts import BoundChangeContract, bind_contract, bound_contract_from_record
from .git import GitError, GitWorkspace
from .persistence import Store


def contract_for_candidate(store: Store, task_id: str, candidate_sha: str, project: Path) -> BoundChangeContract:
    existing = store.contract_binding(task_id, candidate_sha)
    if existing is not None:
        return bound_contract_from_record(dict(existing))

    baseline_sha = store.task_baseline(task_id) or _candidate_parent(project, candidate_sha)
    bound = bind_contract(project, task_id, baseline_sha=baseline_sha, candidate_sha=candidate_sha)
    store.bind_contract(
        task_id,
        candidate_sha,
        bound.baseline_sha,
        bound.version,
        bound.digest,
        bound.canonical_json,
    )
    return bound


def _candidate_parent(project: Path, candidate_sha: str) -> str | None:
    try:
        workspace = GitWorkspace(project)
        parts = workspace.run("rev-list", "--parents", "-n", "1", candidate_sha).stdout.strip().split()
    except (GitError, OSError):
        return None
    return parts[1] if len(parts) > 1 else None
