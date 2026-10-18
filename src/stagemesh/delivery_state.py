"""What "done" means: the honest delivery state of a task, read from the ledger.

Ledger `DONE` only says the lifecycle finished and the candidate was integrated into some ref. Whether the work was published,
checked and merged upstream is a different fact, and so is whether the upstream issue was closed. This module derives one state
from facts the ledger already records plus two it now records (`delivery.pr_published`, `delivery.externally_blocked`):

    IN_PROGRESS            not ledger-DONE yet (implementation, validation, review or integration is still ahead)
    INTEGRATED_LOCALLY     ledger DONE in local mode: integrated into the local integration branch, nothing pushed
    STAGED                 ledger DONE in PR mode: on the wave staging ref, no PR published
    DELIVERY_PENDING       publication was deferred (window deadline, publisher unavailable or failed); the work is preserved
    PUBLISHED              PR open for the exact integrated head
    AWAITING_CHECKS        PR open and the landing guard is waiting for hosted checks or mergeability
    MERGED                 the exact head landed upstream (verified by the landing guard)
    EXTERNALLY_BLOCKED     an external permission or approval is required; recorded once, never worked around

`complete` is true only for MERGED, or INTEGRATED_LOCALLY when the operator chose local mode. Upstream issue closure is derived
from MERGED, never from ledger DONE.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .domain import Stage, TaskStatus
from .persistence import Store

PR_PUBLISHED = "delivery.pr_published"
EXTERNALLY_BLOCKED = "delivery.externally_blocked"
DELIVERY_UNBLOCKED = "delivery.unblocked"
PENDING = "task.pr_delivery_pending"
MERGED_EVENTS = ("window.fast_forward_merged",)
LANDING_STATUS = "window.landing_status"
LANDING_RESULT = "window.landing_result"

STAGING_MARKER = "wave/"

_PERMISSION_PATTERN = re.compile(
    r"permission denied|permission to \S+ denied|authentication failed|not authorized|unauthorized|protected branch|"
    r"resource not accessible|must have admin|access denied|please make sure you have the correct access rights|"
    r"\b(?:http|status|error|code)[: ]+(?:401|403)\b|\b(?:401|403)\s+(?:forbidden|unauthorized)\b|\bforbidden\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Delivery:
    state: str
    complete: bool
    detail: str
    pr_url: str | None = None
    head_sha: str | None = None

    def label(self) -> str:
        return f"{self.state.replace('_', ' ').lower()}: {self.detail}" if self.detail else self.state.replace("_", " ").lower()

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "complete": self.complete, "detail": self.detail, "pr_url": self.pr_url, "head_sha": self.head_sha}


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _latest(
    store: Store, event_type: str, task_id: str, candidate: str | None = None, repository: str | None = None
) -> dict[str, Any] | None:
    """The newest event of this type for exactly this task (and candidate / repository when given).

    Identity is compared on the decoded payload, never on its JSON text: `json.dumps` escapes quotes, backslashes and non-ASCII
    characters, and SQL `LIKE` treats `%` and `_` as wildcards, so a text pattern both misses and over-matches real task ids.
    """
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id') = ? ORDER BY created_at DESC, rowid DESC",
        (event_type, task_id),
    ).fetchall()
    for row in rows:
        payload = _loads(row["payload"])
        if payload.get("task_id") != task_id:
            continue
        if candidate is not None and payload.get("candidate_sha") not in (None, candidate):
            continue
        recorded = payload.get("repository")
        if repository is not None and recorded is not None and str(recorded).casefold() != repository.casefold():
            continue
        return payload
    return None


def is_permission_denial(message: str) -> bool:
    return bool(_PERMISSION_PATTERN.search(message))


# Landing guard outcomes where simply asking again later can change the answer. Everything else it reports (hosted CI failed, PR
# closed, head or base changed, conflicts, policy or evidence problems) is a fact that waiting does not fix: it needs a decision.
_RETRYABLE_LANDING = (
    "hosted ci deadline reached", "github mergeability remained unknown", "merge_deadline_reached", "hosted checks missing after",
    "queue admission", "queue_paused", "landing recovery deadline reached", "deadline reached while verifying pending prs",
)


def landing_retryable(reason: str) -> bool:
    text = reason.casefold()
    return not is_permission_denial(reason) and any(prefix in text for prefix in _RETRYABLE_LANDING)


def record_landing_result(store: Store, task_id: str, candidate_sha: str | None, reason: str) -> dict[str, Any]:
    """Record why a landing attempt did not merge, and whether a later attempt could do better."""
    payload = {
        "task_id": task_id, "candidate_sha": candidate_sha, "reason": reason[:300],
        "permission_denied": is_permission_denial(reason), "retryable": landing_retryable(reason), "at": time.time(),
    }
    record_audit(store, LANDING_RESULT, payload)
    return payload


def latest_landing_result(store: Store, task_id: str, candidate: str | None = None, since: float = 0.0) -> dict[str, Any] | None:
    found = _latest(store, LANDING_RESULT, task_id, candidate)
    return found if found is not None and float(found.get("at", 0.0)) >= since else None


_PULL_URL = re.compile(r"^https?://[^/\s]+/([^/\s]+)/([^/\s]+)/pull/\d+(?:[/?#].*)?$", re.IGNORECASE)


def pr_url_repository(url: str | None) -> str | None:
    """`owner/repo` (lower-cased) of a GitHub pull-request URL, or None when it is not one."""
    match = _PULL_URL.match(url or "")
    return None if match is None else f"{match.group(1)}/{match.group(2)}".casefold()


def record_publication(store: Store, task_id: str, pr_url: str, head_sha: str, base: str, *, repository: str | None = None) -> None:
    """Record the PR's identity and the exact head it was opened for (and the repository, when known)."""
    repository = repository or pr_url_repository(pr_url)
    record_audit(store, PR_PUBLISHED, {
        "task_id": task_id, "pr_url": pr_url, "candidate_sha": head_sha, "base": base, "repository": repository,
    })


def record_external_block(store: Store, task_id: str, candidate_sha: str | None, reason: str, needed: str) -> bool:
    """Record an external-permission blocker once per task and candidate. Returns False when it was already on record."""
    existing = _latest(store, EXTERNALLY_BLOCKED, task_id, candidate_sha)
    cleared = _latest(store, DELIVERY_UNBLOCKED, task_id)
    if existing is not None and (cleared is None or cleared.get("at", 0) < existing.get("at", 0)):
        return False
    record_audit(store, EXTERNALLY_BLOCKED, {"task_id": task_id, "candidate_sha": candidate_sha, "reason": reason[:400], "needed": needed, "at": time.time()})
    return True


def clear_external_block(store: Store, task_id: str, actor: str, reason: str) -> None:
    record_audit(store, DELIVERY_UNBLOCKED, {"task_id": task_id, "actor": actor, "reason": reason[:300], "at": time.time()})


def externally_blocked(store: Store, task_id: str) -> dict[str, Any] | None:
    """The open external-permission blocker for this task, if any (a later explicit unblock clears it)."""
    blocked = _latest(store, EXTERNALLY_BLOCKED, task_id)
    if blocked is None:
        return None
    cleared = _latest(store, DELIVERY_UNBLOCKED, task_id)
    return None if cleared is not None and cleared.get("at", 0) >= blocked.get("at", 0) else blocked


def task_delivery(store: Store, task_id: str, repository: str | None = None) -> Delivery:
    """Honest delivery state. With `repository` ("owner/repo"), a PR recorded for another repository is not this task's PR."""
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    sha = None if latest is None else str(latest["sha"])
    done = task is not None and (task["stage"] == Stage.DONE or task["status"] == TaskStatus.DONE)
    published = _latest(store, PR_PUBLISHED, task_id, sha, repository)
    if published is not None and repository is not None:
        url_repository = pr_url_repository(str(published.get("pr_url")))
        if url_repository is not None and url_repository != repository.casefold():
            published = None
    pr_url = None if published is None else str(published.get("pr_url"))
    blocked = externally_blocked(store, task_id)
    if blocked is not None and not _merged(store, task_id, sha):
        return Delivery("EXTERNALLY_BLOCKED", False, f"needs {blocked.get('needed')}: {blocked.get('reason')}", pr_url, sha)
    if _merged(store, task_id, sha):
        return Delivery("MERGED", True, "the exact reviewed head landed upstream", pr_url, sha)
    if published is not None:
        status = _latest(store, LANDING_STATUS, task_id, sha, repository)
        if status is not None and status.get("state") in {"waiting", "refresh required", "blocked"}:
            return Delivery("AWAITING_CHECKS", False, f"{status.get('state')}: {str(status.get('reason'))[:160]}", pr_url, sha)
        return Delivery("PUBLISHED", False, "PR open for the exact integrated head; not merged", pr_url, sha)
    if not done:
        pending = _latest(store, PENDING, task_id, sha)
        if pending is not None:
            return Delivery("DELIVERY_PENDING", False, f"verified work preserved; {pending.get('reason', 'publication deferred')}", None, sha)
        return Delivery("IN_PROGRESS", False, "the lifecycle has not finished", None, sha)
    ref = _integration_ref(store, task_id, sha)
    if ref is not None and STAGING_MARKER in ref:
        return Delivery("STAGED", False, f"integrated on {ref}; no PR published and nothing merged", None, sha)
    return Delivery("INTEGRATED_LOCALLY", True, f"integrated into {ref or 'the local integration branch'}; nothing pushed upstream", None, sha)


def _merged(store: Store, task_id: str, sha: str | None) -> bool:
    return any(_latest(store, event, task_id, sha) is not None for event in MERGED_EVENTS)


def _integration_ref(store: Store, task_id: str, sha: str | None) -> str | None:
    if sha is None:
        return None
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind='INTEGRATION' AND status='PASSED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, sha),
    ).fetchone()
    return None if row is None else (_loads(row["payload"]).get("integration_ref") or None)
