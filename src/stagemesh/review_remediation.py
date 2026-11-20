from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .persistence import Store

CODE_CHANGE = "code_change"
VALIDATION = "validation"
EXTERNAL_EVIDENCE = "external_evidence"
HUMAN_DECISION = "human_decision"
REMEDIATION_KINDS = frozenset({CODE_CHANGE, VALIDATION, EXTERNAL_EVIDENCE, HUMAN_DECISION})

_VALIDATION_EVIDENCE = re.compile(
    r"\b(?:required|configured|exact-head|full|focused|root|task also requires)\b.{0,180}"
    r"\b(?:gate|command|test|check|lint|build|validation)\b.{0,180}"
    r"\b(?:has no recorded result|is unverified|was not run|did not run|missing|not recorded|no recorded|"
    r"does not record|do not record)\b",
    re.IGNORECASE,
)
_EXTERNAL_EVIDENCE = re.compile(
    r"\b(?:browser|responsive|viewport|screenshot|visual|design|manual|internal acceptance|native device)\b.{0,120}"
    r"\b(?:evidence|record|capture|comparison|review|acceptance|unverified|missing|lack|no recorded|not establish)\b"
    r"|\b(?:evidence|record|capture|comparison|review|acceptance)\b.{0,120}"
    r"\b(?:browser|responsive|viewport|screenshot|visual|design|manual|internal acceptance|native device)\b",
    re.IGNORECASE,
)
_HUMAN_DECISION = re.compile(
    r"\b(?:human|owner|operator)\s+(?:decision|approval|choice)|\brequires?\s+(?:a\s+)?decision\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewRemediation:
    kind: str
    message: str
    finding_id: str | None = None


def classify_review_finding(message: str, declared: object = None) -> str:
    """Classify what can resolve a review finding; ambiguous findings remain code work.

    The conservative fallback is important: prose mentioning tests is not automatically evidence-only.
    Only language saying that a required run or external observation is absent is routed away from implementation.
    """
    if isinstance(declared, str) and declared in REMEDIATION_KINDS:
        return declared
    text = str(message or "").strip()
    if _HUMAN_DECISION.search(text):
        return HUMAN_DECISION
    if _EXTERNAL_EVIDENCE.search(text):
        return EXTERNAL_EVIDENCE
    if _VALIDATION_EVIDENCE.search(text):
        return VALIDATION
    return CODE_CHANGE


def review_remediations(store: Store, task_id: str, candidate_sha: str) -> list[ReviewRemediation]:
    """Return open review findings with their structured or conservative legacy remediation kind."""
    declared: dict[str, str] = {}
    rows = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='REVIEW' AND status='FAILED' "
        "ORDER BY created_at, rowid",
        (task_id, candidate_sha),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        raw = payload.get("findings")
        if not isinstance(raw, list) or not raw:
            response = payload.get("review_response")
            if isinstance(response, str):
                try:
                    parsed = json.loads(response)
                except (TypeError, ValueError):
                    parsed = {}
                raw = parsed.get("findings") if isinstance(parsed, dict) else []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("message"), str):
                continue
            declared[item["message"]] = classify_review_finding(item["message"], item.get("remediation"))

    return [
        ReviewRemediation(
            classify_review_finding(str(row["message"]), declared.get(str(row["message"]))),
            str(row["message"]),
            str(row["id"]),
        )
        for row in store.open_findings_for_candidate(task_id, candidate_sha)
    ]


def remediation_summary(items: list[ReviewRemediation]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in items:
        result.setdefault(item.kind, []).append(item.message)
    return result
