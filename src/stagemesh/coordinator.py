from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage, TaskStatus
from .execution import Executor, FakeExecutor
from .integration import Integrator
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .process_identity import classify_process, process_identity
from .remediation import RemediationPolicy
from .review import Reviewer
from .scheduling import Scheduler
from .validation import Validator


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
    ):
        self.store = store
        self.project = Path(project)
        self.executor = executor or FakeExecutor()
        self.validator = validator or Validator()
        self.reviewer = reviewer or Reviewer()
        self.integrator = integrator or Integrator()
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
            claim_id = self.store.acquire_claim(task_id, "local-worker")
            if claim_id is None:
                return 0
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
            if result.capacity_failure:
                # Provider is unavailable (not-found, rate-limit, capacity exhausted).
                # Release the claim immediately so the task can be re-dispatched rather
                # than being stranded until lease TTL expires.
                self.store.release_claim(claim_id)
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
        if stage is Stage.VALIDATE:
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, bound.digest):
                self.validator.validate(self.store, task_id, sha, self.project)
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.VALIDATION, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.VALIDATE)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.VALIDATION, bound.digest)
        if stage is Stage.REVIEW:
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest):
                self.reviewer.review(self.store, task_id, sha, self.project)
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.REVIEW, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.REVIEW)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.REVIEW, bound.digest)
        if stage is Stage.INTEGRATE:
            bound = contract_for_candidate(self.store, task_id, sha, self.project)
            if not self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest):
                self.integrator.integrate(self.store, task_id, sha, self.project)
            if self.store.has_bound_evidence(task_id, sha, EvidenceKind.INTEGRATION, bound.digest, EvidenceStatus.FAILED):
                return self._remediate_or_block(task_id, sha, Stage.INTEGRATE)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.INTEGRATION, bound.digest)
        return 0

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
        self.store.release_claim(claim_id)
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

    def _advance_with_evidence(self, task_id: str, stage: Stage, sha: str, kind: EvidenceKind, contract_hash: str) -> int:
        if not self.store.has_bound_evidence(task_id, sha, kind, contract_hash, EvidenceStatus.PASSED):
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
        eligible = [finding for finding in findings if self.remediation_policy.should_remediate(self.store, str(finding["id"]))]
        if not eligible:
            self.store.block_task(task_id)
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {"task_id": task_id, "candidate_sha": sha, "stage": failed_stage},
            )
            return 1
        for finding in eligible:
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
                "finding_count": len(eligible),
            },
        )
        return 1
