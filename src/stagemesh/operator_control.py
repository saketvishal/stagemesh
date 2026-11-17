"""Bounded loopback operator actions with signed, current-state-bound intents.

This module does not expose transport or remote-hosting policy.  The existing
activity projections remain read-only; an explicitly enabled local server may
use this contract to plan and confirm the small allowlist below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from .audit import record_audit
from .config import AdverseReviewPolicy
from .domain import TaskStatus
from .objective_roots import objective_root_reason
from .operator import execution_runtime_row
from .operator_actions import OperatorActionError, release_unknown_execution
from .persistence import Store
from .queue_control import QUEUE_CONTROL_EVENT, latest_queue_control
from .recovery import resume_task
from .scheduling import _source_refusal_reason

CONTROL_CONTRACT = "stagemesh.operator-control/1"
CONTROL_RECEIPT_EVENT = "operator.control.receipt"
CONTROL_TTL_SECONDS = 120
MAX_CONTROL_BODY_BYTES = 8 * 1024
ALLOWED_ACTIONS = frozenset(
    {"task_retry", "queue_pause", "queue_resume", "release_unknown_execution"}
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
_TASK_ID = re.compile(r"[^\x00-\x1f\x7f]{1,200}")


class OperatorControlError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class OperatorIntent:
    token: str
    action: str
    task_id: str | None
    execution_id: str | None
    reason: str | None
    idempotency_key: str
    issued_at: int
    expires_at: int
    state_digest: str
    target: str
    preconditions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": CONTROL_CONTRACT,
            "intent": self.token,
            "action": self.action,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "state_digest": self.state_digest,
            "target": self.target,
            "preconditions": list(self.preconditions),
        }


class OperatorControl:
    """Plan and confirm only typed actions against one project store."""

    def __init__(
        self,
        project_id: str,
        secret: str,
        *,
        now: Callable[[], float] = time.time,
        ttl_seconds: int = CONTROL_TTL_SECONDS,
        adverse_policy: AdverseReviewPolicy | None = None,
    ):
        if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
            raise ValueError("operator control project_id must be a bounded identifier")
        if not isinstance(secret, str) or len(secret) < 16:
            raise ValueError("operator control secret must contain at least 16 characters")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("operator control ttl_seconds must be between 1 and 300")
        self.project_id = project_id
        self._secret = secret.encode("utf-8")
        self._now = now
        self.ttl_seconds = ttl_seconds
        self.adverse_policy = adverse_policy
        self._confirm_lock = Lock()

    @property
    def csrf_token(self) -> str:
        return hmac.new(self._secret, b"stagemesh-control-csrf/v1", hashlib.sha256).hexdigest()

    def capabilities(self) -> dict[str, Any]:
        return {
            "contract": CONTROL_CONTRACT,
            "enabled": True,
            "project_id": self.project_id,
            "actions": sorted(ALLOWED_ACTIONS),
            "ttl_seconds": self.ttl_seconds,
            "csrf_token": self.csrf_token,
        }

    def plan(self, store: Store, request: object) -> OperatorIntent:
        payload = _request(
            request,
            fields={"action", "task_id", "execution_id", "reason", "idempotency_key"},
        )
        action = payload.get("action")
        if action not in ALLOWED_ACTIONS:
            raise OperatorControlError("action_not_allowed", "operator action is not allowed")
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not _IDENTIFIER.fullmatch(idempotency_key):
            raise OperatorControlError(
                "invalid_idempotency_key", "idempotency_key must be a bounded identifier"
            )
        task_id = payload.get("task_id")
        execution_id = payload.get("execution_id")
        reason = payload.get("reason")
        if action == "task_retry":
            if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
                raise OperatorControlError("invalid_task_id", "task_retry requires a bounded task_id")
            if execution_id is not None or reason is not None:
                raise OperatorControlError(
                    "unexpected_execution", "task_retry does not accept execution_id or reason"
                )
            _require_retry_eligible(store, task_id)
        elif action == "release_unknown_execution":
            if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
                raise OperatorControlError(
                    "invalid_task_id", "release_unknown_execution requires a bounded task_id"
                )
            if not isinstance(execution_id, str) or not _IDENTIFIER.fullmatch(execution_id):
                raise OperatorControlError(
                    "invalid_execution_id",
                    "release_unknown_execution requires a bounded execution_id",
                )
            reason = _reason(reason)
            _require_unknown_release_eligible(store, task_id, execution_id)
        elif task_id is not None or execution_id is not None or reason is not None:
            raise OperatorControlError(
                "unexpected_target", "queue actions do not accept task_id, execution_id, or reason"
            )
        _require_queue_transition(store, str(action))
        issued = int(self._now())
        state_digest = _state_digest(store, str(action), task_id, execution_id)
        signed = {
            "version": 1,
            "project_id": self.project_id,
            "action": action,
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "issued_at": issued,
            "expires_at": issued + self.ttl_seconds,
            "state_digest": state_digest,
            "nonce": secrets.token_hex(16),
        }
        token = self._sign(signed)
        return OperatorIntent(
            token=token,
            action=str(action),
            task_id=task_id if isinstance(task_id, str) else None,
            execution_id=execution_id if isinstance(execution_id, str) else None,
            reason=reason if isinstance(reason, str) else None,
            idempotency_key=idempotency_key,
            issued_at=issued,
            expires_at=issued + self.ttl_seconds,
            state_digest=state_digest,
            target=(
                f"execution:{execution_id}"
                if execution_id is not None
                else f"task:{task_id}"
                if task_id is not None
                else f"queue:{self.project_id}"
            ),
            preconditions=_preconditions(str(action)),
        )

    def confirm(self, store: Store, token: object) -> dict[str, Any]:
        payload = self._verify(token)
        with self._confirm_lock:
            return self._confirm_verified(store, token, payload)

    def _confirm_verified(
        self,
        store: Store,
        token: object,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        idempotency_key = str(payload["idempotency_key"])
        fingerprint = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        prior = _receipt(store, idempotency_key)
        if prior is not None:
            if prior.get("intent_fingerprint") != fingerprint:
                raise OperatorControlError(
                    "idempotency_conflict",
                    "idempotency_key is already bound to a different intent",
                    status=409,
                )
            return {**prior, "replayed": True}
        action = str(payload["action"])
        task_id = payload.get("task_id")
        execution_id = payload.get("execution_id")
        reason = payload.get("reason")
        if _state_digest(store, action, task_id, execution_id) != payload["state_digest"]:
            raise OperatorControlError(
                "stale_intent", "recorded project state changed after the plan", status=409
            )
        result = _execute(
            store,
            action,
            task_id,
            execution_id,
            reason,
            self.adverse_policy,
        )
        receipt = {
            "contract": CONTROL_CONTRACT,
            "status": "ACCEPTED",
            "project_id": self.project_id,
            "action": action,
            "task_id": task_id,
            "execution_id": execution_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "intent_fingerprint": fingerprint,
            "result": result,
            "recorded_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "replayed": False,
        }
        record_audit(store, CONTROL_RECEIPT_EVENT, receipt)
        return receipt

    def receipt(self, store: Store, idempotency_key: object) -> dict[str, Any] | None:
        if not isinstance(idempotency_key, str) or not _IDENTIFIER.fullmatch(idempotency_key):
            raise OperatorControlError(
                "invalid_idempotency_key", "idempotency_key must be a bounded identifier"
            )
        return _receipt(store, idempotency_key)

    def _sign(self, payload: dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        return body + "." + signature

    def _verify(self, token: object) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 4096 or token.count(".") != 1:
            raise OperatorControlError("invalid_intent", "operator intent is malformed")
        body, signature = token.split(".", 1)
        expected = _b64(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise OperatorControlError("invalid_intent", "operator intent signature is invalid")
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, UnicodeError) as exc:
            raise OperatorControlError("invalid_intent", "operator intent payload is invalid") from exc
        expected_fields = {
            "version",
            "project_id",
            "action",
            "task_id",
            "execution_id",
            "reason",
            "idempotency_key",
            "issued_at",
            "expires_at",
            "state_digest",
            "nonce",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise OperatorControlError("invalid_intent", "operator intent fields are invalid")
        if payload.get("version") != 1 or payload.get("project_id") != self.project_id:
            raise OperatorControlError("invalid_intent", "operator intent identity does not match")
        if payload.get("action") not in ALLOWED_ACTIONS:
            raise OperatorControlError("invalid_intent", "operator intent action is invalid")
        if not isinstance(payload.get("idempotency_key"), str) or not _IDENTIFIER.fullmatch(
            payload["idempotency_key"]
        ):
            raise OperatorControlError("invalid_intent", "operator intent idempotency is invalid")
        if not isinstance(payload.get("state_digest"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", payload["state_digest"]
        ):
            raise OperatorControlError("invalid_intent", "operator intent state identity is invalid")
        expires_at = payload.get("expires_at")
        issued_at = payload.get("issued_at")
        if type(issued_at) is not int or type(expires_at) is not int or expires_at <= issued_at:
            raise OperatorControlError("invalid_intent", "operator intent time bounds are invalid")
        if self._now() > expires_at:
            raise OperatorControlError("expired_intent", "operator intent has expired", status=409)
        return payload


def _request(value: object, *, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - fields:
        raise OperatorControlError("invalid_request", "operator control request fields are invalid")
    return value


def _preconditions(action: str) -> tuple[str, ...]:
    if action == "task_retry":
        return (
            "task is BLOCKED",
            "task has no active claim or running execution",
            "task is not an objective root",
            "recorded source remains eligible",
            "recorded task state still matches this plan",
        )
    if action == "release_unknown_execution":
        return (
            "operator verified the recorded process is dead",
            "execution still belongs to the exact task",
            "execution is still RUNNING with UNKNOWN process identity",
            "no live matching process is observed",
            "recorded task and execution state still match this plan",
        )
    expected = "not already paused" if action == "queue_pause" else "currently paused or stopped"
    return (f"queue is {expected}", "recorded queue state still matches this plan")


def _require_retry_eligible(store: Store, task_id: str) -> None:
    task = store.get_task(task_id)
    if task is None:
        raise OperatorControlError("task_not_found", "task was not found", status=404)
    if task["status"] != TaskStatus.BLOCKED:
        raise OperatorControlError("task_not_blocked", "task is not blocked", status=409)
    if store.has_active_claim(task_id) or any(
        row["task_id"] == task_id for row in store.running_executions()
    ):
        raise OperatorControlError("task_owned", "task has active execution ownership", status=409)
    root_reason = objective_root_reason(store, task)
    if root_reason:
        raise OperatorControlError("objective_root", "objective roots require child planning", status=409)
    refusal = _source_refusal_reason(store, task)
    if refusal:
        raise OperatorControlError("source_ineligible", "recorded source eligibility refuses retry", status=409)


def _require_queue_transition(store: Store, action: str) -> None:
    if action in {"task_retry", "release_unknown_execution"}:
        return
    latest = latest_queue_control(store)
    state = str(latest.get("state")) if latest else "running"
    if action == "queue_pause" and state == "paused":
        raise OperatorControlError("queue_already_paused", "queue admission is already paused", status=409)
    if action == "queue_resume" and state not in {"paused", "stopped"}:
        raise OperatorControlError("queue_not_paused", "queue admission is not paused", status=409)


def _state_digest(
    store: Store,
    action: str,
    task_id: object,
    execution_id: object = None,
) -> str:
    if action == "task_retry" and isinstance(task_id, str):
        task = store.get_task(task_id)
        candidate = store.latest_candidate(task_id)
        state: dict[str, Any] = {
            "task_id": task_id,
            "task": None
            if task is None
            else {
                "stage": str(task["stage"]),
                "status": str(task["status"]),
                "updated_at": task["updated_at"],
            },
            "candidate_sha": None if candidate is None else str(candidate["sha"]),
            "active_claim": store.has_active_claim(task_id),
            "running_executions": sum(
                1 for row in store.running_executions() if row["task_id"] == task_id
            ),
        }
    elif (
        action == "release_unknown_execution"
        and isinstance(task_id, str)
        and isinstance(execution_id, str)
    ):
        execution = _require_unknown_release_eligible(store, task_id, execution_id)
        task = store.get_task(task_id)
        state = {
            "task_id": task_id,
            "task": None
            if task is None
            else {
                "stage": str(task["stage"]),
                "status": str(task["status"]),
                "updated_at": task["updated_at"],
            },
            "execution": {
                "id": execution_id,
                "task_id": str(execution["task_id"]),
                "kind": str(execution["kind"]),
                "status": str(execution["status"]),
                "claim_id": execution["claim_id"],
                "updated_at": execution["updated_at"],
                "process_state": "UNKNOWN",
            },
        }
    else:
        latest = latest_queue_control(store)
        state = {
            "queue_state": "running" if latest is None else str(latest.get("state")),
            "queue_rowid": None if latest is None else int(latest["rowid"]),
        }
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _execute(
    store: Store,
    action: str,
    task_id: object,
    execution_id: object,
    reason: object,
    adverse_policy: AdverseReviewPolicy | None,
) -> dict[str, Any]:
    if action == "task_retry" and isinstance(task_id, str):
        _require_retry_eligible(store, task_id)
        result = resume_task(store, task_id, adverse_policy=adverse_policy)
        task = store.get_task(task_id)
        candidate = store.latest_candidate(task_id)
        audit: dict[str, object] = {"task_id": task_id, "stage": str(task["stage"])}
        if candidate is not None:
            audit["candidate_sha"] = str(candidate["sha"])
        record_audit(store, "task.operator_retry_requested", audit)
        return {"task_id": task_id, "status": str(task["status"]), **result}
    if (
        action == "release_unknown_execution"
        and isinstance(task_id, str)
        and isinstance(execution_id, str)
        and isinstance(reason, str)
    ):
        try:
            result = release_unknown_execution(store, task_id, execution_id, reason)
        except OperatorActionError as exc:
            raise OperatorControlError(
                "action_refused",
                "execution changed or is no longer eligible for release",
                status=409,
            ) from exc
        if result.action != "RELEASED_BY_OPERATOR":
            raise OperatorControlError(
                "action_refused",
                "execution changed before it could be released",
                status=409,
            )
        return {
            "task_id": task_id,
            "execution_id": execution_id,
            "execution_kind": result.kind,
            "process_state": result.process_state,
            "outcome": result.action,
        }
    state = "paused" if action == "queue_pause" else "resumed"
    record_audit(
        store,
        QUEUE_CONTROL_EVENT,
        {
            "state": state,
            "reason": "operator control confirmation",
            "source": "operator-control",
            "terminate_running": False,
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        },
    )
    return {"queue_state": state}


def _reason(value: object) -> str:
    if not isinstance(value, str):
        raise OperatorControlError(
            "invalid_reason", "release_unknown_execution requires a non-empty reason"
        )
    reason = value.strip()
    if not reason or len(reason) > 500 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in reason):
        raise OperatorControlError(
            "invalid_reason", "reason must be 1 to 500 characters without control characters"
        )
    return reason


def _require_unknown_release_eligible(
    store: Store,
    task_id: str,
    execution_id: str,
) -> Any:
    task = store.get_task(task_id)
    if task is None:
        raise OperatorControlError("task_not_found", "task was not found", status=404)
    execution = store.conn.execute(
        "SELECT * FROM executions WHERE id=?",
        (execution_id,),
    ).fetchone()
    if execution is None:
        raise OperatorControlError("execution_not_found", "execution was not found", status=404)
    if str(execution["task_id"]) != task_id:
        raise OperatorControlError(
            "execution_task_mismatch", "execution does not belong to the exact task", status=409
        )
    if str(execution["status"]) != "RUNNING":
        raise OperatorControlError(
            "execution_not_running", "execution is no longer running", status=409
        )
    process_state = str(execution_runtime_row(store, execution)["process_state"])
    if process_state == "LIVE":
        raise OperatorControlError(
            "execution_live", "a live matching process is observed and cannot be released", status=409
        )
    if process_state == "DEAD":
        raise OperatorControlError(
            "execution_dead",
            "execution is provably dead and must use ordinary stale recovery",
            status=409,
        )
    if process_state != "UNKNOWN":
        raise OperatorControlError(
            "execution_not_unknown", "execution identity is not currently unknown", status=409
        )
    return execution


def _receipt(store: Store, idempotency_key: str) -> dict[str, Any] | None:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? "
        "AND CASE WHEN json_valid(payload) THEN json_extract(payload, '$.idempotency_key') END=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 2",
        (CONTROL_RECEIPT_EVENT, idempotency_key),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("idempotency_key") == idempotency_key:
            return payload
    return None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
