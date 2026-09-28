from __future__ import annotations

import time
from dataclasses import dataclass

from .persistence import Store


class RetryValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RetryDecision:
    key: str
    allowed: bool
    next_attempt_at: float
    attempts: int
    reason: str


def backoff_seconds(attempts: int, base: float = 5, cap: float = 300) -> float:
    if attempts < 1:
        raise RetryValidationError("retry attempts must be at least 1")
    if base <= 0 or cap <= 0:
        raise RetryValidationError("retry backoff base and cap must be positive")
    return min(cap, base * (2 ** max(0, attempts - 1)))


class RetryRegistry:
    def __init__(self, store: Store):
        self.store = store

    def decision(self, key: str, now: float | None = None) -> RetryDecision:
        key = _validate_key(key)
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
        key = _validate_key(key)
        reason = _validate_reason(reason)
        now = time.time() if now is None else now
        row = self.store.get_retry_state(key)
        attempts = int(row["attempts"]) + 1 if row else 1
        next_attempt_at = now + backoff_seconds(attempts)
        self.store.upsert_retry_state(key, attempts, next_attempt_at, reason)
        return RetryDecision(key, False, next_attempt_at, attempts, reason)

    def record_success(self, key: str) -> None:
        key = _validate_key(key)
        self.store.clear_retry_state(key)


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise RetryValidationError("retry key must be a non-empty string")
    if len(key) > 200:
        raise RetryValidationError("retry key must be 200 characters or fewer")
    return key.strip()


def _validate_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise RetryValidationError("retry reason must be a non-empty string")
    if len(reason) > 200:
        raise RetryValidationError("retry reason must be 200 characters or fewer")
    return reason.strip()
