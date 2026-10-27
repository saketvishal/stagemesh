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
IMPLEMENTATION_DERIVATION = "candidate.implementation_identity_derived"
_DERIVATIONS = frozenset({"rebase", "reattribution", "reconstruction"})
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


def record_implementation_derivation(store: Store, task_id: str, source_sha: str, derived_sha: str, kind: str) -> bool:
    """Called only after a trusted mechanical transform succeeds and its owned workspace is sealed."""
    if not isinstance(kind, str) or kind not in _DERIVATIONS or source_sha == derived_sha:
        return False
    parent = implementation_identity(store, task_id, source_sha)
    candidate = store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, derived_sha)).fetchone()
    if parent is None or candidate is None or candidate["produced_by"] != parent["provider"]:
        return False
    store.add_audit_event(IMPLEMENTATION_DERIVATION, {
        "task_id": task_id, "from_candidate": source_sha, "candidate_sha": derived_sha,
        "kind": kind, "command_digest": parent["command_digest"],
    })
    return True


def implementation_identity(store: Store, task_id: str, candidate_sha: str) -> dict | None:
    memo: dict[str, dict | None] = {}
    resolving: set[str] = set()
    invalid_graph = False

    def resolve(sha: str) -> dict | None:
        nonlocal invalid_graph
        if invalid_graph:
            return None
        if sha in resolving or len(resolving) >= 32:
            invalid_graph = True
            return None
        if sha in memo:
            return memo[sha]
        resolving.add(sha)
        try:
            candidate = store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=? AND sha=?", (task_id, sha)).fetchone()
            if candidate is None:
                memo[sha] = None
                return None

            def rows(event_type):
                return store.conn.execute(
                    "SELECT payload FROM audit_events WHERE event_type=? "
                    "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.task_id')=? "
                    "AND json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.candidate_sha')=? ORDER BY created_at,rowid",
                    (event_type, task_id, sha),
                )

            identities = []
            for row in rows(IMPLEMENTATION_IDENTITY):
                payload = json.loads(row["payload"])
                if not valid_command_snapshot(payload) or payload.get("provider") != candidate["produced_by"]:
                    continue
                execution_id = payload.get("execution_id")
                if not isinstance(execution_id, str):
                    continue
                execution = store.conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
                if (execution is not None and execution["kind"] == ExecutionKind.IMPLEMENTATION
                        and execution["status"] == ExecutionStatus.SUCCEEDED and execution["task_id"] == task_id
                        and execution["candidate_sha"] == sha and execution["actor"] == candidate["produced_by"]):
                    identities.append({**payload, "origins": [{"execution_id": execution_id, "candidate_sha": sha}]})
            parents = set()
            for row in rows(IMPLEMENTATION_DERIVATION):
                payload = json.loads(row["payload"])
                if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str) or payload["kind"] not in _DERIVATIONS:
                    continue
                parent_sha, digest = payload.get("from_candidate"), payload.get("command_digest")
                if not isinstance(parent_sha, str) or not parent_sha or not isinstance(digest, str) or not _HEX.fullmatch(digest):
                    continue
                edge = (parent_sha, payload["kind"], digest)
                if edge in parents:
                    continue
                parents.add(edge)
                parent = resolve(parent_sha)
                if invalid_graph:
                    memo[sha] = None
                    return None
                if (parent is not None and parent["provider"] == candidate["produced_by"]
                        and parent["command_digest"] == digest):
                    identities.append(parent)
            result = None
            if identities and len({r["command_digest"] for r in identities}) == 1:
                origins = sorted({(o["execution_id"], o["candidate_sha"]) for r in identities for o in r["origins"]})
                result = {"provider": candidate["produced_by"], "command_digest": identities[0]["command_digest"],
                          "alias_digests": sorted({a for r in identities for a in r["alias_digests"]}),
                          "execution_ids": [execution for execution, _ in origins],
                          "origins": [{"execution_id": execution, "candidate_sha": origin} for execution, origin in origins]}
            memo[sha] = result
            return result
        finally:
            resolving.discard(sha)

    result = resolve(candidate_sha)
    return None if invalid_graph else result
