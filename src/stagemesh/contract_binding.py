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
        inherited = _inherited_baseline(store, project, task_id, candidate_sha) if store.task_baseline(task_id) else None
        baseline = inherited or str(frozen["baseline_sha"])
        store.bind_contract(task_id, candidate_sha, baseline, base.version, base.digest, base.canonical_json)
        return replace(base, baseline_sha=baseline, candidate_sha=candidate_sha)

    task_baseline = store.task_baseline(task_id)
    # Without a recorded task baseline (legacy/synthetic runs) there is no stable base to inherit: diff against the parent.
    inherited = _inherited_baseline(store, project, task_id, candidate_sha) if task_baseline else None
    baseline_sha = inherited or task_baseline or _candidate_parent(project, candidate_sha)
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


def _inherited_baseline(store: Store, project: Path, task_id: str, candidate_sha: str) -> str | None:
    """The baseline this candidate inherits from the same task's earlier bindings, or None to keep the original baseline.

    A rebase moves a task's base to the integration tip and binds the rebased candidate to it. A later candidate on the same
    worktree (remediation) descends from the rebased commit, so diffing it against the original baseline would count everything
    the integration ref gained in between as the task's own change.

    Fail-closed: the answer is a baseline only when it cannot hide task-owned changes. A binding qualifies when it belongs to this
    task, its candidate is a strict ancestor of this candidate, its baseline is a strict ancestor of its own candidate, and its
    baseline contains none of the task's own candidates (a base that includes earlier task work would hide it). If the
    qualifying bindings disagree on the baseline and are not a single ancestry chain (so the newest is ambiguous), nothing is
    inherited and the caller keeps the original, older baseline, which can only over-report changes, never hide them.
    """
    git = GitWorkspace(project)

    def is_ancestor(older: str, newer: str) -> bool:
        if older == newer:
            return False
        code = git.run("merge-base", "--is-ancestor", older, newer, check=False).returncode
        if code not in (0, 1):  # a missing/corrupt object is "unknown", never "not an ancestor"
            raise GitError(f"cannot establish ancestry of {older} and {newer}")
        return code == 0

    rows = store.conn.execute(
        "SELECT candidate_sha, baseline_sha FROM contract_bindings WHERE task_id=? AND baseline_sha IS NOT NULL ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    own_candidates = sorted(
        {str(r[0]) for r in store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,))}
        | {str(row["candidate_sha"]) for row in rows}
    )
    qualifying: list[tuple[str, str]] = []
    try:
        for row in rows:
            previous, baseline = str(row["candidate_sha"]), str(row["baseline_sha"])
            if not (is_ancestor(previous, candidate_sha) and is_ancestor(baseline, previous)):
                continue
            if any(own == baseline or is_ancestor(own, baseline) for own in own_candidates):
                continue
            qualifying.append((previous, baseline))
        if not qualifying:
            return None
        if len({baseline for _, baseline in qualifying}) == 1:
            return qualifying[0][1]
        for previous, baseline in qualifying:  # the binding built on by every other qualifying one
            if all(other == previous or is_ancestor(other, previous) for other, _ in qualifying):
                return baseline
    except (GitError, OSError):
        return None
    return None


def _candidate_parent(project: Path, candidate_sha: str) -> str | None:
    try:
        workspace = GitWorkspace(project)
        parts = workspace.run("rev-list", "--parents", "-n", "1", candidate_sha).stdout.strip().split()
    except (GitError, OSError):
        return None
    return parts[1] if len(parts) > 1 else None
