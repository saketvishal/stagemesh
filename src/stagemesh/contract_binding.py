from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .contracts import (
    BoundChangeContract,
    ContractError,
    bind_contract,
    bound_contract_from_record,
    CONTRACT_VERSION,
    canonical_contract_json,
    parse_contract,
    task_contract_path,
)
from .git import GitError, GitWorkspace
from .persistence import Store


class ContractRejected(ContractError):
    """Raised when a task may not start implementation; `reason` is the persisted failure code."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def bind_task_contract(store: Store, project: Path, task_id: str, baseline_sha: str) -> BoundChangeContract:
    """Load, validate, canonicalize and freeze the task contract before the provider runs."""
    existing = store.task_contract(task_id)
    if existing is not None:
        return bound_contract_from_record(dict(existing))
    path = task_contract_path(project, task_id)
    if path is None:
        raise ContractRejected("missing_explicit_contract", f"no task-specific contract exists for {task_id}")
    try:
        contract = parse_contract(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ContractRejected("invalid_contract", f"change contract is invalid: {exc}") from exc
    if not contract.explicit:
        raise ContractRejected("missing_explicit_contract", "change contract must be explicit")
    canonical_json = canonical_contract_json(contract)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    store.bind_task_contract(task_id, baseline_sha, CONTRACT_VERSION, digest, canonical_json)
    return bound_contract_from_record(dict(store.task_contract(task_id)))


def contract_for_candidate(store: Store, task_id: str, candidate_sha: str, project: Path) -> BoundChangeContract:
    existing = store.contract_binding(task_id, candidate_sha)
    if existing is not None:
        return bound_contract_from_record(dict(existing))

    frozen = store.task_contract(task_id)
    if frozen is not None:
        base = bound_contract_from_record(dict(frozen))
        baseline = str(frozen["baseline_sha"])
        store.bind_contract(task_id, candidate_sha, baseline, base.version, base.digest, base.canonical_json)
        return replace(base, baseline_sha=baseline, candidate_sha=candidate_sha)

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
