"""Dynamic per-stage provider pools: eligibility checks, automatic fallback and a readable decision log."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .capacity import CapacityKind
from .concurrency import ProviderLimiter
from .config import BUILTIN_PROVIDERS, SELECTION_POLICIES
from .contract_binding import bound_contract_from_record
from .contracts import candidate_workspace, run_gate
from .domain import ExecutionKind, ExecutionStatus
from .execution import PROVIDER_TIMEOUT, ExecutionResult, Executor
from .git import GitError
from .persistence import Store
from .providers import RuntimeCommandAdapter
from .review import INFRASTRUCTURE_FAILURE
from .routing import RoutingMode
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION, WorkspaceMutation, owned_workspace
from .workspaces import NO_IMPLEMENTATION_CHANGE, task_workspace

IMPLEMENT = "IMPLEMENT"
REVIEW = "REVIEW"
STAGE_CAPABILITY = {IMPLEMENT: "code", REVIEW: "review"}
DEFAULT_PROVIDER_ORDER = BUILTIN_PROVIDERS  # built-ins sort ahead of custom providers when no priority says otherwise
DEFAULT_PRIORITY = 100
DEFAULT_FAILURE_COOLDOWN_SECONDS = 900.0
DEFAULT_QUOTA_COOLDOWN_SECONDS = 21600.0
PROVIDER_FAILURE_EVENT = "provider.failure"
PROVIDER_SELECTION_EVENT = "provider.selection"
PROVIDER_USED_EVENT = "provider.used"
PROVIDER_NO_PROGRESS_EVENT = "provider.no_progress"
ALL_IMPLEMENTATION_PROVIDERS_NO_PROGRESS = "all_implementation_providers_no_progress"
ALL_IMPLEMENTATION_PROVIDERS_EXHAUSTED = "all_implementation_providers_exhausted"
ALL_IMPLEMENTATION_PROVIDERS_FAILED = "all_implementation_providers_failed"
POOL_EXHAUSTED_REASONS = (
    ALL_IMPLEMENTATION_PROVIDERS_NO_PROGRESS,
    ALL_IMPLEMENTATION_PROVIDERS_EXHAUSTED,
    ALL_IMPLEMENTATION_PROVIDERS_FAILED,
)
_CAPACITY_OUTCOMES = frozenset(
    {
        "quota_rate_limit",
        "provider_unavailable",
        "authentication_failure",
        "transient_provider_failure",
        "provider_failure",
    }
)
TASK_ALREADY_SATISFIED = "task_already_satisfied"


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
    *,
    capable: dict[str, set[str]] | None = None,
    priorities: dict[str, int] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Explicit pools are exact. Otherwise a routed provider is tried first and every other provider is a fallback.

    Default order is (priority, built-in rank, name), so with no priorities it is codex, claude, grok, then custom providers
    alphabetically. `capable` (stage -> provider names) keeps providers out of stages they do not serve.
    """
    priorities = priorities or {}

    def rank(name: str) -> tuple[int, int, str]:
        builtin = DEFAULT_PROVIDER_ORDER.index(name) if name in DEFAULT_PROVIDER_ORDER else len(DEFAULT_PROVIDER_ORDER)
        return (priorities.get(name, DEFAULT_PRIORITY), builtin, name)

    pools: dict[str, tuple[str, ...]] = {}
    for stage in (IMPLEMENT, REVIEW):
        eligible_names = [n for n in names if capable is None or n in capable.get(stage, set(names))]
        ordered = sorted(eligible_names, key=rank)
        if stage in explicit:
            pools[stage] = tuple(n for n in explicit[stage] if n in eligible_names)
        elif routing_mode == RoutingMode.SINGLE_AGENT and single_agent_provider:
            pools[stage] = (single_agent_provider,)
        else:
            routed = stage_routes.get(stage)
            pools[stage] = tuple(([routed] if routed else []) + [n for n in ordered if n != routed])
    return pools


def _failure_cooldown_seconds(reason: str, configured: float) -> float:
    if reason == "quota_rate_limit":
        return max(configured, DEFAULT_QUOTA_COOLDOWN_SECONDS)
    return configured


class ProviderPool:
    def __init__(
        self,
        adapters: list[RuntimeCommandAdapter],
        pools: dict[str, tuple[str, ...]],
        *,
        require_independent: bool = True,
        cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        log: ProviderLog | None = None,
        policy: str = "priority",
        weights: dict[str, int] | None = None,
        priorities: dict[str, int] | None = None,
        limiter: ProviderLimiter | None = None,
    ):
        if policy not in SELECTION_POLICIES:
            raise ValueError(f"unknown provider selection policy: {policy}")
        self.policy = policy
        self.weights = weights or {}
        self.priorities = priorities or {}
        self._reasons: dict[tuple[str, str], str] = {}
        self.adapters = {adapter.name: adapter for adapter in adapters}
        self.pools = pools
        self.require_independent = require_independent
        self.cooldown_seconds = cooldown_seconds
        self.log = log or ProviderLog()
        self.limiter = limiter  # set under parallel execution: per-provider slots and provider-wide cooldown

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
            elif self.limiter is not None and self.limiter.cooling(name) is not None:
                verdicts.append(Verdict(name, False, str(self.limiter.cooling(name))))
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
            (PROVIDER_FAILURE_EVENT, now - max(self.cooldown_seconds, DEFAULT_QUOTA_COOLDOWN_SECONDS)),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if payload.get("stage") != stage or payload.get("provider") != provider:
                continue
            reason = str(payload.get("reason") or "provider_failure")
            age = int(now - row["created_at"])
            cooldown = _failure_cooldown_seconds(reason, self.cooldown_seconds)
            if age > cooldown:
                continue
            if reason in _CAPACITY_OUTCOMES:
                return f"provider_cooldown: {reason} {age}s ago (cooldown {int(cooldown)}s)"
            if payload.get("task_id") == task_id:
                return f"recent_failure: {reason} {age}s ago (cooldown {int(cooldown)}s)"
        return None

    def record_failure(
        self,
        store: Store,
        stage: str,
        task_id: str,
        provider: str,
        reason: str,
        *,
        provider_output: str | None = None,
        retry_after: str | None = None,
        next_provider: str | None = None,
    ) -> None:
        payload: dict[str, object] = {"task_id": task_id, "stage": stage, "provider": provider, "reason": reason[:300]}
        if provider_output:
            payload["provider_output"] = provider_output[:500]
        if retry_after:
            payload["retry_after"] = retry_after[:120]
        if next_provider:
            payload["next_provider"] = next_provider
        store.add_audit_event(PROVIDER_FAILURE_EVENT, payload)

    def record_no_progress(
        self,
        store: Store,
        stage: str,
        task_id: str,
        provider: str,
        *,
        next_provider: str | None,
        sequence: list[str],
        provider_output: str | None = None,
    ) -> None:
        """A successful invocation that changed nothing while the task is still unproven. Not a failure cooldown."""
        store.add_audit_event(
            PROVIDER_NO_PROGRESS_EVENT,
            {
                "task_id": task_id,
                "stage": stage,
                "provider": provider,
                "result": NO_IMPLEMENTATION_CHANGE,
                "classification": "no_progress",
                "task_unresolved": True,
                "next_provider": next_provider,
                "sequence": list(sequence),
                **({"provider_output": provider_output[:500]} if provider_output else {}),
            },
        )

    def kind(self, name: str) -> str:
        return "built-in default provider" if name in BUILTIN_PROVIDERS else "custom provider"

    def registry_line(self) -> str:
        stage_of = {"code": "IMPLEMENT", "review": "REVIEW"}
        entries = []
        for name, adapter in self.adapters.items():
            stages = "+".join(sorted(stage_of[c] for c in adapter.capabilities if c in stage_of)) or "none"
            entries.append(
                f"{name} ({'built-in' if name in BUILTIN_PROVIDERS else 'custom'}; {stages}; "
                f"priority {self.priorities.get(name, DEFAULT_PRIORITY)}; weight {self.weights.get(name, 1)})"
            )
        return ", ".join(entries) or "(empty)"

    def announce(self, store: Store, stage: str, task_id: str | None, verdicts: list[Verdict], note: str = "") -> list[RuntimeCommandAdapter]:
        eligible = [self.adapters[v.provider] for v in verdicts if v.eligible]
        self.log(f"stage {stage} starting{f' for task {task_id}' if task_id else ''}{note}")
        self.log(f"  provider registry: {self.registry_line()}")
        self.log(f"  selection policy: {self.policy}")
        self.log(f"  provider pool considered: {', '.join(self.pool(stage)) or '(empty)'}")
        for verdict in verdicts:
            if not verdict.eligible:
                self.log(f"  skipped {verdict.provider}: {verdict.reason}")
        self.log(f"  eligible providers: {', '.join(a.name for a in eligible) or '(none)'}")
        ordered, reasons = self.order(store, stage, eligible)
        self._reasons = {key: value for key, value in self._reasons.items() if key[0] != stage}
        for name, why in reasons.items():
            self._reasons[(stage, name)] = why
        if ordered:
            self.log(f"  selection order ({self.policy}): {' > '.join(a.name for a in ordered)}")
            self.log(f"  preferred provider: {ordered[0].name} ({self.kind(ordered[0].name)}) because {reasons[ordered[0].name]}")
        store.add_audit_event(
            PROVIDER_SELECTION_EVENT,
            {
                "task_id": task_id,
                "stage": stage,
                "policy": self.policy,
                "order": [a.name for a in ordered],
                "verdicts": [v.to_dict() for v in verdicts],
            },
        )
        return ordered

    def why(self, stage: str, name: str, first: bool) -> str:
        if not first:
            return "fallback after the preferred provider failed"
        return self._reasons.get((stage, name), self.policy)

    def uses(self, store: Store, stage: str) -> list[tuple[str, float]]:
        """Chronological (provider, time) records of providers that actually answered for this stage."""
        rows = store.conn.execute(
            "SELECT payload, created_at FROM audit_events WHERE event_type=? ORDER BY created_at, rowid",
            (PROVIDER_USED_EVENT,),
        ).fetchall()
        used: list[tuple[str, float]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if payload.get("stage") == stage and isinstance(payload.get("provider"), str):
                used.append((payload["provider"], row["created_at"]))
        return used

    def record_use(self, store: Store, stage: str, task_id: str, provider: str, outcome: str) -> None:
        store.add_audit_event(
            PROVIDER_USED_EVENT, {"task_id": task_id, "stage": stage, "provider": provider, "outcome": str(outcome)}
        )

    def order(
        self, store: Store, stage: str, eligible: list[RuntimeCommandAdapter]
    ) -> tuple[list[RuntimeCommandAdapter], dict[str, str]]:
        """Rank eligible providers by the selection policy; the first is tried first and the rest are the fallback chain."""
        pool = list(self.pool(stage))
        index = {name: i for i, name in enumerate(pool)}
        base = sorted(eligible, key=lambda a: (self.priorities.get(a.name, DEFAULT_PRIORITY), index.get(a.name, len(pool))))
        reasons: dict[str, str] = {}
        if not base:
            return [], reasons
        if self.policy == "priority":
            ordered = base
            for a in ordered:
                reasons[a.name] = f"priority: priority {self.priorities.get(a.name, DEFAULT_PRIORITY)}, pool position {index.get(a.name, -1) + 1}"
            return ordered, reasons
        uses = self.uses(store, stage)
        if self.policy == "round_robin":
            last = uses[-1][0] if uses else None
            start = pool.index(last) + 1 if last in pool else 0
            cycle = pool[start:] + pool[:start]
            by_name = {a.name: a for a in base}
            ordered = [by_name[n] for n in cycle if n in by_name]
            for a in ordered:
                reasons[a.name] = (
                    f"round_robin: previous {stage} provider was {last}, next eligible in pool order"
                    if last
                    else "round_robin: first selection for this stage, pool order"
                )
            return ordered, reasons
        if self.policy == "least_recently_used":
            last_use: dict[str, float] = {}
            for name, when in uses:
                last_use[name] = when
            ordered = sorted(base, key=lambda a: last_use.get(a.name, float("-inf")))
            for a in ordered:
                reasons[a.name] = (
                    "least_recently_used: never used for this stage"
                    if a.name not in last_use
                    else f"least_recently_used: last used {int(time.time() - last_use[a.name])}s ago"
                )
            return ordered, reasons
        counts: dict[str, int] = {}
        for name, _ in uses:
            counts[name] = counts.get(name, 0) + 1
        weight = lambda a: max(1, self.weights.get(a.name, 1))
        score = lambda a: Fraction(counts.get(a.name, 0) + 1, weight(a))
        ordered = sorted(base, key=score)  # stable: ties keep priority/pool order
        for a in ordered:
            reasons[a.name] = (
                f"weighted: weight {weight(a)}, {counts.get(a.name, 0)} prior use(s), score {score(a)} (lowest score first)"
            )
        return ordered, reasons

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
            names = lambda items: ", ".join(a.name for a in items) or "none"
            message = (
                "independent review is required but cannot be satisfied: no eligible reviewer is distinct from any "
                f"eligible implementer (not_independent). Eligible implementers: {names(implementers)}; eligible reviewers: "
                f"{names(reviewers)}. IMPLEMENT pool: {describe_verdicts(impl)}. REVIEW pool: {describe_verdicts(review)}. "
                "Install/authenticate another provider or set routing.require_independent_review to false for diagnostics."
            )
            return (
                False,
                message,
                impl,
                review,
            )
        return True, "", impl, review

    def preflight_stage(
        self,
        store: Store,
        stage: str,
        task_id: str | None = None,
        *,
        implementer: str | None = None,
    ) -> tuple[bool, str, list[Verdict], list[Verdict]]:
        """Check only the provider pool needed by the task's current stage."""
        impl = self.evaluate(store, IMPLEMENT, task_id)
        review = self.evaluate(store, REVIEW, task_id, implementer if stage == REVIEW else None)
        if stage in {IMPLEMENT, "PLAN"}:
            return self.preflight(store, task_id)
        if stage == REVIEW:
            reviewers = [self.adapters[v.provider] for v in review if v.eligible]
            if self.require_independent and implementer:
                reviewers = [adapter for adapter in reviewers if adapter.name != implementer]
            if not reviewers:
                suffix = f" for implementer {implementer}" if implementer and self.require_independent else ""
                return False, f"no review provider is available{suffix}. REVIEW pool: {describe_verdicts(review)}", impl, review
        return True, "", impl, review

    def review_adapter(
        self, store: Store, task_id: str, candidate_sha: str, implementer: str | None
    ) -> tuple[FallbackReviewAdapter | None, list[Verdict]]:
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
        limiter = self.pool.limiter
        response = json.dumps({"decision": INFRASTRUCTURE_FAILURE, "reason": "no_review_provider_available"})
        for adapter in self.adapters:
            held: str | None = None
            if limiter is not None:
                held = limiter.acquire([adapter.name])  # waits for a free slot; None means the provider is cooling down
                if held is None:
                    reason = limiter.cooling(adapter.name) or "provider_cooldown"
                    self.pool.log(f"  skipped review provider {adapter.name}: {reason}")
                    self.attempts.append({"provider": adapter.name, "reason": reason})
                    previous = adapter.name
                    continue
            if previous is not None:
                self.pool.log(f"  fallback: {previous} failed ({self.attempts[-1]['reason']}) -> trying {adapter.name}")
            self.pool.log(
                f"  selected review provider {adapter.name} ({self.pool.kind(adapter.name)}): "
                f"{self.pool.why(REVIEW, adapter.name, previous is None)}"
            )
            self.name = adapter.name
            try:
                response = adapter.review_candidate(prompt, project, candidate_sha)
            finally:
                if held is not None and limiter is not None:
                    limiter.release(held)
            reason = _infrastructure_reason(response)
            if reason is None:
                self.pool.log(f"  final review provider: {adapter.name} ({self.pool.kind(adapter.name)})")
                self.pool.record_use(self.store, REVIEW, self.task_id, adapter.name, "ANSWERED")
                return response
            evidence = _infrastructure_evidence(response)
            upcoming = next((item.name for item in self.adapters if item.name not in {a["provider"] for a in self.attempts} and item.name != adapter.name), None)
            self.attempts.append({"provider": adapter.name, "reason": reason})
            self.pool.record_failure(
                self.store,
                REVIEW,
                self.task_id,
                adapter.name,
                reason,
                provider_output=evidence.get("provider_output"),
                retry_after=evidence.get("retry_after"),
                next_provider=upcoming,
            )
            if limiter is not None:  # an unavailable reviewer is unavailable for every task, not just this one
                limiter.cool_down(adapter.name, self.pool.cooldown_seconds, reason)
            previous = adapter.name
        self.pool.log("  all eligible review providers failed: " + "; ".join(f"{a['provider']}: {a['reason']}" for a in self.attempts))
        return response


def _infrastructure_payload(response: str) -> dict | None:
    try:
        parsed = json.loads(response)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("decision") == INFRASTRUCTURE_FAILURE:
        return parsed
    return None


def _infrastructure_reason(response: str) -> str | None:
    parsed = _infrastructure_payload(response)
    if parsed is None:
        return None
    return str(parsed.get("reason") or "review_provider_failure")


def _infrastructure_evidence(response: str) -> dict[str, str]:
    parsed = _infrastructure_payload(response) or {}
    evidence: dict[str, str] = {}
    for key in ("provider_output", "retry_after"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            evidence[key] = value
    return evidence


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
        no_progress: list[str] = []
        reasons: list[str] = []
        walk: list[dict[str, str]] = []
        limiter = self.pool.limiter
        remaining = list(eligible)
        index = 0
        previous_name = ""
        while remaining:
            adapter = remaining[0]
            held: str | None = None
            if limiter is not None:
                # Wait for a free slot on any not-yet-tried provider, still honouring the policy order; a saturated preferred
                # provider hands the task to the next one instead of being overloaded.
                if not any(limiter.has_room(a.name) for a in remaining):
                    log(f"  every provider is at capacity; waiting for a slot ({', '.join(a.name for a in remaining)})")
                held = limiter.acquire([a.name for a in remaining])
                if held is None:
                    break  # every remaining provider went into cooldown while waiting
                adapter = next(a for a in remaining if a.name == held)
                if adapter is not remaining[0]:
                    log(f"  provider {remaining[0].name} is at capacity -> using {adapter.name}")
            remaining.remove(adapter)
            if index:
                verb = "made no progress" if reasons[-1].startswith(NO_IMPLEMENTATION_CHANGE) else "failed"
                log(f"  fallback: {previous_name} {verb} ({reasons[-1]}) -> trying {adapter.name}")
            log(
                f"  selected implementation provider {adapter.name} ({self.pool.kind(adapter.name)}): "
                f"{self.pool.why(IMPLEMENT, adapter.name, index == 0)}"
            )
            self.name = adapter.name
            try:
                try:
                    result = adapter.execute(store, task_id, claim_id, project)
                except Exception as exc:  # noqa: BLE001 - one provider crashing must not stop the fallback chain
                    result = ExecutionResult(ExecutionStatus.FAILED, capacity_failure=True, failure_reason=f"{type(exc).__name__}: {exc}")
            finally:
                if held is not None and limiter is not None:
                    limiter.release(held)
            previous_name = adapter.name
            index += 1
            if result.failure_reason == EXTERNAL_WORKSPACE_MUTATION:
                # Not a provider problem: another provider would run in the same tampered workspace. Stop and let the task block.
                log(f"  REFUSED: {adapter.name} stopped because the task workspace was changed outside StageMesh")
                return result
            if result.capacity_failure or result.failure_reason == PROVIDER_TIMEOUT:
                reason = result.failure_reason or "provider_failure"
                reasons.append(reason)
                failures.append(f"{adapter.name}: {reason}")
                classification = "timeout" if result.failure_reason == PROVIDER_TIMEOUT else "capacity"
                walk.append({"provider": adapter.name, "outcome": reason, "classification": classification})
                nxt = remaining[0].name if remaining else None
                self.pool.record_failure(
                    store,
                    IMPLEMENT,
                    task_id,
                    adapter.name,
                    reason,
                    provider_output=result.provider_output,
                    retry_after=result.retry_after,
                    next_provider=nxt,
                )
                if limiter is not None and result.capacity_failure:
                    limiter.cool_down(adapter.name, self.pool.cooldown_seconds, reason)
                try:
                    _reset_worktree(store, project, task_id, claim_id)
                except WorkspaceMutation:
                    return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
                continue
            if result.failure_reason == NO_IMPLEMENTATION_CHANGE and result.status is ExecutionStatus.FAILED:
                proof = prove_task_already_satisfied(store, project, task_id)
                if proof is not None:
                    self.pool.record_use(store, IMPLEMENT, task_id, adapter.name, TASK_ALREADY_SATISFIED)
                    log(
                        f"  task already satisfied on {proof['baseline_sha'][:12]} by acceptance gates "
                        f"{', '.join(proof['acceptance_criteria'])}; provider {adapter.name} changed nothing"
                    )
                    return ExecutionResult(
                        ExecutionStatus.SUCCEEDED,
                        failure_reason=TASK_ALREADY_SATISFIED,
                        already_satisfied=True,
                        satisfaction={**proof, "provider": adapter.name, "task_id": task_id},
                    )
                nxt = remaining[0].name if remaining else None
                sequence = [item["provider"] for item in walk] + [adapter.name]
                self.pool.record_no_progress(
                    store,
                    IMPLEMENT,
                    task_id,
                    adapter.name,
                    next_provider=nxt,
                    sequence=sequence,
                    provider_output=result.provider_output,
                )
                reason = f"{NO_IMPLEMENTATION_CHANGE}; task unresolved"
                reasons.append(reason)
                no_progress.append(f"{adapter.name}: {NO_IMPLEMENTATION_CHANGE}")
                walk.append(
                    {
                        "provider": adapter.name,
                        "outcome": NO_IMPLEMENTATION_CHANGE,
                        "classification": "no_progress",
                        **({"provider_output": result.provider_output[:500]} if result.provider_output else {}),
                    }
                )
                try:
                    _reset_worktree(store, project, task_id, claim_id)
                except WorkspaceMutation:
                    return ExecutionResult(ExecutionStatus.FAILED, failure_reason=EXTERNAL_WORKSPACE_MUTATION)
                continue
            self.pool.record_use(store, IMPLEMENT, task_id, adapter.name, str(result.status))
            log(f"  final implementation provider: {adapter.name} ({self.pool.kind(adapter.name)}); candidate {result.candidate_sha or '-'}; result {result.status}")
            return result
        ordered = [f"{item['provider']}: {item['outcome']}" for item in walk]
        if no_progress and not failures:
            log("  REFUSED: every eligible implementation provider made no progress: " + "; ".join(ordered))
            return ExecutionResult(
                ExecutionStatus.FAILED,
                failure_reason=ALL_IMPLEMENTATION_PROVIDERS_NO_PROGRESS + ": " + "; ".join(ordered),
                provider_attempts=walk,
            )
        if no_progress and failures:
            log("  REFUSED: every eligible implementation provider stopped without a candidate: " + "; ".join(ordered))
            return ExecutionResult(
                ExecutionStatus.FAILED,
                failure_reason=ALL_IMPLEMENTATION_PROVIDERS_EXHAUSTED + ": " + "; ".join(ordered),
                provider_attempts=walk,
            )
        log("  REFUSED: every eligible implementation provider failed: " + "; ".join(ordered))
        return ExecutionResult(
            ExecutionStatus.FAILED,
            capacity_failure=True,
            failure_reason=ALL_IMPLEMENTATION_PROVIDERS_FAILED + ": " + "; ".join(ordered),
            provider_attempts=walk,
        )


def _classification(outcome: str) -> str:
    if outcome == PROVIDER_TIMEOUT:
        return "timeout"
    if outcome == NO_IMPLEMENTATION_CHANGE:
        return "no_progress"
    if outcome in _CAPACITY_OUTCOMES:
        return "capacity"
    return outcome or "unknown"


def pool_exhaustion_evidence(reason: str | None, attempts: list | None = None) -> dict[str, object]:
    """Structured audit for a pass that tried every eligible implementation provider and produced no candidate."""
    text = reason or ""
    prefix = next((item for item in POOL_EXHAUSTED_REASONS if text == item or text.startswith(item + ":")), None)
    if prefix is None and not attempts:
        return {}
    if attempts:
        outcomes = [
            {
                "provider": str(item.get("provider") or ""),
                "outcome": str(item.get("outcome") or ""),
                "classification": str(item.get("classification") or _classification(str(item.get("outcome") or ""))),
                **({"provider_output": str(item.get("provider_output"))[:500]} if item.get("provider_output") else {}),
            }
            for item in attempts
        ]
    else:
        body = text[len(prefix) + 2 :] if prefix and text.startswith(prefix + ": ") else ""
        outcomes = []
        for part in [piece.strip() for piece in body.split(";") if piece.strip()]:
            provider, separator, outcome = part.partition(":")
            outcome = outcome.strip() if separator else part
            provider = provider.strip() if separator else ""
            outcomes.append(
                {"provider": provider, "outcome": outcome, "classification": _classification(outcome)}
            )
    return {
        "pool_exhausted": True,
        "candidate_produced": False,
        "provider_sequence": [item["provider"] for item in outcomes if item["provider"]],
        "provider_outcomes": outcomes,
        "no_further_provider": "every eligible configured implementation provider was exhausted",
    }


def prove_task_already_satisfied(store: Store, project: Path, task_id: str) -> dict[str, object] | None:
    """Proof that the frozen baseline already meets every acceptance criterion via its named required test.

    Fail closed when the contract has no executable criterion, a criterion is not exactly a required-test name,
    or any of those gates fails. Provider text, exit code, and an empty diff are not proof.
    """
    row = store.task_contract(task_id)
    if row is None:
        return None
    try:
        bound = bound_contract_from_record(dict(row))
    except (TypeError, ValueError):
        return None
    contract = bound.contract
    criteria = contract.acceptance_criteria
    baseline = bound.baseline_sha
    if not contract.explicit or not criteria or not baseline or len(set(criteria)) != len(criteria):
        return None
    by_name = {gate.name: gate for gate in contract.required_tests}
    gates = []
    for criterion in criteria:
        gate = by_name.get(criterion)
        if gate is None:
            return None
        gates.append(gate)
    try:
        with candidate_workspace(project, baseline) as checkout:
            results = [run_gate(checkout, gate) for gate in gates]
    except (GitError, OSError):
        return None
    if not results or any(result.status != "PASSED" for result in results):
        return None
    return {
        "baseline_sha": baseline,
        "contract_hash": bound.digest,
        "acceptance_criteria": list(criteria),
        "gates": [{"name": result.name, "status": result.status, "returncode": result.returncode} for result in results],
    }


def _reset_worktree(store: Store, project: Path, task_id: str, claim_id: str | None) -> None:
    """Discard a failed provider's partial edits so the next provider starts from the last commit, and seal that as the workspace state."""
    with owned_workspace(store, project, task_id, ExecutionKind.IMPLEMENTATION, claim_id=claim_id) as lease:
        for args in (["reset", "--hard", "HEAD"], ["clean", "-fdq"]):
            subprocess.run(["git", *args], cwd=lease.path, capture_output=True, check=False)
        lease.seal()
