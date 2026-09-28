from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CIWaitDecision:
    should_wait: bool
    release_worker: bool
    poll_after_seconds: float
    reason: str


def decide_ci_wait(status: str, elapsed_seconds: float, max_seconds: float = 1800) -> CIWaitDecision:
    normalized = status.upper()
    if normalized in {"SUCCESS", "PASSED"}:
        return CIWaitDecision(False, False, 0, "ci passed")
    if normalized in {"FAILED", "ERROR", "CANCELLED"}:
        return CIWaitDecision(False, False, 0, "ci failed")
    if elapsed_seconds >= max_seconds:
        return CIWaitDecision(False, True, 0, "ci wait timed out")
    return CIWaitDecision(True, True, min(60, max(5, elapsed_seconds / 10 or 5)), "ci pending")


def now_seconds() -> float:
    return time.time()
