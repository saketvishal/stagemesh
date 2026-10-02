from __future__ import annotations

from dataclasses import dataclass

from .domain import Stage


class RoutingMode:
    SINGLE_AGENT = "SINGLE_AGENT"
    STAGED = "STAGED"


class RoutingValidationError(ValueError):
    pass


ROUTING_MODES = {RoutingMode.SINGLE_AGENT, RoutingMode.STAGED}


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
        self.providers = _validate_providers(providers)
        self.mode = _validate_mode(mode)
        self.stage_routes = _validate_stage_routes(stage_routes or {})
        self.single_agent_provider = _validate_optional_provider_name(single_agent_provider)

    def choose(self, capability: str) -> Provider | None:
        capability = _validate_capability(capability)
        return self._choose(capability)

    def choose_for_stage(self, stage: Stage | str, capability: str) -> Provider | None:
        capability = _validate_capability(capability)
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
        return min(candidates, key=lambda item: item.priority) if candidates else None

    def _provider_by_name(self, name: str, capability: str) -> Provider | None:
        for provider in self.providers:
            if provider.name == name and capability in provider.capabilities and provider.capacity_available:
                return provider
        return None


def _validate_mode(mode: str) -> str:
    if mode not in ROUTING_MODES:
        raise RoutingValidationError(f"routing mode must be one of: {', '.join(sorted(ROUTING_MODES))}")
    return mode


def _validate_provider_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RoutingValidationError("provider name must be a non-empty string")
    return name.strip()


def _validate_optional_provider_name(name: str | None) -> str | None:
    if name is None:
        return None
    return _validate_provider_name(name)


def _validate_capability(capability: str) -> str:
    if not isinstance(capability, str) or not capability.strip():
        raise RoutingValidationError("provider capability must be a non-empty string")
    return capability.strip()


def _validate_providers(providers: list[Provider]) -> list[Provider]:
    validated: list[Provider] = []
    seen: set[str] = set()
    for provider in providers:
        name = _validate_provider_name(provider.name)
        if name in seen:
            raise RoutingValidationError(f"duplicate provider name: {name}")
        if not provider.capabilities:
            raise RoutingValidationError(f"provider {name} must declare at least one capability")
        capabilities = frozenset(_validate_capability(capability) for capability in provider.capabilities)
        validated.append(Provider(name, capabilities, provider.capacity_available, provider.priority))
        seen.add(name)
    return validated


def _validate_stage_routes(stage_routes: dict[str, str]) -> dict[str, str]:
    valid_stages = {str(stage) for stage in Stage}
    validated: dict[str, str] = {}
    for stage, provider in stage_routes.items():
        if stage not in valid_stages:
            raise RoutingValidationError(f"unsupported routed stage: {stage}")
        validated[stage] = _validate_provider_name(provider)
    return validated
