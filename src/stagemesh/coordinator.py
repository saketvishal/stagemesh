from __future__ import annotations

from pathlib import Path

from .audit import record_audit
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage
from .execution import Executor, FakeExecutor
from .integration import Integrator
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .remediation import RemediationPolicy
from .review import Reviewer
from .scheduling import Scheduler
from .validation import Validator


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
    ):
        self.store = store
        self.project = Path(project)
        self.executor = executor or FakeExecutor()
        self.validator = validator or Validator()
        self.reviewer = reviewer or Reviewer()
        self.integrator = integrator or Integrator()
        self.remediation_policy = remediation_policy

    def recover(self) -> None:
        # Unknown executions are deliberately not guessed from PID alone. Recovery
        # remains conservative until durable process identity proves ownership.
        for execution in self.store.running_executions():
            if (
                execution["pid"] is None
                or execution["process_create_time"] is None
                or execution["boot_id"] is None
            ):
                continue

    def tick(self) -> int:
        self.recover()
        progressed = 0
        for task in self.store.tasks():
            if task["status"] == "DONE" or task["stage"] == Stage.DONE:
                continue
            if not Scheduler(self.store).decision(task["id"]).eligible:
                continue
            progressed += self._advance_task(task["id"])
        return progressed

    def _advance_task(self, task_id: str) -> int:
        task = self.store.get_task(task_id)
        if task is None:
            return 0
        stage = Stage(task["stage"])
        if stage is Stage.PLAN:
            self.store.advance_task(task_id, Stage.IMPLEMENT)
            record_audit(
                self.store,
                "task.advance",
                {"task_id": task_id, "stage": Stage.IMPLEMENT},
            )
            return 1

        if stage is Stage.IMPLEMENT:
            claim_id = self.store.acquire_claim(task_id, "local-worker")
            if claim_id is None:
                return 0
            result = self.executor.run(
                self.store,
                task_id,
                claim_id,
                self.project,
            )
            if result.capacity_failure:
                self.store.release_claim(claim_id)
                record_audit(
                    self.store,
                    "task.capacity_failure",
                    {
                        "task_id": task_id,
                        "claim_id": claim_id,
                        "executor": self.executor.name,
                        "reason": result.failure_reason
                        or "unknown_capacity_failure",
                    },
                )
                return 0
            if (
                result.status is ExecutionStatus.SUCCEEDED
                and result.candidate_sha
                and result.durable_handoff
            ):
                self.store.advance_task(task_id, Stage.VALIDATE)
                record_audit(
                    self.store,
                    "candidate.produced",
                    {
                        "task_id": task_id,
                        "candidate_sha": result.candidate_sha,
                        "executor": self.executor.name,
                    },
                )
                return 1
            # A non-capacity implementation failure must not strand an active claim.
            self.store.release_claim(claim_id)
            record_audit(
                self.store,
                "task.implementation_failed",
                {
                    "task_id": task_id,
                    "claim_id": claim_id,
                    "executor": self.executor.name,
                    "reason": result.failure_reason or "implementation_failure",
                },
            )
            return 0

        candidate = self.store.latest_candidate(task_id)
        if candidate is None or not candidate["durable_handoff"]:
            return 0
        sha = str(candidate["sha"])

        if stage is Stage.VALIDATE:
            if not self.store.has_any_evidence(
                task_id,
                sha,
                EvidenceKind.VALIDATION,
            ):
                status = self.validator.validate(
                    self.store,
                    task_id,
                    sha,
                    self.project,
                )
                if status is EvidenceStatus.FAILED:
                    return self._schedule_remediation(
                        task_id,
                        sha,
                        Stage.VALIDATE,
                    )
            if self.store.has_evidence(
                task_id,
                sha,
                EvidenceKind.VALIDATION,
                EvidenceStatus.FAILED,
            ):
                return self._schedule_remediation(
                    task_id,
                    sha,
                    Stage.VALIDATE,
                )
            return self._advance_with_evidence(
                task_id,
                stage,
                sha,
                EvidenceKind.VALIDATION,
            )

        if stage is Stage.REVIEW:
            if not self.store.has_any_evidence(
                task_id,
                sha,
                EvidenceKind.REVIEW,
            ):
                status = self.reviewer.review(
                    self.store,
                    task_id,
                    sha,
                    self.project,
                )
                if status is EvidenceStatus.FAILED:
                    return self._schedule_remediation(
                        task_id,
                        sha,
                        Stage.REVIEW,
                    )
            if self.store.has_evidence(
                task_id,
                sha,
                EvidenceKind.REVIEW,
                EvidenceStatus.FAILED,
            ):
                return self._schedule_remediation(
                    task_id,
                    sha,
                    Stage.REVIEW,
                )
            return self._advance_with_evidence(
                task_id,
                stage,
                sha,
                EvidenceKind.REVIEW,
            )

        if stage is Stage.INTEGRATE:
            if not self.store.has_any_evidence(
                task_id,
                sha,
                EvidenceKind.INTEGRATION,
            ):
                self.integrator.integrate(
                    self.store,
                    task_id,
                    sha,
                    self.project,
                )
            return self._advance_with_evidence(
                task_id,
                stage,
                sha,
                EvidenceKind.INTEGRATION,
            )
        return 0

    def _schedule_remediation(
        self,
        task_id: str,
        candidate_sha: str,
        failed_stage: Stage,
    ) -> int:
        if self.remediation_policy is None:
            return 0
        findings = self.store.open_findings_for_candidate(
            task_id,
            candidate_sha,
        )
        if not findings:
            return 0
        attempts = self.store.remediation_attempt_count_for_task(task_id)
        if attempts >= self.remediation_policy.max_attempts:
            self.store.block_task(task_id)
            record_audit(
                self.store,
                "task.remediation_exhausted",
                {
                    "task_id": task_id,
                    "candidate_sha": candidate_sha,
                    "failed_stage": failed_stage,
                    "attempts": attempts,
                },
            )
            return 0

        finding = findings[0]
        self.remediation_policy.record_attempt(
            self.store,
            str(finding["id"]),
            "SCHEDULED",
            {
                "task_id": task_id,
                "candidate_sha": candidate_sha,
                "failed_stage": str(failed_stage),
                "attempt": attempts + 1,
            },
        )
        self.store.advance_task(task_id, Stage.IMPLEMENT)
        record_audit(
            self.store,
            "task.remediation_scheduled",
            {
                "task_id": task_id,
                "candidate_sha": candidate_sha,
                "failed_stage": failed_stage,
                "attempt": attempts + 1,
                "finding_id": str(finding["id"]),
            },
        )
        return 1

    def _advance_with_evidence(
        self,
        task_id: str,
        stage: Stage,
        sha: str,
        kind: EvidenceKind,
    ) -> int:
        if not self.store.has_evidence(
            task_id,
            sha,
            kind,
            EvidenceStatus.PASSED,
        ):
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
            {
                "task_id": task_id,
                "stage": decision.target,
                "candidate_sha": sha,
                "reason": decision.reason,
            },
        )
        return 1
