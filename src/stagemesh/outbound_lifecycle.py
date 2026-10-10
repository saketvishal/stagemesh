from __future__ import annotations

import json
from typing import Any

from .domain import Stage, TaskStatus
from .observability import _latest_blocked_reason
from .persistence import Store
from .retry import RetryRegistry
from .task_sources import GitHubOutboundSync, _bounded_block_reason

GITHUB_SOURCE = "github"


def sync_outbound_outcomes(
    store: Store,
    sync: GitHubOutboundSync,
    github_sources: frozenset[str],
    tasks: list[Any],
    transitioned: dict[str, str],
) -> list[dict[str, Any]]:
    """Publish DONE/BLOCKED outcomes of GitHub-sourced tasks; never raises and never changes local task state.

    `tasks` are the rows this coordinator owns (a parallel worker never touches another task's issue). `transitioned` maps task ids that reached DONE or BLOCKED during this tick to that outcome. Only those tasks are published,
    plus tasks whose last outbound attempt for the same outcome failed, so enabling sync never comments on or closes
    historical issues. Local-only tasks are skipped. A failed publish is recorded as a source event and retried with backoff.
    """
    results: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task["id"])
        if task["source"] not in github_sources or task["source_id"] is None:
            continue
        outcome = _outcome(task)
        if outcome is None:
            continue
        issue = str(task["source_id"])
        subject = None
        key = f"github:{issue}:{'outbound' if outcome == 'DONE' else 'blocked'}"
        try:
            subject = _subject(store, task_id, outcome)
            if subject is None:
                continue
            state = _last_attempt(store, issue, outcome, subject)
            if state == "OK" or (state is None and transitioned.get(task_id) != outcome):
                continue
            if not RetryRegistry(store).decision(key).allowed:
                continue
            if outcome == "DONE":
                sync.publish_done(issue, subject)
            else:
                sync.publish_blocked(issue, subject)
            results.append({"task_id": task_id, "issue": issue, "outcome": outcome})
        except Exception as exc:  # noqa: BLE001 - outbound sync is advisory; the local lifecycle is the source of truth
            try:
                RetryRegistry(store).record_failure(key, "ERROR")  # a raising transport must back off like a failing one
                sync.publish(
                    GITHUB_SOURCE,
                    issue,
                    "ERROR",
                    {
                        "outcome": outcome,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        **({} if subject is None else {"candidate_sha" if outcome == "DONE" else "reason": subject}),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
    return results


def outcomes(tasks: list[Any]) -> dict[str, str]:
    return {str(t["id"]): o for t in tasks if (o := _outcome(t)) is not None}


def _outcome(task: Any) -> str | None:
    if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
        return "DONE"
    if task["status"] == TaskStatus.BLOCKED:
        return "BLOCKED"
    return None


def _subject(store: Store, task_id: str, outcome: str) -> str | None:
    if outcome == "DONE":
        candidate = store.latest_candidate(task_id)
        return str(candidate["sha"]) if candidate is not None else None
    return _bounded_block_reason(_latest_blocked_reason(store, task_id))


def _last_attempt(store: Store, issue: str, outcome: str, subject: str) -> str | None:
    """Status of the newest outbound event for this exact outcome and subject (candidate SHA or block reason), or None."""
    rows = store.conn.execute(
        "SELECT status, payload FROM source_events WHERE source=? AND source_id=? AND direction='outbound' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 200",
        (GITHUB_SOURCE, issue),
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("outcome", outcome) != outcome:
            continue
        key = "candidate_sha" if outcome == "DONE" else "reason"
        if payload.get(key) == subject and row["status"] != "BACKOFF":
            return str(row["status"])
    return None
