from __future__ import annotations

import time
from dataclasses import dataclass


class CapacityKind:
    AVAILABLE = "AVAILABLE"
    CAPACITY = "CAPACITY"
    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CapacityState:
    provider: str
    kind: str
    retry_after_seconds: float | None = None
    checked_at: float = 0.0

    @property
    def usable(self) -> bool:
        return self.kind == CapacityKind.AVAILABLE


class CapacityRegistry:
    def __init__(self) -> None:
        self._states: dict[str, CapacityState] = {}

    def record(self, provider: str, kind: str, retry_after_seconds: float | None = None) -> CapacityState:
        state = CapacityState(provider, kind, retry_after_seconds, time.time())
        self._states[provider] = state
        return state

    def get(self, provider: str) -> CapacityState:
        return self._states.get(provider, CapacityState(provider, CapacityKind.UNKNOWN, None, 0.0))

    def choose_primary_secondary(self, primary: str, secondary: str) -> str | None:
        if self.get(primary).usable:
            return primary
        if self.get(secondary).usable:
            return secondary
        return None
