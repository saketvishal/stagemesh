from __future__ import annotations

import time
from dataclasses import dataclass

from .persistence import Store


@dataclass(frozen=True)
class RetryDecision:
    key: str
    allowed: bool
    next_attempt_at: float
    attempts: int
    reason: str


def backoff_seconds(attempts: int, base: float = 5, cap: float = 300) -> float:
    return min(cap, base * (2 ** max(0, attempts - 1)))


class RetryRegistry:
    def __init__(self, store: Store):
        self.store = store

    def decision(self, key: str, now: float | None = None) -> RetryDecision:
        now = time.time() if now is None else now
        row = self.store.get_retry_state(key)
        if row is None:
            return RetryDecision(key, True, now, 0, "new")
        allowed = float(row["next_attempt_at"]) <= now
        return RetryDecision(
            key,
            allowed,
            float(row["next_attempt_at"]),
            int(row["attempts"]),
            "ready" if allowed else "backoff",
        )

    def record_failure(self, key: str, reason: str, now: float | None = None) -> RetryDecision:
        now = time.time() if now is None else now
        row = self.store.get_retry_state(key)
        attempts = int(row["attempts"]) + 1 if row else 1
        next_attempt_at = now + backoff_seconds(attempts)
        self.store.upsert_retry_state(key, attempts, next_attempt_at, reason)
        return RetryDecision(key, False, next_attempt_at, attempts, reason)

    def record_success(self, key: str) -> None:
        self.store.clear_retry_state(key)
