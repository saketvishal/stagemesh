"""Honest, bounded measurement of how much work delivery costs, and budgets that stop blind repetition.

What is recorded (all as small audit events; nothing here stores prompts, provider output, secrets or client content):

* `provider.call`    one per provider invocation: task, candidate, provider, purpose (`implementation`, `remediation`, `review`,
                     `diagnosis`), outcome, a short normalized failure class, duration, and the usage the provider itself reported.
* `stage.transition` one per lifecycle stage change of a task.

Everything else a report needs is already in the ledger: executions (stage, actor, timing), validation evidence
(`validation_reuse` says which checks ran and which were reused), the window journal and the delivery events.

Usage honesty: a provider that does not report usage yields `reported: false` and `null` counts. Unknown is never stored or summed
as zero; totals say how many calls were unknown. Counts are labelled `unit: "tokens"` and stored under `input` / `output` /
`cached_input` (the audit redactor masks any key that contains the word for a credential, so a literal key would be lost).

Budgets are enforced only at safe stage boundaries (before the next stage starts or the next wave is admitted) and are derived from
the ledger, so they survive new windows and restarts. A fresh start by the operator (`retry-task`, rebind, rebaseline) resets a
task's own counters; the window budget is scoped to the window by time.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .audit import record_audit
from .persistence import Store

PROVIDER_CALL = "provider.call"
STAGE_TRANSITION = "stage.transition"
PURPOSES = ("diagnosis", "implementation", "remediation", "review", "research")
BUDGET_EXHAUSTED = "task.budget_exhausted"

_SPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


# --- usage ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Provider-reported usage. `None` means the provider did not say; it is never treated as zero."""

    input: int | None = None
    output: int | None = None
    cached_input: int | None = None

    @property
    def reported(self) -> bool:
        return any(value is not None for value in (self.input, self.output, self.cached_input))

    def to_dict(self) -> dict[str, Any]:
        return {"input": self.input, "output": self.output, "cached_input": self.cached_input, "unit": "tokens", "reported": self.reported}


_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output")
_CACHED_KEYS = ("cached_input_tokens", "cache_read_input_tokens", "cached_tokens")


def _count(source: dict[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = source.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _usage_from(obj: Any) -> Usage | None:
    if not isinstance(obj, dict):
        return None
    usage = Usage(_count(obj, _INPUT_KEYS), _count(obj, _OUTPUT_KEYS), _count(obj, _CACHED_KEYS))
    return usage if usage.reported else None


def _json_objects(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
            if isinstance(value, dict):
                return [value]
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found


def extract_usage(stdout: str, stderr: str = "") -> Usage:
    """Usage a provider reported in its own structured output; `Usage()` (unknown) when it reported none or the shape is ambiguous.

    Three shapes are understood, each unambiguous about aggregation: a final `result` object with a `usage` block (the total), a
    stream of `turn.completed` events each with a `usage` block (summed), and a single object carrying a `usage` block. Anything
    else - several unlabelled usage blocks, free text such as "tokens used: 12k" - is reported as unknown rather than guessed.
    """
    objects = _json_objects(stdout)
    results = [_usage_from(o.get("usage")) for o in objects if o.get("type") == "result" and "usage" in o]
    results = [u for u in results if u is not None]
    if results:
        return results[-1]
    turns = [_usage_from(o.get("usage")) for o in objects if o.get("type") == "turn.completed" and "usage" in o]
    turns = [u for u in turns if u is not None]
    if turns:
        def total(name: str) -> int | None:
            values = [getattr(u, name) for u in turns if getattr(u, name) is not None]
            return sum(values) if values else None

        return Usage(total("input"), total("output"), total("cached_input"))
    blocks = [u for o in objects if (u := _usage_from(o.get("usage"))) is not None]
    return blocks[0] if len(blocks) == 1 else Usage()


_local = threading.local()


def note_call_usage(usage: Usage) -> None:
    """Called by a provider adapter on the calling thread right after its subprocess ends."""
    _local.usage = usage


def take_call_usage() -> Usage:
    """The usage the adapter noted for the call this thread just made (unknown if it noted none); clears it either way."""
    usage = getattr(_local, "usage", None)
    _local.usage = None
    return usage if isinstance(usage, Usage) else Usage()


# --- recording -----------------------------------------------------------------------------------------------------------------


def failure_class(reason: str | None) -> str | None:
    """A short, normalized, secret-free label so equivalent failures compare equal across candidates and windows."""
    if not reason:
        return None
    text = _HEX.sub("<sha>", str(reason))
    text = _DIGITS.sub("#", text)
    return _SPACE.sub(" ", text).strip().casefold()[:80] or None


def record_provider_call(
    store: Store,
    *,
    task_id: str,
    purpose: str,
    provider: str,
    outcome: str,
    duration_seconds: float | None,
    candidate_sha: str | None = None,
    reason: str | None = None,
    usage: Usage | None = None,
) -> None:
    """Record one provider invocation. Never raises: measurement must not change what it measures."""
    if purpose not in PURPOSES:
        purpose = "implementation"
    try:
        record_audit(store, PROVIDER_CALL, {
            "task_id": task_id, "candidate_sha": candidate_sha, "provider": provider[:80], "purpose": purpose, "outcome": outcome,
            "failure_class": None if outcome == "succeeded" else failure_class(reason),
            "duration_seconds": None if duration_seconds is None else round(max(0.0, float(duration_seconds)), 3),
            "usage": (usage or Usage()).to_dict(),
        })
    except Exception:  # noqa: BLE001
        pass


class CallRecorder:
    """Times one provider invocation and records it once, whichever way it ends. Used by every executor that spawns a provider."""

    def __init__(self, store: Store, task_id: str, purpose: str, provider: str):
        self.store, self.task_id, self.purpose, self.provider = store, task_id, purpose, provider
        self.usage = Usage()
        self._started = time.monotonic()
        self._done = False

    def observe(self, stdout: str, stderr: str = "") -> None:
        self.usage = extract_usage(stdout, stderr)

    def finish(self, outcome: str, reason: str | None = None, candidate: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        record_provider_call(
            self.store, task_id=self.task_id, purpose=self.purpose, provider=self.provider, outcome=outcome, reason=reason,
            candidate_sha=candidate, duration_seconds=time.monotonic() - self._started, usage=self.usage,
        )


# --- reading -------------------------------------------------------------------------------------------------------------------


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _since_reset(store: Store, task_id: str) -> float:
    from .diagnosis import _last_reset

    try:
        return float(_last_reset(store, task_id))
    except Exception:  # noqa: BLE001
        return 0.0


def provider_calls(store: Store, task_id: str, since: float = 0.0, until: float | None = None) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT payload, created_at FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id') = ? AND created_at>=? "
        "AND (? IS NULL OR created_at<=?) ORDER BY created_at, rowid",
        (PROVIDER_CALL, task_id, since, until, until),
    ).fetchall()
    return [{**_loads(row["payload"]), "at": float(row["created_at"])} for row in rows]


def equivalent_failure_streak(calls: list[dict[str, Any]]) -> tuple[int, str | None]:
    """How many of the most recent calls failed the same way with no success between them, and that failure class."""
    streak, signature = 0, None
    for call in reversed(calls):
        if call.get("outcome") == "succeeded":
            break
        current = call.get("failure_class") or call.get("outcome")
        if signature is None:
            signature = current
        if current != signature:
            break
        streak += 1
    return streak, signature


def usage_totals(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over *reported* usage only, with the unknown calls counted separately (never as zero)."""
    totals: dict[str, int | None] = {"input": None, "output": None, "cached_input": None}
    reported = 0
    for call in calls:
        usage = call.get("usage") or {}
        if not usage.get("reported"):
            continue
        reported += 1
        for name in totals:
            value = usage.get(name)
            if isinstance(value, int):
                totals[name] = (totals[name] or 0) + value
    return {**totals, "unit": "tokens", "calls_reporting": reported, "calls_unknown": len(calls) - reported}


# --- budgets -------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetPolicy:
    """Limits checked at stage boundaries. `None` disables a limit."""

    max_provider_calls_per_task: int | None = 30
    max_equivalent_failures: int | None = 5
    max_provider_calls_per_window: int | None = None
    max_reported_usage_per_task: int | None = None  # input + output reported by providers; unknown calls are not counted

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_config(cls, budgets: Any) -> BudgetPolicy:
        return cls(**{name: getattr(budgets, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class BudgetVerdict:
    exceeded: str  # "equivalent_failures" | "provider_calls" | "reported_usage" | "window_provider_calls"
    detail: str
    observed: int
    limit: int


def check_task_budget(store: Store, task_id: str, policy: BudgetPolicy) -> BudgetVerdict | None:
    """Called between stages, never during one. Counts everything since the task's last fresh start, across windows and restarts."""
    calls = provider_calls(store, task_id, _since_reset(store, task_id))
    if policy.max_equivalent_failures:
        streak, signature = equivalent_failure_streak(calls)
        if streak >= policy.max_equivalent_failures:
            return BudgetVerdict(
                "equivalent_failures",
                f"{streak} consecutive provider calls failed the same way ({signature}); a further identical attempt would repeat the loop",
                streak, policy.max_equivalent_failures,
            )
    if policy.max_provider_calls_per_task and len(calls) >= policy.max_provider_calls_per_task:
        return BudgetVerdict("provider_calls", f"{len(calls)} provider calls since the task's last fresh start", len(calls), policy.max_provider_calls_per_task)
    if policy.max_reported_usage_per_task:
        totals = usage_totals(calls)
        spent = (totals["input"] or 0) + (totals["output"] or 0)
        if spent >= policy.max_reported_usage_per_task:
            return BudgetVerdict(
                "reported_usage", f"{spent} units of provider-reported usage ({totals['calls_unknown']} call(s) reported none and are not counted)",
                spent, policy.max_reported_usage_per_task,
            )
    return None


def check_window_budget(store: Store, since: float, policy: BudgetPolicy) -> BudgetVerdict | None:
    if not policy.max_provider_calls_per_window:
        return None
    row = store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=? AND created_at>=?", (PROVIDER_CALL, since)).fetchone()
    count = int(row[0])
    if count >= policy.max_provider_calls_per_window:
        return BudgetVerdict("window_provider_calls", f"{count} provider calls in this window", count, policy.max_provider_calls_per_window)
    return None


def block_reason(task_id: str, verdict: BudgetVerdict) -> str:
    return (
        f"budget_exhausted ({verdict.exceeded}): {verdict.detail}; limit {verdict.limit}. Stopped at a stage boundary; verified work is "
        f"preserved. Run `stagemesh diagnose --task {task_id}`, fix the cause, then `stagemesh retry-task` (or raise the limit under "
        "`budgets` in .stagemesh/config.json)."
    )


# --- reports -------------------------------------------------------------------------------------------------------------------


def task_efficiency(store: Store, task_id: str, since: float = 0.0, until: float | None = None) -> dict[str, Any]:
    calls = provider_calls(store, task_id, since, until)
    by_purpose: dict[str, int] = {}
    for call in calls:
        by_purpose[str(call.get("purpose"))] = by_purpose.get(str(call.get("purpose")), 0) + 1
    streak, signature = equivalent_failure_streak(calls)
    failures: dict[str, int] = {}
    for call in calls:
        if call.get("outcome") != "succeeded":
            key = str(call.get("failure_class") or call.get("outcome"))
            failures[key] = failures.get(key, 0) + 1
    transitions = [
        _loads(r["payload"]) for r in store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? AND json_extract(payload, '$.task_id') = ? AND created_at>=? "
            "AND (? IS NULL OR created_at<=?) ORDER BY created_at, rowid", (STAGE_TRANSITION, task_id, since, until, until),
        )
    ]
    ran = reused = 0
    validation_seconds = 0.0
    for row in store.conn.execute("SELECT payload FROM evidence WHERE task_id=? AND kind='VALIDATION' AND created_at>=?", (task_id, since)):
        reuse = _loads(row["payload"]).get("validation_reuse") or {}
        ran += len(reuse.get("ran") or [])
        reused += len(reuse.get("reused") or [])
    for row in store.conn.execute(
        "SELECT started_at, updated_at FROM executions WHERE task_id=? AND kind='VALIDATION' AND started_at>=?", (task_id, since)
    ):
        validation_seconds += max(0.0, float(row["updated_at"]) - float(row["started_at"]))
    task = store.get_task(task_id)
    merged = store.conn.execute(
        "SELECT created_at FROM audit_events WHERE event_type='window.fast_forward_merged' AND json_extract(payload, '$.task_id') = ? "
        "ORDER BY created_at LIMIT 1", (task_id,),
    ).fetchone()
    return {
        "task_id": task_id,
        "provider_attempts": len(calls),
        "attempts_by_purpose": by_purpose,
        "equivalent_failure_retries": sum(count - 1 for count in failures.values() if count > 1),
        "current_equivalent_failure_streak": {"count": streak, "class": signature},
        "failure_classes": failures,
        "stage_transitions": len(transitions),
        "checks_executed": ran,
        "checks_reused": reused,
        "validation_seconds": round(validation_seconds, 3),
        "usage": usage_totals(calls),
        "created_at": None if task is None else float(task["created_at"]),
        "merged_at": None if merged is None else float(merged["created_at"]),
        "seconds_to_merge": None if (task is None or merged is None) else round(float(merged["created_at"]) - float(task["created_at"]), 3),
    }


def efficiency_report(store: Store, task_ids: list[str] | None = None, since: float = 0.0, until: float | None = None) -> dict[str, Any]:
    ids = task_ids if task_ids is not None else [str(r["id"]) for r in store.tasks()]
    tasks = [task_efficiency(store, task_id, since, until) for task_id in ids]
    tasks = [t for t in tasks if t["provider_attempts"] or t["stage_transitions"] or t["checks_executed"] or t["checks_reused"]]
    calls = [call for t in ids for call in provider_calls(store, t, since, until)]
    return {
        "tasks": tasks,
        "totals": {
            "provider_attempts": sum(t["provider_attempts"] for t in tasks),
            "equivalent_failure_retries": sum(t["equivalent_failure_retries"] for t in tasks),
            "stage_transitions": sum(t["stage_transitions"] for t in tasks),
            "checks_executed": sum(t["checks_executed"] for t in tasks),
            "checks_reused": sum(t["checks_reused"] for t in tasks),
            "validation_seconds": round(sum(t["validation_seconds"] for t in tasks), 3),
            "usage": usage_totals(calls),
        },
        "note": "usage totals count only calls whose provider reported usage; calls_unknown is how many reported none (never counted as zero)",
    }


def format_efficiency(report: dict[str, Any]) -> list[str]:
    totals = report["totals"]
    usage = totals["usage"]
    lines = [
        f"provider attempts: {totals['provider_attempts']} (equivalent-failure retries: {totals['equivalent_failure_retries']})",
        f"checks executed: {totals['checks_executed']}, reused: {totals['checks_reused']}; validation time {totals['validation_seconds']:.1f}s; stage transitions: {totals['stage_transitions']}",
        "usage (provider-reported): "
        + (f"input {usage['input']}, output {usage['output']}, cached input {usage['cached_input']}" if usage["calls_reporting"] else "unknown")
        + f"; {usage['calls_reporting']} call(s) reported, {usage['calls_unknown']} unknown",
    ]
    for task in report["tasks"]:
        lines.append(
            f"  {task['task_id']}: {task['provider_attempts']} attempt(s) {task['attempts_by_purpose']}, "
            f"{task['equivalent_failure_retries']} equivalent retry(ies), {task['checks_executed']} ran/{task['checks_reused']} reused"
            + ("" if task["seconds_to_merge"] is None else f", {task['seconds_to_merge']:.0f}s to merge")
        )
    return lines
