"""Did a step-capped run make meaningful progress, and how many capped runs in a row have not?

Hitting the step cap is not a failure by itself: the task keeps its stage, candidate and evidence in the ledger, so the next run
resumes exactly there and never repeats finished implementation. What must not happen is a task re-entering window after window
and spending its steps on the same failing loop. "Progress" is therefore defined by facts that cannot be faked by busy work:

* the task reached a later lifecycle stage than the run began at, or finished;
* the run ended on a new candidate whose failure is different from the one it began with (the earlier cause was resolved), or on a
  new candidate with no failure at all.

More evidence rows, more remediation attempts or a new but equivalent failure are activity, not progress. Each step-capped run is
recorded (`task.step_cap`); the streak of consecutive unproductive capped runs is read back from the ledger, so it survives windows
and restarts, and an operator action that gives the task a fresh start (unblock, contract rebind, rebaseline) ends it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .domain import Stage, TaskStatus
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

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "candidate": self.candidate, "failure": self.failure}


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


def progress_state(store: Store, task_id: str) -> ProgressState:
    task = store.get_task(task_id)
    latest = store.latest_candidate(task_id)
    candidate = None if latest is None else str(latest["sha"])
    stage = Stage(task["stage"]) if task is not None else Stage.PLAN
    return ProgressState(
        stage.value, "MISSING" if task is None else str(task["status"]), _RANK.get(stage, 0), candidate, _failure_signature(store, task_id, candidate),
    )


def made_progress(before: ProgressState, after: ProgressState) -> tuple[bool, str]:
    """Whether the run moved the task forward, and the fact that says so (or why not)."""
    if after.status == TaskStatus.DONE and before.status != TaskStatus.DONE:
        return True, "the task completed"
    if after.rank > before.rank:
        return True, f"advanced from {before.stage} to {after.stage}"
    if after.candidate != before.candidate:
        if after.failure is None:
            return True, "a new candidate with no failed check"
        if after.failure != before.failure:
            return True, "a new candidate with a different failure: the earlier cause was resolved"
        return False, "a new candidate failed the same way as before"
    return False, f"still at {after.stage} on the same candidate with the same failure"


def record_step_cap(store: Store, task_id: str, before: ProgressState, after: ProgressState, steps: int) -> dict[str, Any]:
    progressed, reason = made_progress(before, after)
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
