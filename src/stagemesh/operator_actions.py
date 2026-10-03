from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .contracts import candidate_hygiene_findings, changed_files
from .domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage, TaskStatus
from .git import GitError, GitWorkspace
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .process_identity import classify_process, process_identity
from .validation import Validator


class OperatorActionError(ValueError):
    pass


def _age(now: float, created_at: float | None) -> float | None:
    return round(now - created_at, 3) if created_at is not None else None


def _process_state(store: Store, execution_id: str, pid: int | None) -> str:
    return classify_process(store.execution_process_identity(execution_id), process_identity(pid))


def task_details(store: Store, task_id: str, now: float | None = None) -> dict[str, Any]:
    """Decision-ready view of one task: candidate, latest evidence, active claim and running executions."""
    now = time.time() if now is None else now
    candidate = store.latest_candidate(task_id)
    details: dict[str, Any] = {
        "latest_candidate": None,
        "latest_validation": None,
        "latest_review": None,
        "active_claim": None,
        "active_executions": [],
    }
    if candidate is not None:
        sha = str(candidate["sha"])
        details["latest_candidate"] = {
            "sha": sha,
            "producer": candidate["produced_by"],
            "durable_handoff": bool(candidate["durable_handoff"]),
            "created_at": candidate["created_at"],
        }
        for key, kind in (("latest_validation", EvidenceKind.VALIDATION), ("latest_review", EvidenceKind.REVIEW)):
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
