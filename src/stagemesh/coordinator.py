from __future__ import annotations

import time

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .baseline import resolve_integration_ref
from .block_kinds import BlockKind, block_kind_for, record_block, runtime_fingerprint
from .contract_binding import contract_for_candidate
from .contracts import _matches, task_contract_path
from .efficiency import BudgetPolicy, record_provider_call, take_call_usage
from .diagnosis import (
    CONTRACT_SCOPE,
    DIAGNOSIS_EVENT,
    DIAGNOSIS_STOP_EVENT,
    PROVIDER_NO_PROGRESS,
    STALE_BASELINE,
    VALIDATION_GATE,
    Diagnosis,
    DiagnosisPolicy,
    diagnose,
    normalize,
)
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage, TaskStatus
from .execution import Executor, FakeExecutor
from .git import GitWorkspace
from .integration import Integrator
from .integration_recovery import (
    DEFAULT_MAX_RECOVERIES,
    _conflict_files,
    base_relation,
    block_with_decision,
    build_context,
    classify_integration_failure,
    record_decision,
    record_recovery,
    recoveries,
)
from .lifecycle import evidence_allows_advance
from .outbound_lifecycle import outcomes, sync_outbound_outcomes
from .persistence import Store
from .process_identity import classify_process, process_identity
from .provider_pool import pool_exhaustion_evidence
from .queue_control import OperatorStopRequested
from .recovery import RecoveryRefusal, advance_baseline, rebaseline_task
from .recovery_decision import (
    ENVIRONMENT_BLOCKED,
    INCONCLUSIVE_VALIDATION,
    JUDGMENT_REQUIRED,
    REMEDIATION_EXHAUSTED,
    DecisionAdvisor,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryContext,
    RecoveryDecision,
    decide,
)
from .remediation import RemediationPolicy, finding_identity
from .remediation_recovery import (
    MAX_EXHAUSTION_ROUNDS,
    ROUND_EVENT,
    SWITCH_EVENT,
    judgment_reason,
    producers_of_failed_candidates,
    rounds,
    stalled_since_last_round,
)
from .review import Reviewer, adverse_gate, independent_review_verified
from .review_disposition import (
    UNRESOLVED_EVENT,
    AdverseSummary,
    reconcile_withdrawals,
    recheck_review_authorized,
    unresolved_adverse_review_findings,
)
from .scheduling import Scheduler
from .task_completion import record_task_owner_state
from .task_selection import contract_state
from .validation import Validator
from .workspace_guard import (
    EXTERNAL_WORKSPACE_MUTATION,
    WorkspaceMutation,
    verify_candidate_workspace,
)
from .workspaces import NO_IMPLEMENTATION_CHANGE


def _is_workspace_mutation_reason(reason: str | None) -> bool:
    return bool(reason) and EXTERNAL_WORKSPACE_MUTATION in str(reason)


@dataclass(frozen=True)
class TargetSelection:
    task_id: str


class TargetSelectionError(ValueError):
    pass


class Coordinator:
    def __init__(
        self,
        store: Store,
        project: Path,
        executor: Executor | None = None,
        validator: Validator | None = None,
        reviewer: Reviewer | None = None,
        integrator: Integrator | None = None,
        remediation_policy: RemediationPolicy | None = None,
        target: TargetSelection | None = None,
        require_independent_review: bool = False,
        worker_id: str = "local-worker",
        diagnosis_policy: DiagnosisPolicy | None = None,
        guard: Any | None = None,
        outbound_sync: Any | None = None,
        outbound_sources: frozenset[str] = frozenset(),
        max_integration_recoveries: int = DEFAULT_MAX_RECOVERIES,
        recovery_advisor: DecisionAdvisor | None = None,
        budget_policy: BudgetPolicy | None = None,
        adverse_policy: Any | None = None,
    ):
        self.max_integration_recoveries = max_integration_recoveries
        self.recovery_advisor = recovery_advisor  # only consulted for contexts the deterministic rules leave ambiguous
        self.guard = guard  # optional autonomy supervisor: allow(), integration_verified(), recover_unknown(), execution_finished(), candidate_committed()
        self.worker_id = worker_id
        self.diagnosis_policy = diagnosis_policy or DiagnosisPolicy()
        self.budget_policy = budget_policy or BudgetPolicy()
        self.store = store
        self.project = Path(project)
        self.executor = executor or FakeExecutor()
        self.validator = validator or Validator()
        self.require_independent_review = require_independent_review
        self.reviewer = reviewer or Reviewer(require_independent=require_independent_review)
        self.integrator = integrator or Integrator(require_independent_review=require_independent_review)
        self.remediation_policy = remediation_policy or RemediationPolicy()
        self.target = target
        self.adverse_policy = adverse_policy
        self.adverse_override: dict[str, str] | None = None
        self.last_adverse_block: tuple[str, ...] | None = None
        self.outbound_sync = outbound_sync
        self.outbound_sources = outbound_sources

    def recover(self) -> None:
        for execution in self.store.running_executions():
            if not self._target_allows(str(execution["task_id"])):
                continue
            task = self.store.get_task(str(execution["task_id"]))
            if task is not None and (task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE):
                self.store.mark_orphan_running_execution_failed(execution["id"], "TASK_ALREADY_DONE")
                continue
            if self._execution_claim_is_missing_or_inactive(execution):
                saved = self.store.execution_process_identity(execution["id"])
                state = classify_process(saved, process_identity(saved.pid))
                if state == "DEAD":
                    self.store.mark_orphan_running_execution_failed(execution["id"], "INACTIVE_CLAIM_DEAD_PROCESS")
                continue
            if execution["kind"] != "IMPLEMENTATION":
                continue
            saved = self.store.execution_process_identity(execution["id"])
            state = classify_process(saved, process_identity(saved.pid))
            if state != "DEAD":
                continue
            self.store.recover_stale_execution_claim(execution["id"], "DEAD_PROCESS_IDENTITY")

    def _execution_claim_is_missing_or_inactive(self, execution) -> bool:
        return not self.store.has_active_claim_for_execution(execution["id"])

    def validate_target(self) -> None:
        if self.target is None:
            return
        task_id = self.target.task_id
        task = self.store.get_task(task_id)
        if task is None:
            raise TargetSelectionError(f"target task does not exist: {task_id}")
        if task["status"] == TaskStatus.BLOCKED:
            raise TargetSelectionError(f"target task is blocked: {task_id}")
        if task["status"] == TaskStatus.DONE or task["stage"] == Stage.DONE:
            raise TargetSelectionError(f"target task is done: {task_id}")
        decision = Scheduler(self.store).decision(task_id)
        if not decision.eligible:
            raise TargetSelectionError(f"target task is not eligible: {task_id} ({decision.reason})")

    def tick(self) -> int:
        self.validate_target()
        self.recover()
        self.last_execution_refusals: list[dict[str, Any]] = []
        progressed = 0
        before = outcomes(self._selected_tasks()) if self.outbound_sync is not None else {}
        for task in self._selected_tasks():
            if task["status"] == "DONE" or task["stage"] == Stage.DONE:
                continue
            if not Scheduler(self.store).decision(task["id"]).eligible:
                continue
            progressed += self._advance_task(task["id"])
        self._sync_outbound(before)
        return progressed

    def _sync_outbound(self, before: dict[str, str]) -> None:
        """Publish DONE/BLOCKED transitions of GitHub-sourced tasks; failures are recorded and never alter local state."""
        if self.outbound_sync is None or not self.outbound_sources:
            return
        after = outcomes(self._selected_tasks())
        sync_outbound_outcomes(
            self.store,
            self.outbound_sync,
            self.outbound_sources,
            self._selected_tasks(),
            {task_id: outcome for task_id, outcome in after.items() if before.get(task_id) != outcome},
        )

    def _selected_tasks(self):
        if self.target is None:
            return self.store.tasks()
        task = self.store.get_task(self.target.task_id)
        return [task] if task is not None else []

    def _target_allows(self, task_id: str) -> bool:
        return self.target is None or self.target.task_id == task_id

    def _advance_task(self, task_id: str) -> int:
        task = self.store.get_task(task_id)
        if task is None:
            return 0
        # Selection can precede another connection's ownership publication.
        # Recovery has already removed only owners proven dead; never start a
        # new stage over any remaining owner, including claimless cleanup fences.
        owners = self.store.conn.execute(
            "SELECT id, kind FROM executions WHERE task_id=? AND status='RUNNING' ORDER BY id",
            (task_id,),
        ).fetchall()
        if owners:
            if not hasattr(self, "last_execution_refusals"):
                self.last_execution_refusals = []
            self.last_execution_refusals.append({
                "task_id": task_id,
                "execution_ids": [str(row["id"]) for row in owners],
                "kinds": sorted({str(row["kind"]) for row in owners}),
                "reason": "task has an existing live or unknown stage execution",
            })
            return 0
        stage = Stage(task["stage"])
        if self.store.task_contract(task_id) is not None or task_contract_path(self.project, task_id) is not None:
            state = contract_state(self.store, self.project, task_id, auto_plan=False)
            if state != "valid":
                record_block(self.store, task_id, BlockKind.HUMAN_DECISION,
                    f"invalid_contract: {state}; code remediation cannot change the frozen contract",
                    stage=str(stage), detail={"failure_code": "invalid_contract"})
                return 1
        if stage is Stage.PLAN:
            self.store.advance_task(task_id, Stage.IMPLEMENT)
            record_audit(self.store, "task.advance", {"task_id": task_id, "stage": Stage.IMPLEMENT})
            record_task_owner_state(self.store, task_id, "task_advanced", self.project)
            return 1
        if stage is Stage.IMPLEMENT:
            if self._implementation_may_still_run(task_id):
                return 0
            if self._provider_stalled(task_id):
                return 1
            worker_id = self.worker_id
            claim_id = self.store.acquire_claim(task_id, worker_id)
            if claim_id is None:
                return 0
            record_audit(
                self.store,
                "task.claimed",
                {
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "worker_id": worker_id,
                    "claim_type": "IMPLEMENTATION",
                    "executor": self.executor.name,
                },
            )
            try:
                result = self.executor.run(self.store, task_id, claim_id, self.project)
            except OperatorStopRequested as exc:
                if not exc.cleanup_complete:
                    execution = self.store.latest_execution_for_claim(claim_id)
                    if execution is not None:
                        self.store.retain_execution_for_cleanup(str(execution["id"]), str(exc))
                    raise
                execution = self.store.latest_execution_for_claim(claim_id)
                if execution is not None and execution["status"] == ExecutionStatus.RUNNING:
                    self.store.finish_execution(execution["id"], ExecutionStatus.FAILED, result="operator_stop")
                self._release_unsuccessful_implementation(task_id, claim_id, status="STOPPED", reason="operator_stop",
                    candidate_sha=None, durable_handoff=False)
                raise
            except Exception as exc:  # noqa: BLE001 - provider crashes must release implementation claims.
                self._release_unsuccessful_implementation(
                    task_id,
                    claim_id,
                    status="EXCEPTION",
                    reason=f"{type(exc).__name__}: {exc}",
                    candidate_sha=None,
                    durable_handoff=False,
                )
                return 0
            if _is_workspace_mutation_reason(result.failure_reason):
                # The execution's workspace was changed outside StageMesh. Another attempt would run on the same tampered state, so the
                # task stops (blocked) until an operator restores or removes the workspace; nothing from it is adopted.
                self._release_unsuccessful_implementation(
                    task_id, claim_id, status=str(result.status), reason=EXTERNAL_WORKSPACE_MUTATION, candidate_sha=None, durable_handoff=False
                )
                self._block_on_mutation(task_id, "IMPLEMENT")
                return 1
            if result.already_satisfied:
                self.store.release_claim(claim_id)
                if self.guard is not None:
                    self.guard.execution_finished(task_id)
                self.store.advance_task(task_id, Stage.DONE)
                record_audit(
                    self.store,
                    "task.already_satisfied",
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "executor": self.executor.name,
                        "candidate_sha": None,
                        **(result.satisfaction or {}),
                    },
                )
                record_task_owner_state(self.store, task_id, "task_already_satisfied", self.project)
                return 1
            if result.capacity_failure:
                # Provider is unavailable (not-found, rate-limit, capacity exhausted).
                # Release the claim immediately so the task can be re-dispatched rather
                # than being stranded until lease TTL expires.
                self.store.release_claim(claim_id)
                if self.guard is not None:
                    self.guard.execution_finished(task_id)  # whatever the provider left behind is the owner's, not a second writer's
                record_audit(
                    self.store,
                    "task.capacity_failure",
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "executor": self.executor.name,
                        "reason": result.failure_reason or "unknown_capacity_failure",
                        **pool_exhaustion_evidence(result.failure_reason, result.provider_attempts),
                    },
                )
                return 0
            if result.status is ExecutionStatus.SUCCEEDED and result.candidate_sha and result.durable_handoff:
                if self.guard is not None:
                    self.guard.candidate_committed(task_id, result.candidate_sha)  # StageMesh's own commit is registered, not inferred
                self.store.advance_task(task_id, Stage.VALIDATE)
                record_audit(
                    self.store,
                    "candidate.produced",
                    {"task_id": task_id, "candidate_sha": result.candidate_sha, "executor": self.executor.name},
                )
                record_task_owner_state(self.store, task_id, "candidate_produced", self.project)
                return 1
            reason = result.failure_reason
            if result.status is ExecutionStatus.SUCCEEDED:
                reason = reason or "implementation_succeeded_without_durable_candidate"
            else:
                reason = reason or "implementation_failed"
            self._release_unsuccessful_implementation(
                task_id,
                claim_id,
                status=str(result.status),
                reason=reason,
                candidate_sha=result.candidate_sha,
                durable_handoff=result.durable_handoff,
                provider_attempts=result.provider_attempts,
                provider_output=result.provider_output,
            )
            # A repeated no-progress failure is answered now (next recovery round or a typed block), not at the next dispatch.
            moved = self.store.get_task(task_id)
            if moved is not None and Stage(moved["stage"]) is Stage.IMPLEMENT and self._provider_stalled(task_id):
                return 1
            return 0
        candidate = self.store.latest_candidate(task_id)
        if candidate is None or not candidate["durable_handoff"]:
            return 0
        sha = str(candidate["sha"])
        if self.guard is not None and not self.guard.allow(stage, task_id, sha):
            return 0  # fail closed: the supervisor recorded why (external mutation, evidence not bound to this candidate, ...)
        prepare = getattr(self.integrator, "refresh_before_verification", None)
        if stage in {Stage.VALIDATE, Stage.REVIEW} and callable(prepare):
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            failed = any(self.store.has_bound_evidence(task_id, sha, kind, bound.digest, EvidenceStatus.FAILED)
                         for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW))
            # A replacement SHA must not bypass a persisted failure or its remediation budget.
            if not failed and not self.store.open_findings_for_candidate(task_id, sha):
                required = (EvidenceKind.VALIDATION,) if stage is Stage.REVIEW else ()
                if not self._workspace_intact(task_id, sha, f"{stage}:before_freshness_check", require=required):
                    return 1
                try:
                    refreshed = prepare(self.store, task_id, sha, self.project)
                except WorkspaceMutation:
                    self._block_on_mutation(task_id, str(stage))
                    return 1
                if refreshed:
                    record_audit(self.store, "candidate.refreshed_before_verification", {
                        "task_id": task_id, "from_candidate": sha, "stage": str(stage),
                    })
                    record_task_owner_state(self.store, task_id, "candidate_refreshed_before_verification", self.project)
                    return 1  # the replacement must earn its own validation and review on later ticks
        if stage is Stage.VALIDATE:
            if not self._workspace_intact(task_id, sha, "VALIDATE:before_validation"):
                return 1
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, bound.digest):
                self.validator.validate(self.store, task_id, sha, self.project)
                if not self._workspace_intact(task_id, sha, "VALIDATE:after_validation"):
                    return 1  # the evidence just recorded is not accepted: the task does not advance or remediate on it
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.VALIDATE)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.VALIDATION, bound.digest)
        if stage is Stage.REVIEW:
            if not self._workspace_intact(task_id, sha, "REVIEW:before_review", require=(EvidenceKind.VALIDATION,)):
                return 1
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            reconcile_withdrawals(self.store, task_id, sha, bound.digest)
            if not self._review_satisfied(task_id, sha, bound.digest):
                pending = AdverseSummary(tuple(unresolved_adverse_review_findings(self.store, task_id, sha)))
                if pending and not recheck_review_authorized(self.store, task_id, sha):
                    # An earlier reviewer's adverse finding is still open on this exact candidate. Asking again until someone
                    # passes would only bury it, so no new reviewer runs: the finding is remediated or dispositioned explicitly.
                    record_audit(self.store, UNRESOLVED_EVENT, {
                        "task_id": task_id, "candidate_sha": sha, "reason": pending.reason(),
                        "finding_ids": [item["finding_id"] for item in pending.findings],
                    })
                    return self._remediate_or_block(task_id, sha, Stage.REVIEW)
                review_status = self.reviewer.review(self.store, task_id, sha, self.project)
                if not self._workspace_intact(task_id, sha, "REVIEW:after_review", require=(EvidenceKind.VALIDATION,)):
                    return 1
                reconcile_withdrawals(self.store, task_id, sha, bound.digest)
                if (self.adverse_policy is not None and getattr(self.reviewer, "separate_roles", False) and review_status == EvidenceStatus.PASSED
                        and not self._review_satisfied(task_id, sha, bound.digest)
                        and not self.store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.FAILED)):
                    current = [p for p in self.store.bound_evidence_payloads(task_id, sha, EvidenceKind.REVIEW, bound.digest)
                               if p.get("adverse_policy_digest") == self.adverse_policy.digest()]
                    if any(p.get("review_role") == "normal" for p in current) and not any(p.get("review_role") == "adverse" for p in current):
                        record_task_owner_state(self.store, task_id, "normal_review_completed_adverse_pending", self.project)
                        return 1  # normal completion is progress; the next budget-checked tick earns adverse review
            if self._review_satisfied(task_id, sha, bound.digest):
                return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.REVIEW, bound.digest)
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.REVIEW)
            if not self._review_satisfied(task_id, sha, bound.digest):
                capacity_row = self.store.conn.execute(
                    "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? ORDER BY rowid DESC LIMIT 1",
                    (task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.CAPACITY),
                ).fetchone()
                if capacity_row is not None:
                    try:
                        cap_payload = json.loads(capacity_row["payload"])
                    except (TypeError, ValueError):
                        cap_payload = {}
                    failure_reason = cap_payload.get("review_infrastructure_failure") or "review_infrastructure_failure"
                    if failure_reason == "budget_exhausted":
                        record_block(
                            self.store, task_id, BlockKind.RECOVERABLE_OPERATIONAL,
                            str(cap_payload.get("review_budget_reason") or "review provider budget exhausted"),
                            stage=str(stage), candidate_sha=sha, runtime=runtime_fingerprint(self.project),
                            detail={"failure_code": "budget_exhausted"},
                        )
                        return 1
                    if failure_reason in {
                        "authentication_failure",
                        "quota_exceeded",
                        "quota_rate_limit",
                        "provider_unavailable",
                        "independent_review_unavailable",
                        "review_role_identity_unavailable",
                        "implementation_identity_unavailable",
                        "provider_timeout",
                    }:
                        providers_text = ""
                        if cap_payload.get("review_providers_considered"):
                            providers_text = "; ".join(
                                f"{c.get('provider')}: {c.get('reason')}"
                                for c in cap_payload["review_providers_considered"]
                            )
                        reason_msg = f"review infrastructure failure on candidate {sha[:10]}: {failure_reason}"
                        if providers_text:
                            reason_msg += f" ({providers_text})"
                        record_block(
                            self.store,
                            task_id,
                            BlockKind.EXTERNAL_DEPENDENCY,
                            reason_msg,
                            stage=str(stage),
                            candidate_sha=sha,
                            runtime=runtime_fingerprint(self.project),
                            detail={
                                "review_infrastructure_failure": failure_reason,
                                "providers_considered": cap_payload.get("review_providers_considered"),
                            },
                        )
                        return 1
                return 0  # review infrastructure failure or unsatisfied independence: stay in REVIEW, no remediation
            return 0
        if stage is Stage.INTEGRATE:
            if self.adverse_policy is not None:
                bound = contract_for_candidate(self.store, task_id, sha, self.project)
                decision = adverse_gate(
                    self.store,
                    task_id,
                    sha,
                    bound.digest,
                    self.adverse_policy,
                    override=self.adverse_override,
                )
                if not decision.allowed:
                    self.last_adverse_block = decision.reasons
                    self.store.block_task(task_id)
                    record_audit(
                        self.store,
                        "adverse_review.blocked",
                        {"task_id": task_id, "reasons": list(decision.reasons)},
                    )
                    record_task_owner_state(self.store, task_id, "adverse_review_blocked", self.project)
                    return 1
            if not self._workspace_intact(task_id, sha, "INTEGRATE:before_integration", require=(EvidenceKind.VALIDATION, EvidenceKind.REVIEW)):
                return 1
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            pending = AdverseSummary(tuple(unresolved_adverse_review_findings(self.store, task_id, sha)))
            if pending:  # nothing integrates while an adverse review finding on this candidate is unresolved
                record_audit(self.store, UNRESOLVED_EVENT, {
                    "task_id": task_id, "candidate_sha": sha, "reason": pending.reason(),
                    "finding_ids": [item["finding_id"] for item in pending.findings], "stage": "INTEGRATE",
                })
                record_block(self.store, task_id, BlockKind.HUMAN_DECISION, pending.reason(), stage=str(Stage.INTEGRATE),
                             candidate_sha=sha, detail={"failure_code": "unresolved_adverse_review_finding"})
                return 1
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest):
                try:
                    self.integrator.integrate(self.store, task_id, sha, self.project)
                except WorkspaceMutation:
                    self._block_on_mutation(task_id, "INTEGRATE")
                    return 1
                moved = self.store.get_task(task_id)
                if moved is not None and Stage(moved["stage"]) is not Stage.INTEGRATE:
                    return 1  # the integrator rebased the candidate and sent it back to VALIDATE: earlier failures belong to the old SHA
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest) and self.store.has_bound_evidence(
                task_id, sha, EvidenceKind.INTEGRATION, bound.digest, EvidenceStatus.FAILED
            ):
                return self._remediate_or_block(task_id, sha, Stage.INTEGRATE)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.INTEGRATION, bound.digest)
        return 0

    def _workspace_intact(self, task_id: str, sha: str, stage: str, require: tuple[EvidenceKind, ...] = ()) -> bool:
        """Prove the candidate is exactly what the authorized execution sealed; on any difference record it, block the task and say no."""
        try:
            verify_candidate_workspace(self.store, self.project, task_id, sha, stage, require=require)
        except WorkspaceMutation:
            self._block_on_mutation(task_id, stage)
            return False
        return True

    def _block_on_mutation(self, task_id: str, stage: str) -> None:
        self.store.block_task(task_id)
        record_audit(self.store, "task.blocked", {"task_id": task_id, "reason": EXTERNAL_WORKSPACE_MUTATION, "stage": stage})

    def _implementation_may_still_run(self, task_id: str) -> bool:
        """True when a prior implementation process is LIVE or of UNKNOWN state; lease expiry is not proof of death."""
        for execution in self.store.running_executions():
            if execution["task_id"] != task_id or execution["kind"] != "IMPLEMENTATION":
                continue
            saved = self.store.execution_process_identity(execution["id"])
            state = classify_process(saved, process_identity(saved.pid))
            if state == "DEAD":
                self.store.mark_orphan_running_execution_failed(execution["id"], "DEAD_PROCESS_IDENTITY")
                continue
            if state == "UNKNOWN" and self.guard is not None and self.guard.recover_unknown(task_id, str(execution["id"])):
                continue  # fenced under the explicit recovery policy (never declared dead): the task continues on a replacement worktree
            return True
        return False

    def _release_unsuccessful_implementation(
        self,
        task_id: str,
        claim_id: str,
        *,
        status: str,
        reason: str,
        candidate_sha: str | None,
        durable_handoff: bool,
        provider_attempts: list | None = None,
        provider_output: str | None = None,
    ) -> None:
        execution = self.store.latest_execution_for_claim(claim_id)
        if execution is not None and execution["status"] == ExecutionStatus.RUNNING:
            # The executor call has returned or raised, so its provider is no longer ours to wait on.
            self.store.finish_execution(execution["id"], ExecutionStatus.FAILED, result="executor_aborted")
        self.store.release_claim(claim_id)
        if self.guard is not None:
            self.guard.execution_finished(task_id)  # the owner's leftovers are not an external mutation
        if self._should_recheck_existing_candidate_after_noop(task_id, reason):
            candidate = self.store.latest_candidate(task_id)
            sha = str(candidate["sha"]) if candidate is not None else None
            self.store.advance_task(task_id, Stage.REVIEW)
            record_audit(
                self.store,
                "task.recheck_existing_candidate",
                {
                    "task_id": task_id,
                    "candidate_sha": sha,
                    "from_stage": "IMPLEMENT",
                    "to_stage": "REVIEW",
                    "reason": reason,
                },
            )
        record_audit(
            self.store,
            "task.implementation_cancelled" if status == "STOPPED" else "task.implementation_unsuccessful",
            {
                "task_id": task_id,
                "claim_id": claim_id,
                "execution_id": execution["id"] if execution is not None else None,
                "execution_status": execution["status"] if execution is not None else None,
                "result_status": status,
                "executor": self.executor.name,
                "reason": reason,
                "candidate_sha": candidate_sha,
                "durable_handoff": durable_handoff,
                **({"provider_output": provider_output[:500]} if provider_output else {}),
                **pool_exhaustion_evidence(reason, provider_attempts),
            },
        )

    def _should_recheck_existing_candidate_after_noop(self, task_id: str, reason: str) -> bool:
        """A no-op remediation after REVIEW failure means re-check the candidate, not force an artificial diff."""
        if NO_IMPLEMENTATION_CHANGE not in reason:
            return False
        candidate = self.store.latest_candidate(task_id)
        if candidate is None or not candidate["durable_handoff"]:
            return False
        sha = str(candidate["sha"])
        try:
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
        except Exception:  # noqa: BLE001 - ordinary failure handling will diagnose invalid contract/candidate state.
            return False
        return self.store.has_bound_evidence(
            task_id,
            sha,
            EvidenceKind.VALIDATION,
            bound.digest,
            EvidenceStatus.PASSED,
        ) and self.store.has_bound_evidence(
            task_id,
            sha,
            EvidenceKind.REVIEW,
            bound.digest,
            EvidenceStatus.FAILED,
        )

    def _diagnose(self, task_id: str, sha: str) -> Diagnosis | None:
        """Diagnose the task's failures (and, when an analyst is configured, run its separate read-only pass); never raises."""
        policy = self.diagnosis_policy
        try:
            diagnosis = diagnose(self.store, task_id, self.project, policy.repeat_threshold)
            if diagnosis is None:
                return None
            wanted = policy.dispatch == "every_failure" or (policy.dispatch == "on_repeat" and diagnosis.repeated)
            if policy.analyst is not None and wanted:
                take_call_usage()
                started = time.monotonic()
                try:
                    diagnosis.provider_analysis = policy.analyst(diagnosis, sha)
                    record_provider_call(
                        self.store, task_id=task_id, purpose="diagnosis", candidate_sha=sha, duration_seconds=time.monotonic() - started,
                        provider=str((diagnosis.provider_analysis or {}).get("provider") or "analyst"),
                        outcome="succeeded" if diagnosis.provider_analysis else "failed", reason=None if diagnosis.provider_analysis else "no usable analysis",
                        usage=take_call_usage(),
                    )
                except Exception as exc:  # noqa: BLE001 - the diagnostic pass is optional; it must never block the lifecycle
                    diagnosis.provider_analysis = None
                    record_provider_call(
                        self.store, task_id=task_id, purpose="diagnosis", provider="analyst", outcome="failed", reason=type(exc).__name__,
                        candidate_sha=sha, duration_seconds=time.monotonic() - started,
                    )
                    record_audit(self.store, "task.diagnosis_provider_failed", {"task_id": task_id, "reason": f"{type(exc).__name__}: {exc}"[:300]})
            record_audit(self.store, DIAGNOSIS_EVENT, diagnosis.audit_payload())
            return diagnosis
        except Exception as exc:  # noqa: BLE001 - diagnosis is advice; a bug in it must not change remediation behavior
            record_audit(self.store, "task.diagnosis_error", {"task_id": task_id, "reason": f"{type(exc).__name__}: {exc}"[:300]})
            return None

    def _provider_stalled(self, task_id: str) -> bool:
        """Before another implementation attempt: stop when the provider already made no progress on repeat. True means blocked."""
        if not self.diagnosis_policy.stop_on_repeat:
            return False
        try:
            diagnosis = diagnose(self.store, task_id, self.project, self.diagnosis_policy.repeat_threshold)
        except Exception:  # noqa: BLE001 - diagnosis is advice; it must not break dispatch
            return False
        if diagnosis is None or diagnosis.category != PROVIDER_NO_PROGRESS or not diagnosis.repeated:
            return False
        if not stalled_since_last_round(self.store, task_id):
            return False  # a recovery round already granted the next attempt and it has not run yet
        record_audit(self.store, DIAGNOSIS_EVENT, diagnosis.audit_payload())
        record_audit(self.store, DIAGNOSIS_STOP_EVENT, {**diagnosis.audit_payload(), "remediation_attempts": 0})
        sha = diagnosis.candidate_sha or ""
        findings = self.store.open_findings_for_candidate(task_id, sha) if sha else []
        self._on_remediation_exhausted(task_id, sha, Stage.IMPLEMENT, findings, diagnosis, "repeated_failure_diagnosed", queue=False)
        return self.store.get_task(task_id)["status"] == TaskStatus.BLOCKED

    def _review_satisfied(self, task_id: str, sha: str, contract_hash: str) -> bool:
        if self.adverse_policy is not None:
            return adverse_gate(self.store, task_id, sha, contract_hash, self.adverse_policy).allowed
        if unresolved_adverse_review_findings(self.store, task_id, sha):
            return False  # a PASS from another reviewer never erases an unresolved adverse finding on the same candidate
        for payload in self.store.bound_evidence_payloads(task_id, sha, EvidenceKind.REVIEW, contract_hash):
            if not self.require_independent_review or independent_review_verified(payload):
                return True
        return False

    def _integration_durable(self, task_id: str, sha: str) -> bool:
        """DONE requires the configured integration ref to actually contain the exact candidate."""
        if self.integrator.integration_ref is None:
            return True  # synthetic evidence-only integrator (no durable ref configured)
        if self.integrator.ref_contains(self.project, sha):
            # DONE only after integration is verified: expected content landed and post-merge checks passed.
            return self.guard is None or bool(self.guard.integration_verified(task_id, sha))
        record_audit(
            self.store,
            "integration.ref_missing_candidate",
            {"task_id": task_id, "candidate_sha": sha, "integration_ref": self.integrator.integration_ref},
        )
        return False

    def _advance_with_evidence(self, task_id: str, stage: Stage, sha: str, kind: EvidenceKind, contract_hash: str) -> int:
        if not self.store.has_bound_evidence(task_id, sha, kind, contract_hash, EvidenceStatus.PASSED):
            return 0
        if kind is EvidenceKind.INTEGRATION and not self._integration_durable(task_id, sha):
            return 0
        decision = evidence_allows_advance(
            current=stage,
            candidate_sha=sha,
            evidence_sha=sha,
            kind=kind,
            status=EvidenceStatus.PASSED,
        )
        self.store.advance_task(task_id, decision.target)
        record_audit(
            self.store,
            "task.advance",
            {"task_id": task_id, "stage": decision.target, "candidate_sha": sha, "reason": decision.reason},
        )
        record_task_owner_state(self.store, task_id, "task_advanced_with_evidence", self.project)
        return 1

    def _recover_integration(self, task_id: str, sha: str) -> int | None:
        """Repair a routine integration failure without the operator; None when the failure is some other kind (ordinary remediation).

        Every step is chosen by `recovery_decision.decide`: retry the same reviewed candidate, refresh it onto the current ref (a new SHA
        that is validated and independently reviewed again), redo the task on a clean worktree, or block with a typed kind.
        """
        failure = classify_integration_failure(self.store, task_id, sha)
        if failure is None:
            return None
        attempted = recoveries(self.store, task_id)
        bound = contract_for_candidate(self.store, task_id, sha, self.project)
        can_refresh = failure.category != "merge_conflict_with_current_target" and hasattr(self.integrator, "refresh")
        for _ in range(3):  # a refresh that does not apply is followed by exactly one more decision in the same tick
            ctx = build_context(
                failure,
                sha,
                attempted,
                self.max_integration_recoveries,
                relation=self._base_relation(sha),
                validation_state=self._evidence_state(task_id, sha, EvidenceKind.VALIDATION),
                review_state=self._evidence_state(task_id, sha, EvidenceKind.REVIEW),
                in_scope=lambda path: bool(_matches(path, bound.contract.allowed_files)),
                can_refresh_mechanically=can_refresh,
            )
            decision = decide(ctx, self.recovery_advisor)
            record_decision(self.store, task_id, ctx, decision)
            if decision.blocks:
                block_with_decision(self.store, task_id, sha, failure, attempted, decision, runtime_fingerprint(self.project))
                return 1
            if decision.action in {RecoveryAction.REFRESH_REVALIDATE, RecoveryAction.REFRESH_REVALIDATE_REREVIEW}:
                applied = self.integrator.refresh(self.store, task_id, sha, self.project)  # type: ignore[attr-defined]
                if not applied:  # keep the files the refresh conflicted in: scope and repeat-conflict rules need them
                    failure.files = _conflict_files(str(getattr(self.integrator, "last_refresh_failure", "") or "")) or failure.files
                record = record_recovery(self.store, task_id, sha, failure, attempted, decision.action, outcome="applied" if applied else "did_not_apply")
                if applied:
                    return 1  # a new candidate SHA is back at VALIDATE; earlier evidence belongs to the old SHA
                attempted, can_refresh = [*attempted, record], False
                continue
            record = record_recovery(self.store, task_id, sha, failure, attempted, decision.action)
            if decision.action is RecoveryAction.REPAIR_WORKTREE:
                blocked = self._redo_on_current_base(task_id, sha, failure)
                if blocked is not None:
                    refused = (
                        blocked
                        if isinstance(blocked, RecoveryDecision)
                        else RecoveryDecision(RecoveryAction.BLOCK_EXTERNAL, "rules", blocked)
                    )
                    block_with_decision(self.store, task_id, sha, failure, [*attempted, record], refused, runtime_fingerprint(self.project))
                return 1
            return 1  # RETRY_INTEGRATION (anything else an advisor could not legally say here): the next tick integrates the same candidate
        return 1

    def _base_relation(self, sha: str) -> str:
        ref = getattr(self.integrator, "integration_ref", None)
        git = GitWorkspace(self.project)
        tip = None
        if ref:
            result = git.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
            tip = result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
        return base_relation(lambda older, newer: git.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0, tip, sha)

    def _evidence_state(self, task_id: str, sha: str, kind: EvidenceKind) -> str | None:
        row = self.store.conn.execute(
            "SELECT status FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (task_id, sha, kind)
        ).fetchone()
        return None if row is None else str(row["status"])

    def _redo_on_current_base(self, task_id: str, sha: str, failure) -> str | RecoveryDecision | None:  # type: ignore[no-untyped-def]
        """Redo the task on a clean worktree at the current integration ref; the old candidate, evidence and worktree stay as history.

        None on success, otherwise why the redo was refused.
        """
        ref = self.integrator.integration_ref or resolve_integration_ref(self.project)
        if not ref:
            return "no integration ref is configured to refresh against"
        message = (
            f"{ref} advanced after review and candidate {sha[:10]} no longer integrates ({failure.category}"
            + (f"; conflicting files: {', '.join(failure.files)}" if failure.files else "")
            + "). Redo the task on the current base and keep what already landed there."
        )
        self.store.upsert_finding(finding_identity(sha, message, None), task_id, sha, "error", message)
        self.store.advance_task(task_id, Stage.IMPLEMENT)  # a baseline cannot be moved while the task is at INTEGRATE
        try:
            advance_baseline(self.store, self.project, task_id, ref, reason=f"integration recovery: {failure.category}", yes=True)
        except RecoveryRefusal as exc:
            self.store.advance_task(task_id, Stage.INTEGRATE)
            if exc.code == "baseline_not_ancestor" and hasattr(self.integrator, "reconstruct_onto_current_tip"):
                applied, reason, files, code = self.integrator.reconstruct_onto_current_tip(self.store, task_id, sha, self.project)  # type: ignore[attr-defined]
                if applied:
                    record_task_owner_state(self.store, task_id, "integration_recovery_reconstructed_on_current_tip", self.project)
                    return None
                if files:
                    failure.files = files
                if code in {"ambiguous_delta", "out_of_scope_delta"}:
                    return RecoveryDecision(RecoveryAction.ABORT_UNSAFE, "rules", f"reconstructing the task-owned delta on the current {ref} was unsafe ({code}: {reason[:200]})")
                if code == "patch_conflict" or (code == "patch_failed" and failure.files):
                    conflict_files = files or failure.files
                    return RecoveryDecision(RecoveryAction.BLOCK_HUMAN_DECISION, "rules", f"reconstructing the task-owned delta on the current {ref} still conflicts in {', '.join(conflict_files) or 'the patch'}; automatic recovery cannot decide which side wins")
                return f"reconstructing the task-owned delta on the current {ref} was refused ({code}: {reason[:200]})"
            return f"restarting on the current {ref} was refused ({exc.code}: {str(exc)[:200]})"
        record_task_owner_state(self.store, task_id, "integration_recovery_redo_on_current_base", self.project)
        return None

    def _auto_rebaseline(self, task_id: str, diagnosis: Diagnosis) -> bool:
        if diagnosis.category != STALE_BASELINE:
            return False
        integration_ref = self.integrator.integration_ref or resolve_integration_ref(self.project)
        if not integration_ref:
            return False
        try:
            rebaseline_task(
                self.store,
                self.project,
                task_id,
                integration_ref,
                validate=True,
                reason="automatic stale baseline recovery",
            )
        except RecoveryRefusal as exc:
            record_audit(
                self.store,
                "task.auto_rebaseline_refused",
                {"task_id": task_id, "code": exc.code, "message": str(exc)[:300]},
            )
            return False
        return True

    def _remediate_or_block(self, task_id: str, sha: str, failed_stage: Stage) -> int:
        kind = {Stage.VALIDATE: EvidenceKind.VALIDATION, Stage.REVIEW: EvidenceKind.REVIEW,
                Stage.INTEGRATE: EvidenceKind.INTEGRATION}[failed_stage]
        evidence = self.store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY rowid DESC LIMIT 1",
            (task_id, sha, kind),
        ).fetchone()
        if evidence is not None:
            try:
                invalid = next((item for item in json.loads(evidence["payload"]).get("findings", [])
                                if item.get("code") == "invalid_contract"), None)
            except (TypeError, ValueError):
                invalid = None
            if invalid is not None:
                record_block(self.store, task_id, BlockKind.HUMAN_DECISION,
                    f"invalid_contract: {invalid.get('message', 'contract repair required')}; no implementation retry",
                    stage=str(failed_stage), candidate_sha=sha, detail={"failure_code": "invalid_contract"})
                return 1
        findings = self.store.open_findings_for_candidate(task_id, sha)
        if not findings:
            self.store.upsert_finding(
                f"{sha}:failed-{failed_stage}",
                task_id,
                sha,
                "error",
                f"{failed_stage} failed without structured findings",
            )
            findings = self.store.open_findings_for_candidate(task_id, sha)
        if failed_stage is Stage.INTEGRATE:
            recovered = self._recover_integration(task_id, sha)
            if recovered is not None:
                return recovered
        if judgment_reason([str(f["message"]) for f in findings]) is not None:
            # A finding that says a decision is needed is not repaired by more attempts: ask the question now, not after the budget.
            return self._on_remediation_exhausted(task_id, sha, failed_stage, findings, None, "judgment_required")
        diagnosis = self._diagnose(task_id, sha)
        if diagnosis is not None and self._auto_rebaseline(task_id, diagnosis):
            return 1
        if diagnosis is not None and diagnosis.stops_remediation and (
            diagnosis.environment_unavailable or diagnosis.diagnosis_required or diagnosis.inconclusive_validation or self.diagnosis_policy.stop_on_repeat
        ):
            # The same failure again, a proven launch outage, or a gate that never finished.
            # An unfinished gate is not a reason to implement again.
            record_audit(
                self.store,
                DIAGNOSIS_STOP_EVENT,
                {**diagnosis.audit_payload(), "remediation_attempts": self.store.task_remediation_count(task_id, str(failed_stage))},
            )
            reason = (
                "validation_environment_unavailable" if diagnosis.environment_unavailable
                else "gate_timeout_diagnosis_required" if diagnosis.diagnosis_required
                else "validation_inconclusive" if diagnosis.inconclusive_validation
                else "stale_baseline_diagnosed" if diagnosis.category == STALE_BASELINE
                else "repeated_failure_diagnosed"
            )
            return self._on_remediation_exhausted(task_id, sha, failed_stage, findings, diagnosis, reason)
        # The budget is scoped to task + failed stage so a new candidate SHA cannot reset it.
        if self.store.task_remediation_count(task_id, str(failed_stage)) >= self.remediation_policy.max_attempts:
            if diagnosis is not None and self._reopen_budget_once(task_id, failed_stage, diagnosis):
                self.store.add_task_remediation(task_id, str(failed_stage), sha)
                return self._queue_remediation(task_id, sha, failed_stage, findings, budget_reopened=True)
            return self._on_remediation_exhausted(task_id, sha, failed_stage, findings, diagnosis, "remediation_budget_exhausted")
        self.store.add_task_remediation(task_id, str(failed_stage), sha)
        return self._queue_remediation(task_id, sha, failed_stage, findings)

    def _queue_remediation(self, task_id: str, sha: str, failed_stage: Stage, findings: list, **extra: Any) -> int:
        for finding in findings:
            self.remediation_policy.record_attempt(
                self.store,
                str(finding["id"]),
                "QUEUED",
                {
                    "candidate_sha": sha,
                    "failed_stage": failed_stage,
                    "next_stage": Stage.IMPLEMENT,
                },
            )
        self.store.advance_task(task_id, Stage.IMPLEMENT)
        record_audit(
            self.store,
            "task.remediation_queued",
            {
                "task_id": task_id,
                "candidate_sha": sha,
                "stage": failed_stage,
                "finding_count": len(findings),
                **extra,
            },
        )
        record_task_owner_state(self.store, task_id, "remediation_queued", self.project)
        return 1

    def _reopen_budget_once(self, task_id: str, failed_stage: Stage, diagnosis: Diagnosis) -> bool:
        if diagnosis.category in {CONTRACT_SCOPE, STALE_BASELINE, VALIDATION_GATE}:
            return False  # more code cannot fix an immutable scope or an unavailable validation environment
        signature = {"stage": str(failed_stage), "category": diagnosis.category, "summary": normalize(diagnosis.summary[:300])}
        for row in self.store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type=? ORDER BY created_at DESC, rowid DESC",
            ("task.parent_budget_reopened",),
        ):
            payload = _loads(row["payload"])
            previous = payload.get("signature")
            if isinstance(previous, dict):
                previous = {**previous, "summary": normalize(str(previous.get("summary", "")))}
            if payload.get("task_id") == task_id and previous == signature:
                return False
        cleared = self.store.clear_task_remediations(task_id, str(failed_stage))
        record_audit(
            self.store,
            "task.parent_budget_reopened",
            {
                "task_id": task_id,
                "stage": str(failed_stage),
                "reason": "budget_is_safety_not_completion",
                "signature": signature,
                "cleared": cleared,
            },
        )
        record_task_owner_state(self.store, task_id, "parent_budget_reopened", self.project)
        return True

    def _implementation_health(self, task_id: str) -> dict[str, str]:
        """provider -> "ok" or the reason it cannot take the next implementation attempt (empty when there is no provider pool)."""
        pool = getattr(self.executor, "pool", None)
        if pool is None:
            return {}
        try:
            return {v.provider: ("ok" if v.eligible else v.reason) for v in pool.evaluate(self.store, "IMPLEMENT", task_id)}
        except Exception:  # noqa: BLE001 - health is an input to a decision, never a reason to fail the lifecycle
            return {}

    def _on_remediation_exhausted(
        self, task_id: str, sha: str, failed_stage: Stage, findings: list, diagnosis: Diagnosis | None, cause: str, *, queue: bool = True
    ) -> int:
        """The remediation budget (or an identical repeat) ended the ordinary attempts. Recover with a different approach, or block with
        the kind of block it really is; budget exhaustion alone is never a question for the operator."""
        done = rounds(self.store, task_id)
        messages = [str(f["message"]) for f in findings]
        needs_judgment = judgment_reason(messages)
        if needs_judgment is None and diagnosis is not None and diagnosis.category == CONTRACT_SCOPE:
            needs_judgment = "the contract's allowed files and the work disagree: should the contract be widened (stagemesh rebind-contract) to include what the work needed, or must the work stay inside it?"
        health = self._implementation_health(task_id)
        if needs_judgment is not None:
            category = JUDGMENT_REQUIRED
        elif diagnosis is not None and diagnosis.inconclusive_validation:
            category = INCONCLUSIVE_VALIDATION
        elif diagnosis is not None and diagnosis.category in {STALE_BASELINE, VALIDATION_GATE}:
            category = ENVIRONMENT_BLOCKED  # no provider attempt can fix a baseline that cannot advance or a gate that cannot run
        else:
            category = REMEDIATION_EXHAUSTED
        ctx = RecoveryContext(
            stage=str(failed_stage),
            failure_category=category,
            candidate_sha=sha,
            validation_state=self._evidence_state(task_id, sha, EvidenceKind.VALIDATION),
            review_state=self._evidence_state(task_id, sha, EvidenceKind.REVIEW),
            retry_history=tuple(RecoveryAttempt(str(r.get("action")), REMEDIATION_EXHAUSTED) for r in done),
            provider_health=health,
            recovery_attempts=len(done),
            max_attempts=MAX_EXHAUSTION_ROUNDS,
        )
        decision = decide(ctx, self.recovery_advisor)
        record_decision(self.store, task_id, ctx, decision)
        tried = [str(r.get("action")) for r in done]
        if decision.blocks:
            reason = needs_judgment if decision.action is RecoveryAction.BLOCK_HUMAN_DECISION and needs_judgment else decision.reason
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {
                    "task_id": task_id,
                    "candidate_sha": sha,
                    "stage": failed_stage,
                    "reason": cause,
                    "block_kind": block_kind_for(decision).value,
                    "why_automatic_recovery_stopped": reason,
                    "recoveries_attempted": tried,
                },
            )
            record_block(
                self.store,
                task_id,
                block_kind_for(decision),
                f"{failed_stage} failed on {sha[:10]} ({cause}): {reason}",
                stage=str(failed_stage),
                candidate_sha=sha,
                runtime=runtime_fingerprint(self.project),
                detail={"recoveries_attempted": tried, "open_findings": len(findings)},
            )
            return 1
        record_audit(
            self.store,
            ROUND_EVENT,
            {"task_id": task_id, "candidate_sha": sha, "stage": failed_stage, "action": decision.action.value, "cause": cause, "round": len(done) + 1},
        )
        if decision.action is RecoveryAction.SWITCH_PROVIDER:
            record_audit(self.store, SWITCH_EVENT, {"task_id": task_id, "avoid": producers_of_failed_candidates(self.store, task_id)})
        if not queue:
            return 1  # the task is about to be dispatched again: the switch / clean worktree applies to that attempt
        return self._queue_remediation(task_id, sha, failed_stage, findings, recovery_round=len(done) + 1, recovery_action=decision.action.value)


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
