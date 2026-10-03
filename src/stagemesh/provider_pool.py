"""Dynamic per-stage provider pools: eligibility checks, automatic fallback and a readable decision log."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capacity import CapacityKind
from .execution import PROVIDER_TIMEOUT, ExecutionResult, Executor
from .domain import ExecutionStatus
from .persistence import Store
from .providers import RuntimeCommandAdapter
from .review import INFRASTRUCTURE_FAILURE
from .routing import RoutingMode
from .workspaces import prepare_task_workspace, task_workspace

IMPLEMENT = "IMPLEMENT"
REVIEW = "REVIEW"
STAGE_CAPABILITY = {IMPLEMENT: "code", REVIEW: "review"}
DEFAULT_PROVIDER_ORDER = ("codex", "claude", "grok")
DEFAULT_FAILURE_COOLDOWN_SECONDS = 900.0
PROVIDER_FAILURE_EVENT = "provider.failure"
PROVIDER_SELECTION_EVENT = "provider.selection"


@dataclass(frozen=True)
class Verdict:
    provider: str
    eligible: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"provider": self.provider, "eligible": self.eligible, "reason": self.reason}


class ProviderLog:
    """Writes provider decisions to stderr (keeps --json stdout clean) and remembers them for reports."""

    def __init__(self, stream: Any = None, echo: bool = True):
        self.lines: list[str] = []
        self._stream = stream
        self._echo = echo

    def __call__(self, message: str) -> None:
        self.lines.append(message)
        if self._echo:
            print(f"[stagemesh] {message}", file=self._stream or sys.stderr, flush=True)


def describe_verdicts(verdicts: list[Verdict]) -> str:
    return "; ".join(f"{v.provider}: {v.reason}" for v in verdicts) or "no providers configured"


def default_pools(
    names: list[str],
    stage_routes: dict[str, str],
    explicit: dict[str, tuple[str, ...]],
    single_agent_provider: str | None = None,
    routing_mode: str = RoutingMode.STAGED,
) -> dict[str, tuple[str, ...]]:
    """Explicit pools win. Otherwise a routed provider is merely tried first and every other provider is a fallback."""
    ordered = [n for n in DEFAULT_PROVIDER_ORDER if n in names] + sorted(n for n in names if n not in DEFAULT_PROVIDER_ORDER)
    pools: dict[str, tuple[str, ...]] = {}
    for stage in (IMPLEMENT, REVIEW):
        if stage in explicit:
            pools[stage] = explicit[stage]
        elif routing_mode == RoutingMode.SINGLE_AGENT and single_agent_provider:
            pools[stage] = (single_agent_provider,)
        else:
            routed = stage_routes.get(stage)
            pools[stage] = tuple(([routed] if routed else []) + [n for n in ordered if n != routed])
    return pools


class ProviderPool:
    def __init__(
        self,
        adapters: list[RuntimeCommandAdapter],
        pools: dict[str, tuple[str, ...]],
        *,
        require_independent: bool = True,
        cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        log: ProviderLog | None = None,
    ):
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.pools = pools
        self.require_independent = require_independent
        self.cooldown_seconds = cooldown_seconds
        self.log = log or ProviderLog()

    def pool(self, stage: str) -> tuple[str, ...]:
        return self.pools.get(stage, ())

    def evaluate(
        self, store: Store, stage: str, task_id: str | None = None, implementer: str | None = None
    ) -> list[Verdict]:
        """One verdict per pool member, in pool order, saying why it is or is not usable right now."""
        capability = STAGE_CAPABILITY[stage]
        implementer_adapter = self.adapters.get(implementer) if implementer else None
        verdicts: list[Verdict] = []
        for name in self.pool(stage):
            adapter = self.adapters.get(name)
            if adapter is None:
                verdicts.append(Verdict(name, False, "not_configured: no command configured for this provider"))
            elif capability not in adapter.capabilities:
                verdicts.append(Verdict(name, False, f"missing_capability: does not support '{capability}'"))
            elif adapter.check_capacity() != CapacityKind.AVAILABLE:
                verdicts.append(Verdict(name, False, f"cli_not_installed: '{adapter.command[0]}' is not callable on PATH"))
            else:
                recent = self._recent_failure(store, stage, task_id, name)
                if recent is not None:
                    verdicts.append(Verdict(name, False, recent))
                elif stage == REVIEW and self.require_independent and implementer and _same(adapter, implementer, implementer_adapter):
                    why = "produced the candidate" if adapter.name == implementer else "uses the same command as the implementer"
                    verdicts.append(Verdict(name, False, f"not_independent: {why}"))
                else:
                    verdicts.append(Verdict(name, True, "eligible"))
        return verdicts

    def _recent_failure(self, store: Store, stage: str, task_id: str | None, provider: str) -> str | None:
        if task_id is None:
            return None
        now = time.time()
        rows = store.conn.execute(
            "SELECT payload, created_at FROM audit_events WHERE event_type=? AND created_at>=? ORDER BY created_at DESC",
            (PROVIDER_FAILURE_EVENT, now - self.cooldown_seconds),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if payload.get("task_id") == task_id and payload.get("stage") == stage and payload.get("provider") == provider:
                age = int(now - row["created_at"])
                return f"recent_failure: {payload.get('reason')} {age}s ago (cooldown {int(self.cooldown_seconds)}s)"
        return None

    def record_failure(self, store: Store, stage: str, task_id: str, provider: str, reason: str) -> None:
        store.add_audit_event(
            PROVIDER_FAILURE_EVENT, {"task_id": task_id, "stage": stage, "provider": provider, "reason": reason[:300]}
        )

    def announce(self, store: Store, stage: str, task_id: str | None, verdicts: list[Verdict], note: str = "") -> list[RuntimeCommandAdapter]:
        eligible = [self.adapters[v.provider] for v in verdicts if v.eligible]
        self.log(f"stage {stage} starting{f' for task {task_id}' if task_id else ''}{note}")
        self.log(f"  provider pool considered: {', '.join(self.pool(stage)) or '(empty)'}")
        for verdict in verdicts:
            if not verdict.eligible:
                self.log(f"  skipped {verdict.provider}: {verdict.reason}")
        self.log(f"  eligible in order: {', '.join(a.name for a in eligible) or '(none)'}")
        store.add_audit_event(
            PROVIDER_SELECTION_EVENT,
            {"task_id": task_id, "stage": stage, "verdicts": [v.to_dict() for v in verdicts]},
        )
        return eligible

    def preflight(self, store: Store, task_id: str | None = None) -> tuple[bool, str, list[Verdict], list[Verdict]]:
        """Can this configuration possibly implement AND independently review? Returns (ok, diagnostic, impl, review)."""
        impl = self.evaluate(store, IMPLEMENT, task_id)
        review = self.evaluate(store, REVIEW, task_id)
        implementers = [self.adapters[v.provider] for v in impl if v.eligible]
        reviewers = [self.adapters[v.provider] for v in review if v.eligible]
        if not implementers:
            return False, f"no implementation provider is available. IMPLEMENT pool: {describe_verdicts(impl)}", impl, review
        if self.require_independent and not any(
            r.name != i.name and r.command != i.command for i in implementers for r in reviewers
        ):
            names = lambda items: ", ".join(a.name for a in items) or "none"  # noqa: E731
            return (
                False,
                "independent review is required but cannot be satisfied: no eligible reviewer is distinct from any "
                f"eligible implementer (not_independent). Eligible implementers: {names(implementers)}; eligible reviewers: "
                f"{names(reviewers)}. IMPLEMENT pool: {describe_verdicts(impl)}. REVIEW pool: {describe_verdicts(review)}. "
                "Install/authenticate another provider or set routing.require_independent_review to false for diagnostics.",
                impl,
                review,
            )
        return True, "", impl, review

    def review_adapter(
        self, store: Store, task_id: str, candidate_sha: str, implementer: str | None
    ) -> tuple["FallbackReviewAdapter | None", list[Verdict]]:
        verdicts = self.evaluate(store, REVIEW, task_id, implementer)
        eligible = self.announce(store, REVIEW, task_id, verdicts, f" (candidate {candidate_sha[:10]}, implementer {implementer})")
        if not self.require_independent and implementer:
            # Independence is optional here, but a provider never reviews its own candidate: prefer others, else deterministic.
            implementer_adapter = self.adapters.get(implementer)
            eligible = [a for a in eligible if not _same(a, implementer, implementer_adapter)]
        if not eligible:
            if self.require_independent:
                self.log(
                    "  REFUSED: independent review cannot be satisfied; providers considered: " + describe_verdicts(verdicts)
                )
            else:
                self.log("  no review provider eligible; using deterministic contract review")
            return None, verdicts
        return FallbackReviewAdapter(self, store, task_id, eligible), verdicts


def _same(adapter: RuntimeCommandAdapter, implementer: str, implementer_adapter: RuntimeCommandAdapter | None) -> bool:
    if adapter.name.casefold() == implementer.casefold():
        return True
    return implementer_adapter is not None and adapter.command == implementer_adapter.command


class FallbackReviewAdapter:
    """Tries eligible reviewers in order; `name` always names the provider whose answer is returned."""

    def __init__(self, pool: ProviderPool, store: Store, task_id: str, adapters: list[RuntimeCommandAdapter]):
        self.pool = pool
        self.store = store
        self.task_id = task_id
        self.adapters = adapters
        self.name = adapters[0].name
        self.attempts: list[dict[str, str]] = []

    def review_candidate(self, prompt: str, project: Path, candidate_sha: str) -> str:
        previous: str | None = None
        for adapter in self.adapters:
            if previous is not None:
                self.pool.log(f"  fallback: {previous} failed ({self.attempts[-1]['reason']}) -> trying {adapter.name}")
            self.pool.log(f"  selected review provider {adapter.name}")
            self.name = adapter.name
            response = adapter.review_candidate(prompt, project, candidate_sha)
            reason = _infrastructure_reason(response)
            if reason is None:
                self.pool.log(f"  final review provider: {adapter.name}")
                return response
            self.attempts.append({"provider": adapter.name, "reason": reason})
            self.pool.record_failure(self.store, REVIEW, self.task_id, adapter.name, reason)
            previous = adapter.name
        self.pool.log("  all eligible review providers failed: " + "; ".join(f"{a['provider']}: {a['reason']}" for a in self.attempts))
        return response


def _infrastructure_reason(response: str) -> str | None:
    try:
        parsed = json.loads(response)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("decision") == INFRASTRUCTURE_FAILURE:
        return str(parsed.get("reason") or "review_provider_failure")
    return None


class PooledExecutor(Executor):
    """Implementation executor that walks the IMPLEMENT pool, falling back when a provider is unavailable."""

    name = "pool"

    def __init__(self, pool: ProviderPool):
        self.pool = pool

    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        log = self.pool.log
        worktree = task_workspace(project, task_id)
        verdicts = self.pool.evaluate(store, IMPLEMENT, task_id)
        eligible = self.pool.announce(
            store, IMPLEMENT, task_id, verdicts, f"; isolated worktree {worktree} (project checkout is never edited)"
        )
        if not eligible:
            log("  REFUSED: no implementation provider available: " + describe_verdicts(verdicts))
            return ExecutionResult(
                ExecutionStatus.FAILED,
                capacity_failure=True,
                failure_reason="no_implementation_provider_available: " + describe_verdicts(verdicts),
            )
        failures: list[str] = []
        reasons: list[str] = []
        for index, adapter in enumerate(eligible):
            if index:
                log(f"  fallback: {eligible[index - 1].name} failed ({reasons[-1]}) -> trying {adapter.name}")
            log(f"  selected implementation provider {adapter.name}")
            self.name = adapter.name
            try:
                result = adapter.execute(store, task_id, claim_id, project)
            except Exception as exc:  # noqa: BLE001 - one provider crashing must not stop the fallback chain
                result = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True, failure_reason=f"{type(exc).__name__}: {exc}")
            if result.capacity_failure or result.failure_reason == PROVIDER_TIMEOUT:
                reason = result.failure_reason or "provider_failure"
                reasons.append(reason)
                failures.append(f"{adapter.name}: {reason}")
                self.pool.record_failure(store, IMPLEMENT, task_id, adapter.name, reason)
                _reset_worktree(project, task_id)
                continue
            log(f"  final implementation provider: {adapter.name}; candidate {result.candidate_sha or '-'}; result {result.status}")
            return result
        log("  REFUSED: every eligible implementation provider failed: " + "; ".join(failures))
        return ExecutionResult(
            ExecutionStatus.FAILED,
            capacity_failure=True,
            failure_reason="all_implementation_providers_failed: " + "; ".join(failures),
        )


def _reset_worktree(project: Path, task_id: str) -> None:
    """Discard a failed provider's partial edits so the next provider starts from the last commit."""
    path = prepare_task_workspace(project, task_id)
    for args in (["reset", "--hard", "HEAD"], ["clean", "-fdq"]):
        subprocess.run(["git", *args], cwd=path, capture_output=True, check=False)
