"""Serialize one coordinator stage per database/task before ownership publication."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .concurrency import IntegrationLock


def stage_dispatch_lock(database: Path, task_id: str) -> IntegrationLock:
    """Reuse the existing native lock, without waiting or trusting file contents.

    Files are stable resource anchors, not PID/lease records. Never unlink one
    on release: another runner may already have opened the same resource.
    """
    database = database.resolve()
    key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return IntegrationLock(database.with_name(database.name + ".stage-locks") / (key + ".lock"), timeout_seconds=0)


def stage_dispatch_refusal(refusals: list[dict[str, object]]) -> tuple[str, str]:
    """Keep unavailable admission or lost eligibility distinct from actual owners."""
    codes = {str(item.get("reason_code") or "active_execution") for item in refusals}
    if codes == {"stage_dispatch_unavailable"}:
        return "REFUSED:stage_dispatch_unavailable", "task stage dispatch lock is unavailable; no new stage work started"
    if codes == {"stage_dispatch_ineligible"}:
        return "REFUSED:stage_dispatch_ineligible", "task is no longer eligible at stage dispatch; no new stage work started"
    if codes == {"active_execution"} and all(item.get("execution_ids") for item in refusals):
        return "REFUSED:active_execution", "existing stage ownership was retained; no new stage work started"
    return "REFUSED:stage_dispatch", "selected stage dispatches were refused; inspect the per-task reasons"
