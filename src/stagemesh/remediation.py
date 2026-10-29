from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .persistence import Store
from .task_completion import task_owner_state


def bounded_failure_text(value: object, limit: int = 2500, secrets: tuple[str, ...] = ()) -> str:
    """Redact complete diagnostics before taking a bounded tail for persistence or prompts."""
    from .redaction import SECRET_MARKERS, redact_text

    known = [v for k, v in os.environ.items() if any(m in k.lower() for m in SECRET_MARKERS)]
    text = redact_text(str(value or ""), sorted((*known, *secrets), key=len, reverse=True))
    text = re.sub(r"(?i)(bearer\s+)\S+|((?:token|secret|password|passwd|api[_-]?key|authorization)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+",
                  lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    return text[-limit:]


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


def findings_to_verify(store: Store, task_id: str, candidate_sha: str) -> list[dict[str, object]]:
    """Concrete findings of the candidate being remediated that a review of `candidate_sha` must show resolved."""
    latest = store.latest_task_remediation(task_id)
    if latest is None or str(latest["candidate_sha"]) == candidate_sha:
        return []
    return [
        {"id": r["id"], "severity": r["severity"], "message": r["message"]}
        for r in store.open_findings_for_candidate(task_id, str(latest["candidate_sha"]))
    ]


def unresolved_findings(store: Store, task_id: str, candidate_sha: str) -> list[dict[str, object]]:
    """Findings handed to remediation that the new candidate's own review raised again (same message, still OPEN)."""
    current = {str(r["message"]) for r in store.open_findings_for_candidate(task_id, candidate_sha)}
    return [f for f in findings_to_verify(store, task_id, candidate_sha) if f["message"] in current]


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
                        item[k] = bounded_failure_text(ev_f[k])
        gates = ev_payload.get("gates", [])
        if isinstance(gates, list):
            for g in gates:
                if isinstance(g, dict) and g.get("status") not in (None, "PASSED"):
                    failed_gates.append(
                        {
                            "name": g.get("name"),
                            "returncode": g.get("returncode"),
                            "command": [bounded_failure_text(p, 500) for p in g.get("command", [])],
                            "duration_seconds": g.get("duration_seconds"),
                            "failure_kind": g.get("failure_kind"),
                            "stderr": bounded_failure_text(g.get("stderr_tail") or g.get("stderr"), 1500),
                            "stdout": bounded_failure_text(g.get("stdout_tail") or g.get("stdout")),
                        }
                    )
    context: dict[str, object] = {
        "stage": str(latest["stage"]),
        "candidate_sha": str(latest["candidate_sha"]),
        "findings": raw_findings,
        "task_owner_state": task_owner_state(store, task_id, project),
    }
    failure_details = _latest_failure_details(store, task_id, str(latest["candidate_sha"]))
    if failure_details:
        context["failure_details"] = failure_details
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


def _latest_failure_details(store: Store, task_id: str, candidate_sha: str) -> list[dict[str, object]]:
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND status='FAILED' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return []
    details: list[dict[str, object]] = []
    for finding in payload.get("findings", []):
        if not isinstance(finding, dict):
            continue
        detail = {
            key: finding[key]
            for key in ("code", "message", "command", "returncode", "failure_kind")
            if key in finding
        }
        for key in ("stdout", "stderr"):
            value = finding.get(key)
            if isinstance(value, str) and value:
                detail[key] = value[-4000:]
        if detail:
            details.append(detail)
    return details[:20]


def _latest_diagnosis(store: Store, task_id: str, candidate_sha: str) -> dict[str, object] | None:
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


class ToolRecoveryRefused(RuntimeError):
    """A recovery gate changed after planning, before any recovery command launched."""


def tool_recovery_plan(
    store: Store, task_id: str, evidence: dict[str, object], setup_actions: dict[str, list[str]] | None = None
) -> dict[str, object]:
    """Recommend recovery without treating a recorded plan as an executed action.

    Only an execution-aware caller can act on this plan. Once that caller records a recovery receipt, a repeated
    signature stops rather than advancing from retry to setup or claiming another attempt.
    """
    from .diagnosis import (
        TOOL_FAILURE_EVENT,
        TOOL_RECOVERY_EVENT,
        plan_tool_recovery,
        tool_failure_signature,
    )

    signature = tool_failure_signature(evidence)
    attempted, execution_status, refused_executions = _tool_recovery_attempts(
        store, task_id, signature, TOOL_RECOVERY_EVENT
    )
    if attempted:
        return {
            **plan_tool_recovery(evidence, attempted, setup_actions=setup_actions),
            "execution_status": execution_status,
        }
    recommendations: deque[str] = deque(maxlen=20)
    recommendation_count = 0
    for row in store.task_audit_events(TOOL_FAILURE_EVENT, task_id):
        if row["event_type"] != TOOL_FAILURE_EVENT:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        prior = payload.get("evidence")
        if payload.get("task_id") != task_id or not isinstance(prior, dict) or tool_failure_signature(prior) != signature:
            continue
        if str(payload.get("execution_id") or "") in refused_executions:
            continue
        recovery = prior.get("recovery")
        action = recovery.get("action") if isinstance(recovery, dict) else None
        recommendations.append(action if action in ("retry_once", "setup", "stop") else "unknown")
        recommendation_count += 1
    if recommendations:
        return {
            "action": "stop",
            "signature": signature,
            "attempted": [],
            "previous_recommendations": list(recommendations),
            "previous_recommendation_count": recommendation_count,
            "execution_status": "NOT_CONFIRMED",
            "operator_action": (
                "This failure signature was already recorded; recovery execution is not confirmed. "
                "Verify what ran before retrying or running configured setup. "
                + str(evidence.get("next_action") or "Inspect the failure evidence.")
            ),
        }
    return {**plan_tool_recovery(evidence, setup_actions=setup_actions), "execution_status": "NOT_CONFIRMED"}


def _tool_recovery_attempts(
    store: Store, task_id: str, signature: str, event_type: str
) -> tuple[list[str], str, set[str]]:
    attempts: dict[str, tuple[str, str, str]] = {}
    execution_status = "NOT_CONFIRMED"
    for row in store.task_audit_events(event_type, task_id):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("task_id") != task_id or payload.get("signature") != signature:
            continue
        action = payload.get("action")
        if action in ("retry", "setup"):
            row_id = row["id"] if "id" in row else len(attempts)
            attempt_id = str(payload.get("attempt_id") or row_id)
            execution_status = str(payload.get("status") or "UNKNOWN")
            attempts[attempt_id] = (
                str(action), execution_status, str(payload.get("execution_id") or "")
            )
    refused_executions = {
        execution_id for _action, status, execution_id in attempts.values()
        if status == "REFUSED" and execution_id
    }
    return (
        [action for action, status, _execution_id in attempts.values() if status != "REFUSED"],
        execution_status,
        refused_executions,
    )


def execute_tool_recovery(
    store: Store,
    task_id: str,
    execution_id: str,
    evidence: dict[str, object],
    plan: dict[str, object],
    *,
    retry: Callable[[], bool] | None = None,
    run_setup: Callable[[tuple[str, ...]], bool] | None = None,
    setup_actions: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Execute one pre-authorized recovery action and durably account for it before invocation."""
    from .audit import record_audit
    from .diagnosis import TOOL_RECOVERY_EVENT, plan_tool_recovery, tool_failure_signature

    signature = tool_failure_signature(evidence)
    attempted, execution_status, _refused_executions = _tool_recovery_attempts(
        store, task_id, signature, TOOL_RECOVERY_EVENT
    )
    if attempted:
        from .diagnosis import plan_tool_recovery

        return {
            **plan_tool_recovery(evidence, attempted, setup_actions=setup_actions),
            "execution_status": execution_status,
        }
    authorized = plan_tool_recovery(evidence, (), setup_actions=setup_actions)
    if (
        plan.get("signature") != signature
        or plan.get("action") != authorized.get("action")
        or plan.get("setup_command") != authorized.get("setup_command")
    ):
        return {
            **authorized,
            "action": "stop",
            "operator_action": "Recovery plan is stale or does not match the recorded failure; inspect before retrying.",
        }

    action = str(plan.get("action") or "")
    callback: Callable[[], bool] | None = retry
    attempted = "retry"
    setup_command: tuple[str, ...] = ()
    if action == "setup":
        raw = plan.get("setup_command")
        if not isinstance(raw, list) or not raw or not all(isinstance(part, str) and part for part in raw):
            return {**plan, "action": "stop", "operator_action": "Configured setup command is invalid."}
        setup_command = tuple(raw)
        callback = (lambda: bool(run_setup(setup_command))) if run_setup is not None else None
        attempted = "setup"
    elif action != "retry_once":
        return plan
    if callback is None:
        return {**plan, "execution_status": "NOT_CONFIRMED"}

    attempt_id = str(uuid.uuid4())
    started = {
        "task_id": task_id,
        "execution_id": execution_id,
        "signature": signature,
        "attempt_id": attempt_id,
        "action": attempted,
        "status": "STARTED",
    }
    recovery_event_id = record_audit(store, TOOL_RECOVERY_EVENT, started)
    try:
        succeeded = bool(callback())
        status = "SUCCEEDED" if succeeded else "FAILED"
        failure = None
    except ToolRecoveryRefused as exc:
        status = "REFUSED"
        failure = str(exc)
        record_audit(store, TOOL_RECOVERY_EVENT, {
            **started,
            "status": status,
            "recovery_event_id": recovery_event_id,
            "failure": failure,
        })
        return {
            **plan,
            "attempted": [],
            "execution_status": status,
            "recovery_event_id": recovery_event_id,
            "failure": failure,
        }
    except Exception as exc:  # record before preserving the caller's original safety/error semantics
        status = "FAILED"
        failure = type(exc).__name__
        record_audit(store, TOOL_RECOVERY_EVENT, {
            **started,
            "status": status,
            "recovery_event_id": recovery_event_id,
            "failure": failure,
        })
        raise
    record_audit(store, TOOL_RECOVERY_EVENT, {
        **started, "status": status, "recovery_event_id": recovery_event_id, "failure": failure,
    })
    result = {
        **plan,
        "attempted": [attempted],
        "execution_status": status,
        "recovery_event_id": recovery_event_id,
    }
    if failure:
        result["failure"] = failure
    return result
