"""First-class repair commands for a stuck task: rebind its contract, rebaseline it, and a read-only doctor.

These replace hand-editing SQLite. Every repair goes through Store APIs, refuses while a worker could be using the task, never deletes
a candidate, finding, evidence row or audit event, and writes one audit event saying who changed what and why. Nothing here knows
about any particular project: the supported contract version is a constant of this build, never copied from a stored row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .audit import record_audit
from .baseline import BaselineAnalysis, analyze_baseline, resolve_integration_ref
from .contract_binding import contract_for_candidate
from .contracts import (
    CONTRACT_VERSION,
    ContractError,
    canonical_contract_json,
    changed_files,
    parse_contract,
    task_contract_path,
)
from .diagnosis import STALE_BASELINE, VALIDATION_GATE, diagnose, format_findings
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .git import GitError, GitWorkspace
from .lifecycle import evidence_allows_advance
from .operator_actions import OperatorActionError, _operator, task_details
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .redaction import redact_mapping
from .remediation import latest_candidate_findings
from .timing import format_task_summary, task_timing
from .validation import Validator

REBIND_EVENT = "task.contract_rebound"
REBASELINE_EVENT = "task.rebaselined"
ADVANCE_EVENT = "task.baseline_advanced"
# Ambiguous baseline situations a rebaseline refuses unless the operator passes --force (and a merge-base still exists).
FORCEABLE_REFUSALS = frozenset({"candidate_not_based_on_baseline", "baseline_diverged"})


class RecoveryRefusal(OperatorActionError):
    """A repair was refused; `code` is stable for scripts, the message says what to do instead."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"refused": True, "code": self.code, "message": str(self), **self.detail}


def _require_repairable(store: Store, task_id: str) -> Any:
    task = store.get_task(task_id)
    if task is None:
        raise RecoveryRefusal("unknown_task", f"task does not exist: {task_id}")
    if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
        raise RecoveryRefusal("task_done", f"task is done: {task_id}")
    if store.has_active_claim(task_id):
        raise RecoveryRefusal("active_claim", f"task {task_id} has an active claim; wait for it or run `stagemesh recover-stale --task {task_id}` first")
    if any(row["task_id"] == task_id for row in store.running_executions()):
        raise RecoveryRefusal("running_execution", f"task {task_id} has a running execution; wait for it or run `stagemesh recover-stale --task {task_id}` first")
    return task


def _resume_at_validate(store: Store, task_id: str, task: Any) -> dict[str, Any]:
    """After a repair: unblock a blocked task and, when a failed candidate is waiting in IMPLEMENT, send it back to be re-validated."""
    changes: dict[str, Any] = {"unblocked": False, "stage_reset": False}
    if task["status"] == TaskStatus.BLOCKED:
        changes["unblocked"] = store.unblock_task(task_id)
    current = store.get_task(task_id)
    if current["stage"] == Stage.IMPLEMENT and store.latest_candidate(task_id) is not None:
        store.advance_task(task_id, Stage.VALIDATE)
        record_audit(store, "task.advance", {"task_id": task_id, "stage": Stage.VALIDATE, "reason": "operator repair: re-validate the existing candidate"})
        changes["stage_reset"] = True
    return changes


def _revalidate(store: Store, project: Path, task_id: str) -> dict[str, Any]:
    candidate = store.latest_candidate(task_id)
    if candidate is None:
        return {"ran": False, "reason": "task has no candidate"}
    sha = str(candidate["sha"])
    stage = store.get_task(task_id)["stage"]
    if stage != Stage.VALIDATE:
        return {"ran": False, "reason": f"task is at {stage}, not VALIDATE", "candidate_sha": sha}
    status = Validator().validate(store, task_id, sha, project)
    result: dict[str, Any] = {"ran": True, "candidate_sha": sha, "status": str(status), "advanced": False}
    if status is EvidenceStatus.PASSED:
        bound = contract_for_candidate(store, task_id, sha, project)
        if store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.PASSED):
            decision = evidence_allows_advance(
                current=Stage.VALIDATE, candidate_sha=sha, evidence_sha=sha, kind=EvidenceKind.VALIDATION, status=EvidenceStatus.PASSED
            )
            store.advance_task(task_id, decision.target)
            record_audit(store, "task.advance", {"task_id": task_id, "stage": decision.target, "candidate_sha": sha, "reason": decision.reason})
            result["advanced"] = True
    return result


# --- rebind-contract -------------------------------------------------------------------------------------------------------------


def rebind_contract(store: Store, project: Path, task_id: str, *, validate: bool = False, reason: str | None = None, force: bool = False) -> dict[str, Any]:
    """Re-read .stagemesh/contracts/<id>.json, canonicalize it and replace the task's frozen contract with it.

    The stored version is always this build's CONTRACT_VERSION: it is not read from the old row, so a hand-edited row
    (`unsupported bound contract version: 2`) is repaired by the rebind rather than propagated.
    """
    task = _require_repairable(store, task_id)
    path = task_contract_path(project, task_id)
    if path is None:
        raise RecoveryRefusal("missing_contract_file", f"no contract file at .stagemesh/contracts/{task_id}.json to rebind")
    try:
        contract = parse_contract(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise RecoveryRefusal("invalid_contract", f"contract file is invalid: {exc}") from exc
    if not contract.explicit:
        raise RecoveryRefusal("invalid_contract", "change contract must be explicit")
    canonical = canonical_contract_json(contract)
    if len(canonical) > MAX_CANONICAL_CONTRACT_CHARS:
        raise RecoveryRefusal("invalid_contract", f"contract is {len(canonical)} characters canonicalized; the limit is {MAX_CANONICAL_CONTRACT_CHARS}")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    old = store.task_contract(task_id)
    old_digest = str(old["digest"]) if old is not None else None
    old_version = int(old["version"]) if old is not None else None
    candidate = store.latest_candidate(task_id)
    candidate_sha = str(candidate["sha"]) if candidate is not None else None
    # Candidate history: evidence that passed under a different digest stops satisfying the task once the digest changes.
    stale_digests = {str(b["digest"]) for b in store.contract_bindings_for_task(task_id)} | ({old_digest} if old_digest else set())
    stale_digests.discard(digest)
    at_risk = [row for d in sorted(stale_digests) for row in store.passed_evidence_for_digest(task_id, d)]
    if at_risk and not force:
        kinds = sorted({str(r["kind"]) for r in at_risk})
        raise RecoveryRefusal(
            "history_invalidated",
            f"{len(at_risk)} passed {'/'.join(kinds)} evidence row(s) are bound to the current contract and would stop counting; "
            f"re-run with --force to accept that (the evidence itself is kept)",
            evidence_ids=[str(r["id"]) for r in at_risk],
        )
    binding = store.contract_binding(task_id, candidate_sha) if candidate_sha else None
    store.rebind_task_contract(task_id, CONTRACT_VERSION, digest, canonical, candidate_sha if binding is not None else None)
    changes = _resume_at_validate(store, task_id, task)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "old_digest": old_digest,
        "new_digest": digest,
        "old_version": old_version,
        "new_version": CONTRACT_VERSION,
        "old_canonical_json": str(old["canonical_json"]) if old is not None else None,
        "candidate_sha": candidate_sha,
        "binding_updated": binding is not None,
        "invalidated_evidence": [str(r["id"]) for r in at_risk],
        "forced": bool(force),
        "reason": (reason or "").strip() or f"operator rebound the contract from {path.name}",
        "operator": _operator(),
        "operator_action": "REBIND_CONTRACT",
        **changes,
    }
    record_audit(store, REBIND_EVENT, payload)
    report = {k: v for k, v in payload.items() if k != "old_canonical_json"}
    report.update(contract_path=str(path), digest_changed=old_digest != digest, version_repaired=old_version not in (None, CONTRACT_VERSION), validation=None)
    if validate:
        report["validation"] = _revalidate(store, project, task_id)
    current = store.get_task(task_id)
    report["stage"], report["status"] = current["stage"], current["status"]
    return report


# --- rebaseline-task -------------------------------------------------------------------------------------------------------------


def rebaseline_task(
    store: Store,
    project: Path,
    task_id: str,
    integration_ref: str,
    *,
    validate: bool = False,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move the task baseline to the candidate's merge-base with the integration ref when that removes unrelated files from its diff."""
    task = _require_repairable(store, task_id)
    analysis = analyze_baseline(store, project, task_id, integration_ref)
    forced = False
    if analysis.refusal in FORCEABLE_REFUSALS and force:
        analysis = _force_analysis(project, analysis)
        forced = analysis.refusal is None
    if analysis.refusal:
        hint = "; pass --force to use the merge-base anyway" if analysis.refusal in FORCEABLE_REFUSALS else ""
        raise RecoveryRefusal(analysis.refusal, f"refusing to rebaseline task {task_id}: {analysis.detail}{hint}", analysis=analysis.to_dict())
    if not analysis.stale and not forced:
        raise RecoveryRefusal("not_stale", f"task {task_id}: {analysis.detail}; nothing to rebaseline", analysis=analysis.to_dict())
    assert analysis.merge_base and analysis.candidate_sha
    binding = store.contract_binding(task_id, analysis.candidate_sha)
    store.rebaseline_task(task_id, analysis.merge_base, analysis.candidate_sha if binding is not None else None)
    changes = _resume_at_validate(store, task_id, task)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "old_baseline": analysis.baseline_sha,
        "new_baseline": analysis.merge_base,
        "candidate_sha": analysis.candidate_sha,
        "integration_ref": integration_ref,
        "integration_sha": analysis.integration_sha,
        "changed_files_before": analysis.changed_before,
        "changed_files_after": analysis.changed_after,
        "removed_files": analysis.unrelated,
        "forced": forced,
        "reason": (reason or "").strip() or "operator rebaselined the task against the integration ref",
        "operator": _operator(),
        "operator_action": "REBASELINE_TASK",
        **changes,
    }
    record_audit(store, REBASELINE_EVENT, payload)
    report = {**payload, "validation": None}
    if validate:
        report["validation"] = _revalidate(store, project, task_id)
    current = store.get_task(task_id)
    report["stage"], report["status"] = current["stage"], current["status"]
    return report


# --- advance-baseline ------------------------------------------------------------------------------------------------------------


def _worktree_quarantined(store: Store, task_id: str) -> bool:
    """The supervisor quarantined this task's worktree and nothing has re-claimed it since."""
    decision = store.conn.execute(
        "SELECT created_at, rowid FROM audit_events WHERE event_type='autonomy.decision' AND json_extract(payload, '$.task_id')=? "
        "AND json_extract(payload, '$.action')='FAIL_CLOSED_QUARANTINE' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if decision is None:
        return False
    reclaimed = store.conn.execute(
        "SELECT 1 FROM audit_events WHERE event_type='autonomy.workspace_ownership' AND json_extract(payload, '$.task_id')=? "
        "AND (created_at > ? OR (created_at = ? AND rowid > ?)) LIMIT 1",
        (task_id, decision["created_at"], decision["created_at"], decision["rowid"]),
    ).fetchone()
    return reclaimed is None


def advance_baseline(
    store: Store,
    project: Path,
    task_id: str,
    target_ref: str | None = None,
    *,
    reason: str | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    """Move a task's baseline forward to `target_ref` (default: the integration ref) so the next run starts fresh from current code.

    Everything is validated before anything changes. Old candidates, evidence and findings are kept as history: they stay bound to the
    old baseline, so they never satisfy a candidate built on the new one. The idle task worktree is snapshotted under
    refs/stagemesh/preserved/ and then reset onto the new base, and the task returns to IMPLEMENT with a fresh remediation budget.
    """
    from .autonomy.gitfacts import GitFacts
    from .autonomy.provenance import load_ownership, save_ownership, tracked_fingerprint
    from .workspace_guard import (
        OWNER_FILE,
        _gitdir,
        _load_ledger,
        _owner_is_active,
        _read_json,
        record_supervised_move,
        unexpected_embedded_repositories,
    )
    from .workspaces import _task_key, prepare_task_workspace, task_workspace

    task = _require_repairable(store, task_id)
    if task["stage"] == Stage.INTEGRATE:
        raise RecoveryRefusal("integrating", f"task {task_id} is at INTEGRATE; wait for integration to finish before moving its baseline")
    if not yes:
        raise RecoveryRefusal("confirmation_required", f"advancing the baseline of task {task_id} resets its worktree and restarts IMPLEMENT; re-run with --yes")
    facts = GitFacts(project)
    ref = target_ref or resolve_integration_ref(project) or "HEAD"
    new_base = facts.resolve(ref)
    if new_base is None:
        raise RecoveryRefusal("unresolvable", f"cannot resolve {ref!r} to a commit")
    old_base = store.task_baseline(task_id)
    if old_base is None:
        raise RecoveryRefusal("no_baseline", f"task {task_id} has no recorded baseline")
    if old_base == new_base:
        raise RecoveryRefusal("already_at_target", f"task {task_id}: baseline is already {new_base[:12]}")
    if not facts.exists(old_base) or not facts.is_ancestor(old_base, new_base):
        raise RecoveryRefusal("baseline_not_ancestor", f"task {task_id}: baseline {old_base[:12]} is not an ancestor of {ref} ({new_base[:12]}); refusing to move it sideways or backwards")

    worktree = task_workspace(project, task_id)
    has_worktree = (worktree / ".git").exists()
    if worktree.exists() and not has_worktree:
        raise RecoveryRefusal("worktree_not_a_checkout", f"task {task_id}: {worktree} exists but is not a git checkout; not touching it (a fresh run would delete it)")
    ledger: dict[str, Any] | None = None
    if has_worktree:
        if _worktree_quarantined(store, task_id):
            raise RecoveryRefusal("worktree_quarantined", f"task {task_id}: its worktree is quarantined by the supervisor; resolve that first")
        gitdir = _gitdir(worktree)
        try:
            ledger, _raw = _load_ledger(gitdir)
            owner = _read_json(gitdir / OWNER_FILE)
        except ValueError as exc:
            raise RecoveryRefusal("worktree_unreadable", f"task {task_id}: the worktree ownership record is unreadable ({exc}); not resetting it") from exc
        if owner is not None and _owner_is_active(owner):
            raise RecoveryRefusal("worktree_in_use", f"task {task_id}: its worktree is owned by a live execution ({owner.get('execution_id') or owner.get('claim_id')}); wait for it or run `stagemesh recover-stale --task {task_id}`")
        embedded = unexpected_embedded_repositories(worktree)
        if embedded:
            raise RecoveryRefusal("worktree_unpreservable", f"task {task_id}: worktree contains embedded repositories that cannot be preserved: {', '.join(embedded[:5])}")

    key = _task_key(task_id)
    preserved: list[str] = []
    snapshot_ref: str | None = None
    superseded = [str(row["sha"]) for row in store.conn.execute("SELECT sha FROM candidates WHERE task_id=? ORDER BY created_at, rowid", (task_id,))]
    latest = store.latest_candidate(task_id)
    ownership = load_ownership(store, task_id)
    old_head: str | None = None

    def preserve(sha: str) -> str:
        ref_name = f"refs/stagemesh/preserved/{key}/{sha[:12]}"
        facts.ensure_ref(ref_name, sha)
        if ref_name not in preserved:
            preserved.append(ref_name)
        return ref_name

    # Nothing in the ledger has changed up to here. From now on only refs are added until the worktree is reset, and the one DB
    # transaction at the end moves the baseline, the stage and the audit event together.
    try:
        if has_worktree:
            work = GitFacts(worktree)
            old_head = work.resolve("HEAD")
            snapshot = work.snapshot_worktree(worktree, f"StageMesh: state of {task_id}'s worktree before its baseline advanced to {new_base[:7]}")
            if snapshot:
                snapshot_ref = preserve(snapshot)
        for sha in superseded:
            if facts.exists(sha):
                preserve(sha)
        if not has_worktree:
            worktree = prepare_task_workspace(project, task_id)  # a worktree for a task that never had one starts at the target
        work = GitFacts(worktree)
        work.git.run("reset", "--hard", new_base)
        work.git.run("clean", "-fdq", check=False)
    except GitError as exc:
        raise RecoveryRefusal("git_error", f"task {task_id}: git failed while preserving or resetting the worktree ({str(exc).strip()[:200]}); the ledger was not changed; preserved so far: {preserved}") from exc
    if work.resolve("HEAD") != new_base:
        raise RecoveryRefusal("worktree_reset_failed", f"task {task_id}: worktree is not at {new_base[:12]} after reset; the ledger was not changed; preserved: {preserved}")
    # The operator reset is authorised and everything it replaces is preserved above, so the workspace ledger is rewritten from its own head.
    previous = str(ledger["head"]) if ledger is not None else (ownership.expected_head if ownership is not None else (old_head or new_base))
    moved = record_supervised_move(project, task_id, previous, new_base, None if latest is None else str(latest["sha"]), require_same_dirty=False)
    if not moved:
        if old_head is not None:
            work.git.run("reset", "--hard", old_head, check=False)
        raise RecoveryRefusal(
            "worktree_reset_unsafe",
            f"task {task_id}: the reset left files StageMesh cannot reseal (ignored paths or embedded repositories); tracked files were put back on {str(old_head)[:12]} and the ledger was not changed; uncommitted or untracked work is in the snapshot ref and can be restored with `git checkout <snapshot> -- .`; preserved: {preserved}",
        )
    if ownership is not None:
        from dataclasses import replace

        save_ownership(store, replace(ownership, expected_head=new_base, tracked_fingerprint=tracked_fingerprint(worktree)))

    payload: dict[str, Any] = {
        "task_id": task_id,
        "old_baseline": old_base,
        "new_baseline": new_base,
        "target_ref": ref,
        "reason": (reason or "").strip() or "operator advanced the task baseline to the integration ref",
        "operator": _operator(),
        "operator_action": "ADVANCE_BASELINE",
        "preserved_refs": preserved,
        "snapshot_ref": snapshot_ref,
        "superseded_candidates": superseded,
        "previous_latest_candidate": None if latest is None else str(latest["sha"]),
        "worktree": str(worktree),
        "old_worktree_head": old_head,
        "worktree_reset": True,
        "ownership_expected_head_before": None if ownership is None else ownership.expected_head,
        "ledger_head_before": None if ledger is None else str(ledger["head"]),
        "previous_stage": str(task["stage"]),
        "previous_status": str(task["status"]),
    }
    cleared = store.advance_task_baseline(task_id, new_base, redact_mapping(payload))
    current = store.get_task(task_id)
    return {**payload, "remediations_cleared": cleared, "unblocked": task["status"] == TaskStatus.BLOCKED, "stage": current["stage"], "status": current["status"]}


def auto_rebaseline_blocked_stale_baselines(
    store: Store,
    project: Path,
    *,
    integration_ref: str | None = None,
    task_id: str | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Recover blocked stale-baseline tasks without requiring an operator command."""
    ref = integration_ref or resolve_integration_ref(project)
    if not ref or limit < 1:
        return []
    recovered: list[dict[str, Any]] = []
    for task in store.tasks():
        current_id = str(task["id"])
        if task_id is not None and current_id != task_id:
            continue
        if task["status"] != TaskStatus.BLOCKED:
            continue
        diagnosis = diagnose(store, current_id, project)
        if diagnosis is None or diagnosis.category != STALE_BASELINE:
            continue
        try:
            report = rebaseline_task(
                store,
                project,
                current_id,
                ref,
                validate=True,
                reason="automatic stale baseline recovery",
            )
        except RecoveryRefusal as exc:
            record_audit(
                store,
                "task.auto_rebaseline_refused",
                {"task_id": current_id, "code": exc.code, "message": str(exc)[:300]},
            )
            continue
        recovered.append(report)
        if len(recovered) >= limit:
            break
    return recovered


def auto_rebind_blocked_missing_validation_gates(
    store: Store,
    project: Path,
    *,
    task_id: str | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Recover blocked tasks when a stale frozen contract has no executable validation gate.

    This is intentionally narrower than the operator repair command: it only runs for a BLOCKED task whose diagnosis is the
    specific missing-planned-check validation gate failure, and only when the task now has an explicit contract file with gates.
    Passed evidence remains protected by rebind_contract's normal history checks.
    """
    if limit < 1:
        return []
    recovered: list[dict[str, Any]] = []
    for task in store.tasks():
        current_id = str(task["id"])
        if task_id is not None and current_id != task_id:
            continue
        if task["status"] != TaskStatus.BLOCKED:
            continue
        diagnosis = diagnose(store, current_id, project)
        if diagnosis is None:
            continue
        latest = diagnosis.failing_evidence[-1] if diagnosis.failing_evidence else {}
        codes = set(latest.get("codes") or ())
        if "planned_check_missing_command" not in codes:
            continue
        path = task_contract_path(project, current_id)
        if path is None:
            continue
        try:
            contract = parse_contract(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ContractError) as exc:
            record_audit(
                store,
                "task.auto_rebind_refused",
                {"task_id": current_id, "code": "invalid_contract", "message": str(exc)[:300]},
            )
            continue
        if not contract.explicit or not contract.gates:
            continue
        try:
            report = rebind_contract(
                store,
                project,
                current_id,
                validate=True,
                reason="automatic missing validation gate recovery",
            )
        except RecoveryRefusal as exc:
            record_audit(
                store,
                "task.auto_rebind_refused",
                {"task_id": current_id, "code": exc.code, "message": str(exc)[:300]},
            )
            continue
        report["auto_recovery"] = "missing_validation_gate"
        recovered.append(report)
        if len(recovered) >= limit:
            break
    return recovered


def auto_revalidate_blocked_validation_gates(
    store: Store,
    project: Path,
    *,
    task_id: str | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """Re-run a blocked validation candidate once after an environment/gate setup failure.

    This is for normal recovery after the validation runtime changes (for example a project Python path is now available).
    It does not grant a fresh implementation budget or alter the candidate; it only reruns the bound validation gate once per
    candidate and contract. A failed recheck must not renew its own retry budget.
    """
    if limit < 1:
        return []
    recovered: list[dict[str, Any]] = []
    for task in store.tasks():
        current_id = str(task["id"])
        if task_id is not None and current_id != task_id:
            continue
        if task["status"] != TaskStatus.BLOCKED or task["stage"] != Stage.VALIDATE:
            continue
        if store.has_active_claim(current_id) or any(row["task_id"] == current_id for row in store.running_executions()):
            continue
        diagnosis = diagnose(store, current_id, project)
        if diagnosis is None or diagnosis.category != VALIDATION_GATE:
            continue
        candidate = store.latest_candidate(current_id)
        if candidate is None:
            continue
        candidate_sha = str(candidate["sha"])
        evidence = store.conn.execute(
            "SELECT id, created_at, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (current_id, candidate_sha, EvidenceKind.VALIDATION, EvidenceStatus.FAILED),
        ).fetchone()
        if evidence is None:
            continue
        evidence_id = str(evidence["id"])
        if _auto_revalidation_already_tried(store, current_id, candidate_sha, evidence_id, float(evidence["created_at"])):
            continue
        report = _revalidate(store, project, current_id)
        payload = {
            "task_id": current_id,
            "candidate_sha": candidate_sha,
            "evidence_id": evidence_id,
            "contract_hash": _loads(evidence["payload"]).get("contract_hash"),
            "reason": "automatic blocked validation gate recheck",
            "validation": report,
        }
        record_audit(store, "task.auto_revalidated_blocked_validation", payload)
        recovered.append({"task_id": current_id, "auto_recovery": "blocked_validation_recheck", **payload})
        if len(recovered) >= limit:
            break
    return recovered


def _auto_revalidation_already_tried(
    store: Store, task_id: str, candidate_sha: str, evidence_id: str, evidence_created_at: float
) -> bool:
    evidence = store.conn.execute("SELECT payload FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    contract_hash = _loads(evidence["payload"]).get("contract_hash") if evidence else None
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC",
        ("task.auto_revalidated_blocked_validation",),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if (
            payload.get("task_id") == task_id
            and payload.get("candidate_sha") == candidate_sha
            and payload.get("contract_hash") == contract_hash
        ):
            return True
    return False


def auto_reintegrate_blocked_runtime_failures(
    store: Store, project: Path, *, task_id: str | None = None, limit: int = 1,
) -> list[dict[str, Any]]:
    """Retry an unavailable integration runtime once, preserving the exact reviewed candidate.

    Content conflicts and exhausted rebase budgets remain blocked. A retry cannot mint another
    retry merely by writing a fresh failure row, and active ownership is never taken over.
    """
    recovered: list[dict[str, Any]] = []
    if limit < 1:
        return recovered
    for task in store.tasks():
        current_id = str(task["id"])
        if task_id is not None and current_id != task_id:
            continue
        if task["status"] != TaskStatus.BLOCKED or task["stage"] != Stage.INTEGRATE:
            continue
        if store.has_active_claim(current_id) or any(row["task_id"] == current_id for row in store.running_executions()):
            continue
        candidate = store.latest_candidate(current_id)
        if candidate is None:
            continue
        sha = str(candidate["sha"])
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (current_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED),
        ).fetchone()
        payload = _loads(row["payload"]) if row else {}
        codes = {f.get("code") for f in payload.get("findings", []) if isinstance(f, dict)}
        if codes != {"integration_rebase_unavailable"}:
            continue
        contract_hash = payload.get("contract_hash")
        previous = store.conn.execute("SELECT payload FROM audit_events WHERE event_type=?", ("task.auto_reintegrated_runtime",))
        if any(
            item.get("task_id") == current_id and item.get("candidate_sha") == sha and item.get("contract_hash") == contract_hash
            for item in (_loads(r["payload"]) for r in previous)
        ):
            continue
        store.unblock_task(current_id)
        action = {"task_id": current_id, "candidate_sha": sha, "contract_hash": contract_hash, "auto_recovery": "integration_runtime_recheck"}
        record_audit(store, "task.auto_reintegrated_runtime", action)
        recovered.append(action)
        if len(recovered) >= limit:
            break
    return recovered


def _force_analysis(project: Path, analysis: BaselineAnalysis) -> BaselineAnalysis:
    git = GitWorkspace(project)
    try:
        merge_base = git.run("merge-base", str(analysis.candidate_sha), str(analysis.integration_sha)).stdout.strip()
        analysis.changed_after = changed_files(project, str(analysis.candidate_sha), merge_base)
    except (GitError, OSError):
        analysis.refusal, analysis.detail = "no_merge_base", "the candidate shares no history with the integration ref"
        return analysis
    analysis.merge_base, analysis.refusal, analysis.stale = merge_base, None, True
    return analysis


# --- task-doctor -----------------------------------------------------------------------------------------------------------------


def task_doctor(store: Store, project: Path, task_id: str, integration_ref: str | None = None, threshold: int = 2) -> dict[str, Any]:
    """Read-only summary of one task: everything an operator used to dig out of SQLite, plus the next command to run."""
    task = store.get_task(task_id)
    if task is None:
        raise RecoveryRefusal("unknown_task", f"task does not exist: {task_id}")
    details = task_details(store, task_id)
    candidate = store.latest_candidate(task_id)
    candidate_sha = str(candidate["sha"]) if candidate is not None else None
    ref = resolve_integration_ref(project, integration_ref)

    contract = _contract_summary(store, project, task_id, candidate_sha)
    baseline: dict[str, Any] = {"sha": store.task_baseline(task_id), "integration_ref": ref, "stale": None}
    if candidate_sha and ref:
        analysis = analyze_baseline(store, project, task_id, ref)
        baseline.update(
            stale=analysis.stale, proposed_sha=analysis.merge_base, unrelated_files=analysis.unrelated, detail=analysis.detail, refusal=analysis.refusal
        )

    validation_failures: list[dict[str, Any]] = []
    if candidate_sha:
        row = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status='FAILED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id, candidate_sha, EvidenceKind.VALIDATION),
        ).fetchone()
        payload = _loads(row["payload"]) if row is not None else {}
        validation_failures = [
            {k: f.get(k) for k in ("code", "severity", "message", "path") if f.get(k) is not None}
            for f in payload.get("findings", [])
            if isinstance(f, dict)
        ]
    review_findings = [f for f in latest_candidate_findings(store, task_id) if f["source"] == EvidenceKind.REVIEW]

    diagnosis = diagnose(store, task_id, project, threshold, ref)
    report: dict[str, Any] = {
        "task_id": task_id,
        "title": task["title"],
        "stage": str(task["stage"]),
        "status": str(task["status"]),
        "queue_control": _queue_control_summary(store),
        "active_claim": details["active_claim"],
        "active_executions": details["active_executions"],
        "latest_candidate": details["latest_candidate"],
        "latest_validation": details["latest_validation"],
        "latest_review": details["latest_review"],
        "latest_integration": details["latest_integration"],
        "baseline": baseline,
        "contract": contract,
        "validation_failures": validation_failures,
        "review_findings": review_findings,
        "diagnosis": (
            {
                "category": diagnosis.category,
                "stage": diagnosis.stage,
                "repeated": diagnosis.repeated,
                "summary": diagnosis.summary,
                "recommendation": diagnosis.recommendation,
            }
            if diagnosis
            else None
        ),
        "recent_repairs": _recent_repairs(store, task_id),
        "timing": task_timing(store, task_id),
    }
    report["recommended_command"], report["recommendation_reason"] = _recommend(report, task_id, ref)
    return report


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _contract_summary(store: Store, project: Path, task_id: str, candidate_sha: str | None) -> dict[str, Any]:
    frozen = store.task_contract(task_id)
    binding = store.contract_binding(task_id, candidate_sha) if candidate_sha else None
    path = task_contract_path(project, task_id)
    file_digest, file_error = None, None
    if path is not None:
        try:
            canonical = canonical_contract_json(parse_contract(json.loads(path.read_text(encoding="utf-8"))))
            file_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (OSError, ValueError, ContractError) as exc:
            file_error = str(exc)[:300]
    versions = [int(r["version"]) for r in (frozen, binding) if r is not None]
    current = frozen if frozen is not None else binding
    return {
        "current": {"digest": current["digest"], "version": current["version"]} if current is not None else None,
        "frozen": {"digest": frozen["digest"], "version": frozen["version"]} if frozen is not None else None,
        "candidate_binding": {"digest": binding["digest"], "version": binding["version"]} if binding is not None else None,
        "supported_version": CONTRACT_VERSION,
        "version_supported": all(v == CONTRACT_VERSION for v in versions),
        "file": {"path": str(path.relative_to(project)).replace("\\", "/") if path else None, "digest": file_digest, "error": file_error},
        "file_matches_frozen": (file_digest == frozen["digest"]) if frozen is not None and file_digest else None,
    }


def _recent_repairs(store: Store, task_id: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT event_type, payload, created_at FROM audit_events WHERE event_type IN (?, ?) ORDER BY created_at DESC, rowid DESC LIMIT 50",
        (REBIND_EVENT, REBASELINE_EVENT),
    ).fetchall()
    out = []
    for row in rows:
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            out.append({"event": row["event_type"], "at": row["created_at"], "operator": payload.get("operator"), "reason": payload.get("reason")})
    return out[:limit]


def _recommend(report: dict[str, Any], task_id: str, ref: str | None) -> tuple[str, str]:
    contract, diagnosis = report["contract"], report["diagnosis"]
    if report["active_claim"] or report["active_executions"]:
        return f"stagemesh recover-stale --task {task_id}", "a claim or execution is active; wait for it, or recover it if its process is dead"
    if report["status"] == "DONE":
        return "", "task is done"
    if contract["current"] is not None and not contract["version_supported"]:
        return f"stagemesh rebind-contract --task {task_id} --validate", f"stored contract version is not supported (this build supports {CONTRACT_VERSION})"
    if contract["file"]["error"]:
        return f"stagemesh rebind-contract --task {task_id}", f"the contract file is invalid: {contract['file']['error']}"
    if contract["file_matches_frozen"] is False:
        return f"stagemesh rebind-contract --task {task_id} --validate", "the contract file differs from the frozen contract"
    if report["baseline"].get("stale"):
        return f"stagemesh rebaseline-task --task {task_id} --to {ref}", "the baseline is stale: other work's files appear in this task's diff"
    if report["status"] == "BLOCKED":
        return f"stagemesh retry-task --task {task_id}", diagnosis["recommendation"] if diagnosis else "task is blocked; unblock it once the cause is fixed"
    if contract["frozen"] is None and contract["file"]["path"] is None:
        return "stagemesh continue", "no contract yet; continue auto-plans one deterministically"
    return "stagemesh continue", "nothing is wrong with the task's bookkeeping"


def format_doctor(report: dict[str, Any]) -> str:
    c, b = report["contract"], report["baseline"]
    lines = [f"task {report['task_id']}: {report['title']}", f"  stage/status: {report['stage']}/{report['status']}"]
    control = report.get("queue_control") or {}
    if control:
        from .queue_visibility import format_queue_control

        lines.extend(format_queue_control(control, "  "))
    claim = report["active_claim"]
    lines.append(f"  active claim: {claim['id']} ({claim['stage']}, worker {claim['worker_id']})" if claim else "  active claim: none")
    execs = report["active_executions"]
    lines.append("  running executions: " + (", ".join(f"{e['id']} {e['kind']} {e['process_state']}" for e in execs) if execs else "none"))
    cand = report["latest_candidate"]
    lines.append(f"  latest candidate: {cand['sha']} (by {cand['producer']})" if cand else "  latest candidate: none")
    for key in ("latest_validation", "latest_review", "latest_integration"):
        if report[key]:
            lines.append(f"  {key.replace('_', ' ')}: {report[key]['status']}")
    lines.append(f"  baseline: {b['sha']}" + (f" (stale vs {b['integration_ref']}; proposed {b['proposed_sha']})" if b.get("stale") else ""))
    if b.get("stale"):
        lines.append(f"    unrelated files in diff: {', '.join(b['unrelated_files'][:10])}")
    current = c["current"]
    if current:
        unsupported = "" if c["version_supported"] else f" (UNSUPPORTED; supported {c['supported_version']})"
        lines.append(f"  contract: digest {current['digest'][:16]} version {current['version']}{unsupported}")
    else:
        lines.append("  contract: not bound yet")
    if c["file"]["path"]:
        match = {True: "matches", False: "DIFFERS from frozen", None: "not compared"}[c["file_matches_frozen"]]
        lines.append(f"  contract file: {c['file']['path']} ({match})" + (f" error: {c['file']['error']}" if c["file"]["error"] else ""))
    if report["validation_failures"]:
        lines.append("  latest validation failures:")
        lines.extend(f"    - [{f.get('code', '?')}] {f.get('message', '')}" for f in report["validation_failures"])
    if report["review_findings"]:
        lines.extend(format_findings(report["review_findings"]))
    d = report["diagnosis"]
    lines.append(f"  diagnosis: {d['category']} at {d['stage']}: {d['summary']}" if d else "  diagnosis: none")
    for repair in report["recent_repairs"]:
        lines.append(f"  repair: {repair['event']} by {repair['operator']}: {repair['reason']}")
    if report["timing"]["executions"]:
        lines.extend("  " + line for line in format_task_summary(report["timing"]).splitlines())
    lines.append(f"  next: {report['recommended_command'] or '(nothing)'}  # {report['recommendation_reason']}")
    return "\n".join(lines)


def _queue_control_summary(store: Store) -> dict[str, Any]:
    from .queue_visibility import queue_control_report

    return queue_control_report(store)
