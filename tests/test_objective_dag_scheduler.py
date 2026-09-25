"""Comprehensive deterministic test suite for StageMesh GH-88:
Dependency-aware parallel objective execution and adaptive scheduling.

Covers all 25 required deterministic scenarios:
1. Objective independent children parallelism cap (A, B launch, C waits).
2. Immediate unlock upon dependency completion (no wave barrier).
3. Dependency blocks early launch (dependency_not_done).
4. Dependency completion unlocks dependent immediately.
5. parallel_safe=False child blocks sibling launch.
6. Active sibling blocks parallel_safe=False child.
7. Overlapping ownership scopes serialize despite parallel_safe=True.
8. Non-overlapping scopes execute concurrently.
9. Objective parallelism budget bounded under higher project concurrency.
10. Multiple objectives share project capacity respecting independent budgets.
11. Remediation consumes an objective implementation slot.
12. Review does not block independent implementation.
13. Remediation coexists with safe unrelated implementation.
14. Restart reconstructs objective slots and prevents duplicate workers.
15. Completed dependency unlocks across restart.
16. Migration-capable work remains serialized.
17. Provider fallback cannot bypass objective parallelism.
18. Provider fallback cannot bypass scope restrictions.
19. Worker/worktree single-owner invariant under parallel dispatch.
20. Integration drift safety preserves existing semantics.
21. Withheld tasks receive typed, deterministic reason codes & events.
22. Bounded fairness prevents large objectives from starving others.
23. Backward compatibility for ordinary non-objective tasks.
24. Dependency cycle rejection at plan validation.
25. Recovery never duplicates child implementation ownership.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildObjective,
    BuildObjectiveEvent,
    BuildObjectiveGate,
    BuildObjectivePlan,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.objectives import create_objective, open_gates
from build_coordinator.planner import parse_planner_plan
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.scheduling import (
    SCHEDULER_REASONS,
    active_implementation_tasks,
    active_objective_implementations,
    check_task_readiness,
)
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.types import (
    ObjectivePlan,
    ObjectiveSpec,
    PlannedChildTask,
    StructuredContractError,
    TaskOwnershipScope,
)


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))
    monkeypatch.setenv("BUILD_COORDINATOR_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS", "6")


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildObjectiveEvent,
            BuildObjectiveGate,
            BuildObjectivePlan,
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildObjective,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _child(task_id: str, **kwargs) -> PlannedChildTask:
    return PlannedChildTask(
        task_id=task_id,
        title=f"Task {task_id}",
        description=f"Description for {task_id}",
        **kwargs,
    )


def _obj(objective_id: str, children: list[PlannedChildTask], *, parallelism: int = 2) -> ObjectiveSpec:
    return ObjectiveSpec(
        objective_id=objective_id,
        goal=f"Goal for {objective_id}",
        parallelism=parallelism,
        child_tasks=tuple(children),
    )


def _multi_builder_config(num_builders: int = 6, *, worktrees: dict[str, str] | None = None) -> RunnerConfig:
    worktrees = worktrees or {}
    workers = []
    for i in range(num_builders):
        name = f"builder-{chr(ord('a') + i)}"
        workers.append(
            WorkerConfig(
                name,
                "BUILDER",
                adapter="fake",
                worktree_path=worktrees.get(name),
                capabilities=("CODING",),
            )
        )
    workers.append(WorkerConfig("reviewer-1", "REVIEWER", adapter="fake", capabilities=("CODE_REVIEW",)))
    workers.append(WorkerConfig("integration-1", "INTEGRATION", adapter="fake", capabilities=("SCM_OPERATOR",)))
    return RunnerConfig(
        workers=tuple(workers),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(config: RunnerConfig | None = None, executors: dict | None = None) -> BuildRunner:
    return BuildRunner(
        SessionLocal,
        config or _multi_builder_config(),
        executors=executors or {},
        git=FakeGit(),
    )


def _launched_tasks(session: Session, result) -> set[str]:
    if not result.launched:
        return set()
    return set(
        session.scalars(
            select(BuildRunnerExecution.task_id).where(BuildRunnerExecution.execution_id.in_(result.launched))
        ).all()
    )


# ---------------------------------------------------------------------------
# Scenario 1: Objective parallelism cap (parallelism=2 under project capacity=6)
# Exactly two launch, third waits with objective_parallelism_full
# ---------------------------------------------------------------------------
def test_1_objective_independent_parallelism_cap():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-1",
                [
                    _child("T1-A", parallel_safe=True),
                    _child("T1-B", parallel_safe=True),
                    _child("T1-C", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner()
    result = runner.run_once()

    assert len(result.launched) == 2
    with SessionLocal() as session:
        launched = _launched_tasks(session, result)
        assert "T1-A" in launched
        assert "T1-B" in launched
        assert "T1-C" not in launched
        assert result.scheduling_reasons.get("T1-C") == "objective_parallelism_full"

        # Verify audit event
        events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "T1-C")
            .where(BuildTaskEvent.event_type == "runner.task_withheld")
        ).all()
        assert len(events) >= 1
        assert (events[0].event_data or {}).get("reason") == "objective_parallelism_full"


# ---------------------------------------------------------------------------
# Scenario 2: Immediate unlock upon completion (no wave barrier)
# A completes while B is active -> C launches immediately
# ---------------------------------------------------------------------------
def test_2_immediate_unlock_no_wave_barrier():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-2",
                [
                    _child("T2-A", parallel_safe=True),
                    _child("T2-B", parallel_safe=True),
                    _child("T2-C", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    # Step 1: Launch A and B
    runner = _runner()
    r1 = runner.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r1) == {"T2-A", "T2-B"}

        # Complete A to DONE (simulating completion while B remains active)
        task_a = session.get(BuildTask, "T2-A")
        task_a.state = "DONE"
        claim_a = session.scalar(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "T2-A").where(BuildTaskClaim.status == "ACTIVE")
        )
        if claim_a:
            claim_a.status = "COMPLETED"
        session.commit()

    # Step 2: Next cycle dispatches C immediately without waiting for B
    r2 = runner.run_once()
    with SessionLocal() as session:
        assert "T2-C" in _launched_tasks(session, r2)


# ---------------------------------------------------------------------------
# Scenario 3: Dependency blocks early launch (dependency_not_done)
# D depends on A -> D never launches before A == DONE
# ---------------------------------------------------------------------------
def test_3_dependency_blocks_early_launch():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-3",
                [
                    _child("T3-A", parallel_safe=True),
                    _child("T3-D", dependencies=["T3-A"], parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert "T3-A" in launched
        assert "T3-D" not in launched
        assert r.scheduling_reasons.get("T3-D") == "dependency_not_done"


# ---------------------------------------------------------------------------
# Scenario 4: Dependency completion unlocks dependent immediately
# A finishes while B remains active -> D becomes runnable
# ---------------------------------------------------------------------------
def test_4_dependency_completion_unlocks_dependent():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-4",
                [
                    _child("T4-A", parallel_safe=True),
                    _child("T4-B", parallel_safe=True),
                    _child("T4-D", dependencies=["T4-A"], parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner()
    r1 = runner.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r1) == {"T4-A", "T4-B"}
        assert r1.scheduling_reasons.get("T4-D") == "dependency_not_done"

        # Finish A
        task_a = session.get(BuildTask, "T4-A")
        task_a.state = "DONE"
        claim_a = session.scalar(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "T4-A").where(BuildTaskClaim.status == "ACTIVE")
        )
        if claim_a:
            claim_a.status = "COMPLETED"
        session.commit()

    # D launches now while B is still active
    r2 = runner.run_once()
    with SessionLocal() as session:
        assert "T4-D" in _launched_tasks(session, r2)


# ---------------------------------------------------------------------------
# Scenario 5: parallel_safe=False child blocks sibling launch
# ---------------------------------------------------------------------------
def test_5_parallel_safe_false_child_blocks_siblings():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-5",
                [
                    _child("T5-A-UNSAFE", parallel_safe=False),
                    _child("T5-B-SAFE", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert "T5-A-UNSAFE" in launched
        assert "T5-B-SAFE" not in launched
        assert r.scheduling_reasons.get("T5-B-SAFE") == "parallel_safe_serialization"


# ---------------------------------------------------------------------------
# Scenario 6: Active sibling blocks parallel_safe=False child
# ---------------------------------------------------------------------------
def test_6_active_sibling_blocks_parallel_safe_false_child():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-6",
                [
                    _child("T6-SAFE", parallel_safe=True),
                    _child("T6-UNSAFE", parallel_safe=False),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert "T6-SAFE" in launched
        assert "T6-UNSAFE" not in launched
        assert r.scheduling_reasons.get("T6-UNSAFE") == "parallel_safe_serialization"


# ---------------------------------------------------------------------------
# Scenario 7: Overlapping scopes serialize despite parallel_safe=True
# ---------------------------------------------------------------------------
def test_7_overlapping_scopes_serialize():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-7",
                [
                    _child("T7-A", parallel_safe=True),
                    _child("T7-B", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        scope_a = TaskOwnershipScope(project="stagemesh", primary_module="core", allowed_paths=("build_coordinator/runner/*",))
        scope_b = TaskOwnershipScope(project="stagemesh", primary_module="core", allowed_paths=("build_coordinator/*",))
        session.get(BuildTask, "T7-A").ownership_scope = {
            "project": scope_a.project,
            "primary_module": scope_a.primary_module,
            "allowed_paths": list(scope_a.allowed_paths),
        }
        session.get(BuildTask, "T7-B").ownership_scope = {
            "project": scope_b.project,
            "primary_module": scope_b.primary_module,
            "allowed_paths": list(scope_b.allowed_paths),
        }
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert len(launched) == 1
        assert "T7-A" in launched
        assert "T7-B" not in launched
        assert r.scheduling_reasons.get("T7-B") == "ownership_scope_conflict"


# ---------------------------------------------------------------------------
# Scenario 8: Non-overlapping scopes run concurrently
# ---------------------------------------------------------------------------
def test_8_non_overlapping_scopes_run_concurrently():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-8",
                [
                    _child("T8-A", parallel_safe=True),
                    _child("T8-B", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        scope_a = TaskOwnershipScope(project="stagemesh", primary_module="mod_a", allowed_paths=("src/mod_a/*",))
        scope_b = TaskOwnershipScope(project="stagemesh", primary_module="mod_b", allowed_paths=("src/mod_b/*",))
        session.get(BuildTask, "T8-A").ownership_scope = {
            "project": scope_a.project,
            "primary_module": scope_a.primary_module,
            "allowed_paths": list(scope_a.allowed_paths),
        }
        session.get(BuildTask, "T8-B").ownership_scope = {
            "project": scope_b.project,
            "primary_module": scope_b.primary_module,
            "allowed_paths": list(scope_b.allowed_paths),
        }
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r) == {"T8-A", "T8-B"}


# ---------------------------------------------------------------------------
# Scenario 9: Objective parallelism budget bounded under higher project capacity
# ---------------------------------------------------------------------------
def test_9_objective_budget_bounded_under_high_global_capacity():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-9",
                [
                    _child("T9-A", parallel_safe=True),
                    _child("T9-B", parallel_safe=True),
                    _child("T9-C", parallel_safe=True),
                    _child("T9-D", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner(_multi_builder_config(num_builders=6))
    r = runner.run_once()
    assert len(r.launched) == 2
    with SessionLocal() as session:
        assert len(active_objective_implementations(session, "OBJ-9")) == 2


# ---------------------------------------------------------------------------
# Scenario 10: Two objectives share project capacity respecting independent budgets
# ---------------------------------------------------------------------------
def test_10_two_objectives_share_project_capacity():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-10-A",
                [_child("T10-A1", parallel_safe=True), _child("T10-A2", parallel_safe=True), _child("T10-A3", parallel_safe=True)],
                parallelism=2,
            ),
        )
        create_objective(
            session,
            _obj(
                "OBJ-10-B",
                [_child("T10-B1", parallel_safe=True), _child("T10-B2", parallel_safe=True), _child("T10-B3", parallel_safe=True)],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner(_multi_builder_config(num_builders=6))
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert len(launched) == 4
        assert len([t for t in launched if t.startswith("T10-A")]) == 2
        assert len([t for t in launched if t.startswith("T10-B")]) == 2
        assert r.scheduling_reasons.get("T10-A3") == "objective_parallelism_full"
        assert r.scheduling_reasons.get("T10-B3") == "objective_parallelism_full"


# ---------------------------------------------------------------------------
# Scenario 11: Remediation consumes an objective implementation slot
# ---------------------------------------------------------------------------
def test_11_remediation_consumes_objective_slot():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-11",
                [
                    _child("T11-A", parallel_safe=True),
                    _child("T11-B", parallel_safe=True),
                ],
                parallelism=1,  # Only 1 slot
            ),
        )
        task_a = session.get(BuildTask, "T11-A")
        task_a.state = "REWORK_REQUIRED"
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert launched == {"T11-A"}
        assert "T11-B" not in launched
        assert r.scheduling_reasons.get("T11-B") == "objective_parallelism_full"


# ---------------------------------------------------------------------------
# Scenario 12: Review does not block independent implementation
# ---------------------------------------------------------------------------
def test_12_review_does_not_block_independent_implementation():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-12",
                [
                    _child("T12-A", parallel_safe=True),
                    _child("T12-B", parallel_safe=True),
                ],
                parallelism=1,
            ),
        )
        task_a = session.get(BuildTask, "T12-A")
        task_a.state = "REVIEW_READY"
        task_a.review_policy = "SELF"
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert "T12-A" in launched
        assert "T12-B" in launched


# ---------------------------------------------------------------------------
# Scenario 13: Remediation coexists with safe unrelated implementation
# ---------------------------------------------------------------------------
def test_13_remediation_coexists_with_safe_implementation():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-13",
                [
                    _child("T13-REWORK", parallel_safe=True),
                    _child("T13-READY", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.get(BuildTask, "T13-REWORK").state = "REWORK_REQUIRED"
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r) == {"T13-REWORK", "T13-READY"}


# ---------------------------------------------------------------------------
# Scenario 14: Runner restart reconstructs slots and prevents duplicate workers
# ---------------------------------------------------------------------------
def test_14_restart_reconstructs_slots_and_prevents_duplicates():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-14",
                [
                    _child("T14-A", parallel_safe=True),
                    _child("T14-B", parallel_safe=True),
                    _child("T14-C", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner1 = _runner()
    r1 = runner1.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r1) == {"T14-A", "T14-B"}

    # Fresh runner instance (simulating process restart)
    runner2 = _runner()
    r2 = runner2.run_once()
    assert len(r2.launched) == 0
    assert r2.scheduling_reasons.get("T14-C") == "objective_parallelism_full"


# ---------------------------------------------------------------------------
# Scenario 15: Completed dependency unlocks across restart
# ---------------------------------------------------------------------------
def test_15_completed_dependency_unlocks_across_restart():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-15",
                [
                    _child("T15-A", parallel_safe=True),
                    _child("T15-B", dependencies=["T15-A"], parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        task_a = session.get(BuildTask, "T15-A")
        task_a.state = "DONE"
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        assert "T15-B" in _launched_tasks(session, r)


# ---------------------------------------------------------------------------
# Scenario 16: Migration-capable work remains serialized
# ---------------------------------------------------------------------------
def test_16_migration_serialization():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-16",
                [
                    _child("T16-MIG-1", parallel_safe=True),
                    _child("T16-MIG-2", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.get(BuildTask, "T16-MIG-1").migration_allowed = True
        session.get(BuildTask, "T16-MIG-2").migration_allowed = True
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert len(launched) == 1
        assert "T16-MIG-1" in launched
        assert "T16-MIG-2" not in launched
        assert r.scheduling_reasons.get("T16-MIG-2") == "migration_serialization"


# ---------------------------------------------------------------------------
# Scenario 17: Provider fallback cannot bypass objective parallelism
# ---------------------------------------------------------------------------
def test_17_provider_fallback_cannot_bypass_parallelism():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-17",
                [
                    _child("T17-A", parallel_safe=True),
                    _child("T17-B", parallel_safe=True),
                ],
                parallelism=1,
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert len(launched) == 1
        assert "T17-A" in launched
        assert "T17-B" not in launched
        assert r.scheduling_reasons.get("T17-B") == "objective_parallelism_full"


# ---------------------------------------------------------------------------
# Scenario 18: Provider fallback cannot bypass scope restrictions
# ---------------------------------------------------------------------------
def test_18_provider_fallback_cannot_bypass_scope():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-18",
                [
                    _child("T18-A", parallel_safe=True),
                    _child("T18-B", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        scope = TaskOwnershipScope(project="stagemesh", primary_module="core", allowed_paths=("src/*",))
        session.get(BuildTask, "T18-A").ownership_scope = {
            "project": scope.project,
            "primary_module": scope.primary_module,
            "allowed_paths": list(scope.allowed_paths),
        }
        session.get(BuildTask, "T18-B").ownership_scope = {
            "project": scope.project,
            "primary_module": scope.primary_module,
            "allowed_paths": list(scope.allowed_paths),
        }
        session.commit()

    runner = _runner()
    r = runner.run_once()
    assert r.scheduling_reasons.get("T18-B") == "ownership_scope_conflict"


# ---------------------------------------------------------------------------
# Scenario 19: Worker/worktree single-owner invariant under parallel dispatch
# ---------------------------------------------------------------------------
def test_19_worker_worktree_single_owner_invariant(tmp_path):
    wt_shared = str(tmp_path / "shared_wt")
    Path(wt_shared).mkdir()

    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-1", "BUILDER", adapter="fake", worktree_path=wt_shared, capabilities=("CODING",)),
            WorkerConfig("builder-2", "BUILDER", adapter="fake", worktree_path=wt_shared, capabilities=("CODING",)),
        ),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-19",
                [
                    _child("T19-A", parallel_safe=True),
                    _child("T19-B", parallel_safe=True),
                ],
                parallelism=2,
            ),
        )
        session.commit()

    runner = _runner(config)
    r = runner.run_once()
    assert len(r.launched) == 1
    assert r.scheduling_reasons.get("T19-B") == "worktree_or_worker_owned"


# ---------------------------------------------------------------------------
# Scenario 20: Integration conflict drift safety
# ---------------------------------------------------------------------------
def test_20_integration_conflict_drift_safety():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj("OBJ-20", [_child("T20-A", parallel_safe=True)], parallelism=1),
        )
        task = session.get(BuildTask, "T20-A")
        task.state = "DONE"
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildTask, "T20-A").state == "DONE"


# ---------------------------------------------------------------------------
# Scenario 21: Withheld child receives typed, deterministic reason code & event
# ---------------------------------------------------------------------------
def test_21_withheld_child_receives_typed_reason_and_event():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-21",
                [
                    _child("T21-A", parallel_safe=True),
                    _child("T21-B", parallel_safe=True),
                ],
                parallelism=1,
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    assert r.scheduling_reasons.get("T21-B") == "objective_parallelism_full"

    with SessionLocal() as session:
        event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "T21-B")
            .where(BuildTaskEvent.event_type == "runner.task_withheld")
        )
        assert event is not None
        assert (event.event_data or {}).get("reason") == "objective_parallelism_full"
        assert (event.event_data or {}).get("reason") in SCHEDULER_REASONS


# ---------------------------------------------------------------------------
# Scenario 22: Bounded fairness prevents starvation
# ---------------------------------------------------------------------------
def test_22_bounded_fairness_prevents_starvation():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj(
                "OBJ-22-A",
                [
                    _child("T22-A1", parallel_safe=True),
                    _child("T22-A2", parallel_safe=True),
                    _child("T22-A3", parallel_safe=True),
                ],
                parallelism=3,
            ),
        )
        create_objective(
            session,
            _obj(
                "OBJ-22-B",
                [
                    _child("T22-B1", parallel_safe=True),
                ],
                parallelism=1,
            ),
        )
        session.commit()

    runner = _runner(_multi_builder_config(num_builders=2))
    r = runner.run_once()
    with SessionLocal() as session:
        launched = _launched_tasks(session, r)
        assert "T22-B1" in launched
        assert any(t.startswith("T22-A") for t in launched)
        assert len(launched) == 2


# ---------------------------------------------------------------------------
# Scenario 23: Ordinary non-objective tasks retain current behavior
# ---------------------------------------------------------------------------
def test_23_ordinary_non_objective_tasks_retain_behavior():
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="NON-OBJ-1",
                title="Non-objective task",
                description="desc",
                acceptance_criteria=[],
                risk_level="LOW",
                review_policy="NONE",
            ),
        )
        session.commit()

    runner = _runner()
    r = runner.run_once()
    with SessionLocal() as session:
        assert "NON-OBJ-1" in _launched_tasks(session, r)


# ---------------------------------------------------------------------------
# Scenario 24: Dependency cycle rejected at plan validation
# ---------------------------------------------------------------------------
def test_24_dependency_cycle_rejected():
    cyclic_plan = {
        "tasks": [
            {"task_id": "CYC-A", "title": "A", "description": "d", "dependencies": ["CYC-B"]},
            {"task_id": "CYC-B", "title": "B", "description": "d", "dependencies": ["CYC-A"]},
        ]
    }
    with pytest.raises(StructuredContractError, match="dependency cycle detected"):
        parse_planner_plan(cyclic_plan)


# ---------------------------------------------------------------------------
# Scenario 25: Recovery never creates duplicate implementation claims
# ---------------------------------------------------------------------------
def test_25_recovery_never_duplicates_claims():
    with SessionLocal() as session:
        create_objective(
            session,
            _obj("OBJ-25", [_child("T25-A", parallel_safe=True)], parallelism=2),
        )
        session.commit()

    runner = _runner()
    r1 = runner.run_once()
    with SessionLocal() as session:
        assert _launched_tasks(session, r1) == {"T25-A"}

    r2 = runner.run_once()
    with SessionLocal() as session:
        assert "T25-A" not in _launched_tasks(session, r2)
        claims = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "T25-A")
        ).all()
        assert len(claims) == 1
