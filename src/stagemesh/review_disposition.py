"""Adverse review findings keep their identity, provenance and resolution history, and a later PASS cannot erase them.

A finding is identified by `finding_identity(candidate, message, path)` and lives in the `findings` table. This module adds, as
audit events (so nothing in the schema changes and the history is append-only):

* `review.finding_recorded`   which reviewer raised it, on which exact candidate, under which contract digest and baseline, and
                              which review evidence row carried it;
* `review.finding_disposition` an explicit, evidenced decision about it: who decided, on what evidence, and why.

A candidate is review-satisfied only when an independent PASS exists *and* no blocking review finding on that candidate is still
open. The only ways a finding stops blocking are a recorded disposition (which requires an actor and an evidence reference) or a new
candidate, which has its own findings and needs its own review. A second reviewer answering PASS is not a disposition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .persistence import Store

FINDING_RECORDED = "review.finding_recorded"
FINDING_DISPOSITION = "review.finding_disposition"
UNRESOLVED_EVENT = "review.adverse_finding_unresolved"

DISPOSITIONS = ("refuted_with_evidence", "waived_by_owner")


class DispositionError(ValueError):
    pass


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def record_review_findings(
    store: Store,
    task_id: str,
    candidate_sha: str,
    reviewer: str,
    findings: list[tuple[str, str, str]],
    *,
    contract_hash: str | None,
    baseline_sha: str | None,
    review_evidence_id: str | None,
) -> None:
    """Persist who raised each finding (identity, severity, message) against which candidate, contract and baseline."""
    for identity, severity, message in findings:
        record_audit(
            store,
            FINDING_RECORDED,
            {
                "task_id": task_id,
                "candidate_sha": candidate_sha,
                "finding_id": identity,
                "severity": severity,
                "message": message[:500],
                "reviewer": reviewer,
                "contract_hash": contract_hash,
                "baseline_sha": baseline_sha,
                "review_evidence_id": review_evidence_id,
            },
        )


def _events(store: Store, event_type: str, task_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)
    ).fetchall()
    return [payload for row in rows if (payload := _loads(row["payload"])).get("task_id") == task_id]


def unresolved_adverse_review_findings(store: Store, task_id: str, candidate_sha: str) -> list[dict[str, Any]]:
    """Open, blocking findings that a reviewer raised against this exact candidate, with their provenance."""
    from .autonomy.review_policy import severity_blocks

    origin = {item["finding_id"]: item for item in _events(store, FINDING_RECORDED, task_id) if item.get("candidate_sha") == candidate_sha}
    unresolved = []
    for row in store.open_findings_for_candidate(task_id, candidate_sha):
        item = origin.get(str(row["id"]))
        if item is not None and severity_blocks(str(row["severity"])):
            unresolved.append({**item, "severity": str(row["severity"]), "message": str(row["message"])})
    return unresolved


def finding_history(store: Store, task_id: str, finding_id: str) -> list[dict[str, Any]]:
    """Every recorded step for one finding, oldest first: raised, then any dispositions."""
    history = [{"event": FINDING_RECORDED, **item} for item in _events(store, FINDING_RECORDED, task_id) if item.get("finding_id") == finding_id]
    history += [{"event": FINDING_DISPOSITION, **item} for item in _events(store, FINDING_DISPOSITION, task_id) if item.get("finding_id") == finding_id]
    return history


def record_disposition(
    store: Store, task_id: str, finding_id: str, *, disposition: str, actor: str, evidence_ref: str, rationale: str
) -> dict[str, Any]:
    """Resolve one adverse finding by explicit, evidenced decision. Nothing else closes a review finding."""
    if disposition not in DISPOSITIONS:
        raise DispositionError(f"disposition must be one of: {', '.join(DISPOSITIONS)}")
    for name, value in (("actor", actor), ("evidence reference", evidence_ref), ("rationale", rationale)):
        if not value or not value.strip():
            raise DispositionError(f"a disposition requires a non-empty {name}")
    row = store.get_finding(finding_id)
    if row is None or str(row["task_id"]) != task_id:
        raise DispositionError(f"no finding {finding_id} on task {task_id}")
    if row["status"] != "OPEN":
        raise DispositionError(f"finding {finding_id} is already {row['status']}")
    payload = {
        "task_id": task_id,
        "finding_id": finding_id,
        "candidate_sha": str(row["candidate_sha"]),
        "disposition": disposition,
        "actor": actor.strip(),
        "evidence_ref": evidence_ref.strip(),
        "rationale": rationale.strip()[:1000],
        "original_message": str(row["message"])[:500],
    }
    store.close_finding(finding_id)
    record_audit(store, FINDING_DISPOSITION, payload)
    return payload


@dataclass(frozen=True)
class AdverseSummary:
    findings: tuple[dict[str, Any], ...]

    def __bool__(self) -> bool:
        return bool(self.findings)

    def reason(self) -> str:
        reviewers = sorted({str(item.get("reviewer")) for item in self.findings})
        first = self.findings[0]
        return (
            f"{len(self.findings)} unresolved adverse review finding(s) from {', '.join(reviewers)} on candidate "
            f"{str(first.get('candidate_sha'))[:10]}: {str(first.get('message'))[:200]}. A later PASS does not resolve them; "
            "fix the candidate, or record an evidenced disposition (`stagemesh finding-disposition`)."
        )
