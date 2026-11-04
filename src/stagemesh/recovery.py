"""First-class repair commands for a stuck task: rebind its contract, rebaseline it, and a read-only doctor.

These replace hand-editing SQLite. Every repair goes through Store APIs, refuses while a worker could be using the task, never deletes
a candidate, finding, evidence row or audit event, and writes one audit event saying who changed what and why. Nothing here knows
about any particular project: the supported contract version is a constant of this build, never copied from a stored row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .audit import record_audit
from .auto_plan import outside_label_selector
from .autonomy.destructive_git import execute_preserved_cleanup
from .baseline import BaselineAnalysis, analyze_baseline, resolve_integration_ref
from .capabilities import (
    CapabilityRegistryError,
    capability_policy_snapshot,
    task_capability_summary,
)
from .config import AdverseReviewPolicy, ConfigValidationError, load_config
from .contract_binding import contract_for_candidate
from .contracts import (
    CONTRACT_VERSION,
    ContractError,
    _invariant_gate,
    bound_contract_from_record,
    canonical_contract_json,
    changed_files,
    contract_admission_problem,
    parse_contract,
    task_contract_path,
)
from .diagnosis import (
    STALE_BASELINE,
    VALIDATION_GATE,
    diagnose,
    format_findings,
    latest_tool_failure,
)
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .git import GitError, GitWorkspace
from .lifecycle import evidence_allows_advance
from .operator_actions import OperatorActionError, _operator, task_details
from .persistence import MAX_CANONICAL_CONTRACT_CHARS, Store
from .planning_execution import planning_execution_receipts
from .redaction import redact_mapping
from .remediation import latest_candidate_findings
from .review import adverse_gate, adverse_outcome
from .spec_planning import (
    NEXT_APPROVE_SPEC,
    NEXT_CHALLENGE_SPEC,
    NEXT_REFRESH_CHALLENGE,
    NEXT_REFRESH_SPEC,
    NEXT_RESOLVE_CHALLENGE_DECISIONS,
    NEXT_RESOLVE_DECISIONS,
    NEXT_REVISE_SPEC,
    NEXT_WRITE_SPEC,
    evaluate_planning_gate,
    load_planning_artifacts,
    load_planning_challenge,
    planning_artifact_paths,
    planning_challenge_path,
)
from .timing import format_task_summary, task_timing
from .validation import Validator
from .validation_services import (
    ValidationServiceError,
    discover_validation_services,
    e2e_policy_projection,
)

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
    """After a repair: unblock a blocked task and, when a candidate is waiting in IMPLEMENT, send it to the appropriate stage."""
    changes: dict[str, Any] = {"unblocked": False, "stage_reset": False}
    if task["status"] == TaskStatus.BLOCKED:
        changes["unblocked"] = store.unblock_task(task_id)
    current = store.get_task(task_id)
    cand = store.latest_candidate(task_id)
    if current["stage"] == Stage.IMPLEMENT and cand is not None:
        sha = str(cand["sha"])
        bound = store.contract_binding(task_id, sha)
        digest = str(bound["digest"]) if bound is not None else None
        if digest and store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, digest, EvidenceStatus.PASSED):
            target_stage = Stage.REVIEW
            reason = "operator repair: resume validated candidate at review"
        else:
            target_stage = Stage.VALIDATE
            reason = "operator repair: re-validate the existing candidate"
        store.advance_task(task_id, target_stage)
        record_audit(store, "task.advance", {"task_id": task_id, "stage": target_stage, "reason": reason})
        changes["stage_reset"] = True
    return changes


def resume_task(
    store: Store,
    task_id: str,
    *,
    adverse_policy: AdverseReviewPolicy | None = None,
) -> dict[str, Any]:
    """Resume a stuck or blocked task at the earliest incomplete stage warranted by its candidate and bound evidence."""
    task = _require_repairable(store, task_id)
    unblocked = False
    if task["status"] == TaskStatus.BLOCKED:
        unblocked = store.unblock_task(task_id)
    current = store.get_task(task_id)
    cand = store.latest_candidate(task_id)
    advanced_to: Stage | None = None
    if cand is not None:
        sha = str(cand["sha"])
        bound = store.contract_binding(task_id, sha)
        digest = str(bound["digest"]) if bound is not None else None
        stage = Stage(current["stage"])
        if digest and store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, digest, EvidenceStatus.PASSED):
            review_passed = store.has_bound_evidence(
                task_id, sha, EvidenceKind.REVIEW, digest, EvidenceStatus.PASSED
            )
            review_gate_passed = (
                review_passed
                and (
                    adverse_policy is None
                    or adverse_gate(store, task_id, sha, digest, adverse_policy).allowed
                )
            )
            if review_gate_passed:
                if stage in (Stage.IMPLEMENT, Stage.VALIDATE, Stage.REVIEW):
                    store.advance_task(task_id, Stage.INTEGRATE)
                    advanced_to = Stage.INTEGRATE
            elif stage in (Stage.IMPLEMENT, Stage.VALIDATE):
                store.advance_task(task_id, Stage.REVIEW)
                advanced_to = Stage.REVIEW
        elif stage == Stage.IMPLEMENT and cand["durable_handoff"]:
            store.advance_task(task_id, Stage.VALIDATE)
            advanced_to = Stage.VALIDATE
    if advanced_to is not None:
        record_audit(store, "task.advance", {"task_id": task_id, "stage": advanced_to, "reason": f"stage-specific resumption: resume at {advanced_to}"})
    return {"unblocked": unblocked, "stage": store.get_task(task_id)["stage"]}


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


def rebind_contract(store: Store, project: Path, task_id: str, *, validate: bool = False, reason: str | None = None,
                    force: bool = False, review_invariants: tuple[int, ...] = (), command_invariants: tuple[int, ...] = (),
                    plan: bool = False) -> dict[str, Any]:
    """Re-read .stagemesh/contracts/<id>.json, canonicalize it and replace the task's frozen contract with it.

    The stored version is always this build's CONTRACT_VERSION: it is not read from the old row, so a hand-edited row
    (`unsupported bound contract version: 2`) is repaired by the rebind rather than propagated.
    """
    task = _require_repairable(store, task_id)
    old = store.task_contract(task_id)
    path = task_contract_path(project, task_id)
    classifying = bool(review_invariants or command_invariants)
    if classifying and not (reason or "").strip():
        raise RecoveryRefusal("reason_required", "classifying legacy invariants requires --reason")
    if path is None and not (classifying and old is not None):
        raise RecoveryRefusal("missing_contract_file", f"no contract file at .stagemesh/contracts/{task_id}.json to rebind")
    try:
        # Classification starts from the verified frozen payload, never a possibly
        # edited file. Only selected prefixes may change; all requirements survive.
        if classifying and old is not None:
            original_canonical = str(old["canonical_json"])
            contract = bound_contract_from_record(dict(old)).contract
        else:
            original_canonical = path.read_text(encoding="utf-8")
            contract = parse_contract(json.loads(original_canonical))
    except (OSError, ValueError) as exc:
        raise RecoveryRefusal("invalid_contract", f"contract file is invalid: {exc}") from exc
    if not contract.explicit:
        raise RecoveryRefusal("invalid_contract", "change contract must be explicit")
    if old is None and store.task_baseline(task_id) is None:
        raise RecoveryRefusal("missing_baseline", "task has no recorded baseline; establish its baseline before rebinding rather than guessing from the checkout")
    classification = []
    if classifying:
        unknown = set(json.loads(original_canonical)) - set(json.loads(canonical_contract_json(contract)))
        if unknown:
            raise RecoveryRefusal("unknown_contract_fields", "cannot safely classify a legacy contract with unknown fields: " + ", ".join(sorted(unknown)))
        indices = (*review_invariants, *command_invariants)
        if len(set(indices)) != len(indices) or any(type(i) is not int or i < 1 or i > len(contract.invariants) for i in indices):
            raise RecoveryRefusal("invalid_classification", "invariant indices are unique, one-based positions in the frozen contract")
        invariants = list(contract.invariants)
        for index in indices:
            original = invariants[index - 1]
            if original.startswith(("review:", "command:")):
                raise RecoveryRefusal("invalid_classification", f"invariant {index} already has an explicit type")
            try:
                gate = _invariant_gate(original)
            except ContractError as exc:
                if not str(exc).startswith("ambiguous invariant:"):
                    raise RecoveryRefusal("invalid_classification", str(exc)) from exc
                gate = None
            kind = "review" if index in review_invariants else "command"
            if kind == "review" and gate is not None:
                raise RecoveryRefusal("executable_invariant", f"invariant {index} is an executable check; it cannot be downgraded to review")
            updated = f"{kind}: {original}"
            invariants[index - 1] = updated
            classification.append({"index": index, "kind": kind, "original": original, "replacement": updated})
        contract = replace(contract, invariants=tuple(invariants))
    problem = contract_admission_problem(contract)
    if problem:
        raise RecoveryRefusal("invalid_contract", problem)
    canonical = canonical_contract_json(contract)
    if len(canonical) > MAX_CANONICAL_CONTRACT_CHARS:
        raise RecoveryRefusal("invalid_contract", f"contract is {len(canonical)} characters canonicalized; the limit is {MAX_CANONICAL_CONTRACT_CHARS}")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        capability_policy = capability_policy_snapshot(project, contract.capability_refs)
    except CapabilityRegistryError as exc:
        raise RecoveryRefusal("capability_policy_unavailable", str(exc)) from exc

    old_digest = str(old["digest"]) if old is not None else None
    old_version = int(old["version"]) if old is not None else None
    candidate = store.latest_candidate(task_id)
    candidate_sha = str(candidate["sha"]) if candidate is not None else None
    # Candidate history: evidence that passed under a different digest stops satisfying the task once the digest changes.
    stale_digests = {str(b["digest"]) for b in store.contract_bindings_for_task(task_id)} | ({old_digest} if old_digest else set())
    stale_digests.discard(digest)
    at_risk = [row for d in sorted(stale_digests) for row in store.passed_evidence_for_digest(task_id, d)]
    if plan:
        return {"plan": True, "task_id": task_id, "old_digest": old_digest, "new_digest": digest,
                "capability_policy_digest": capability_policy["digest"],
                "classification": classification, "invalidated_evidence": [str(r["id"]) for r in at_risk],
                "requires_force": bool(at_risk), "new_canonical_json": canonical,
                "stage": task["stage"], "status": task["status"]}
    if at_risk and not force:
        kinds = sorted({str(r["kind"]) for r in at_risk})
        raise RecoveryRefusal(
            "history_invalidated",
            f"{len(at_risk)} passed {'/'.join(kinds)} evidence row(s) are bound to the current contract and would stop counting; "
            f"re-run with --force to accept that (the evidence itself is kept)",
            evidence_ids=[str(r["id"]) for r in at_risk],
        )
    binding = store.contract_binding(task_id, candidate_sha) if candidate_sha else None
    if classifying:
        # Preserve the original before the first binding write. A crash may leave
        # authorization without completion, but can never erase requirements.
        record_audit(store, "task.contract_classification_authorized", {
            "task_id": task_id, "old_digest": old_digest, "new_digest": digest,
            "old_canonical_json": original_canonical, "new_canonical_json": canonical,
            "invariant_classification": classification, "candidate_sha": candidate_sha,
            "reason": reason.strip(), "operator": _operator(), "forced": bool(force),
        })
    store.rebind_task_contract(
        task_id,
        CONTRACT_VERSION,
        digest,
        canonical,
        candidate_sha if binding is not None else None,
        capability_digest=str(capability_policy["digest"]),
        capability_json=str(capability_policy["canonical_json"]),
    )
    changes = _resume_at_validate(store, task_id, task)
    # Even an unchanged rebind must repair a stage that has no current validation.
    if (
        candidate_sha is not None
        and store.get_task(task_id)["stage"] in (Stage.REVIEW, Stage.INTEGRATE)
        and not store.has_bound_evidence(task_id, candidate_sha, EvidenceKind.VALIDATION, digest, EvidenceStatus.PASSED)
    ):
        store.advance_task(task_id, Stage.VALIDATE)
        record_audit(store, "task.advance", {
            "task_id": task_id,
            "stage": Stage.VALIDATE,
            "candidate_sha": candidate_sha,
            "reason": "operator rebind: current candidate and contract require passed validation",
        })
        changes["stage_reset"] = True
    payload: dict[str, Any] = {
        "task_id": task_id,
        "old_digest": old_digest,
        "new_digest": digest,
        "old_version": old_version,
        "new_version": CONTRACT_VERSION,
        "old_canonical_json": str(old["canonical_json"]) if old is not None else original_canonical,
        "new_canonical_json": canonical,
        "capability_policy_digest": capability_policy["digest"],
        "invariant_classification": classification,
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
    allow_integrating: bool = False,
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
    if task["stage"] == Stage.INTEGRATE and not allow_integrating:
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
    snapshot: str | None = None

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
        cleanup = execute_preserved_cleanup(
            store,
            project,
            task_id,
            worktree,
            new_base,
            "advance an operator-authorized task baseline after preserving all superseded state",
            snapshot=snapshot,
        )
        if not cleanup.completed:
            raise GitError("preserved baseline cleanup did not complete")
    except GitError as exc:
        raise RecoveryRefusal("git_error", f"task {task_id}: git failed while preserving or resetting the worktree ({str(exc).strip()[:200]}); the ledger was not changed; preserved so far: {preserved}") from exc
    if work.resolve("HEAD") != new_base:
        raise RecoveryRefusal("worktree_reset_failed", f"task {task_id}: worktree is not at {new_base[:12]} after reset; the ledger was not changed; preserved: {preserved}")
    # The operator reset is authorised and everything it replaces is preserved above, so the workspace ledger is rewritten from its own head.
    previous = str(ledger["head"]) if ledger is not None else (ownership.expected_head if ownership is not None else (old_head or new_base))
    moved = record_supervised_move(
        project,
        task_id,
        previous,
        new_base,
        None if latest is None else str(latest["sha"]),
        require_same_dirty=False,
        initialize_missing=True,
    )
    if not moved:
        if old_head is not None:
            execute_preserved_cleanup(
                store,
                project,
                task_id,
                worktree,
                old_head,
                "roll back an unsafe baseline move after preserving the attempted state",
            )
        raise RecoveryRefusal(
            "worktree_reset_unsafe",
            f"task {task_id}: the reset left files StageMesh cannot reseal (ignored paths or embedded repositories); tracked files were put back on {str(old_head)[:12]} and the ledger was not changed; uncommitted or untracked work is in the snapshot ref and can be restored with `git checkout <snapshot> -- .`; preserved: {preserved}",
        )
    if ownership is not None:
        from dataclasses import replace

        save_ownership(store, replace(
            ownership,
            worktree=str(worktree),
            expected_head=new_base,
            tracked_fingerprint=tracked_fingerprint(worktree),
        ))

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
    required_label: str | None = None,
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
        if outside_label_selector(store, task, required_label):
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
    required_label: str | None = None,
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
        if outside_label_selector(store, task, required_label):
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
    required_label: str | None = None,
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
        if outside_label_selector(store, task, required_label):
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
    store: Store, project: Path, *, task_id: str | None = None, limit: int = 1, required_label: str | None = None,
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
        if outside_label_selector(store, task, required_label):
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
        "capabilities": _capabilities_summary(store, project, task_id),
        "validation_failures": validation_failures,
        "review_findings": review_findings,
        "tool_failure": latest_tool_failure(store, task_id),
        "research": _research_summary(store, project, task_id),
        "planning": _planning_summary(store, project, task_id),
        "adverse_review": _adverse_review_summary(store, project, task_id, candidate_sha),
        "e2e": _e2e_summary(store, project, task_id, candidate_sha),
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
        "remediation_budget": store.task_remediation_summary(task_id),
        "recent_repairs": _recent_repairs(store, task_id),
        "integration_recoveries": _integration_recoveries(store, task_id),
        "integration_blocker": _integration_blocker(store, task_id),
        "block": _block(store, task_id) if task["status"] == TaskStatus.BLOCKED else None,
        "timing": task_timing(store, task_id),
    }
    report["recommended_command"], report["recommendation_reason"] = _recommend(report, task_id, ref)
    return report


def _capabilities_summary(store: Store, project: Path, task_id: str) -> dict[str, Any]:
    """Read-only capability truth for task-doctor, without provider-private data."""
    requested: tuple[str, ...] = ()
    frozen = store.task_contract(task_id)
    if frozen is not None:
        try:
            requested = bound_contract_from_record(dict(frozen)).contract.capability_refs
        except (ContractError, TypeError, ValueError) as exc:
            return {
                "task_id": task_id,
                "status": "unavailable",
                "reason": f"invalid frozen contract: {exc}"[:300],
                "requested": [],
                "capabilities": [],
            }
    try:
        return {
            "status": "available",
            **task_capability_summary(
                project, store.conn, task_id, requested=requested
            ),
        }
    except (CapabilityRegistryError, OSError) as exc:
        return {
            "task_id": task_id,
            "status": "unavailable",
            "reason": str(exc)[:300],
            "requested": list(requested),
            "capabilities": [],
        }


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


def _research_summary(store: Store, project: Path, task_id: str) -> dict[str, Any]:
    """Project opt-in and latest bounded research receipt for task-doctor."""
    try:
        configured = load_config(project).research
    except ConfigValidationError as exc:
        return {"required": None, "status": "config-invalid", "reason": str(exc)[:300]}
    row = store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type='research_stage' "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    scope_row = store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type='research_scope.admission' "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    summary: dict[str, Any] = {
        "required": configured is not None,
        "scope_enforced": configured.enforce_scope if configured is not None else False,
        "configured_provider": configured.provider if configured is not None else None,
        "status": "pending" if configured is not None else "not-required",
        "observed": row is not None,
        "reason": (
            "no research receipt has been recorded yet"
            if configured is not None
            else "the project has no research provider configured"
        ),
    }
    if row is None:
        return summary
    payload = _loads(row["payload"])
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    inspected = payload.get("inspected_paths")
    finding_count = payload.get("finding_count")
    summary.update(
        status=str(payload.get("outcome") or "unknown")[:80],
        at=row["created_at"],
        researcher=str(payload.get("researcher") or "")[:100] or None,
        reason=str(payload.get("reason") or "")[:300] or None,
        execution_id=str(payload.get("execution_id") or "") or None,
        baseline_sha=str(payload.get("baseline_sha") or "")[:64] or None,
        contract_digest=str(payload.get("contract_digest") or "")[:128] or None,
        finding_count=(
            finding_count
            if isinstance(finding_count, int)
            and not isinstance(finding_count, bool)
            and finding_count >= 0
            else 0
        ),
        inspected_path_count=len(inspected) if isinstance(inspected, list) else 0,
        unresolved_scope_gap_count=len(report.get("unresolved_scope_gaps", []))
        if isinstance(report.get("unresolved_scope_gaps"), list)
        else 0,
        recommended_path_count=(
            len(report.get("recommended_implementation_paths", []))
            if isinstance(report.get("recommended_implementation_paths"), list)
            else 0
        ),
        no_touch_path_count=(
            len(report.get("no_touch_paths", []))
            if isinstance(report.get("no_touch_paths"), list)
            else 0
        ),
    )
    if scope_row is not None:
        scope = _loads(scope_row["payload"])
        summary["scope_admission"] = {
            "at": scope_row["created_at"],
            "candidate_sha": str(scope.get("candidate_sha") or "")[:64] or None,
            "allowed": scope.get("allowed") if isinstance(scope.get("allowed"), bool) else None,
            "reason": str(scope.get("reason") or "")[:100] or None,
            "report_digest": str(scope.get("report_digest") or "")[:128] or None,
            "changed_path_count": scope.get("changed_path_count")
            if isinstance(scope.get("changed_path_count"), int)
            and not isinstance(scope.get("changed_path_count"), bool)
            else None,
            "outside_recommendation_count": len(scope.get("outside_recommendation", []))
            if isinstance(scope.get("outside_recommendation"), list)
            else 0,
            "no_touch_violation_count": len(scope.get("no_touch_violations", []))
            if isinstance(scope.get("no_touch_violations"), list)
            else 0,
        }
    return summary


def _planning_summary(store: Store, project: Path, task_id: str) -> dict[str, Any]:
    """Read-only planning gate state without exposing spec contents."""
    spec_path, approval_path = planning_artifact_paths(project, task_id)
    challenge_path = planning_challenge_path(project, task_id)
    try:
        config = load_config(project)
    except ConfigValidationError as exc:
        return {"required": None, "status": "config-invalid", "reason": str(exc)[:300]}
    required = config.require_approved_spec
    challenge_required = config.require_plan_challenge
    spec_payload, approval = load_planning_artifacts(project, task_id)
    challenge_payload = load_planning_challenge(project, task_id)
    execution_receipts = planning_execution_receipts(store, task_id)
    summary: dict[str, Any] = {
        "required": required,
        "challenge_required": challenge_required,
        "status": "not-required",
        "spec_path": spec_path.relative_to(project).as_posix(),
        "approval_path": approval_path.relative_to(project).as_posix(),
        "challenge_path": challenge_path.relative_to(project).as_posix(),
        "spec_present": spec_payload is not None,
        "approval_present": approval is not None,
        "challenge_present": challenge_path.is_file(),
        "planner_execution": execution_receipts["planner"],
        "challenger_execution": execution_receipts["challenger"],
        "next_action": None,
        "reason": "" if required else "the approved-spec gate is disabled",
        "approved_scope": [],
    }
    if not required:
        return summary
    contract = store.task_contract(task_id)
    baseline = store.task_baseline(task_id)
    if contract is None or baseline is None:
        summary.update(status="identity-unavailable", reason="task contract or baseline is not bound yet")
        return summary
    decision = evaluate_planning_gate(
        enabled=True,
        spec_payload=spec_payload,
        approval=approval,
        contract_hash=str(contract["digest"]),
        baseline_sha=baseline,
        require_challenge=challenge_required,
        challenge_payload=challenge_payload,
    )
    statuses = {
        NEXT_WRITE_SPEC: "missing" if spec_payload is None else "invalid",
        NEXT_RESOLVE_DECISIONS: "needs-decision",
        NEXT_REFRESH_SPEC: "stale",
        NEXT_CHALLENGE_SPEC: "challenge-missing"
        if not challenge_path.is_file()
        else "challenge-invalid",
        NEXT_REFRESH_CHALLENGE: "challenge-stale",
        NEXT_REVISE_SPEC: "revision-required",
        NEXT_RESOLVE_CHALLENGE_DECISIONS: "challenge-needs-decision",
        NEXT_APPROVE_SPEC: "awaiting-approval",
    }
    summary.update(
        status="approved" if decision.allowed else statuses.get(decision.next_action, "blocked"),
        next_action=decision.next_action,
        reason=decision.reason,
        approved_scope=list(decision.approved_scope),
    )
    return summary


def _adverse_review_summary(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str | None,
) -> dict[str, Any]:
    """Exact-candidate adverse gate state and bounded role evidence."""
    try:
        policy = load_config(project).adverse_review
    except ConfigValidationError as exc:
        return {"required": True, "status": "config-invalid", "reason": str(exc)[:300]}
    summary: dict[str, Any] = {
        "required": True,
        "status": "pending",
        "candidate_sha": candidate_sha,
        "policy_digest": policy.digest(),
        "blocking_severities": list(policy.blocking_severities),
        "unavailable_reviewer": policy.unavailable_reviewer,
        "provider": None,
        "verdict": None,
        "findings_by_severity": {},
        "reasons": [],
        "override_used": False,
    }
    if candidate_sha is None:
        return summary
    binding = store.contract_binding(task_id, candidate_sha)
    if binding is None:
        summary.update(status="identity-unavailable", reasons=["candidate contract is not bound"])
        return summary
    digest = str(binding["digest"])
    decision = adverse_gate(store, task_id, candidate_sha, digest, policy)
    rows = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
        "AND status IN (?, ?) ORDER BY created_at DESC, rowid DESC",
        (
            task_id,
            candidate_sha,
            EvidenceKind.REVIEW,
            EvidenceStatus.PASSED,
            EvidenceStatus.FAILED,
        ),
    ).fetchall()
    payload = next(
        (
            parsed
            for row in rows
            if (parsed := _loads(row["payload"])).get("contract_hash") == digest
            and parsed.get("adverse_review_gate") is True
        ),
        None,
    )
    if payload is not None:
        roles = payload.get("review_roles") if isinstance(payload.get("review_roles"), dict) else {}
        evidence = roles.get("adverse") if isinstance(roles.get("adverse"), dict) else {}
        counts: dict[str, int] = {}
        findings = evidence.get("findings") if isinstance(evidence.get("findings"), list) else []
        for finding in findings:
            if isinstance(finding, dict):
                severity = str(finding.get("severity") or "unknown")[:40]
                counts[severity] = counts.get(severity, 0) + 1
        summary.update(
            provider=str(evidence.get("provider") or payload.get("review_provider") or "")[:100] or None,
            verdict=adverse_outcome(evidence, policy),
            findings_by_severity=counts,
        )
    override = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='adverse_review.override' "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.candidate_sha')=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.contract_hash')=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.policy_digest')=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, digest, policy.digest()),
    ).fetchone()
    override_used = override is not None
    summary.update(
        status="overridden" if override_used else "passed" if decision.allowed else "blocked",
        reasons=list(decision.reasons),
        override_used=override_used,
    )
    return summary


def _e2e_summary(
    store: Store,
    project: Path,
    task_id: str,
    candidate_sha: str | None,
) -> dict[str, Any]:
    """Latest exact-candidate browser/API evaluation without reading artifact contents."""
    try:
        services = discover_validation_services(project)
    except ValidationServiceError as exc:
        return {
            "required": None,
            "status": "config-invalid",
            "candidate_sha": candidate_sha,
            "reason": str(exc)[:300],
            "configured_services": [],
            "evaluations": [],
            "artifact_paths": [],
        }
    configured = [
        {"name": service.name, "kind": service.kind}
        for service in services.services
        if service.kind in {"browser", "api"}
    ]
    required = bool(configured)
    summary: dict[str, Any] = {
        "required": required,
        "status": "pending" if required else "not-required",
        "candidate_sha": candidate_sha,
        "service_fingerprint": services.fingerprint,
        "configured_services": configured,
        "evaluations": [],
        "artifact_root": None,
        "artifact_paths": [],
        "policy": {"status": "pending", "policies": [], "failure_signatures": []},
        "repeated_failure_count": 0,
        "first_actionable_error": None,
        "reason": "no exact-candidate E2E evaluation evidence is recorded" if required else "no browser or API evaluation service is configured",
    }
    if candidate_sha is None:
        return summary
    row = store.conn.execute(
        "SELECT status, payload, created_at FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, EvidenceKind.VALIDATION),
    ).fetchone()
    if row is None:
        return summary
    payload = _loads(row["payload"])
    policy = e2e_policy_projection(payload)
    raw_evaluations = payload.get("e2e_evaluations")
    if not isinstance(raw_evaluations, list):
        single = payload.get("e2e_evidence")
        raw_evaluations = [single] if isinstance(single, dict) else []
    evaluations: list[dict[str, Any]] = []
    artifacts: list[str] = []
    fingerprints: set[str] = set()
    failed_commands: set[tuple[str, ...]] = set()
    for item in raw_evaluations[:16]:
        if not isinstance(item, dict) or item.get("candidate_sha") != candidate_sha:
            continue
        command = item.get("command") if isinstance(item.get("command"), list) else []
        result = str(item.get("result") or "UNKNOWN")[:20]
        if result == "FAIL":
            failed_commands.add(tuple(str(part) for part in command))
        fingerprint = str(item.get("service_fingerprint") or "")[:64]
        if fingerprint:
            fingerprints.add(fingerprint)
        refs = item.get("artifacts") if isinstance(item.get("artifacts"), list) else []
        paths = [
            str(ref.get("path"))[:500]
            for ref in refs
            if isinstance(ref, dict) and isinstance(ref.get("path"), str)
        ][:32]
        artifacts.extend(paths)
        evaluations.append(
            {
                "kind": str(item.get("kind") or "unknown")[:40],
                "result": result,
                "command": [str(part)[:200] for part in command[:32]],
                "artifact_paths": paths,
            }
        )
    root = payload.get("e2e_artifact_root")
    if isinstance(root, str):
        try:
            summary["artifact_root"] = Path(root).resolve().relative_to(project.resolve()).as_posix()
        except (OSError, ValueError):
            summary["artifact_root"] = None
    first_error = None
    for finding in payload.get("findings", []):
        if not isinstance(finding, dict):
            continue
        command = finding.get("command") if isinstance(finding.get("command"), list) else []
        message = str(finding.get("message") or "")
        if (
            finding.get("code") == "invalid_e2e_evidence"
            or "e2e" in message.casefold()
            or tuple(str(part) for part in command) in failed_commands
        ):
            first_error = {
                key: str(finding[key])[:600]
                for key in ("code", "message", "failure_kind", "stderr", "failure_signature")
                if finding.get(key) is not None
            }
            break
    results = {item["result"] for item in evaluations}
    signatures = set(policy["failure_signatures"])
    repeated_failure_count = 0
    if signatures:
        counts = dict.fromkeys(signatures, 0)
        rows = store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND kind=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 50",
            (task_id, EvidenceKind.VALIDATION),
        ).fetchall()
        for historic in rows:
            historic_signatures = set(
                e2e_policy_projection(_loads(historic["payload"]))["failure_signatures"]
            )
            for signature in signatures & historic_signatures:
                counts[signature] += 1
        repeated_failure_count = max(counts.values(), default=0)
    stale = bool(fingerprints and fingerprints != {services.fingerprint})
    summary.update(
        at=row["created_at"],
        validation_status=str(row["status"]),
        evaluations=evaluations,
        artifact_paths=artifacts[:32],
        first_actionable_error=first_error,
        policy=policy,
        repeated_failure_count=repeated_failure_count,
        status=(
            "stale"
            if stale
            else "failed"
            if "FAIL" in results or (required and not evaluations and str(row["status"]) == str(EvidenceStatus.FAILED))
            else "passed"
            if evaluations and results <= {"PASS"}
            else summary["status"]
        ),
        reason=(
            "service configuration changed after the recorded evaluation"
            if stale
            else (first_error or {}).get("message")
            if first_error
            else None
            if evaluations
            else summary["reason"]
        ),
    )
    return summary


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
    tool_failure = report.get("tool_failure")
    if tool_failure and (tool_failure.get("recovery") or {}).get("action") == "stop":
        action = str((tool_failure.get("recovery") or {}).get("operator_action") or tool_failure["next_action"])
        command = f"stagemesh retry-task --task {task_id}" if report["status"] == "BLOCKED" else ""
        return command, f"tool recovery stopped: {action} Resolve this condition before continuing."
    if report["status"] == "BLOCKED":
        block = report.get("block")
        if block and block["kind"] == "BLOCKED_EXTERNAL_DEPENDENCY":
            return f"stagemesh retry-task --task {task_id}", f"external condition not met: {block['reason']}; resolve condition before retrying"
        if block and block["kind"] == "BLOCKED_HUMAN_DECISION" and "objective root" in str(block["reason"]):
            return "stagemesh plan", f"task {task_id} is an objective root ({block['reason']}); plan child tasks instead"
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
    capabilities = report.get("capabilities") or {}
    if capabilities.get("status") != "available":
        lines.append(
            "  capabilities: unavailable"
            + (f" ({capabilities['reason']})" if capabilities.get("reason") else "")
        )
    else:
        rows = capabilities.get("capabilities") or []
        requested = capabilities.get("requested") or []
        observed = [
            row for row in rows
            if (row.get("usage") or {}).get("status") != "not-observed"
        ]
        lines.append(
            f"  capabilities: {capabilities.get('registry_count', 0)} registered; "
            f"{len(rows)} task-relevant; "
            f"{len(requested)} requested; {len(observed)} observed; "
            f"policy={capabilities.get('skill_evidence_mode', 'unknown')}; "
            f"binding={(capabilities.get('policy_binding') or {}).get('state', 'unknown')}"
        )
        for row in rows:
            usage = row.get("usage") or {}
            if not row.get("requested") and usage.get("status") == "not-observed":
                continue
            registration = "registered" if row.get("registered") else "UNREGISTERED"
            lines.append(
                f"    - {row['id']}: {registration}; "
                f"{'requested; ' if row.get('requested') else ''}usage={usage.get('status', 'not-observed')}"
                + (
                    f"; evidence-missing={usage['evidence_missing']}"
                    if usage.get("evidence_missing")
                    else ""
                )
            )
    if report["validation_failures"]:
        lines.append("  latest validation failures:")
        lines.extend(f"    - [{f.get('code', '?')}] {f.get('message', '')}" for f in report["validation_failures"])
    if report["review_findings"]:
        lines.extend(format_findings(report["review_findings"]))
    tool_failure = report.get("tool_failure")
    if tool_failure:
        command = tool_failure["command"]
        shown = command["text"] or command["executable"] or "unknown command"
        lines.append(f"  latest tool failure: {tool_failure['kind']} ({tool_failure['status']})")
        lines.append(f"    command: {shown}")
        if tool_failure["cwd"]:
            lines.append(f"    cwd: {tool_failure['cwd']}")
        if tool_failure["excerpt"]:
            lines.append(f"    evidence: {tool_failure['excerpt']}")
        lines.append(f"    operator action: {tool_failure['next_action']}")
        recovery = tool_failure.get("recovery") or {}
        if recovery.get("action"):
            lines.append(f"    recovery: {recovery['action']}" + (f" ({recovery['execution_status']})" if recovery.get("execution_status") else ""))
    research = report.get("research") or {}
    lines.append(
        f"  research: {research.get('status', 'unknown')}"
        + (f" by {research['researcher']}" if research.get("researcher") else "")
        + (f"; findings={research['finding_count']}" if research.get("observed") else "")
        + (f"; reason={research['reason']}" if research.get("reason") else "")
    )
    planning = report.get("planning") or {}
    lines.append(
        f"  planning: {planning.get('status', 'unknown')}"
        + (f"; next={planning['next_action']}" if planning.get("next_action") else "")
        + (f"; reason={planning['reason']}" if planning.get("reason") else "")
    )
    adverse = report.get("adverse_review") or {}
    adverse_counts = ",".join(
        f"{severity}:{count}" for severity, count in sorted((adverse.get("findings_by_severity") or {}).items())
    )
    lines.append(
        f"  adverse review: {adverse.get('status', 'unknown')}"
        + (f" by {adverse['provider']}" if adverse.get("provider") else "")
        + (f"; verdict={adverse['verdict']}" if adverse.get("verdict") else "")
        + (f"; findings={adverse_counts}" if adverse_counts else "")
        + ("; override=yes" if adverse.get("override_used") else "")
    )
    e2e = report.get("e2e") or {}
    lines.append(
        f"  e2e: {e2e.get('status', 'unknown')}"
        + (f"; evaluations={len(e2e['evaluations'])}" if e2e.get("evaluations") else "")
        + (f"; artifacts={len(e2e['artifact_paths'])}" if e2e.get("artifact_paths") else "")
        + (
            f"; policy={e2e['policy']['status']}; declared-checks="
            f"{sum(len(item['requirements']) for item in e2e['policy']['policies'])}"
            if (e2e.get("policy") or {}).get("policies")
            else ""
        )
        + (
            f"; repeated-failure={e2e['repeated_failure_count']}"
            if e2e.get("repeated_failure_count")
            else ""
        )
        + (f"; reason={e2e['reason']}" if e2e.get("reason") else "")
    )
    d = report["diagnosis"]
    lines.append(f"  diagnosis: {d['category']} at {d['stage']}: {d['summary']}" if d else "  diagnosis: none")
    budget = report.get("remediation_budget") or {}
    lines.append(
        f"  remediation budget: current={budget.get('current_attempts', 0)}; "
        f"cumulative={budget.get('cumulative_attempts', 0)}"
    )
    for stage in budget.get("stages", []):
        latest = stage.get("latest") or {}
        correlation = (
            f"; retry={latest.get('retry_state') or 'unbound'}"
            + (f"; execution={latest['execution_id']}" if latest.get("execution_id") else "")
        )
        lines.append(
            f"    - {stage['stage']}: epoch={stage['current_epoch']}; "
            f"current={stage['current_attempts']}; cumulative={stage['cumulative_attempts']}"
            f"{correlation}"
        )
    if report.get("block"):
        lines.append(f"  block: {report['block']['kind']}: {report['block']['reason']}" + (" (run-window reconsiders it automatically)" if report["block"]["automatic_reentry"] else ""))
    for recovery in report.get("integration_recoveries", []):
        lines.append(f"  integration recovery #{recovery['attempt']}: {recovery['category']} -> {recovery['action']} (candidate {str(recovery['candidate_sha'])[:7]})")
    if report.get("integration_blocker"):
        from .integration_recovery import format_blocker

        lines.append("  integration blocker: " + format_blocker(report["integration_blocker"]))
    for repair in report["recent_repairs"]:
        lines.append(f"  repair: {repair['event']} by {repair['operator']}: {repair['reason']}")
    if report["timing"]["executions"]:
        lines.extend("  " + line for line in format_task_summary(report["timing"]).splitlines())
    lines.append(f"  next: {report['recommended_command'] or '(nothing)'}  # {report['recommendation_reason']}")
    return "\n".join(lines)


def _block(store: Store, task_id: str) -> dict[str, Any] | None:
    from .block_kinds import BlockKind, classify_block

    info = classify_block(store, task_id)
    if info is None:
        return None
    return {"kind": info.kind.value, "reason": info.reason, "automatic_reentry": info.kind is BlockKind.RECOVERABLE_OPERATIONAL}


def _integration_recoveries(store: Store, task_id: str) -> list[dict[str, Any]]:
    from .integration_recovery import recoveries

    return recoveries(store, task_id)


def _integration_blocker(store: Store, task_id: str) -> dict[str, Any] | None:
    from .integration_recovery import integration_blocker

    return integration_blocker(store, task_id)


def _queue_control_summary(store: Store) -> dict[str, Any]:
    from .queue_visibility import queue_control_report

    return queue_control_report(store)
