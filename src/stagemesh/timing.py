"""Lifecycle execution timing, derived from the durable `executions` records.

An execution's `started_at` is set when the stage begins and `updated_at` when it finishes, so
`duration = finished_at - started_at`. Time spent waiting for a human between stages is never part of an
execution and therefore never counted. Executions that are still RUNNING have no finish time yet.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from .domain import ExecutionKind, ExecutionStatus
from .persistence import Store

_STAGE_ORDER = (ExecutionKind.IMPLEMENTATION, ExecutionKind.VALIDATION, ExecutionKind.REVIEW, ExecutionKind.INTEGRATION)
_STAGE_LABEL = {
    ExecutionKind.IMPLEMENTATION: "IMPLEMENT",
    ExecutionKind.VALIDATION: "VALIDATE",
    ExecutionKind.REVIEW: "REVIEW",
    ExecutionKind.INTEGRATION: "INTEGRATE",
}
_RESULT = {
    ExecutionStatus.SUCCEEDED: "passed",
    ExecutionStatus.FAILED: "failed",
    ExecutionStatus.RUNNING: "running",
    ExecutionStatus.UNKNOWN: "unknown",
}


def format_duration(seconds: float | None) -> str:
    """Compact human duration: 42s, 3m 18s, 1h 04m. Fractions round to the nearest second; hours drop the seconds."""
    if seconds is None:
        return "-"
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_timestamp(epoch: float | None) -> str:
    return "-" if epoch is None else datetime.fromtimestamp(epoch, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def execution_timings(store: Store, task_id: str) -> list[dict[str, Any]]:
    """One record per execution of the task, oldest first, with a per-stage attempt number."""
    rows = store.conn.execute("SELECT * FROM executions WHERE task_id=? ORDER BY started_at, rowid", (task_id,)).fetchall()
    attempts: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row["kind"])
        attempts[kind] = attempts.get(kind, 0) + 1
        status = ExecutionStatus(row["status"])
        finished = None if status is ExecutionStatus.RUNNING else float(row["updated_at"])
        started = float(row["started_at"])
        records.append(
            {
                "execution_id": row["id"],
                "task_id": task_id,
                "stage": _STAGE_LABEL.get(ExecutionKind(kind), kind),
                "kind": kind,
                "attempt": attempts[kind],
                "actor": row["actor"],
                "candidate_sha": row["candidate_sha"],
                "started_at": started,
                "finished_at": finished,
                "duration_seconds": None if finished is None else round(max(0.0, finished - started), 3),
                "status": str(status),
                "result": _RESULT[status],
                "reason": row["result"],
            }
        )
    return records


def task_timing(store: Store, task_id: str) -> dict[str, Any]:
    """Timing records plus a summary. First attempts count per stage; later attempts are retries/remediation."""
    records = execution_timings(store, task_id)
    stages = {_STAGE_LABEL[kind]: 0.0 for kind in _STAGE_ORDER}
    retries = 0.0
    for rec in records:
        if rec["duration_seconds"] is None:
            continue
        if rec["attempt"] == 1 and rec["stage"] in stages:
            stages[rec["stage"]] += rec["duration_seconds"]
        else:
            retries += rec["duration_seconds"]
    return {
        "task_id": task_id,
        "executions": records,
        "summary": {
            "stage_seconds": {k: round(v, 3) for k, v in stages.items()},
            "retries_seconds": round(retries, 3),
            "total_execution_seconds": round(sum(stages.values()) + retries, 3),
            "running": sum(1 for rec in records if rec["finished_at"] is None),
        },
    }


def step_duration(records: list[dict[str, Any]]) -> float | None:
    """Total execution time of the finished executions a lifecycle step ran, or None when it ran none."""
    finished = [rec["duration_seconds"] for rec in records if rec["duration_seconds"] is not None]
    return round(sum(finished), 3) if finished else None


def format_task_summary(timing: dict[str, Any]) -> str:
    summary = timing["summary"]
    lines = [f"Task {timing['task_id']} timing"]
    for stage, seconds in summary["stage_seconds"].items():
        if any(rec["stage"] == stage for rec in timing["executions"]):
            lines.append(f"  {stage}: {format_duration(seconds)}")
    lines.append(f"  retries/remediation: {format_duration(summary['retries_seconds'])}")
    lines.append(f"  total execution time: {format_duration(summary['total_execution_seconds'])}")
    if summary["running"]:
        lines.append(f"  ({summary['running']} execution(s) still running, not counted)")
    return "\n".join(lines)


def format_execution(rec: dict[str, Any], *, verbose: bool = False, now: float | None = None) -> str:
    lines = [f"{rec['stage'].title()} #{rec['attempt']}"]
    if verbose:
        lines.append(f"  task: {rec['task_id']}")
    lines.append(f"  result: {rec['result']}" + (f" ({rec['reason']})" if rec["reason"] else ""))
    lines.append(f"  actor: {rec['actor'] or 'unknown'}")
    if verbose:
        if rec["candidate_sha"]:
            lines.append(f"  candidate: {rec['candidate_sha']}")
        lines.append(f"  started: {format_timestamp(rec['started_at'])}")
        lines.append(f"  finished: {format_timestamp(rec['finished_at'])}")
    if rec["duration_seconds"] is not None:
        lines.append(f"  duration: {format_duration(rec['duration_seconds'])}")
    else:
        elapsed = (time.time() if now is None else now) - rec["started_at"]
        lines.append(f"  duration: {format_duration(elapsed)} so far")
    return "\n".join(lines)


def format_task_timing(timing: dict[str, Any], *, verbose: bool = False) -> str:
    blocks = [format_execution(rec, verbose=verbose) for rec in timing["executions"]]
    blocks.append(format_task_summary(timing))
    return "\n\n".join(blocks)
