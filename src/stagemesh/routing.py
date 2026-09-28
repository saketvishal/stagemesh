from __future__ import annotations

from dataclasses import dataclass

from .domain import Stage


class RoutingMode:
    SINGLE_AGENT = "SINGLE_AGENT"
    STAGED = "STAGED"


@dataclass(frozen=True)
class Provider:
    name: str
    capabilities: frozenset[str]
    capacity_available: bool = True
    priority: int = 100


class Router:
    def __init__(
        self,
        providers: list[Provider],
        mode: str = RoutingMode.STAGED,
        stage_routes: dict[str, str] | None = None,
        single_agent_provider: str | None = None,
    ):
        self.providers = providers
        self.mode = mode
        self.stage_routes = stage_routes or {}
        self.single_agent_provider = single_agent_provider

    def choose(self, capability: str) -> Provider | None:
        return self._choose(capability)

    def choose_for_stage(self, stage: Stage | str, capability: str) -> Provider | None:
        if self.mode == RoutingMode.SINGLE_AGENT and self.single_agent_provider:
            return self._provider_by_name(self.single_agent_provider, capability)
        if self.mode == RoutingMode.STAGED:
            routed = self.stage_routes.get(str(stage))
            if routed:
                return self._provider_by_name(routed, capability)
        return self._choose(capability)

    def _choose(self, capability: str) -> Provider | None:
        candidates = [
            provider
            for provider in self.providers
            if capability in provider.capabilities and provider.capacity_available
        ]
        return sorted(candidates, key=lambda item: item.priority)[0] if candidates else None

    def _provider_by_name(self, name: str, capability: str) -> Provider | None:
        for provider in self.providers:
            if provider.name == name and capability in provider.capabilities and provider.capacity_available:
                return provider
        return None
