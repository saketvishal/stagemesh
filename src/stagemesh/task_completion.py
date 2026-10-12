from __future__ import annotations

import json
from typing import Any

from .baseline import resolve_integration_ref
from .domain import EvidenceKind
from .git import GitWorkspace
from .persistence import Store

OWNER_STATE_EVENT = "task.owner_state"


def task_owner_state(store: Store, task_id: str, project: object | None = None) -> dict[str, Any]:
    """Durable parent view of a task, derived from StageMesh state rather than a worker chat."""
    task = store.get_task(task_id)
    if task is None:
        return {"task_id": task_id, "missing": True}
    candidate = store.latest_candidate(task_id)
    sha = str(candidate["sha"]) if candidate is not None else None
    integration_ref, integration_sha = _integration_state(project)
    state: dict[str, Any] = {
        "task_id": task_id,
        "objective": str(task["title"]),
        "stage": str(task["stage"]),
        "status": str(task["status"]),
        "baseline_sha": store.task_baseline(task_id),
        "integration_ref": integration_ref,
        "integration_sha": integration_sha,
        "candidate_sha": sha,
        "candidate_provider": str(candidate["produced_by"]) if candidate is not None else None,
        "durable_handoff": bool(candidate["durable_handoff"]) if candidate is not None else False,
        "accepted_work": _accepted_work(store, task_id, sha),
        "validation": _latest_evidence(store, task_id, sha, EvidenceKind.VALIDATION),
        "review": _latest_evidence(store, task_id, sha, EvidenceKind.REVIEW),
        "integration": _latest_evidence(store, task_id, sha, EvidenceKind.INTEGRATION),
        "unresolved_findings": _open_findings(store, task_id, sha),
        "fixes_attempted": _fixes_attempted(store, task_id),
        "recovery_history": _recovery_history(store, task_id),
    }
    state["current_problem"] = _current_problem(state)
    state["next_action"] = _next_action(state)
    state["human_decision_required"] = _human_decision_required(state)
    return state


def record_task_owner_state(store: Store, task_id: str, reason: str, project: object | None = None) -> dict[str, Any]:
    state = task_owner_state(store, task_id, project)
    store.add_audit_event(OWNER_STATE_EVENT, {"reason": reason, **state})
    return state


def _integration_state(project: object | None) -> tuple[str | None, str | None]:
    if project is None:
        return None, None
    try:
        from pathlib import Path

        root = Path(project)
        ref = resolve_integration_ref(root)
        if not ref:
            return None, None
        result = GitWorkspace(root).run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return ref, result.stdout.strip() or None
    except Exception:  # noqa: BLE001 - owner-state snapshots must never break the lifecycle.
        return None, None


def _latest_evidence(store: Store, task_id: str, sha: str | None, kind: EvidenceKind) -> dict[str, Any] | None:
    if sha is None:
        return None
    row = store.conn.execute(
        "SELECT status, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, sha, kind),
    ).fetchone()
    if row is None:
        return None
    payload = _loads(row["payload"])
    return {"status": str(row["status"]), "findings": _compact_findings(payload.get("findings")), "summary": _summary(payload)}


def _accepted_work(store: Store, task_id: str, sha: str | None) -> list[str]:
    if sha is None:
        return []
    accepted: list[str] = []
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        if store.conn.execute(
            "SELECT 1 FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status='PASSED' LIMIT 1",
            (task_id, sha, kind),
        ).fetchone():
            accepted.append(str(kind))
    return accepted


def _open_findings(store: Store, task_id: str, sha: str | None) -> list[dict[str, str]]:
    if sha is None:
        return []
    return [
        {"id": str(row["id"]), "severity": str(row["severity"]), "message": str(row["message"])}
        for row in store.open_findings_for_candidate(task_id, sha)
    ]


def _fixes_attempted(store: Store, task_id: str) -> list[dict[str, str]]:
    rows = store.conn.execute(
        "SELECT stage, candidate_sha FROM task_remediations WHERE task_id=? ORDER BY created_at, rowid",
        (task_id,),
    ).fetchall()
    return [{"stage": str(r["stage"]), "candidate_sha": str(r["candidate_sha"])} for r in rows[-20:]]


def _recovery_history(store: Store, task_id: str) -> list[dict[str, Any]]:
    events = {
        "task.rebaselined",
        "task.baseline_advanced",
        "task.contract_rebound",
        "task.auto_retried_provider_failure",
        "task.parent_budget_reopened",
        "task.integration_reconciliation_queued",
    }
    history: list[dict[str, Any]] = []
    for row in store.conn.execute("SELECT event_type, payload FROM audit_events ORDER BY created_at DESC, rowid DESC LIMIT 200"):
        if row["event_type"] not in events:
            continue
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            history.append({"event": str(row["event_type"]), **payload})
        if len(history) >= 20:
            break
    return list(reversed(history))


def _current_problem(state: dict[str, Any]) -> str | None:
    for key in ("integration", "review", "validation"):
        evidence = state.get(key)
        if isinstance(evidence, dict) and evidence.get("status") == "FAILED":
            return f"{key}_failed"
    if state.get("stage") == "IMPLEMENT" and state.get("fixes_attempted"):
        return "implementation_remediation_pending"
    return None


def _next_action(state: dict[str, Any]) -> str:
    if state.get("status") == "DONE" or state.get("stage") == "DONE":
        return "done"
    if state.get("current_problem") in {"validation_failed", "review_failed", "integration_failed"}:
        return "implement_fix_from_recorded_findings"
    if state.get("stage") == "PLAN":
        return "advance_to_implementation"
    return f"continue_{str(state.get('stage')).lower()}"


def _human_decision_required(state: dict[str, Any]) -> bool:
    return state.get("status") == "BLOCKED" and state.get("current_problem") is None


def _summary(payload: dict[str, Any]) -> str:
    findings = _compact_findings(payload.get("findings"))
    if findings:
        return "; ".join(f["message"] for f in findings[:3])[:600]
    return str(payload.get("decision") or "")[:200]


def _compact_findings(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    findings: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "code": str(item.get("code") or ""),
                "severity": str(item.get("severity") or ""),
                "message": str(item.get("message") or "")[:600],
                "path": str(item.get("path") or ""),
            }
        )
    return findings


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
