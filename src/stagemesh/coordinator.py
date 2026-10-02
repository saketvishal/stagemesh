from __future__ import annotations

from pathlib import Path

from .audit import record_audit
from .contract_binding import contract_for_candidate
from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage
from .execution import Executor, FakeExecutor
from .integration import Integrator
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .process_identity import classify_process, process_identity
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
        self.remediation_policy = remediation_policy or RemediationPolicy()

    def recover(self) -> None:
        for execution in self.store.running_executions():
            if execution["claim_id"] is None or execution["kind"] != "IMPLEMENTATION":
                continue
            saved = self.store.execution_process_identity(execution["id"])
            state = classify_process(saved, process_identity(saved.pid))
            if state != "DEAD":
                continue
            self.store.recover_stale_execution_claim(execution["id"], "DEAD_PROCESS_IDENTITY")

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
            record_audit(self.store, "task.advance", {"task_id": task_id, "stage": Stage.IMPLEMENT})
            return 1
        if stage is Stage.IMPLEMENT:
            claim_id = self.store.acquire_claim(task_id, "local-worker")
            if claim_id is None:
                return 0
            result = self.executor.run(self.store, task_id, claim_id, self.project)
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
