from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capacity import CapacityKind, CapacityRegistry
from .domain import ExecutionStatus
from .execution import ExecutionResult
from .persistence import Store
from .providers import ProviderAdapter, record_provider_capacity
from .routing import Provider, Router


@dataclass(frozen=True)
class ProviderAcceptanceResult:
    status: str
    chosen_provider: str | None
    execution_status: str
    capacity_failure_isolated: bool


@dataclass(frozen=True)
class DryRunProviderAdapter:
    name: str
    capacity: str = CapacityKind.AVAILABLE
    capabilities: frozenset[str] = frozenset({"code"})

    def check_capacity(self) -> str:
        return self.capacity

    def execute(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        if self.capacity != CapacityKind.AVAILABLE:
            return ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True)
        return ExecutionResult(ExecutionStatus.SUCCEEDED)


def run_provider_acceptance(store: Store, project: Path) -> ProviderAcceptanceResult:
    primary = DryRunProviderAdapter("primary", CapacityKind.CAPACITY)
    secondary = DryRunProviderAdapter("secondary", CapacityKind.AVAILABLE)
    registry = CapacityRegistry()
    record_provider_capacity(registry, [primary, secondary])
    chosen_name = registry.choose_primary_secondary(primary.name, secondary.name)
    router = Router(
        [
            Provider(primary.name, primary.capabilities, registry.get(primary.name).usable, priority=1),
            Provider(secondary.name, secondary.capabilities, registry.get(secondary.name).usable, priority=2),
        ]
    )
    chosen = router.choose("code")
    task_id = store.upsert_task("provider acceptance", source="provider-acceptance", source_id="provider-acceptance")
    primary_result = primary.execute(store, task_id, None, project)
    secondary_result = secondary.execute(store, task_id, None, project)
    isolated = primary_result.capacity_failure and secondary_result.status is ExecutionStatus.SUCCEEDED
    status = "PASS" if chosen and chosen.name == chosen_name == "secondary" and isolated else "FAIL"
    return ProviderAcceptanceResult(status, chosen.name if chosen else None, secondary_result.status, isolated)
