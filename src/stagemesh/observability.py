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

import re
from statistics import mean, median

_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{15,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|auth)\b\s*[:=]\s*([^\s,;\"']+)"),
)

_SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(token|secret|password|key|credential|auth)")


def redact_sensitive_value(value: Any) -> Any:
    """Recursively scrub secrets from metrics data structures.

    Applies value-based pattern matching (GitHub PATs, API keys, Bearer tokens,
    credential assignments) and key-based masking for sensitive fields.
    """
    if isinstance(value, str):
        result = value
        for pattern in _SECRET_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            if _SENSITIVE_KEY_PATTERN.search(str(k)):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = redact_sensitive_value(v)
        return cleaned
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    return value


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "avg_seconds": None,
            "p50_seconds": None,
            "max_seconds": None,
        }
    return {
        "count": len(values),
        "avg_seconds": round(float(mean(values)), 3),
        "p50_seconds": round(float(median(values)), 3),
        "max_seconds": round(float(max(values)), 3),
    }


def metrics_snapshot(store: Store, now: float | None = None) -> dict[str, Any]:
    """Return a metrics dictionary derived from durable coordinator rows.

    Observable categories (matching legacy build_coordinator/metrics.py):
      - queue_depth: total open tasks + breakdown by stage and status
      - claim_latency: task creation to claim latency (implementation and by_stage)
      - execution_outcomes: total + by_status + by_kind + duration stats
      - provider_usage: execution counts, active claims, busy seconds, and outcomes per provider
      - throughput: completed tasks and executions with total busy time
      - token_usage: estimated context tokens and reported evidence tokens
      - retry_state: active retry entries count
    """
    if now is None:
        now = time.time()

    tasks = store.tasks()
    open_tasks = [t for t in tasks if t["stage"] not in (Stage.DONE,) and t["status"] not in (TaskStatus.DONE,)]

    # 1. Queue depth
    by_stage: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for t in open_tasks:
        stage = str(t["stage"])
        status = str(t["status"])
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1

    queue_depth: dict[str, Any] = {
        "total": len(open_tasks),
        "by_stage": dict(sorted(by_stage.items())),
        "by_status": dict(sorted(by_status.items())),
    }

    # 2. Claim latency: time between task created_at and claim created_at
    task_created_map = {t["id"]: float(t["created_at"]) for t in tasks}
    claims_rows = list(
        store.conn.execute("SELECT id, task_id, stage, created_at FROM claims ORDER BY created_at ASC")
    )
    claim_exec_kinds = {
        str(r["claim_id"]): str(r["kind"])
        for r in store.conn.execute("SELECT claim_id, kind FROM executions WHERE claim_id IS NOT NULL")
    }

    first_claims: dict[tuple[str, str], float] = {}
    implementation_latencies: list[float] = []
    seen_impl_claims: set[str] = set()

    for row in claims_rows:
        cid = str(row["id"])
        tid = str(row["task_id"])
        stg = str(row["stage"])
        claimed_at = float(row["created_at"])
        if (tid, stg) not in first_claims:
            first_claims[(tid, stg)] = claimed_at

        if tid in task_created_map and tid not in seen_impl_claims:
            lat = max(0.0, claimed_at - task_created_map[tid])
            exec_k = claim_exec_kinds.get(cid)
            if exec_k in ("IMPLEMENTATION", "IMPLEMENT") or stg in (str(Stage.IMPLEMENT), "IMPLEMENTATION"):
                implementation_latencies.append(lat)
                seen_impl_claims.add(tid)

    by_claim_stage: dict[str, list[float]] = {}
    for (tid, stg), claimed_at in first_claims.items():
        if tid in task_created_map:
            lat = max(0.0, claimed_at - task_created_map[tid])
            by_claim_stage.setdefault(stg, []).append(lat)

    claim_latency: dict[str, Any] = {
        "implementation": _latency_summary(implementation_latencies),
        "by_stage": {stg: _latency_summary(lats) for stg, lats in sorted(by_claim_stage.items())},
    }

    # 3. Execution outcomes & execution duration
    exec_rows = list(
        store.conn.execute("SELECT id, task_id, claim_id, kind, status, started_at, updated_at FROM executions ORDER BY started_at")
    )
    exec_by_status: dict[str, int] = {}
    exec_by_kind: dict[str, int] = {}
    durations: list[float] = []
    total_exec_busy_seconds = 0.0

    for row in exec_rows:
        s = str(row["status"])
        k = str(row["kind"])
        exec_by_status[s] = exec_by_status.get(s, 0) + 1
        exec_by_kind[k] = exec_by_kind.get(k, 0) + 1

        started = float(row["started_at"])
        updated = float(row["updated_at"])
        if s in ("RUNNING", "LAUNCHED"):
            busy = max(0.0, now - started)
            total_exec_busy_seconds += busy
        else:
            dur = max(0.0, updated - started)
            durations.append(dur)
            total_exec_busy_seconds += dur

    failures_count = exec_by_status.get("FAILED", 0) + exec_by_status.get("UNKNOWN", 0)
    execution_outcomes: dict[str, Any] = {
        "total": len(exec_rows),
        "failures": failures_count,
        "by_status": dict(sorted(exec_by_status.items())),
        "by_kind": dict(sorted(exec_by_kind.items())),
        "duration_seconds": _latency_summary(durations),
    }

    # 4. Provider usage — counting actual executions and claims per provider
    # Map claim_id -> worker_id, and worker_id -> provider
    claims_to_worker = {
        str(row["id"]): str(row["worker_id"])
        for row in store.conn.execute("SELECT id, worker_id FROM claims")
    }
    worker_to_provider = {
        str(row["id"]): str(row["provider"])
        for row in store.conn.execute("SELECT id, provider FROM workers")
    }

    # Active claims per worker
    active_claims_by_worker: dict[str, int] = {}
    for row in store.conn.execute("SELECT worker_id, COUNT(*) FROM claims WHERE active = 1 GROUP BY worker_id"):
        active_claims_by_worker[str(row[0])] = int(row[1])

    provider_metrics: dict[str, dict[str, Any]] = {}

    # Initialize from known workers
    for wid, prov in worker_to_provider.items():
        if prov not in provider_metrics:
            provider_metrics[prov] = {
                "active_claims": 0,
                "active_executions": 0,
                "total_executions": 0,
                "completed_executions": 0,
                "total_busy_seconds": 0.0,
                "outcomes": {},
            }
        provider_metrics[prov]["active_claims"] += active_claims_by_worker.get(wid, 0)

    # Accumulate execution counts and durations by provider
    for row in exec_rows:
        cid = str(row["claim_id"]) if row["claim_id"] else None
        wid = claims_to_worker.get(cid) if cid else None
        prov = worker_to_provider.get(wid) if wid else (str(row["kind"]) if row["kind"] else "default")

        if prov not in provider_metrics:
            provider_metrics[prov] = {
                "active_claims": 0,
                "active_executions": 0,
                "total_executions": 0,
                "completed_executions": 0,
                "total_busy_seconds": 0.0,
                "outcomes": {},
            }

        m = provider_metrics[prov]
        m["total_executions"] += 1
        st = str(row["status"])
        m["outcomes"][st] = m["outcomes"].get(st, 0) + 1

        started = float(row["started_at"])
        updated = float(row["updated_at"])
        if st in ("RUNNING", "LAUNCHED"):
            m["active_executions"] += 1
            m["total_busy_seconds"] += max(0.0, now - started)
        else:
            m["completed_executions"] += 1
            m["total_busy_seconds"] += max(0.0, updated - started)

    for prov, data in provider_metrics.items():
        data["total_busy_seconds"] = round(data["total_busy_seconds"], 3)
        data["outcomes"] = dict(sorted(data["outcomes"].items()))

    # 5. Throughput
    done_tasks = [t for t in tasks if t["stage"] in (Stage.DONE,) or t["status"] in (TaskStatus.DONE,)]
    completed_executions = sum(m["completed_executions"] for m in provider_metrics.values())
    throughput: dict[str, Any] = {
        "completed_tasks": len(done_tasks),
        "completed_executions": completed_executions,
        "total_busy_seconds": round(total_exec_busy_seconds, 3),
    }

    # 6. Token usage
    # Estimate tokens (~4 chars per token) across tasks + extract reported token counts from evidence
    estimated_context_tokens = sum(len(str(t["title"] or "")) // 4 for t in tasks)
    reported_tokens = 0
    evidence_rows = list(store.conn.execute("SELECT payload FROM evidence"))
    for row in evidence_rows:
        try:
            payload = json.loads(str(row["payload"]))
            if isinstance(payload, dict):
                t_count = 0
                if "usage" in payload and isinstance(payload["usage"], dict):
                    t_count = int(payload["usage"].get("total_tokens", 0))
                if t_count == 0:
                    for k in ("tokens", "token_count", "total_tokens"):
                        if k in payload and isinstance(payload[k], (int, float)):
                            t_count = int(payload[k])
                            break
                reported_tokens += t_count
        except (json.JSONDecodeError, TypeError):
            pass

    token_usage: dict[str, Any] = {
        "estimated_context_tokens": estimated_context_tokens,
        "reported_evidence_tokens": reported_tokens,
        "total_tokens": estimated_context_tokens + reported_tokens,
    }

    # 7. Retry state
    retry_count = int(
        store.conn.execute("SELECT COUNT(*) FROM retry_state").fetchone()[0]
    )

    snapshot = {
        "generated_at": now,
        "queue_depth": queue_depth,
        "claim_latency": claim_latency,
        "execution_outcomes": execution_outcomes,
        "provider_usage": dict(sorted(provider_metrics.items())),
        "throughput": throughput,
        "token_usage": token_usage,
        "retry_state": {"active_entries": retry_count},
    }
    return snapshot


def export_metrics_json(store: Store, now: float | None = None) -> str:
    """Return metrics as a formatted JSON string with deterministic keys and secrets scrubbed."""
    snapshot = metrics_snapshot(store, now=now)
    redacted = redact_sensitive_value(snapshot)
    return json.dumps(redacted, indent=2, sort_keys=True)


