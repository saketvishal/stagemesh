"""Adverse review findings keep their identity, provenance and resolution history, and a later PASS cannot erase them.

A finding is identified by `finding_identity(candidate, message, path)` and lives in the `findings` table. This module adds, as
audit events (so nothing in the schema changes and the history is append-only):

* `review.finding_recorded`   which reviewer raised it, on which exact candidate, under which contract digest and baseline, and
                              which review evidence row carried it;
* `review.finding_disposition` an explicit, evidenced decision about it: who decided, on what evidence, and why.

A candidate is review-satisfied only when an independent PASS exists *and* no blocking review finding on that candidate is still
open. The only ways a finding stops blocking are a recorded disposition (which requires an actor and an evidence reference) or a new
candidate, which has its own findings and needs its own review. A second reviewer answering PASS is not a disposition.

Who may decide is part of the record, not a free-text claim. A disposition is refused when its actor is (or is named like) an
implementer of the task, or when it is requested from inside a provider process (`STAGEMESH_PROVIDER_PROCESS`): an implementer's
self-attestation never satisfies a review it was supposed to be independent of. `refuted_with_evidence` additionally needs a
reference that resolves, in the ledger, to independent evidence for the *same* candidate that post-dates the finding; `waived_by_owner`
is an explicit risk decision by a non-implementer and is recorded as owner attestation (never as independent evidence). The audit
record names the actor, the exact candidate, the finding's severity and originating reviewer, what each reference resolved to, and
the implementers it was checked against. Nothing is edited or deleted: dispositions only append.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .persistence import Store

FINDING_RECORDED = "review.finding_recorded"
FINDING_DISPOSITION = "review.finding_disposition"
UNRESOLVED_EVENT = "review.adverse_finding_unresolved"

RECHECK_EVENT = "task.recheck_existing_candidate"
WITHDRAWN = "withdrawn_by_reviewer"
DISPOSITIONS = ("refuted_with_evidence", "waived_by_owner")
PROVIDER_PROCESS_ENV = "STAGEMESH_PROVIDER_PROCESS"
# Generic automation labels that are never an independent decider, whatever provider names a project configures.
AUTOMATION_ACTORS = frozenset({"agent", "ai", "assistant", "bot", "implementer", "provider", "stagemesh", "system", "worker", "automation"})
_EVIDENCE_REF = ("review-evidence", "validation-evidence")


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
        "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at, rowid", (event_type,)
    ).fetchall()
    return [
        {**payload, "_at": float(row["created_at"])}
        for row in rows
        if (payload := _loads(row["payload"])).get("task_id") == task_id
    ]


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


def recheck_review_authorized(store: Store, task_id: str, candidate_sha: str) -> bool:
    """True when a no-op remediation asked for exactly one re-check of this candidate and no review has run since."""
    rechecks = [e for e in _events(store, RECHECK_EVENT, task_id) if e.get("candidate_sha") == candidate_sha]
    if not rechecks:
        return False
    since = rechecks[-1]["_at"]
    newer = store.conn.execute(
        "SELECT 1 FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='REVIEW' AND created_at>=? LIMIT 1",
        (task_id, candidate_sha, since),
    ).fetchone()
    return newer is None


def reconcile_withdrawals(store: Store, task_id: str, candidate_sha: str, contract_hash: str) -> list[str]:
    """A reviewer may withdraw its own finding by passing the same candidate later; no other reviewer's PASS counts.

    The withdrawal is recorded as an explicit disposition that cites the PASSED review evidence, so the history shows who changed
    their verdict and on what evidence. Returns the finding ids resolved this way.
    """
    pending = unresolved_adverse_review_findings(store, task_id, candidate_sha)
    if not pending:
        return []
    passes = store.conn.execute(
        "SELECT id, payload, created_at FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='REVIEW' AND status='PASSED' ORDER BY created_at, rowid",
        (task_id, candidate_sha),
    ).fetchall()
    resolved: list[str] = []
    implementers = task_implementers(store, task_id)
    for finding in pending:
        raised_at = next((e["_at"] for e in _events(store, FINDING_RECORDED, task_id) if e.get("finding_id") == finding["finding_id"]), 0.0)
        reviewer = str(finding.get("reviewer") or "").casefold()
        for row in passes:
            payload = _loads(row["payload"])
            if (
                float(row["created_at"]) >= raised_at
                and payload.get("contract_hash") == contract_hash
                and str(payload.get("review_provider") or "").casefold() == reviewer
                and reviewer
                and reviewer not in implementers  # an implementer's own "withdrawal" is self-attestation
                and payload.get("independent_reviewer") is True
            ):
                record_disposition(
                    store, task_id, finding["finding_id"], disposition=WITHDRAWN, actor=str(finding.get("reviewer")),
                    evidence_ref=f"review-evidence:{row['id']}",
                    rationale="the reviewer that raised this finding passed the same candidate on re-check",
                )
                resolved.append(finding["finding_id"])
                break
    return resolved


def finding_history(store: Store, task_id: str, finding_id: str) -> list[dict[str, Any]]:
    """Every recorded step for one finding, oldest first: raised, then any dispositions."""
    history = [{"event": FINDING_RECORDED, **item} for item in _events(store, FINDING_RECORDED, task_id) if item.get("finding_id") == finding_id]
    history += [{"event": FINDING_DISPOSITION, **item} for item in _events(store, FINDING_DISPOSITION, task_id) if item.get("finding_id") == finding_id]
    return history


def task_implementers(store: Store, task_id: str) -> set[str]:
    """Every identity that produced or ran implementation for the task, lower-cased (candidate producers and implementation actors)."""
    names = {str(row["produced_by"]) for row in store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=?", (task_id,))}
    names |= {
        str(row["actor"])
        for row in store.conn.execute("SELECT actor FROM executions WHERE task_id=? AND kind='IMPLEMENTATION' AND actor IS NOT NULL", (task_id,))
    }
    return {name.strip().casefold() for name in names if name and name.strip()}


def _is_implementer(actor: str, implementers: set[str]) -> bool:
    who = actor.strip().casefold()
    return who in implementers or who in AUTOMATION_ACTORS or any(who.startswith((f"{name}:", f"{name}/")) for name in implementers)


def _resolve_evidence(store: Store, task_id: str, row: Any, ref: str, raised_at: float, implementers: set[str], raised_by: str) -> dict[str, Any]:
    """What one typed reference resolves to in the ledger. `independent` is true only for passing, post-finding, same-candidate review
    evidence from a provider that neither implemented the candidate nor raised the finding."""
    kind, _, identifier = ref.partition(":")
    resolved: dict[str, Any] = {"ref": ref, "verified": False, "independent": False}
    if kind not in _EVIDENCE_REF or not identifier.strip():
        resolved["note"] = "not a ledger reference"
        return resolved
    found = store.conn.execute("SELECT * FROM evidence WHERE id=?", (identifier.strip(),)).fetchone()
    expected_kind = "REVIEW" if kind == "review-evidence" else "VALIDATION"
    if found is None or str(found["task_id"]) != task_id or str(found["candidate_sha"]) != str(row["candidate_sha"]) or str(found["kind"]) != expected_kind:
        resolved["note"] = "no such evidence for this task, candidate and kind"
        return resolved
    payload = _loads(found["payload"])
    provider = str(payload.get("review_provider") or "") if expected_kind == "REVIEW" else ""
    resolved.update(
        verified=True, evidence_id=str(found["id"]), evidence_kind=expected_kind, status=str(found["status"]), provider=provider or None,
        recorded_at=float(found["created_at"]),
    )
    later = float(found["created_at"]) >= raised_at
    passed = str(found["status"]) == "PASSED"
    if expected_kind == "REVIEW":
        independent_provider = bool(provider) and provider.casefold() not in implementers and provider.casefold() != raised_by.casefold()
        resolved["independent"] = passed and later and independent_provider and payload.get("independent_reviewer") is True
        if not resolved["independent"]:
            resolved["note"] = "review evidence is not an independent, passing review recorded after the finding"
    else:
        resolved["note"] = "machine validation shows the candidate behaves; it cannot rebut a reviewer's judgement alone"
    return resolved


def record_disposition(
    store: Store, task_id: str, finding_id: str, *, disposition: str, actor: str, evidence_ref: str | tuple[str, ...] | list[str], rationale: str
) -> dict[str, Any]:
    """Resolve one adverse finding by explicit, attributable, evidenced decision. Nothing else closes a review finding."""
    if disposition not in (*DISPOSITIONS, WITHDRAWN):
        raise DispositionError(f"disposition must be one of: {', '.join(DISPOSITIONS)}")
    refs = [evidence_ref] if isinstance(evidence_ref, str) else list(evidence_ref)
    refs = [ref.strip() for ref in refs if isinstance(ref, str) and ref.strip()]
    for name, value in (("actor", actor), ("rationale", rationale)):
        if not value or not value.strip():
            raise DispositionError(f"a disposition requires a non-empty {name}")
    if not refs:
        raise DispositionError("a disposition requires a non-empty evidence reference")
    row = store.get_finding(finding_id)
    if row is None or str(row["task_id"]) != task_id:
        raise DispositionError(f"no finding {finding_id} on task {task_id}")
    prior = [e for e in _events(store, FINDING_DISPOSITION, task_id) if e.get("finding_id") == finding_id]
    if prior:
        # A crash between the audit record and the close leaves the finding open with its decision on record; finish that, once.
        if row["status"] == "OPEN":
            store.close_finding(finding_id)
        raise DispositionError(f"finding {finding_id} already has a recorded disposition ({prior[-1].get('disposition')})")
    if row["status"] != "OPEN":
        raise DispositionError(f"finding {finding_id} is already {row['status']}")

    implementers = task_implementers(store, task_id)
    origin = next((e for e in _events(store, FINDING_RECORDED, task_id) if e.get("finding_id") == finding_id), {})
    raised_by = str(origin.get("reviewer") or "")
    if disposition != WITHDRAWN:
        if os.environ.get(PROVIDER_PROCESS_ENV):
            raise DispositionError("a finding disposition is an operator decision; it cannot be recorded from inside a provider process")
        if _is_implementer(actor, implementers):
            raise DispositionError(
                f"{actor.strip()!r} implemented (or is automation acting for) this task; an implementer's self-attestation cannot "
                "resolve a review finding on its own candidate"
            )
    raised_at = float(origin.get("_at") or 0.0)
    resolved = [_resolve_evidence(store, task_id, row, ref, raised_at, implementers, raised_by) for ref in refs]
    independent = any(item["independent"] for item in resolved)
    if disposition == "refuted_with_evidence" and not independent:
        raise DispositionError(
            "refuting a blocking review finding needs independent evidence for this exact candidate: cite `review-evidence:<id>` of a "
            "passing review recorded after the finding by a reviewer that neither implemented the candidate nor raised the finding "
            f"(your references resolved to: {[item.get('note') or 'unverified' for item in resolved]}); "
            "use waived_by_owner for an explicit risk decision"
        )
    payload = {
        "task_id": task_id,
        "finding_id": finding_id,
        "candidate_sha": str(row["candidate_sha"]),
        "disposition": disposition,
        "actor": actor.strip(),
        "evidence_ref": refs[0],
        "evidence_refs": refs,
        "evidence_resolved": resolved,
        "independent_evidence_verified": independent,
        "support": "independent_evidence" if independent else "owner_attestation",
        "rationale": rationale.strip()[:1000],
        "original_message": str(row["message"])[:500],
        "severity": str(row["severity"]),
        "raised_by": raised_by or None,
        "contract_hash": origin.get("contract_hash"),
        "implementers_checked": sorted(implementers),
    }
    record_audit(store, FINDING_DISPOSITION, payload)  # the decision is on record before the finding stops blocking
    store.close_finding(finding_id)
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
