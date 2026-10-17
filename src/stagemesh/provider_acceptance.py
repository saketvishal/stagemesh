from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .capacity import CapacityKind, CapacityRegistry
from .concurrency import IntegrationLock, IntegrationLockTimeout
from .domain import ExecutionStatus, Stage
from .execution import PROVIDER_TIMEOUT, ExecutionResult, kill_active_provider_processes
from .git import GitError, GitWorkspace
from .persistence import Store
from .provider_cooldown import DEFAULT_FAILURE_COOLDOWN_SECONDS, DEFAULT_QUOTA_COOLDOWN_SECONDS
from .providers import record_provider_capacity
from .review import VERDICT_FAIL_WITH_FINDINGS, VERDICT_PASS, classify_review_response
from .routing import Provider, Router

LIVE_PROVIDER_ACCEPTANCE_EVENT = "provider.live_acceptance"
DEFAULT_LIVE_SMOKE_TIMEOUT_SECONDS = 180.0
DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS = 7 * 24 * 60 * 60  # same default as config.DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS


def provider_smoke_timeout_seconds(configured: float | None = None) -> float:
    """Wall-clock limit per smoke stage: explicit value, environment override, or 180s."""
    if configured is not None and configured > 0:
        return float(configured)
    try:
        value = float(os.environ.get("STAGEMESH_PROVIDER_SMOKE_TIMEOUT_SECONDS", ""))
    except (ValueError, TypeError):
        return DEFAULT_LIVE_SMOKE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_LIVE_SMOKE_TIMEOUT_SECONDS


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
    implementation_checked: bool
    review_checked: bool
    implementation_ready: bool
    review_ready: bool
    review_json_ok: bool
    failure_reason: str | None
    timed_out: bool = False
    timeout_seconds: float | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "execution_status": str(self.execution_status),
            "candidate_sha": self.candidate_sha,
            "file_ok": self.file_ok,
            "implementation_checked": self.implementation_checked,
            "review_checked": self.review_checked,
            "implementation_ready": self.implementation_ready,
            "review_ready": self.review_ready,
            "review_json_ok": self.review_json_ok,
            "failure_reason": self.failure_reason,
            "timed_out": self.timed_out,
            "timeout_seconds": self.timeout_seconds,
            "checked_at": time.time(),
        }


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


def run_live_provider_smoke(
    adapter,
    *,
    timeout_seconds: float | None = None,
    keep_temp: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> LiveProviderSmokeResult:
    """Run one configured provider against a throwaway one-file repository."""
    import dataclasses

    effective_timeout = provider_smoke_timeout_seconds(
        timeout_seconds if timeout_seconds is not None else getattr(adapter, "timeout_seconds", None)
    )
    if dataclasses.is_dataclass(adapter) and hasattr(adapter, "timeout_seconds"):
        adapter = dataclasses.replace(adapter, timeout_seconds=effective_timeout)
    elif hasattr(adapter, "timeout_seconds"):
        try:
            adapter.timeout_seconds = effective_timeout
        except (AttributeError, TypeError):
            pass

    if adapter.check_capacity() != CapacityKind.AVAILABLE:
        return LiveProviderSmokeResult(
            "FAIL",
            adapter.name,
            ExecutionStatus.FAILED,
            None,
            False,
            False,
            False,
            False,
            False,
            False,
            "provider_unavailable",
            timed_out=False,
            timeout_seconds=effective_timeout,
        )
    temp = tempfile.TemporaryDirectory(prefix="stagemesh-provider-smoke-", ignore_cleanup_errors=True)
    project = Path(temp.name)
    try:
        _prepare_smoke_project(project)
        store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
        store.migrate()
        result = ExecutionResult(ExecutionStatus.SUCCEEDED)
        candidate_sha: str | None = None
        implementation_checked = "code" in adapter.capabilities
        file_ok = not implementation_checked
        review_checked = "review" in adapter.capabilities
        implementation_ready = "code" not in adapter.capabilities
        review_ready = "review" not in adapter.capabilities
        review_json_ok = False
        failure_reason: str | None = None
        timed_out = False
        try:
            if "code" in adapter.capabilities:
                task_id = store.upsert_task(
                    "Change provider-smoke.txt so it contains exactly: after",
                    source="provider-smoke",
                    source_id="provider-smoke",
                )
                try:
                    result = adapter.execute(store, task_id, None, project)
                except (subprocess.TimeoutExpired, TimeoutError):
                    kill_active_provider_processes()
                    result = ExecutionResult(ExecutionStatus.FAILED, failure_reason=PROVIDER_TIMEOUT)
                    timed_out = True
                except GitError as exc:
                    result = ExecutionResult(
                        ExecutionStatus.FAILED,
                        failure_reason=_provider_smoke_git_failure_reason(exc),
                    )

                if result.failure_reason == PROVIDER_TIMEOUT or (
                    result.status is ExecutionStatus.FAILED and "timeout" in str(result.failure_reason or "").lower()
                ):
                    timed_out = True
                    failure_reason = PROVIDER_TIMEOUT
                    implementation_ready = False
                else:
                    # The provider writes inside the task worktree; verify the candidate tree instead of the disposable checkout path.
                    candidate = store.latest_candidate(task_id)
                    candidate_sha = str(candidate["sha"]) if candidate is not None else None
                    content = _candidate_file(project, candidate_sha, "provider-smoke.txt") if candidate_sha else None
                    file_ok = content is not None and content.strip() == "after"
                    implementation_ready = result.status is ExecutionStatus.SUCCEEDED and file_ok
                    if not implementation_ready and failure_reason is None:
                        failure_reason = result.failure_reason
            if should_stop():
                return LiveProviderSmokeResult(
                    "FAIL", adapter.name, ExecutionStatus.FAILED, candidate_sha,
                    file_ok, implementation_checked, False, implementation_ready,
                    False, False, "provider_smoke_cancelled",
                )
            if "review" in adapter.capabilities:
                if candidate_sha is None:
                    candidate_sha = _manual_review_candidate(project)
                try:
                    response = adapter.review_candidate(
                        "Review candidate for StageMesh provider acceptance. Reply with one JSON verdict: "
                        '{"decision":"PASS"} or {"decision":"FAIL_WITH_FINDINGS","findings":[{"severity":"error","message":"concrete defect"}]}.',
                        project,
                        candidate_sha,
                    )
                except (subprocess.TimeoutExpired, TimeoutError):
                    kill_active_provider_processes()
                    response = json.dumps({"decision": "INFRASTRUCTURE_FAILURE", "reason": PROVIDER_TIMEOUT})
                    timed_out = True

                verdict = classify_review_response(response)
                if (
                    verdict.reason == PROVIDER_TIMEOUT
                    or (verdict.provider_failure and "timeout" in str(verdict.reason or "").lower())
                ):
                    timed_out = True
                    review_json_ok = False
                    review_ready = False
                    if failure_reason is None:
                        failure_reason = PROVIDER_TIMEOUT
                else:
                    review_json_ok = verdict.kind in {VERDICT_PASS, VERDICT_FAIL_WITH_FINDINGS}
                    review_ready = review_json_ok
                    if not review_ready and failure_reason is None:
                        failure_reason = verdict.reason or "invalid_review_json"
        finally:
            store.close()
        status = "PASS" if implementation_ready and review_ready and not timed_out else "FAIL"
        execution_status = (
            ExecutionStatus.FAILED
            if (timed_out or result.status is not ExecutionStatus.SUCCEEDED or status != "PASS")
            else ExecutionStatus.SUCCEEDED
        )
        return LiveProviderSmokeResult(
            status,
            adapter.name,
            execution_status,
            candidate_sha,
            file_ok,
            implementation_checked,
            review_checked,
            implementation_ready,
            review_ready,
            review_json_ok,
            failure_reason,
            timed_out=timed_out,
            timeout_seconds=effective_timeout,
        )
    finally:
        if keep_temp:
            temp._finalizer.detach()
        else:
            temp.cleanup()


def record_live_provider_acceptance(store: Store, result: LiveProviderSmokeResult) -> None:
    """Store a sanitized live readiness result; no command text, stdout/stderr, env or secrets are persisted."""
    store.add_audit_event(LIVE_PROVIDER_ACCEPTANCE_EVENT, result.to_payload())


def _temporary_check_expired(payload: dict[str, Any]) -> bool:
    checked_at = payload.get("checked_at")
    reason = payload.get("failure_reason")
    if not isinstance(reason, str):
        return False
    delay = {
        "quota_rate_limit": DEFAULT_QUOTA_COOLDOWN_SECONDS,
        PROVIDER_TIMEOUT: DEFAULT_FAILURE_COOLDOWN_SECONDS,
        "transient_provider_failure": DEFAULT_FAILURE_COOLDOWN_SECONDS,
    }.get(reason)
    return (
        payload.get("status") == "FAIL"
        and delay is not None
        and isinstance(checked_at, (int, float))
        and not isinstance(checked_at, bool)
        and math.isfinite(checked_at)
        and checked_at > 0
        and time.time() >= checked_at + delay
    )


def refresh_expired_quota_checks(
    store: Store,
    project: Path,
    adapters: list[Any],
    *,
    deadline: float | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.time,
) -> None:
    """Recheck quota, timeout and transient failures before a real window, never during diagnostics.

    The existing bounded smoke runs in a disposable repository, not the client checkout.
    An OS lock coalesces checks from concurrent starters; fresh failures renew the backoff.
    """
    from .provider_cooldown import active_cooldowns, cleared_at
    from .provider_pool import PROVIDER_FAILURE_EVENT

    def due(name: str) -> bool:
        payload = _latest_live_acceptance_payload(store, name)
        cooling = active_cooldowns(store, DEFAULT_FAILURE_COOLDOWN_SECONDS)
        if payload is None or not _temporary_check_expired(payload) or any(
            item["provider"] == name for item in cooling
        ):
            return False
        # Task-scoped timeout routing stays unchanged, but a provider smoke must
        # not bypass a newer timeout's backoff on any task or stage.
        for row in store.conn.execute(
            "SELECT payload, created_at FROM audit_events WHERE event_type=? AND created_at>?",
            (PROVIDER_FAILURE_EVENT, cleared_at(store, name)),
        ):
            try:
                failure = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(failure, dict) or failure.get("provider") != name or failure.get("reason") != PROVIDER_TIMEOUT:
                continue
            retry_at = failure.get("retry_at")
            if not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at):
                retry_at = float(row["created_at"]) + DEFAULT_FAILURE_COOLDOWN_SECONDS
            if time.time() < retry_at:
                return False
        return True

    for adapter in adapters:
        if should_stop() or (deadline is not None and clock() >= deadline):
            return
        if not due(adapter.name):
            continue
        key = hashlib.sha256(adapter.name.encode("utf-8")).hexdigest()
        lock = IntegrationLock(project / ".stagemesh" / "provider-checks" / f"{key}.lock", timeout_seconds=0)
        try:
            with lock.hold(adapter.name):
                if should_stop() or (deadline is not None and clock() >= deadline):
                    return
                if not due(adapter.name):
                    continue
                timeout = provider_smoke_timeout_seconds()
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - clock()) / 2)
                    if timeout <= 0:
                        return
                print(f"[stagemesh] {adapter.name}: refreshing expired provider check", file=sys.stderr, flush=True)
                try:
                    result = run_live_provider_smoke(
                        adapter, timeout_seconds=timeout,
                        should_stop=lambda: should_stop() or (deadline is not None and clock() >= deadline),
                    )
                except Exception:  # fail closed without persisting provider output or secrets
                    result = LiveProviderSmokeResult(
                        "FAIL", adapter.name, ExecutionStatus.FAILED, None,
                        False, "code" in adapter.capabilities, "review" in adapter.capabilities,
                        False, False, False, "provider_refresh_failure",
                    )
                if result.failure_reason == "provider_smoke_cancelled":
                    return  # interruption is not new provider readiness evidence
                record_live_provider_acceptance(store, result)
                print(f"[stagemesh] {adapter.name}: provider recheck {result.status}", file=sys.stderr, flush=True)
        except IntegrationLockTimeout:
            continue  # another starter owns the probe; never duplicate it or assume a pass


def live_provider_readiness(
    store: Store, provider: str, capability: str, *, ttl_seconds: float | None = None
) -> tuple[bool, str]:
    payload = _latest_live_acceptance_payload(store, provider)
    if payload is None:
        return False, "live_acceptance_unproven: run stagemesh provider-acceptance --live --provider " + provider
    capability_prefix = {"code": "implementation", "review": "review"}.get(capability)
    capability_ready = capability_prefix is not None and (
        payload.get(f"{capability_prefix}_checked") is True
        and payload.get(f"{capability_prefix}_ready") is True
    )
    # Expiry requests a real recheck; it never establishes readiness on its own.
    if not capability_ready and _temporary_check_expired(payload):
        return False, (
            "live_acceptance_retry_due: temporary failure cooldown elapsed; a real run-window refreshes this check; "
            "or run stagemesh provider-acceptance --live --provider " + provider
        )
    if capability == "code" and (
        payload.get("implementation_checked") is not True or payload.get("implementation_ready") is not True
    ):
        if payload.get("timed_out") is True or payload.get("failure_reason") == PROVIDER_TIMEOUT:
            return False, "live_acceptance_failed: provider_timeout"
        if payload.get("failure_reason"):
            return False, f"live_acceptance_failed: {payload['failure_reason']}"
        return False, "live_acceptance_failed: implementation_not_ready"
    if capability == "review" and (payload.get("review_checked") is not True or payload.get("review_ready") is not True):
        if payload.get("timed_out") is True or payload.get("failure_reason") == PROVIDER_TIMEOUT:
            return False, "live_acceptance_failed: provider_timeout"
        if payload.get("failure_reason"):
            return False, f"live_acceptance_failed: {payload['failure_reason']}"
        return False, "live_acceptance_failed: review_not_ready"
    if capability not in {"code", "review"} and payload.get("status") != "PASS":
        reason = str(payload.get("failure_reason") or "live_acceptance_failed")
        return False, f"live_acceptance_failed: {reason}"
    if _live_acceptance_is_stale(payload, ttl_seconds=ttl_seconds):
        return False, "live_acceptance_stale: run stagemesh provider-acceptance --live --provider " + provider
    return True, "live_acceptance_passed"


def _live_acceptance_is_stale(payload: dict[str, Any], *, now: float | None = None, ttl_seconds: float | None = None) -> bool:
    checked_at = payload.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return True
    current = time.time() if now is None else now
    return current - float(checked_at) > (DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS if ttl_seconds is None else ttl_seconds)


LIVE_MISSING, LIVE_FAILED, LIVE_STALE, LIVE_PASSING = "missing", "failed", "stale", "passing"
LIVE_RETRY_DUE = "retry_due"


def live_acceptance_status(
    store: Store, provider: str, capability: str = "code", *, ttl_seconds: float | None = None, now: float | None = None
) -> dict[str, Any]:
    """The last live check for a provider as one of missing / failed / stale / passing, with its age and the TTL in force."""
    ttl = DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    payload = _latest_live_acceptance_payload(store, provider)
    ready, reason = live_provider_readiness(store, provider, capability, ttl_seconds=ttl_seconds)
    state = LIVE_PASSING if ready else LIVE_MISSING if payload is None else LIVE_STALE if reason.startswith("live_acceptance_stale") else LIVE_FAILED
    if reason.startswith("live_acceptance_retry_due"):
        state = LIVE_RETRY_DUE
    checked_at = None if payload is None else payload.get("checked_at")
    current = time.time() if now is None else now
    age = None if not isinstance(checked_at, (int, float)) else max(0.0, current - float(checked_at))
    return {"state": state, "reason": reason, "checked_at": checked_at, "age_seconds": age, "ttl_seconds": ttl}


def _latest_live_acceptance_payload(store: Store, provider: str) -> dict[str, Any] | None:
    rows = store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC",
        (LIVE_PROVIDER_ACCEPTANCE_EVENT,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("provider", "")).casefold() == provider.casefold():
            return payload
    return None


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


def _provider_smoke_git_failure_reason(exc: GitError) -> str:
    message = str(exc).lower()
    if "permission denied" in message or "unable to index file" in message:
        return "provider_smoke_git_permission_denied"
    return "provider_smoke_git_error"


def _candidate_file(project: Path, candidate_sha: str, path: str) -> str | None:
    result = GitWorkspace(project).run("show", f"{candidate_sha}:{path}", check=False)
    return result.stdout if result.returncode == 0 else None


def _manual_review_candidate(project: Path) -> str:
    (project / "provider-smoke.txt").write_text("after\n", encoding="utf-8")
    return GitWorkspace(project).commit_all("StageMesh provider review smoke candidate")
