from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import record_audit
from .baseline import resolve_integration_ref
from .domain import EvidenceKind, EvidenceStatus
from .git import GitWorkspace
from .persistence import Store

OWNER_STATE_EVENT = "task.owner_state"


def task_owner_state(store: Store, task_id: str, project: Path | None = None) -> dict[str, Any]:
    task = store.get_task(task_id)
    candidate = store.latest_candidate(task_id)
    candidate_sha = str(candidate["sha"]) if candidate is not None else None
    integration_ref = resolve_integration_ref(project) if project is not None else None
    state: dict[str, Any] = {
        "task_id": task_id,
        "objective": None if task is None else str(task["title"]),
        "stage": None if task is None else str(task["stage"]),
        "status": None if task is None else str(task["status"]),
        "baseline_sha": store.task_baseline(task_id),
        "integration_ref": integration_ref,
        "integration_sha": _integration_sha(project, integration_ref),
        "candidate_sha": candidate_sha,
        "candidate_provider": None if candidate is None else str(candidate["produced_by"]),
        "durable_handoff": None if candidate is None else bool(candidate["durable_handoff"]),
        "accepted_work": _accepted_work(store, task_id),
        "validation": _latest_evidence(store, task_id, candidate_sha, EvidenceKind.VALIDATION),
        "review": _latest_evidence(store, task_id, candidate_sha, EvidenceKind.REVIEW),
        "integration": _latest_evidence(store, task_id, candidate_sha, EvidenceKind.INTEGRATION),
        "unresolved_findings": _open_findings(store, task_id, candidate_sha=candidate_sha),
        "historical_open_findings": _open_findings(store, task_id, exclude_candidate_sha=candidate_sha),
        "review_findings": _open_findings(store, task_id, EvidenceKind.REVIEW, candidate_sha=candidate_sha),
        "fixes_attempted": _fixes_attempted(store, task_id),
        "recovery_history": _recovery_history(store, task_id),
        "block": _block_summary(store, task_id),
    }
    state["current_problem"] = _current_problem(state)
    state["human_decision_required"] = _human_decision_required(state)
    state["next_action"] = _next_action(state)
    return state


def record_task_owner_state(store: Store, task_id: str, reason: str, project: Path | None = None) -> dict[str, Any]:
    state = task_owner_state(store, task_id, project)
    record_audit(store, OWNER_STATE_EVENT, {"reason": reason, **state})
    return state


def _integration_sha(project: Path | None, integration_ref: str | None) -> str | None:
    if project is None or integration_ref is None:
        return None
    result = GitWorkspace(project).run("rev-parse", "--verify", "--quiet", f"{integration_ref}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _latest_evidence(store: Store, task_id: str, candidate_sha: str | None, kind: EvidenceKind) -> dict[str, Any] | None:
    if candidate_sha is None:
        return None
    row = store.conn.execute(
        "SELECT status, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, kind),
    ).fetchone()
    if row is None:
        return None
    payload = _loads(row["payload"])
    return {
        "status": str(row["status"]),
        "summary": _summary(payload),
        "contract_hash": payload.get("contract_hash"),
        "findings": _compact_findings(payload.get("findings")),
    }


def _accepted_work(store: Store, task_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT candidate_sha, kind, payload FROM evidence WHERE task_id=? AND status=? ORDER BY created_at, rowid",
        (task_id, EvidenceStatus.PASSED),
    )
    return [
        {"candidate_sha": str(row["candidate_sha"]), "kind": str(row["kind"]), "summary": _summary(_loads(row["payload"]))}
        for row in rows
    ]


def _open_findings(
    store: Store,
    task_id: str,
    source: EvidenceKind | None = None,
    *,
    candidate_sha: str | None = None,
    exclude_candidate_sha: str | None = None,
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT id, candidate_sha, severity, message FROM findings WHERE task_id=? AND status='OPEN' ORDER BY created_at, rowid",
        (task_id,),
    )
    found = [
        {"id": str(row["id"]), "candidate_sha": str(row["candidate_sha"]), "severity": str(row["severity"]), "message": str(row["message"])}
        for row in rows
    ]
    if candidate_sha is not None:
        found = [finding for finding in found if finding["candidate_sha"] == candidate_sha]
    if exclude_candidate_sha is not None:
        found = [finding for finding in found if finding["candidate_sha"] != exclude_candidate_sha]
    found = [finding for finding in found if _active_failed_kind(store, task_id, finding["candidate_sha"]) is not None]
    if source is None:
        return found
    return [finding for finding in found if _active_failed_kind(store, task_id, finding["candidate_sha"]) == source]


def _active_failed_kind(store: Store, task_id: str, candidate_sha: str) -> EvidenceKind | None:
    row = store.conn.execute(
        "SELECT kind, status FROM evidence WHERE task_id=? AND candidate_sha=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha),
    ).fetchone()
    if row is None or row["status"] != EvidenceStatus.FAILED:
        return None
    try:
        return EvidenceKind(str(row["kind"]))
    except ValueError:
        return None


def _fixes_attempted(store: Store, task_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT stage, candidate_sha, cleared FROM task_remediations WHERE task_id=? ORDER BY created_at, rowid",
        (task_id,),
    )
    return [
        {"stage": str(row["stage"]), "candidate_sha": str(row["candidate_sha"]), "cleared": bool(row["cleared"])}
        for row in rows
    ]


def _recovery_history(store: Store, task_id: str) -> list[dict[str, Any]]:
    wanted = {
        "integration.recovery",
        "recovery.decision",
        "task.baseline_advanced",
        "task.rebaselined",
        "task.parent_budget_reopened",
        "task.remediation_exhausted",
        "task.integration_reconciliation_queued",
    }
    history: list[dict[str, Any]] = []
    for row in store.conn.execute("SELECT event_type, payload FROM audit_events ORDER BY created_at, rowid"):
        event_type = str(row["event_type"])
        if event_type not in wanted:
            continue
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            history.append({"event": event_type, "summary": _summary(payload), "payload": payload})
    return history[-20:]


def _current_problem(state: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("integration", "review", "validation"):
        evidence = state.get(key)
        if isinstance(evidence, dict) and evidence.get("status") == EvidenceStatus.FAILED:
            return {"kind": f"{key}_failed", "summary": evidence.get("summary"), "findings": evidence.get("findings") or []}
    if state.get("unresolved_findings"):
        return {"kind": "open_findings", "findings": state["unresolved_findings"]}
    return None


def _next_action(state: dict[str, Any]) -> str:
    if state.get("human_decision_required"):
        return "await_human_decision"
    stage = str(state.get("stage") or "")
    return {
        "PLAN": "advance_to_implementation",
        "IMPLEMENT": "implement_or_fix_from_parent_state",
        "VALIDATE": "run_scoped_validation",
        "REVIEW": "run_independent_review",
        "INTEGRATE": "integrate_exact_reviewed_candidate",
        "DONE": "done",
    }.get(stage, "recover_task_state")


def _human_decision_required(state: dict[str, Any]) -> bool:
    block = state.get("block")
    if isinstance(block, dict):
        return block.get("kind") == "BLOCKED_HUMAN_DECISION"
    problem = state.get("current_problem") or {}
    findings = problem.get("findings") if isinstance(problem, dict) else []
    for finding in findings or []:
        message = str(finding.get("message") if isinstance(finding, dict) else finding).lower()
        if "decision" in message or "judgment" in message or "choose" in message:
            return True
    return False


def _block_summary(store: Store, task_id: str) -> dict[str, Any] | None:
    from .block_kinds import classify_block

    info = classify_block(store, task_id)
    if info is None:
        return None
    return {"kind": info.kind.value, "reason": info.reason, "source": info.source}


def _summary(payload: dict[str, Any]) -> str | None:
    for key in ("summary", "reason", "message", "detail", "why_automatic_recovery_stopped"):
        value = payload.get(key)
        if value:
            return str(value)[:500]
    findings = _compact_findings(payload.get("findings"))
    if findings:
        return "; ".join(str(finding.get("message", "")) for finding in findings)[:500]
    return None


def _compact_findings(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    findings = []
    for item in raw:
        if isinstance(item, dict):
            findings.append({k: item.get(k) for k in ("code", "severity", "message", "path") if item.get(k) is not None})
        else:
            findings.append({"message": str(item)})
    return findings[:10]


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
