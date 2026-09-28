from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    capabilities: frozenset[str]
    capacity_available: bool = True
    priority: int = 100


class Router:
    def __init__(self, providers: list[Provider], mode: str = "STAGED"):
        self.providers = providers
        self.mode = mode

    def choose(self, capability: str) -> Provider | None:
        candidates = [
            provider
            for provider in self.providers
            if capability in provider.capabilities and provider.capacity_available
        ]
        return sorted(candidates, key=lambda item: item.priority)[0] if candidates else None
