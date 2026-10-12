"""What happens when the remediation budget runs out: recover, and only ask a person about a genuine decision.

An exhausted budget says that the same approach stopped working, not that a person has to decide anything. Before a task stops, the
failure is put to `recovery_decision.decide`, which walks a bounded ladder over the open findings:

1. `SWITCH_PROVIDER`   the next implementation attempt avoids the provider(s) that produced the failing candidates (when another one is
                       healthy), so a different model reads the same findings;
2. `REPAIR_WORKTREE`   a narrowed attempt on the worktree at the latest candidate (workspace ownership already proves it is exactly what was
                       sealed, with no stray edits): the prompt carries only the still-open findings and tells the provider to start
                       from the current files rather than continue an earlier approach.

After both rungs a task that still fails is blocked as `BLOCKED_RECOVERABLE_OPERATIONAL` with the exact record of what was tried; it is
reconsidered by `run-window` once the runtime or configuration changes. Only findings that name a product, spec, security or legal choice
(`judgment_reason`) skip the ladder and block as `BLOCKED_HUMAN_DECISION`, with those findings as the question.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .diagnosis import _last_reset
from .persistence import Store

ROUND_EVENT = "recovery.exhaustion_round"
SWITCH_EVENT = "recovery.switch_provider"
MAX_EXHAUSTION_ROUNDS = 2

# Wording in a finding that says the open question is a choice, not a defect. Deliberately narrow: a false positive hands a person a
# defect StageMesh could have repaired (a finding that merely mentions a "spec conflict" as a conditional is a defect, and says how to
# fix it), so only an explicit statement that a product, legal or security decision is required counts.
_JUDGMENT = re.compile(
    r"(needs? (a )?(product|legal|security) decision|requires? (a )?(product|legal|security) decision|"
    r"legal (advice|judg(e)?ment)|unauthori[sz]ed practice of law)",
    re.IGNORECASE,
)


def judgment_reason(messages: list[str]) -> str | None:
    """The finding text that names a product, spec, security or legal decision, or None when every open finding is repairable."""
    for message in messages:
        found = _JUDGMENT.search(message)
        if found:
            return f"a finding states a decision is needed: {message.strip()[:300]}"
    return None


def rounds(store: Store, task_id: str) -> list[dict[str, Any]]:
    """Exhaustion recoveries since an operator last gave the task a fresh budget."""
    since = _last_reset(store, task_id)
    found: list[dict[str, Any]] = []
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? AND created_at>=? ORDER BY created_at, rowid", (ROUND_EVENT, since)):
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            found.append(payload)
    return found


def stalled_since_last_round(store: Store, task_id: str) -> bool:
    """True when a no-progress/failed implementation attempt happened after the latest exhaustion round (or there was no round yet).

    A round grants one more attempt; the repeated-no-progress diagnosis stays true until that attempt has actually run, so without
    this check a single stale diagnosis would spend every round before any attempt was made.
    """
    since = _last_reset(store, task_id)
    last_round = 0.0
    for row in store.conn.execute("SELECT payload, created_at FROM audit_events WHERE event_type=? AND created_at>=? ORDER BY created_at DESC, rowid DESC LIMIT 100", (ROUND_EVENT, since)):
        if _loads(row["payload"]).get("task_id") == task_id:
            last_round = float(row["created_at"])
            break
    if last_round == 0.0:
        return True
    for row in store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type IN ('provider.no_progress', 'task.implementation_unsuccessful') AND created_at>? ORDER BY created_at DESC LIMIT 200",
        (last_round,),
    ):
        if _loads(row["payload"]).get("task_id") == task_id:
            return True
    return False


def avoided_providers(store: Store, task_id: str) -> set[str]:
    """Providers the latest exhaustion round asked the next implementation attempt to avoid."""
    since = _last_reset(store, task_id)
    for row in store.conn.execute("SELECT payload FROM audit_events WHERE event_type=? AND created_at>=? ORDER BY created_at DESC, rowid DESC LIMIT 100", (SWITCH_EVENT, since)):
        payload = _loads(row["payload"])
        if payload.get("task_id") == task_id:
            return {str(name) for name in payload.get("avoid", [])}
    return set()


def producers_of_failed_candidates(store: Store, task_id: str) -> list[str]:
    """The providers that produced the task's candidates since the last fresh budget, newest first (the ones a switch moves away from)."""
    since = _last_reset(store, task_id)
    rows = store.conn.execute("SELECT produced_by FROM candidates WHERE task_id=? AND created_at>=? ORDER BY created_at DESC, rowid DESC", (task_id, since))
    return list(dict.fromkeys(str(row["produced_by"]) for row in rows))


def narrowed_note(store: Store, task_id: str) -> str | None:
    """The instruction a narrowed attempt adds to the remediation prompt, or None when no narrowed round is in effect."""
    last = rounds(store, task_id)
    if not last or last[-1].get("action") != "REPAIR_WORKTREE":
        return None
    return (
        f"Recovery attempt {len(last)}: earlier attempts did not resolve these findings. Start from the files as they are now and do not "
        "continue an earlier approach. Resolve only the findings listed, and change no file the findings do not require."
    )


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
