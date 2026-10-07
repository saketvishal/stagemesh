from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .diagnosis import DIAGNOSIS_EVENT, DIAGNOSIS_STOP_EVENT, PROVIDER_NO_PROGRESS, STALE_BASELINE, Diagnosis, DiagnosisPolicy, diagnose
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage, TaskStatus
from .execution import Executor, FakeExecutor
from .integration import Integrator
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .process_identity import classify_process, process_identity
from .remediation import RemediationPolicy
from .review import Reviewer, independent_review_verified
from .scheduling import Scheduler
from .serialized_integration import REBASE_CONFLICT, STALE_BASE
from .validation import Validator
from .workspace_guard import EXTERNAL_WORKSPACE_MUTATION, WorkspaceMutation, verify_candidate_workspace


_REF_STATE_CODES = frozenset({REBASE_CONFLICT, STALE_BASE})


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
    ):
        self.guard = guard  # optional autonomy supervisor: allow(), integration_verified(), recover_unknown(), execution_finished(), candidate_committed()
        self.worker_id = worker_id
        self.diagnosis_policy = diagnosis_policy or DiagnosisPolicy()
        self.store = store
        self.project = Path(project)
        self.executor = executor or FakeExecutor()
        self.validator = validator or Validator()
        self.require_independent_review = require_independent_review
        self.reviewer = reviewer or Reviewer(require_independent=require_independent_review)
        self.integrator = integrator or Integrator(require_independent_review=require_independent_review)
        self.remediation_policy = remediation_policy or RemediationPolicy()
        self.target = target

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
        progressed = 0
        for task in self._selected_tasks():
            if task["status"] == "DONE" or task["stage"] == Stage.DONE:
                continue
            if not Scheduler(self.store).decision(task["id"]).eligible:
                continue
            progressed += self._advance_task(task["id"])
        return progressed

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
        stage = Stage(task["stage"])
        if stage is Stage.PLAN:
            self.store.advance_task(task_id, Stage.IMPLEMENT)
            record_audit(self.store, "task.advance", {"task_id": task_id, "stage": Stage.IMPLEMENT})
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
            if result.failure_reason == EXTERNAL_WORKSPACE_MUTATION:
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
            )
            return 0
        candidate = self.store.latest_candidate(task_id)
        if candidate is None or not candidate["durable_handoff"]:
            return 0
        sha = str(candidate["sha"])
        if self.guard is not None and not self.guard.allow(stage, task_id, sha):
            return 0  # fail closed: the supervisor recorded why (external mutation, evidence not bound to this candidate, ...)
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
            if not self._review_satisfied(task_id, sha, bound.digest):
                self.reviewer.review(self.store, task_id, sha, self.project)
                if not self._workspace_intact(task_id, sha, "REVIEW:after_review", require=(EvidenceKind.VALIDATION,)):
                    return 1
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.REVIEW)
            if not self._review_satisfied(task_id, sha, bound.digest):
                return 0  # review infrastructure failure or unsatisfied independence: stay in REVIEW, no remediation
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.REVIEW, bound.digest)
        if stage is Stage.INTEGRATE:
            if not self._workspace_intact(task_id, sha, "INTEGRATE:before_integration", require=(EvidenceKind.VALIDATION, EvidenceKind.REVIEW)):
                return 1
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest):
                try:
                    self.integrator.integrate(self.store, task_id, sha, self.project)
                except WorkspaceMutation:
                    self._block_on_mutation(task_id, "INTEGRATE")
                    return 1
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest, EvidenceStatus.FAILED):
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
    ) -> None:
        execution = self.store.latest_execution_for_claim(claim_id)
        if execution is not None and execution["status"] == ExecutionStatus.RUNNING:
            # The executor call has returned or raised, so its provider is no longer ours to wait on.
            self.store.finish_execution(execution["id"], ExecutionStatus.FAILED, result="executor_aborted")
        self.store.release_claim(claim_id)
        if self.guard is not None:
            self.guard.execution_finished(task_id)  # the owner's leftovers are not an external mutation
        record_audit(
            self.store,
            "task.implementation_unsuccessful",
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
            },
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
                try:
                    diagnosis.provider_analysis = policy.analyst(diagnosis, sha)
                except Exception as exc:  # noqa: BLE001 - the diagnostic pass is optional; it must never block the lifecycle
                    diagnosis.provider_analysis = None
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
        self.store.block_task(task_id)
        record_audit(self.store, DIAGNOSIS_EVENT, diagnosis.audit_payload())
        record_audit(self.store, DIAGNOSIS_STOP_EVENT, {**diagnosis.audit_payload(), "remediation_attempts": 0})
        record_audit(
            self.store,
            "task.remediation_exhausted",
            {"task_id": task_id, "candidate_sha": diagnosis.candidate_sha, "stage": "IMPLEMENT", "reason": "repeated_failure_diagnosed"},
        )
        return True

    def _review_satisfied(self, task_id: str, sha: str, contract_hash: str) -> bool:
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
        return 1

    def _integration_failure_is_ref_state(self, task_id: str, sha: str) -> bool:
        """True when the latest failed INTEGRATION evidence for the candidate is a typed rebase conflict or stale base."""
        row = self.store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? AND status=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED),
        ).fetchone()
        if row is None:
            return False
        try:
            findings = json.loads(row["payload"]).get("findings", [])
        except (TypeError, ValueError):
            return False
        return any(isinstance(f, dict) and f.get("code") in _REF_STATE_CODES for f in findings)

    def _remediate_or_block(self, task_id: str, sha: str, failed_stage: Stage) -> int:
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
        if failed_stage is Stage.INTEGRATE and self._integration_failure_is_ref_state(task_id, sha):
            # The code is fine; the integration ref moved and the candidate cannot be rebased onto it. A provider re-run
            # would only reproduce the same tree, so stop with the typed finding for the operator.
            self.store.block_task(task_id)
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {"task_id": task_id, "candidate_sha": sha, "stage": failed_stage, "reason": "integration_ref_state"},
            )
            return 1
        diagnosis = self._diagnose(task_id, sha)
        if diagnosis is not None and (diagnosis.repeated or diagnosis.stops_remediation) and self.diagnosis_policy.stop_on_repeat:
            # The same failure again (or one a provider cannot fix, like a stale baseline): another implementation attempt would only
            # repeat it. Stop with the diagnosis and the operator command that fixes it.
            self.store.block_task(task_id)
            record_audit(
                self.store,
                DIAGNOSIS_STOP_EVENT,
                {**diagnosis.audit_payload(), "remediation_attempts": self.store.task_remediation_count(task_id, str(failed_stage))},
            )
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {
                    "task_id": task_id,
                    "candidate_sha": sha,
                    "stage": failed_stage,
                    "reason": "stale_baseline_diagnosed" if diagnosis.category == STALE_BASELINE else "repeated_failure_diagnosed",
                },
            )
            return 1
        # The budget is scoped to task + failed stage so a new candidate SHA cannot reset it.
        if self.store.task_remediation_count(task_id, str(failed_stage)) >= self.remediation_policy.max_attempts:
            self.store.block_task(task_id)
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {"task_id": task_id, "candidate_sha": sha, "stage": failed_stage},
            )
            return 1
        self.store.add_task_remediation(task_id, str(failed_stage), sha)
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
            },
        )
        return 1
