"""Secret-free command provenance for independent review roles; no command execution."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from contextvars import ContextVar

from .domain import ExecutionKind, ExecutionStatus
from .persistence import Store

IMPLEMENTATION_IDENTITY = "candidate.implementation_identity"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_review_identity: ContextVar[dict | None] = ContextVar("review_command_identity", default=None)


def valid_command_snapshot(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("provider"), str) or not snapshot["provider"].strip():
        return False
    digest, aliases = snapshot.get("command_digest"), snapshot.get("alias_digests")
    return bool(isinstance(digest, str) and _HEX.fullmatch(digest) and isinstance(aliases, list)
                and digest in aliases and all(isinstance(a, str) and _HEX.fullmatch(a) for a in aliases))


def command_digest(command: Sequence[str]) -> str:
    if isinstance(command, (str, bytes)) or not command or not all(isinstance(p, str) and p for p in command):
        raise ValueError("command identity requires nonempty argv")
    # Same conservative, case-insensitive alias policy as typed review policy; argv is never persisted.
    return hashlib.sha256(json.dumps([p.strip("\"'").casefold() for p in command], separators=(",", ":")).encode()).hexdigest()


def command_snapshot(provider: str, command: Sequence[str], aliases: Sequence[Sequence[str]] = ()) -> dict:
    digest = command_digest(command)
    return {"provider": provider, "command_digest": digest,
            "alias_digests": sorted({digest, *(command_digest(c) for c in aliases)})}


def note_review_identity(snapshot: dict) -> None:
    _review_identity.set(snapshot)


def take_review_identity() -> dict | None:
    value = _review_identity.get()
    _review_identity.set(None)
    return value


def record_implementation_identity(store: Store, task_id: str, candidate_sha: str, execution_id: str, snapshot: dict) -> None:
    if not valid_command_snapshot(snapshot):
        raise ValueError("invalid command identity snapshot")
    store.add_audit_event(IMPLEMENTATION_IDENTITY, {
        **snapshot, "task_id": task_id, "candidate_sha": candidate_sha, "execution_id": execution_id,
    })


def implementation_identity(store: Store, task_id: str, candidate_sha: str) -> dict | None:
    candidate = store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, candidate_sha)).fetchone()
    if candidate is None:
        return None
    receipts = []
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
        "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.candidate_sha')=? ORDER BY created_at,rowid",
        (IMPLEMENTATION_IDENTITY, task_id, candidate_sha),
    )
    for row in rows:
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict) or payload.get("provider") != candidate["produced_by"]:
            continue
        digest, aliases = payload.get("command_digest"), payload.get("alias_digests")
        if not isinstance(digest, str) or not _HEX.fullmatch(digest) or not isinstance(aliases, list):
            continue
        if not aliases or digest not in aliases or not all(isinstance(a, str) and _HEX.fullmatch(a) for a in aliases):
            continue
        execution_id = payload.get("execution_id")
        if not isinstance(execution_id, str):
            continue
        execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if (execution is not None and execution["kind"] == ExecutionKind.IMPLEMENTATION
                and execution["status"] == ExecutionStatus.SUCCEEDED and execution["task_id"] == task_id
                and execution["candidate_sha"] == candidate_sha and execution["actor"] == candidate["produced_by"]):
            receipts.append(payload)
    if not receipts or len({r["command_digest"] for r in receipts}) != 1:
        return None  # Never reconstruct missing/ambiguous historic commands from current provider config.
    return {"provider": candidate["produced_by"], "command_digest": receipts[0]["command_digest"],
            "alias_digests": sorted({a for r in receipts for a in r["alias_digests"]}),
            "execution_ids": [r["execution_id"] for r in receipts]}
