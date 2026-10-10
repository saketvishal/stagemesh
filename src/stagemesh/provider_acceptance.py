from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .capacity import CapacityKind, CapacityRegistry
from .domain import ExecutionStatus, Stage
from .execution import ExecutionResult
from .git import GitWorkspace
from .persistence import Store
from .providers import record_provider_capacity
from .routing import Provider, Router


@dataclass(frozen=True)
class ProviderAcceptanceResult:
    status: str
    chosen_provider: str | None
    execution_status: str
    capacity_failure_isolated: bool
    single_agent_provider: str | None
    review_provider: str | None


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


@dataclass(frozen=True)
class LiveProviderSmokeResult:
    status: str
    provider: str
    execution_status: str
    candidate_sha: str | None
    file_ok: bool
    failure_reason: str | None


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
    staged_router = Router(
        [
            Provider("implementer", frozenset({"code"}), True, priority=1),
            Provider("reviewer", frozenset({"review"}), True, priority=2),
        ],
        stage_routes={str(Stage.IMPLEMENT): "implementer", str(Stage.REVIEW): "reviewer"},
    )
    single_router = Router(
        [
            Provider("solo", frozenset({"code", "review"}), True, priority=1),
            Provider("other", frozenset({"code", "review"}), True, priority=2),
        ],
        mode="SINGLE_AGENT",
        single_agent_provider="solo",
    )
    review_provider = staged_router.choose_for_stage(Stage.REVIEW, "review")
    single_provider = single_router.choose_for_stage(Stage.REVIEW, "review")
    task_id = store.upsert_task("provider acceptance", source="provider-acceptance", source_id="provider-acceptance")
    primary_result = primary.execute(store, task_id, None, project)
    secondary_result = secondary.execute(store, task_id, None, project)
    isolated = primary_result.capacity_failure and secondary_result.status is ExecutionStatus.SUCCEEDED
    routing_ok = (
        review_provider is not None
        and review_provider.name == "reviewer"
        and single_provider is not None
        and single_provider.name == "solo"
    )
    status = "PASS" if chosen and chosen.name == chosen_name == "secondary" and isolated and routing_ok else "FAIL"
    return ProviderAcceptanceResult(
        status,
        chosen.name if chosen else None,
        secondary_result.status,
        isolated,
        single_provider.name if single_provider else None,
        review_provider.name if review_provider else None,
    )


def run_live_provider_smoke(adapter, *, keep_temp: bool = False) -> LiveProviderSmokeResult:
    """Run one configured provider against a throwaway one-file repository."""
    if adapter.check_capacity() != CapacityKind.AVAILABLE:
        return LiveProviderSmokeResult("FAIL", adapter.name, ExecutionStatus.FAILED, None, False, "provider_unavailable")
    temp = tempfile.TemporaryDirectory(prefix="stagemesh-provider-smoke-")
    project = Path(temp.name)
    try:
        _prepare_smoke_project(project)
        store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
        store.migrate()
        try:
            task_id = store.upsert_task(
                "Change provider-smoke.txt so it contains exactly: after",
                source="provider-smoke",
                source_id="provider-smoke",
            )
            result = adapter.execute(store, task_id, None, project)
            # The provider writes inside the task worktree; verify the candidate tree instead of the disposable checkout path.
            candidate = store.latest_candidate(task_id)
            candidate_sha = str(candidate["sha"]) if candidate is not None else None
            content = _candidate_file(project, candidate_sha, "provider-smoke.txt") if candidate_sha else None
            file_ok = content is not None and content.strip() == "after"
        finally:
            store.close()
        status = "PASS" if result.status is ExecutionStatus.SUCCEEDED and file_ok else "FAIL"
        return LiveProviderSmokeResult(
            status,
            adapter.name,
            result.status,
            result.candidate_sha,
            file_ok,
            result.failure_reason,
        )
    finally:
        if keep_temp:
            temp._finalizer.detach()  # noqa: SLF001 - explicit debug escape hatch for this short-lived CLI.
        else:
            temp.cleanup()


def _prepare_smoke_project(project: Path) -> None:
    (project / ".stagemesh" / "contracts").mkdir(parents=True)
    (project / "provider-smoke.txt").write_text("before\n", encoding="utf-8")
    contract = {
        "objective": "Change provider-smoke.txt so it contains exactly: after",
        "allowed_files": ["provider-smoke.txt"],
        "required_tests": [
            {
                "name": "provider-smoke-content",
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('provider-smoke.txt').read_text(encoding='utf-8').strip() == 'after'",
                ],
            }
        ],
    }
    (project / ".stagemesh" / "contracts" / "provider-smoke.json").write_text(
        json.dumps(contract),
        encoding="utf-8",
    )
    git = GitWorkspace(project)
    git.init_if_needed()
    git.commit_all("Initial provider smoke fixture")


def _candidate_file(project: Path, candidate_sha: str, path: str) -> str | None:
    result = GitWorkspace(project).run("show", f"{candidate_sha}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None
