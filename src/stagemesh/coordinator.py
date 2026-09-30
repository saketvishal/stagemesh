from __future__ import annotations

from pathlib import Path

from .domain import EvidenceKind, EvidenceStatus, ExecutionStatus, Stage
from .execution import Executor, FakeExecutor
from .integration import Integrator
from .lifecycle import evidence_allows_advance
from .persistence import Store
from .review import Reviewer
from .scheduling import Scheduler
from .audit import record_audit
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
    ):
        self.store = store
        self.project = Path(project)
        self.executor = executor or FakeExecutor()
        self.validator = validator or Validator()
        self.reviewer = reviewer or Reviewer()
        self.integrator = integrator or Integrator()

    def recover(self) -> None:
        for execution in self.store.running_executions():
            if execution["pid"] is None or execution["process_create_time"] is None or execution["boot_id"] is None:
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
            if not self.store.has_evidence(task_id, sha, EvidenceKind.VALIDATION):
                self.validator.validate(self.store, task_id, sha, self.project)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.VALIDATION)
        if stage is Stage.REVIEW:
            if not self.store.has_evidence(task_id, sha, EvidenceKind.REVIEW):
                self.reviewer.review(self.store, task_id, sha, self.project)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.REVIEW)
        if stage is Stage.INTEGRATE:
            if not self.store.has_evidence(task_id, sha, EvidenceKind.INTEGRATION):
                self.integrator.integrate(self.store, task_id, sha, self.project)
            return self._advance_with_evidence(task_id, stage, sha, EvidenceKind.INTEGRATION)
        return 0

    def _advance_with_evidence(self, task_id: str, stage: Stage, sha: str, kind: EvidenceKind) -> int:
        if not self.store.has_evidence(task_id, sha, kind, EvidenceStatus.PASSED):
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
