from __future__ import annotations

from pathlib import Path

import pytest

from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, ExecutionStatus, ProcessIdentity, Stage
from stagemesh.execution import ExecutionResult, FakeExecutor
from stagemesh.lifecycle import LifecycleError, evidence_allows_advance
from stagemesh.persistence import Store
from stagemesh.process_identity import classify_process
from stagemesh.review import Reviewer
from stagemesh.task_sources import DiscoveredTask, GitHubIssueSource, sync_source


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "state.sqlite3")
    db.migrate()
    yield db
    db.close()


def run_until_idle(coord: Coordinator, limit: int = 20) -> None:
    for _ in range(limit):
        if coord.tick() == 0:
            return
    raise AssertionError("coordinator did not become idle")


def test_live_worker_survives_coordinator_restart_without_duplicate_dispatch(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    store.advance_task(task_id, Stage.IMPLEMENT)
    assert store.acquire_claim(task_id, "worker-a", lease_seconds=60)
    restarted = Coordinator(store, tmp_path)
    assert restarted.tick() == 0
    assert len(list(store.conn.execute("SELECT * FROM claims WHERE active=1"))) == 1


def test_dead_worker_recovers_safely_after_lease(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    store.advance_task(task_id, Stage.IMPLEMENT)
    assert store.acquire_claim(task_id, "worker-a", lease_seconds=-1)
    assert Coordinator(store, tmp_path).tick() == 1
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE


def test_pid_reuse_cannot_impersonate_old_worker() -> None:
    old = ProcessIdentity(pid=100, create_time=1.0, boot_id="a", executable="worker")
    reused = ProcessIdentity(pid=100, create_time=2.0, boot_id="a", executable="worker")
    assert classify_process(old, reused) == "DEAD"


def test_identity_uncertainty_fails_safely() -> None:
    unknown = ProcessIdentity(pid=100, create_time=None, boot_id="a")
    assert classify_process(unknown, None) == "UNKNOWN"


def test_implementation_survives_validation_interruption(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    coord = Coordinator(store, tmp_path)
    coord.tick()
    coord.tick()
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert store.get_task(task_id)["stage"] == Stage.VALIDATE
    restarted = Coordinator(store, tmp_path)
    restarted.tick()
    assert store.latest_candidate(task_id)["sha"] == candidate["sha"]


def test_live_validation_survives_restart_lease_expiry(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    sha = "abc123"
    store.add_candidate(task_id, sha, "fake", True)
    store.advance_task(task_id, Stage.VALIDATE)
    store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
    Coordinator(store, tmp_path).recover()
    row = next(store.running_executions())
    assert row["status"] == ExecutionStatus.RUNNING


def test_dead_validation_restarts_against_same_candidate(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    sha = "abc123"
    store.add_candidate(task_id, sha, "fake", True)
    store.advance_task(task_id, Stage.VALIDATE)
    execution_id = store.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION, candidate_sha=sha)
    store.finish_execution(execution_id, ExecutionStatus.UNKNOWN, sha)
    Coordinator(store, tmp_path).tick()
    assert store.has_evidence(task_id, sha, EvidenceKind.VALIDATION)


def test_reviewer_provider_failure_does_not_restart_implementation(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    coord = Coordinator(store, tmp_path, reviewer=Reviewer(fail_capacity=True))
    for _ in range(5):
        coord.tick()
    assert store.get_task(task_id)["stage"] == Stage.REVIEW
    assert len(list(store.conn.execute("SELECT * FROM candidates WHERE task_id=?", (task_id,)))) == 1


def test_stale_sha_cannot_advance() -> None:
    with pytest.raises(LifecycleError):
        evidence_allows_advance(
            current=Stage.VALIDATE,
            candidate_sha="sha-b",
            evidence_sha="sha-a",
            kind=EvidenceKind.VALIDATION,
            status=EvidenceStatus.PASSED,
        )


def test_failed_validation_cannot_advance() -> None:
    with pytest.raises(LifecycleError):
        evidence_allows_advance(
            current=Stage.VALIDATE,
            candidate_sha="sha",
            evidence_sha="sha",
            kind=EvidenceKind.VALIDATION,
            status=EvidenceStatus.FAILED,
        )


def test_completed_task_remains_completed_after_restart(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    run_until_idle(Coordinator(store, tmp_path))
    assert store.get_task(task_id)["stage"] == Stage.DONE
    assert Coordinator(store, tmp_path).tick() == 0


class HandoffExecutor(FakeExecutor):
    def run(self, store: Store, task_id: str, claim_id: str | None, project: Path) -> ExecutionResult:
        result = super().run(store, task_id, claim_id, project)
        return ExecutionResult(result.status, result.candidate_sha, durable_handoff=True)


def test_worker_unable_to_push_still_produces_durable_candidate_handoff(store: Store, tmp_path: Path) -> None:
    task_id = store.upsert_task("work")
    coord = Coordinator(store, tmp_path, executor=HandoffExecutor())
    coord.tick()
    coord.tick()
    candidate = store.latest_candidate(task_id)
    assert candidate is not None
    assert candidate["durable_handoff"] == 1


def test_targeted_operations_do_not_mutate_unrelated_tasks(store: Store, tmp_path: Path) -> None:
    first = store.upsert_task("first")
    second = store.upsert_task("second")
    store.advance_task(first, Stage.IMPLEMENT)
    assert store.get_task(second)["stage"] == Stage.PLAN


def test_source_sync_distinguishes_empty_unknown_and_deferred(store: Store) -> None:
    ids = sync_source(store, [DiscoveredTask("github", "1", "deferred", eligible=False)])
    assert ids == []
    tasks, status = GitHubIssueSource(error="rate-limit").discover()
    assert tasks == []
    assert status == "UNKNOWN"
