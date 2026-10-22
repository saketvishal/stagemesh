from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from .persistence import Store
from .task_completion import task_owner_state


class RemediationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    identity: str
    candidate_sha: str
    severity: str
    message: str
    status: str = "OPEN"


def finding_identity(candidate_sha: str, message: str, path: str | None = None) -> str:
    candidate_sha = _validate_text(candidate_sha, "candidate sha")
    message = _validate_text(message, "finding message", 2000)
    path = _validate_optional_text(path, "finding path", 1000)
    raw = f"{candidate_sha}\0{path or ''}\0{message}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class RemediationPolicy:
    def __init__(self, max_attempts: int = 3):
        if not isinstance(max_attempts, int):
            raise RemediationValidationError("remediation max attempts must be an integer")
        if max_attempts < 1:
            raise RemediationValidationError("remediation max attempts must be at least 1")
        self.max_attempts = max_attempts

    def should_remediate(self, store: Store, finding_id: str) -> bool:
        finding = store.get_finding(finding_id)
        if finding is None or finding["status"] != "OPEN":
            return False
        return store.remediation_attempt_count(finding_id) < self.max_attempts

    def record_attempt(self, store: Store, finding_id: str, status: str, payload: dict[str, object] | None = None) -> str:
        return store.add_remediation_attempt(finding_id, status, payload or {"attempted_at": time.time()})


def recorded_findings(store: Store, task_id: str, candidate_sha: str) -> list[dict[str, object]]:
    """The exact findings recorded against a candidate, verbatim and in the order they were raised (all statuses)."""
    failed = store.conn.execute(
        "SELECT kind FROM evidence WHERE task_id=? AND candidate_sha=? AND status='FAILED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha),
    ).fetchone()
    source = str(failed["kind"]) if failed is not None else None
    rows = store.conn.execute(
        "SELECT id, severity, message, status FROM findings WHERE task_id=? AND candidate_sha=? ORDER BY created_at, rowid", (task_id, candidate_sha)
    )
    return [
        {"id": r["id"], "severity": r["severity"], "message": r["message"], "status": r["status"], "candidate_sha": candidate_sha, "source": source}
        for r in rows
    ]


def latest_candidate_findings(store: Store, task_id: str, open_only: bool = True) -> list[dict[str, object]]:
    candidate = store.latest_candidate(task_id)
    if candidate is None:
        return []
    found = recorded_findings(store, task_id, str(candidate["sha"]))
    return [f for f in found if f["status"] == "OPEN"] if open_only else found


def remediation_context(store: Store, task_id: str, project: Path | None = None) -> dict[str, object] | None:
    """Persisted failure context for the most recent remediation of a task, or None for a first attempt."""
    latest = store.latest_task_remediation(task_id)
    if latest is None:
        return None
    findings = store.open_findings_for_candidate(task_id, str(latest["candidate_sha"]))
    raw_findings = [{"id": row["id"], "severity": row["severity"], "message": row["message"]} for row in findings]
    evidence_row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND status='FAILED' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, str(latest["candidate_sha"])),
    ).fetchone()
    failed_gates: list[dict[str, object]] = []
    if evidence_row is not None:
        import json
        try:
            ev_payload = json.loads(evidence_row["payload"])
        except (TypeError, ValueError):
            ev_payload = {}
        ev_findings = {f.get("message"): f for f in ev_payload.get("findings", []) if isinstance(f, dict)}
        for item in raw_findings:
            msg = item.get("message")
            if msg in ev_findings:
                ev_f = ev_findings[msg]
                for k in ("stdout", "stderr", "failure_kind", "code", "path"):
                    if ev_f.get(k):
                        item[k] = ev_f[k]
        gates = ev_payload.get("gates", [])
        if isinstance(gates, list):
            for g in gates:
                if isinstance(g, dict) and g.get("status") not in (None, "PASSED"):
                    failed_gates.append(
                        {
                            "name": g.get("name"),
                            "returncode": g.get("returncode"),
                            "command": g.get("command"),
                            "duration_seconds": g.get("duration_seconds"),
                            "stderr": (g.get("stderr_tail") or g.get("stderr") or "")[:1500],
                            "stdout": (g.get("stdout_tail") or g.get("stdout") or "")[-2500:],
                        }
                    )
    context: dict[str, object] = {
        "stage": str(latest["stage"]),
        "candidate_sha": str(latest["candidate_sha"]),
        "findings": raw_findings,
        "task_owner_state": task_owner_state(store, task_id, project),
    }
    if failed_gates:
        context["failed_gates"] = failed_gates
    diagnosis = _latest_diagnosis(store, task_id, str(latest["candidate_sha"]))
    if diagnosis is not None:
        context["diagnosis"] = diagnosis
    from .remediation_recovery import narrowed_note

    note = narrowed_note(store, task_id)
    if note is not None:
        context["recovery_note"] = note
    return context


def _latest_diagnosis(store: Store, task_id: str, candidate_sha: str) -> dict[str, object] | None:
    import json

    rows = store.conn.execute("SELECT payload FROM audit_events WHERE event_type='task.diagnosis' ORDER BY created_at DESC, rowid DESC LIMIT 50")
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if payload.get("task_id") == task_id and payload.get("candidate_sha") == candidate_sha:
            return {k: payload.get(k) for k in ("category", "summary", "recommendation", "provider_analysis")}
    return None


def _validate_text(value: str, field: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemediationValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise RemediationValidationError(f"{field} must be {max_length} characters or fewer")
    return normalized


def _validate_optional_text(value: str | None, field: str, max_length: int = 200) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field, max_length)


def tool_recovery_plan(
    store: Store, task_id: str, evidence: dict[str, object], setup_actions: dict[str, list[str]] | None = None
) -> dict[str, object]:
    """Bounded recovery decision for new tool failure evidence, consuming this task's earlier evidence for the signature.

    Each earlier occurrence of the same signature is treated as one spent recovery (retry first, then configured
    setup), so a repeated signature reaches ``stop`` instead of looping. Other tasks' evidence is never consulted.
    """
    import json

    from .diagnosis import TOOL_FAILURE_EVENT, plan_tool_recovery, tool_failure_signature

    signature = tool_failure_signature(evidence)
    attempted: list[str] = []
    rows = store.audit_events(200)
    for row in reversed(rows):
        if row["event_type"] != TOOL_FAILURE_EVENT:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        prior = payload.get("evidence") if isinstance(payload, dict) else None
        if payload.get("task_id") != task_id or not isinstance(prior, dict) or tool_failure_signature(prior) != signature:
            continue
        recovery = prior.get("recovery")
        taken = recovery.get("action") if isinstance(recovery, dict) else None
        attempted.append({"retry_once": "retry", "setup": "setup"}.get(str(taken), "stop"))
    return plan_tool_recovery(evidence, [a for a in attempted if a != "stop"], setup_actions)
