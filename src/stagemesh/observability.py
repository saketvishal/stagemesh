from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .domain import ExecutionStatus, Stage, TaskStatus
from .persistence import Store


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    task_count: int
    blocked_task_count: int
    running_count: int
    done_count: int
    failed_execution_count: int
    unknown_execution_count: int
    backlog_state: str


def health(store: Store) -> HealthReport:
    tasks = store.tasks()
    running = list(store.running_executions())
    done = [task for task in tasks if task["stage"] == Stage.DONE or task["status"] == "DONE"]
    blocked = [task for task in tasks if task["status"] == TaskStatus.BLOCKED]
    failed_execution_count = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status=?",
            (ExecutionStatus.FAILED,),
        ).fetchone()[0]
    )
    unknown_execution_count = int(
        store.conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status=?",
            (ExecutionStatus.UNKNOWN,),
        ).fetchone()[0]
    )
    backlog_state = "EMPTY" if not tasks else "ACTIVE"
    return HealthReport(
        ok=not blocked and failed_execution_count == 0 and unknown_execution_count == 0,
        task_count=len(tasks),
        blocked_task_count=len(blocked),
        running_count=len(running),
        done_count=len(done),
        failed_execution_count=failed_execution_count,
        unknown_execution_count=unknown_execution_count,
        backlog_state=backlog_state,
    )


# ---------------------------------------------------------------------------
# Metrics snapshot — legacy coordinator_metrics() observable contract
# ---------------------------------------------------------------------------

def metrics_snapshot(store: Store, now: float | None = None) -> dict[str, Any]:
    """Return a JSON-serialisable metrics dict derived solely from durable rows.

    Observable categories (matching legacy build_coordinator/metrics.py):
      - queue_depth: total open tasks + breakdown by stage
      - execution_outcomes: total + by_status + by_kind
      - provider_usage: execution counts by provider/worker_id
      - retry_state: active retry entries count

    No secrets are included — worker credentials and environment variables
    are never serialised.
    """
    if now is None:
        now = time.time()

    tasks = store.tasks()
    open_tasks = [t for t in tasks if t["stage"] not in (Stage.DONE,) and t["status"] not in (TaskStatus.DONE,)]

    # Queue depth
    by_stage: dict[str, int] = {}
    for t in open_tasks:
        stage = str(t["stage"])
        by_stage[stage] = by_stage.get(stage, 0) + 1

    queue_depth: dict[str, Any] = {
        "total": len(open_tasks),
        "by_stage": dict(sorted(by_stage.items())),
    }

    # Execution outcomes
    exec_rows = list(
        store.conn.execute("SELECT kind, status, candidate_sha FROM executions ORDER BY started_at")
    )
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for row in exec_rows:
        s = str(row["status"])
        k = str(row["kind"])
        by_status[s] = by_status.get(s, 0) + 1
        by_kind[k] = by_kind.get(k, 0) + 1

    execution_outcomes: dict[str, Any] = {
        "total": len(exec_rows),
        "by_status": dict(sorted(by_status.items())),
        "by_kind": dict(sorted(by_kind.items())),
    }

    # Provider / worker usage — derived from executions table (no secret fields)
    worker_rows = list(
        store.conn.execute(
            "SELECT id, provider FROM workers ORDER BY id"
        )
    )
    provider_usage: dict[str, int] = {}
    for row in worker_rows:
        prov = str(row["provider"])
        provider_usage[prov] = provider_usage.get(prov, 0) + 1

    # Retry state
    retry_count = int(
        store.conn.execute("SELECT COUNT(*) FROM retry_state").fetchone()[0]
    )

    return {
        "generated_at": now,
        "queue_depth": queue_depth,
        "execution_outcomes": execution_outcomes,
        "provider_usage": dict(sorted(provider_usage.items())),
        "retry_state": {"active_entries": retry_count},
    }


def export_metrics_json(store: Store, now: float | None = None) -> str:
    """Return metrics as a compact JSON string (no secrets, deterministic keys)."""
    snapshot = metrics_snapshot(store, now=now)
    return json.dumps(snapshot, sort_keys=True)

