from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, ProcessIdentity, Stage
from stagemesh.execution import ExecutionResult, FakeExecutor
from stagemesh.lifecycle import LifecycleError, evidence_allows_advance
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process
from stagemesh.review import Reviewer
from stagemesh.task_sources import DiscoveredTask, GitHubIssueSource, OutboundSync, sync_source
from stagemesh.workers import heartbeat_worker, register_worker
from stagemesh.scheduling import Scheduler
from stagemesh.remediation import RemediationPolicy, finding_identity


def assert_raises(exc_type, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def run_until_idle(coord: Coordinator, limit: int = 20) -> None:
    for _ in range(limit):
        if coord.tick() == 0:
            return
    raise AssertionError("coordinator did not become idle")


class HandoffExecutor(FakeExecutor):
    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        result = super().run(store, task_id, claim_id, project)
        return ExecutionResult(result.status, result.candidate_sha, durable_handoff=True)


def with_store(fn) -> None:
    with tempfile.TemporaryDirectory(prefix="stagemesh-invariant-") as raw:
        tmp = Path(raw)
        store = Store(tmp / "state.sqlite3")
        store.migrate()
        try:
            fn(store, tmp / "project")
        finally:
            store.close()
            shutil.rmtree(tmp / "project" / ".git", ignore_errors=True)


def main() -> int:
    def live_worker_restart(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        store.advance_task(task_id, Stage.IMPLEMENT)
        assert store.acquire_claim(task_id, "worker-a", lease_seconds=60)
        assert Coordinator(store, project).tick() == 0

    def dead_worker_recovers(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        store.advance_task(task_id, Stage.IMPLEMENT)
        assert store.acquire_claim(task_id, "worker-a", lease_seconds=-1)
        assert Coordinator(store, project).tick() == 1
        assert store.get_task(task_id)["stage"] == Stage.VALIDATE

    def implementation_survives_validation(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project)
        coord.tick()
        coord.tick()
        candidate = store.latest_candidate(task_id)
        assert candidate is not None
        Coordinator(store, project).tick()
        assert store.latest_candidate(task_id)["sha"] == candidate["sha"]

    def live_validation_survives(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        sha = "abc123"
        store.add_candidate(task_id, sha, "fake", True)
        store.advance_task(task_id, Stage.VALIDATE)
        store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
        Coordinator(store, project).recover()
        assert next(store.running_executions())["status"] == ExecutionStatus.RUNNING

    def dead_validation_restarts_same_sha(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        sha = "abc123"
        store.add_candidate(task_id, sha, "fake", True)
        store.advance_task(task_id, Stage.VALIDATE)
        execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
        store.finish_execution(execution_id, ExecutionStatus.UNKNOWN, sha)
        Coordinator(store, project).tick()
        assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION)

    def reviewer_capacity_no_reimplementation(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project, reviewer=Reviewer(fail_capacity=True))
        for _ in range(5):
            coord.tick()
        assert store.get_task(task_id)["stage"] == Stage.REVIEW
        assert len(list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))) == 1

    def completed_not_redispatched(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        run_until_idle(Coordinator(store, project))
        assert store.get_task(task_id)["stage"] == Stage.DONE
        assert Coordinator(store, project).tick() == 0

    def durable_handoff(store: Store, project: Path) -> None:
        task_id = store.upsert_task("work")
        coord = Coordinator(store, project, executor=HandoffExecutor())
        coord.tick()
        coord.tick()
        assert store.latest_candidate(task_id)["durable_handoff"] == 1

    def targeted_ops(store: Store, project: Path) -> None:
        first = store.upsert_task("first")
        second = store.upsert_task("second")
        store.advance_task(first, Stage.IMPLEMENT)
        assert store.get_task(second)["stage"] == Stage.PLAN

    def source_semantics(store: Store, project: Path) -> None:
        assert sync_source(store, [DiscoveredTask("github", "1", "deferred", eligible=False)]) == []
        tasks, status = GitHubIssueSource(error="rate-limit").discover()
        assert tasks == []
        assert status == "UNKNOWN"

    def worker_heartbeat_and_outbound_sync(store: Store, project: Path) -> None:
        register_worker(
            store,
            "worker-1",
            "codex",
            {"code"},
            ProcessIdentity(pid=1, create_time=2.0, boot_id="boot", executable="codex"),
            lease_seconds=1,
        )
        heartbeat_worker(store, "worker-1", lease_seconds=10)
        assert store.workers()[0]["provider"] == "codex"
        OutboundSync(store).publish("github", "1", "DONE", {"sha": "abc"})
        assert store.source_events()[0]["direction"] == "outbound"

    def dependency_scheduling(store: Store, project: Path) -> None:
        first = store.upsert_task("first", source="local", source_id="first")
        second = store.upsert_task("second", source="local", source_id="second")
        store.add_dependency(second, first)
        scheduler = Scheduler(store)
        assert scheduler.decision(second).eligible is False
        store.advance_task(first, Stage.DONE)
        assert scheduler.decision(second).eligible is True

    def finding_convergence_is_bounded(store: Store, project: Path) -> None:
        task_id = store.upsert_task("review task")
        sha = "review-sha"
        finding_id = finding_identity(sha, "bug")
        store.upsert_finding(finding_id, task_id, sha, "P1", "bug")
        policy = RemediationPolicy(max_attempts=2)
        assert policy.should_remediate(store, finding_id) is True
        policy.record_attempt(store, finding_id, "FAILED")
        assert policy.should_remediate(store, finding_id) is True
        policy.record_attempt(store, finding_id, "FAILED")
        assert policy.should_remediate(store, finding_id) is False
        store.close_finding(finding_id)
        assert policy.should_remediate(store, finding_id) is False

    cases = [
        live_worker_restart,
        dead_worker_recovers,
        implementation_survives_validation,
        live_validation_survives,
        dead_validation_restarts_same_sha,
        reviewer_capacity_no_reimplementation,
        completed_not_redispatched,
        durable_handoff,
        targeted_ops,
        source_semantics,
        worker_heartbeat_and_outbound_sync,
        dependency_scheduling,
        finding_convergence_is_bounded,
    ]
    for case in cases:
        with_store(case)

    assert classify_process(
        ProcessIdentity(pid=100, create_time=1.0, boot_id="a", executable="worker"),
        ProcessIdentity(pid=100, create_time=2.0, boot_id="a", executable="worker"),
    ) == "DEAD"
    assert classify_process(ProcessIdentity(pid=100, create_time=None, boot_id="a"), None) == "UNKNOWN"
    assert_raises(
        LifecycleError,
        evidence_allows_advance,
        current=Stage.VALIDATE,
        candidate_sha="sha-b",
        evidence_sha="sha-a",
        kind=EvidenceKind.VALIDATION,
        status=EvidenceStatus.PASSED,
    )
    assert_raises(
        LifecycleError,
        evidence_allows_advance,
        current=Stage.VALIDATE,
        candidate_sha="sha",
        evidence_sha="sha",
        kind=EvidenceKind.VALIDATION,
        status=EvidenceStatus.FAILED,
    )
    print("invariants: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
