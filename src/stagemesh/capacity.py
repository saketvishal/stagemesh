from __future__ import annotations

import time
from dataclasses import dataclass


class CapacityKind:
    AVAILABLE = "AVAILABLE"
    CAPACITY = "CAPACITY"
    AUTH = "AUTH"
    PERMISSION = "PERMISSION"
    UNKNOWN = "UNKNOWN"


class CapacityValidationError(ValueError):
    pass


CAPACITY_KINDS = {
    CapacityKind.AVAILABLE,
    CapacityKind.CAPACITY,
    CapacityKind.AUTH,
    CapacityKind.PERMISSION,
    CapacityKind.UNKNOWN,
}


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
        provider = _validate_provider(provider)
        kind = _validate_kind(kind)
        retry_after_seconds = _validate_retry_after(retry_after_seconds)
        state = CapacityState(provider, kind, retry_after_seconds, time.time())
        self._states[provider] = state
        return state

    def get(self, provider: str) -> CapacityState:
        provider = _validate_provider(provider)
        return self._states.get(provider, CapacityState(provider, CapacityKind.UNKNOWN, None, 0.0))

    def choose_primary_secondary(self, primary: str, secondary: str) -> str | None:
        if self.get(primary).usable:
            return primary
        if self.get(secondary).usable:
            return secondary
        return None

    def snapshot(self, providers: list[str] | tuple[str, ...] | None = None) -> list[dict[str, object]]:
        names = list(providers) if providers is not None else sorted(self._states)
        return [
            {
                "provider": state.provider,
                "kind": state.kind,
                "usable": state.usable,
                "retry_after_seconds": state.retry_after_seconds,
                "checked_at": state.checked_at,
            }
            for state in (self.get(name) for name in names)
        ]


def _validate_provider(provider: str) -> str:
    if not isinstance(provider, str) or not provider.strip():
        raise CapacityValidationError("provider name must be a non-empty string")
    if len(provider) > 100:
        raise CapacityValidationError("provider name must be 100 characters or fewer")
    return provider.strip()


def _validate_kind(kind: str) -> str:
    if kind not in CAPACITY_KINDS:
        raise CapacityValidationError(f"capacity kind must be one of: {', '.join(sorted(CAPACITY_KINDS))}")
    return kind


def _validate_retry_after(retry_after_seconds: float | None) -> float | None:
    if retry_after_seconds is None:
        return None
    if retry_after_seconds < 0:
        raise CapacityValidationError("retry_after_seconds must be non-negative")
    return retry_after_seconds
