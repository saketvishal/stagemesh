"""Did a step-capped run make meaningful progress, and how many capped runs in a row have not?

Hitting the step cap is not a failure by itself: the task keeps its stage, candidate and evidence in the ledger, so the next run
resumes exactly there and never repeats finished implementation. What must not happen is a task re-entering window after window
and spending its steps on the same failing loop. "Progress" is therefore defined by facts that cannot be faked by busy work:

* the task reached a later lifecycle stage than the run began at, or finished;
* the run ended on a new candidate whose failure is different from the one it began with (the earlier cause was resolved), or on a
  new candidate with no failure at all;
* an independent failed check produced a new, persisted remediation handoff. That is forward progress even though the lifecycle
  deliberately returns from REVIEW or VALIDATE to IMPLEMENT on the same candidate;
* a new exact-candidate browser capture was persisted, or an executed independent review role passed. These are bounded lifecycle
  milestones even when the task deliberately remains in REVIEW until the next role runs.

Other evidence rows, repeated remediation attempts or a new but equivalent failure are activity, not progress. Each step-capped run is
recorded (`task.step_cap`); the streak of consecutive unproductive capped runs is read back from the ledger, so it survives windows
and restarts, and an operator action that gives the task a fresh start (unblock, contract rebind, rebaseline) ends it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from .persistence import Store

STEP_CAP_EVENT = "task.step_cap"
STEP_CAP_STALL_LIMIT = 2  # consecutive capped runs without meaningful progress before the task stops as a typed blocker

_RANK = {
    Stage.PLAN: 0,
    Stage.IMPLEMENT: 1,
    Stage.VALIDATE: 2,
    Stage.REVIEW: 3,
    Stage.INTEGRATE: 4,
    Stage.DONE: 5,
}


@dataclass(frozen=True)
class ProgressState:
    stage: str
    status: str
    rank: int
    candidate: str | None
    failure: str | None  # signature of the latest failed check on the latest candidate, None when it has none
    remediation: str | None  # active stage/candidate handoff lineage; a new lineage is progress exactly once
    milestones: tuple[str, ...]  # exact browser captures and executed independent review-role passes

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "candidate": self.candidate,
            "failure": self.failure,
            "remediation": self.remediation,
            "milestones": list(self.milestones),
        }


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _failure_signature(store: Store, task_id: str, candidate: str | None) -> str | None:
    """A stable digest of what failed (stage, codes, failed gates and their timeout class), insensitive to shas, numbers and paths."""
    if candidate is None:
        return None
    from .diagnosis import normalize

    row = store.conn.execute(
        "SELECT kind, payload FROM evidence WHERE task_id=? AND candidate_sha=? AND status='FAILED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate),
    ).fetchone()
    if row is None:
        return None
    payload = _loads(row["payload"])
    findings = [f for f in payload.get("findings", []) if isinstance(f, dict)]
    parts: list[str] = [str(row["kind"])]
    for finding in sorted(findings, key=lambda f: (str(f.get("code")), str(f.get("message")))):
        diagnosis = (finding.get("execution") or {}).get("diagnosis") if isinstance(finding.get("execution"), dict) else None
        timeout = diagnosis.get("timeout_class") if isinstance(diagnosis, dict) else None
        parts.append("|".join((str(finding.get("code")), normalize(str(finding.get("message", ""))), str(timeout or ""))))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _milestones(store: Store, task_id: str, candidate: str | None) -> tuple[str, ...]:
    """Durable, candidate-bound facts that legitimately consume more than one capped child step."""
    if candidate is None:
        return ()
    tokens: set[str] = set()
    for row in store.task_audit_events("task.browser_evidence_captured", task_id):
        payload = _loads(row["payload"])
        capture_id = payload.get("capture_id")
        if payload.get("candidate_sha") == candidate and isinstance(capture_id, str) and capture_id:
            tokens.add(f"browser:{capture_id}")
    from .review import ROLE_ADVERSE, ROLE_NORMAL, independent_review_verified

    rows = store.conn.execute(
        "SELECT id, payload FROM evidence WHERE task_id=? AND candidate_sha=? "
        "AND kind=? AND status=? ORDER BY rowid",
        (task_id, candidate, EvidenceKind.REVIEW, EvidenceStatus.PASSED),
    )
    for row in rows:
        payload = _loads(row["payload"])
        if (
            payload.get("review_role") in {ROLE_NORMAL, ROLE_ADVERSE}
            and payload.get("contract_hash")
            and independent_review_verified(payload)
        ):
            tokens.add(f"review:{row['id']}")
    return tuple(sorted(tokens))


def progress_state(store: Store, task_id: str) -> ProgressState:
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    candidate = None if latest is None else str(latest["sha"])
    try:
        stage = Stage(task["stage"]) if task is not None else Stage.PLAN
    except ValueError:  # an unknown stage label is "no rank", never a crash in the run summary
        stage = Stage.PLAN
    remediation = store.conn.execute(
        "SELECT stage, candidate_sha FROM task_remediations WHERE task_id=? AND cleared=0 ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return ProgressState(
        stage.value,
        "MISSING" if task is None else str(task["status"]),
        _RANK.get(stage, 0),
        candidate,
        _failure_signature(store, task_id, candidate),
        None if remediation is None else f"{remediation['stage']}:{remediation['candidate_sha']}",
        _milestones(store, task_id, candidate),
    )


def made_progress(before: ProgressState, after: ProgressState) -> tuple[bool, str]:
    """Whether the run moved the task forward, and the fact that says so (or why not)."""
    if after.status == TaskStatus.DONE and before.status != TaskStatus.DONE:
        return True, "the task completed"
    if after.rank > before.rank:
        return True, f"advanced from {before.stage} to {after.stage}"
    new_milestones = set(after.milestones) - set(before.milestones)
    if any(item.startswith("review:") for item in new_milestones):
        return True, "an executed independent review role passed on the exact candidate"
    if any(item.startswith("browser:") for item in new_milestones):
        return True, "new exact-candidate browser evidence was captured"
    if after.remediation is not None and after.remediation != before.remediation:
        return True, "accepted new failed-check findings for implementation remediation"
    if after.candidate != before.candidate:
        if after.failure is None:
            return True, "a new candidate with no failed check"
        if after.failure != before.failure:
            return True, "a new candidate with a different failure: the earlier cause was resolved"
        return False, "a new candidate failed the same way as before"
    return False, f"still at {after.stage} on the same candidate with the same failure"


def record_step_cap(store: Store, task_id: str, before: ProgressState, after: ProgressState, steps: int) -> dict[str, Any]:
    progressed, reason = made_progress(before, after)
    if reason == "accepted new failed-check findings for implementation remediation":
        remediation_stage, _, candidate_sha = after.remediation.partition(":")
        rows = store.conn.execute(
            "SELECT COUNT(*) FROM task_remediations WHERE task_id=? AND stage=? AND candidate_sha=?",
            (task_id, remediation_stage, candidate_sha),
        ).fetchone()
        if rows is not None and int(rows[0]) > 1:
            progressed = False
            reason = "the same remediation lineage was already attempted for this candidate"
    note = {
        "task_id": task_id, "progressed": progressed, "reason": reason, "steps": steps,
        "before": before.to_dict(), "after": after.to_dict(),
    }
    record_audit(store, STEP_CAP_EVENT, note)
    return note


def _last_fresh_start(store: Store, task_id: str) -> float:
    from .diagnosis import _last_reset

    return _last_reset(store, task_id)


def unproductive_step_caps(store: Store, task_id: str) -> int:
    """Consecutive most-recent capped runs without meaningful progress since the task last had a fresh start. Survives restarts."""
    since = _last_fresh_start(store, task_id)
    count = 0
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? AND created_at>? ORDER BY created_at DESC, rowid DESC LIMIT 200", (STEP_CAP_EVENT, since)
    ).fetchall()
    for row in rows:
        payload = _loads(row["payload"])
        if payload.get("task_id") != task_id:
            continue
        if payload.get("progressed"):
            break
        count += 1
    return count


def stalled(store: Store, task_id: str, limit: int = STEP_CAP_STALL_LIMIT) -> bool:
    return unproductive_step_caps(store, task_id) >= limit
