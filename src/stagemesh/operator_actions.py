from __future__ import annotations

import getpass
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .contracts import candidate_hygiene_findings, changed_files
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, Stage, TaskStatus
from .execution_ownership import (
    execution_tree_unresolved,
    recovery_identity_state,
    tree_cleanup_state,
    validation_tree_unresolved,
)
from .git import GitError, GitWorkspace
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .process_identity import classify_process, process_identity
from .review_identity import (
    IMPLEMENTATION_DERIVATION,
    IMPLEMENTATION_IDENTITY,
    command_snapshot,
    implementation_identity,
    record_implementation_identity,
)
from .validation import Validator
from .workspace_guard import pin_candidate


class OperatorActionError(ValueError):
    pass


def _age(now: float, created_at: float | None) -> float | None:
    return round(now - created_at, 3) if created_at is not None else None


def _process_state(store: Store, execution_id: str, pid: int | None) -> str:
    execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    saved = store.execution_process_identity(execution_id)
    parent_state = classify_process(saved, process_identity(saved.pid))
    return recovery_identity_state(execution, parent_state)


def task_details(store: Store, task_id: str, now: float | None = None) -> dict[str, Any]:
    """Decision-ready view of one task: candidate, latest evidence, active claim and running executions."""
    now = time.time() if now is None else now
    task = store.get_task(task_id)
    candidate = store.latest_candidate(task_id)
    details: dict[str, Any] = {
        "latest_candidate": None,
        "latest_validation": None,
        "latest_review": None,
        "latest_integration": None,
        "source_state": None,
        "source_reason": None,
        "active_claim": None,
        "active_executions": [],
    }
    if task is not None and task["source_id"] is not None:
        state = store.source_state(str(task["source"]), str(task["source_id"]))
        details["source_state"] = state or None
        reason = state.get("retirement_reason")
        if isinstance(reason, str) and reason:
            details["source_reason"] = reason
    if candidate is not None:
        sha = str(candidate["sha"])
        details["latest_candidate"] = {
            "sha": sha,
            "producer": candidate["produced_by"],
            "durable_handoff": bool(candidate["durable_handoff"]),
            "created_at": candidate["created_at"],
        }
        for key, kind in (("latest_validation", EvidenceKind.VALIDATION), ("latest_review", EvidenceKind.REVIEW), ("latest_integration", EvidenceKind.INTEGRATION)):
            row = store.conn.execute(
                "SELECT status, created_at FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (task_id, sha, kind),
            ).fetchone()
            if row is not None:
                details[key] = {"status": row["status"], "candidate_sha": sha, "created_at": row["created_at"]}
    executions = [dict(row) for row in store.running_executions() if row["task_id"] == task_id]
    claim = store.conn.execute("SELECT * FROM claims WHERE task_id=? AND active=1", (task_id,)).fetchone()
    if claim is not None:
        linked = next((e for e in executions if e["claim_id"] == claim["id"]), None)
        details["active_claim"] = {
            "id": claim["id"],
            "stage": claim["stage"],
            "worker_id": claim["worker_id"],
            "pid": linked["pid"] if linked else None,
            "lease_expires_at": claim["lease_expires_at"],
            "lease_expired": claim["lease_expires_at"] < now,
            "age_seconds": _age(now, claim["created_at"]),
        }
    details["active_executions"] = [
        {
            "id": e["id"],
            "kind": e["kind"],
            "status": e["status"],
            "pid": e["pid"],
            "candidate_sha": e["candidate_sha"],
            "claim_id": e["claim_id"],
            "age_seconds": _age(now, e["started_at"]),
            "process_state": _process_state(store, e["id"], e["pid"]),
            **({"cleanup_state": tree_cleanup_state(e)} if execution_tree_unresolved(e) else {}),
        }
        for e in executions
    ]
    return details


@dataclass(frozen=True)
class RecoveryAction:
    execution_id: str
    kind: str
    pid: int | None
    process_state: str
    action: str  # RELEASED | SKIPPED_LIVE | SKIPPED_UNKNOWN | NOT_RECOVERABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "kind": self.kind,
            "pid": self.pid,
            "process_state": self.process_state,
            "action": self.action,
        }


def recover_stale(store: Store, task_id: str) -> list[RecoveryAction]:
    """Release RUNNING executions of a task only when their saved process identity is provably DEAD."""
    if store.get_task(task_id) is None:
        raise OperatorActionError(f"task does not exist: {task_id}")
    actions: list[RecoveryAction] = []
    for execution in [row for row in store.running_executions() if row["task_id"] == task_id]:
        eid = str(execution["id"])
        state = _process_state(store, eid, execution["pid"])
        if state == "LIVE":
            action = "SKIPPED_LIVE"
        elif state != "DEAD":
            action = "SKIPPED_UNKNOWN"
        else:
            reason = "OPERATOR_RECOVER_STALE_DEAD_PROCESS"
            if store.has_active_claim_for_execution(eid) and execution["kind"] == ExecutionKind.IMPLEMENTATION:
                released = store.recover_stale_execution_claim(eid, reason)
            else:
                released = store.mark_orphan_running_execution_failed(eid, reason)
            action = "RELEASED" if released else "NOT_RECOVERABLE"
        actions.append(RecoveryAction(eid, str(execution["kind"]), execution["pid"], state, action))
    return actions


RELEASE_UNKNOWN_REASON = "OPERATOR_RELEASE_UNKNOWN_IDENTITY"


def release_unknown_execution(store: Store, task_id: str, execution_id: str, reason: str) -> RecoveryAction:
    """Terminalize ONE running execution whose process identity cannot be established, on explicit operator instruction.

    `recover_stale` never touches such executions: a missing pid or create time proves nothing about liveness. This is the
    deliberate, inspected override. It refuses LIVE processes (never released) and provably DEAD ones (use `recover_stale`),
    marks the execution FAILED (the row and its history stay), releases the claim of an implementation execution, and records
    who did what and why in the audit log.
    """
    reason = (reason or "").strip()
    if not reason:
        raise OperatorActionError("a non-empty --reason is required: say what you inspected that shows the execution is dead")
    task = store.get_task(task_id)
    if task is None:
        raise OperatorActionError(f"task does not exist: {task_id}")
    execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    if execution is None or str(execution["task_id"]) != task_id:
        raise OperatorActionError(f"execution {execution_id} does not belong to task {task_id}")
    if execution["status"] != "RUNNING":
        raise OperatorActionError(f"execution {execution_id} is {execution['status']}, not RUNNING; nothing to release")
    state = _process_state(store, execution_id, execution["pid"])
    if state == "LIVE":
        raise OperatorActionError(f"execution {execution_id} is LIVE (pid {execution['pid']}); a live execution is never released")
    if state == "DEAD":
        raise OperatorActionError(
            f"execution {execution_id} is provably DEAD; use plain `recover-stale --task {task_id}` instead of the unknown-identity override"
        )
    kind = str(execution["kind"])
    payload = {
        "task_id": task_id, "execution_id": execution_id,
        "previous_status": str(execution["status"]), "execution_kind": kind,
        "task_stage": str(task["stage"]), "task_status": str(task["status"]),
        "claim_id": execution["claim_id"], "pid": execution["pid"], "process_state": state,
        "reason": reason, "operator": _operator(), "operator_action": "RELEASE_UNKNOWN_EXECUTION",
    }
    if execution_tree_unresolved(execution):
        payload["process_tree_scope"] = execution["process_tree_scope"]
        payload["process_identity"] = {
            key: execution[key] for key in ("pid", "process_create_time", "boot_id", "executable")
        }
        release = (store.release_inspected_validation_execution if validation_tree_unresolved(execution)
                   else store.release_inspected_execution_tree)
        released = release(execution_id, task_id, payload)
        action = "RELEASED_BY_OPERATOR" if released else "NOT_RECOVERABLE"
        if not released:
            record_audit(store, "recovery.operator_release_unknown", {**payload, "outcome": action})
        return RecoveryAction(execution_id, kind, execution["pid"], state, action)
    if kind == ExecutionKind.IMPLEMENTATION and store.has_active_claim_for_execution(execution_id):
        released = store.recover_stale_execution_claim(execution_id, RELEASE_UNKNOWN_REASON)
    else:
        released = store.mark_orphan_running_execution_failed(execution_id, RELEASE_UNKNOWN_REASON)
    action = "RELEASED_BY_OPERATOR" if released else "NOT_RECOVERABLE"
    record_audit(store, "recovery.operator_release_unknown", {**payload, "outcome": action})
    return RecoveryAction(execution_id, kind, execution["pid"], state, action)


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - an unnamed operator must not block recovery
        return "unknown"


def adopt_candidate(
    store: Store,
    project: Path,
    task_id: str,
    sha: str,
    producer: str,
    *,
    validate: bool = False,
) -> dict[str, Any]:
    """Register an existing commit as the task's latest durable candidate after verifying it."""
    task = store.get_task(task_id)
    if task is None:
        raise OperatorActionError(f"task does not exist: {task_id}")
    if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
        raise OperatorActionError(f"task is done: {task_id}")
    if store.conn.execute("SELECT 1 FROM claims WHERE task_id=? AND active=1", (task_id,)).fetchone():
        raise OperatorActionError(f"task has an active claim; run recover-stale first: {task_id}")
    workspace = GitWorkspace(project)
    try:
        resolved = workspace.run("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").stdout.strip()
    except GitError as exc:
        raise OperatorActionError(f"commit does not exist in {project}: {sha}") from exc
    baseline = store.task_baseline(task_id)
    if baseline is None:
        raise OperatorActionError(f"task has no recorded baseline to verify against: {task_id}")
    if resolved == baseline:
        raise OperatorActionError("candidate equals the task baseline")
    if workspace.run("merge-base", "--is-ancestor", baseline, resolved, check=False).returncode != 0:
        raise OperatorActionError(f"candidate {resolved} is not based on task baseline {baseline}")
    changed = changed_files(project, resolved, baseline)
    if not changed:
        raise OperatorActionError("candidate has no changes relative to the task baseline")
    noise = candidate_hygiene_findings(project, resolved, changed)
    if noise:
        paths = ", ".join(str(item["path"]) for item in noise)
        raise OperatorActionError(f"candidate contains cache/noise files: {paths}")
    latest = store.latest_candidate(task_id)
    known = store.conn.execute("SELECT 1 FROM candidates WHERE task_id=? AND sha=?", (task_id, resolved)).fetchone()
    already_latest = latest is not None and latest["sha"] == resolved
    if known and not already_latest:
        raise OperatorActionError(
            f"candidate {resolved} is already registered and is not the latest; refusing to reorder history"
        )
    if not already_latest:
        store.add_candidate(task_id, resolved, producer, True)
        pin_candidate(project, task_id, resolved)  # the operator's registration is an authorized change of candidate
        record_audit(
            store,
            "candidate.adopted",
            {
                "task_id": task_id,
                "candidate_sha": resolved,
                "producer": producer,
                "baseline_sha": baseline,
                "provider_invoked": False,
                "changed_files": changed,
            },
        )
        store.advance_task(task_id, Stage.VALIDATE)
        record_audit(store, "task.advance", {"task_id": task_id, "stage": Stage.VALIDATE, "candidate_sha": resolved})
    else:
        pin_candidate(project, task_id, resolved)  # repeat adoption may be needed after a local operator repair moved the worktree HEAD
    report: dict[str, Any] = {
        "task_id": task_id,
        "candidate_sha": resolved,
        "producer": producer,
        "baseline_sha": baseline,
        "changed_files": changed,
        "already_latest": already_latest,
        "validation": None,
    }
    if validate:
        status = Validator().validate(store, task_id, resolved, project)
        report["validation"] = str(status)
        if status is EvidenceStatus.PASSED and store.get_task(task_id)["stage"] == Stage.VALIDATE:
            bound = contract_for_candidate(store, task_id, resolved, project)
            if store.has_bound_evidence(task_id, resolved, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.PASSED):
                decision = evidence_allows_advance(
                    current=Stage.VALIDATE,
                    candidate_sha=resolved,
                    evidence_sha=resolved,
                    kind=EvidenceKind.VALIDATION,
                    status=EvidenceStatus.PASSED,
                )
                store.advance_task(task_id, decision.target)
                record_audit(
                    store,
                    "task.advance",
                    {"task_id": task_id, "stage": decision.target, "candidate_sha": resolved, "reason": decision.reason},
                )
    task = store.get_task(task_id)
    report["stage"], report["status"] = task["stage"], task["status"]
    return report


def _same_executable(recorded: str, configured: str) -> bool:
    """Fail closed: the configured command must resolve to exactly the recorded executable path."""
    if not Path(recorded).is_absolute():
        return False
    try:
        found = shutil.which(configured)
        if not found:
            return False
        return os.path.normcase(os.path.realpath(found)) == os.path.normcase(os.path.realpath(recorded))
    except (OSError, ValueError):
        return False


def recover_implementation_identity(
    store: Store, task_id: str, candidate_sha: str, execution_id: str, reason: str, adapters: list[Any],
) -> dict[str, Any]:
    """Re-establish the frozen implementer identity of a legacy candidate from proven execution evidence.

    Never invokes a provider, never alters the candidate or the execution, and does not touch review requirements: the
    recovered identity is the same event a fresh implementation would have recorded, so independent review still applies.
    """
    reason = (reason or "").strip()
    if not reason:
        raise OperatorActionError("a non-empty --reason is required: say what evidence shows this execution produced the candidate")
    if store.get_task(task_id) is None:
        raise OperatorActionError(f"task does not exist: {task_id}")
    candidate = store.conn.execute("SELECT * FROM candidates WHERE task_id=? AND sha=?", (task_id, candidate_sha)).fetchone()
    if candidate is None:
        raise OperatorActionError(f"candidate {candidate_sha} is not registered for task {task_id}; the exact sha is required")
    producer = str(candidate["produced_by"])
    execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    if execution is None:
        raise OperatorActionError(f"execution does not exist: {execution_id}")
    if (execution["kind"] != ExecutionKind.IMPLEMENTATION or execution["status"] != ExecutionStatus.SUCCEEDED
            or execution["task_id"] != task_id or execution["candidate_sha"] != candidate_sha
            or execution["actor"] != producer):
        raise OperatorActionError(
            f"execution {execution_id} is not a SUCCEEDED IMPLEMENTATION execution of {producer} that produced "
            f"candidate {candidate_sha} for task {task_id}"
        )
    others = store.conn.execute(
        "SELECT id FROM executions WHERE task_id=? AND kind=? AND status=? AND candidate_sha=? AND id!=?",
        (task_id, ExecutionKind.IMPLEMENTATION, ExecutionStatus.SUCCEEDED, candidate_sha, execution_id),
    ).fetchall()
    if others:
        raise OperatorActionError(f"ambiguous evidence: other successful implementation executions produced {candidate_sha}")
    matching = [a for a in adapters if getattr(a, "name", None) == producer]
    if len(matching) != 1:
        raise OperatorActionError(f"provider {producer} is not configured exactly once")
    adapter = matching[0]
    if "code" not in getattr(adapter, "capabilities", ()):
        raise OperatorActionError(f"provider {producer} has no configured implementation command")
    configured = (
        getattr(adapter, "implement_command", None)
        if getattr(adapter, "implement_command", None) is not None
        else getattr(adapter, "command", None)
    )
    if not configured and hasattr(adapter, "command_for"):
        configured = adapter.command_for("code")
    if not configured:
        raise OperatorActionError(f"provider {producer} has no configured implementation command")
    recorded = execution["executable"]
    if not isinstance(recorded, str) or not recorded or not _same_executable(recorded, configured[0]):
        raise OperatorActionError(
            f"execution {execution_id} recorded executable does not match the configured {producer} command"
        )
    command = adapter.command_for("code") if hasattr(adapter, "command_for") else configured
    if not command:
        raise OperatorActionError(f"provider {producer} has no configured implementation command")
    try:
        all_cmds = adapter.all_commands() if hasattr(adapter, "all_commands") else (command,)
        snapshot = command_snapshot(producer, command, all_cmds)
    except ValueError as exc:
        raise OperatorActionError(f"provider {producer} command identity is unusable: {exc}") from exc
    report: dict[str, Any] = {
        "task_id": task_id, "candidate_sha": candidate_sha, "execution_id": execution_id, "provider": producer,
        "command_digest": snapshot["command_digest"], "alias_digests": snapshot["alias_digests"],
        "provider_invoked": False, "recovered": False,
    }
    existing = implementation_identity(store, task_id, candidate_sha)
    if existing is not None and existing["command_digest"] == snapshot["command_digest"]:
        return report  # idempotent: the identity is already proven and identical
    prior = store.conn.execute(
        "SELECT 1 FROM audit_events WHERE event_type IN (?, ?) "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.candidate_sha')=? LIMIT 1",
        (IMPLEMENTATION_IDENTITY, IMPLEMENTATION_DERIVATION, task_id, candidate_sha),
    ).fetchone()
    if existing is not None or prior is not None:
        raise OperatorActionError(f"candidate {candidate_sha} already has conflicting or unresolved identity evidence; refusing to add more")
    record_audit(store, "candidate.implementation_identity_reconstructed", {
        "task_id": task_id, "candidate_sha": candidate_sha, "execution_id": execution_id, "provider": producer,
        "command_digest": snapshot["command_digest"], "alias_digests": snapshot["alias_digests"],
        "reason": reason, "operator": _operator(), "operator_action": "RECOVER_IMPLEMENTATION_IDENTITY",
        "provider_invoked": False,
    })
    record_implementation_identity(store, task_id, candidate_sha, execution_id, snapshot)
    report["recovered"] = True
    return report
