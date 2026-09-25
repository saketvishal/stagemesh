from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.events import record_event
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.runner import BuildRunner
import build_coordinator.runner.orchestrator as orchestrator_module
from build_coordinator.runner.git_safety import FakeGit, MechanicalMergeAssessment
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.routing import ProviderConfig
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.service import (
    ClaimRequest,
    TaskSpec,
    claim_task,
    recover_expired,
    recover_execution_retry_exhausted,
    set_mode,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.types import EventInput


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _task(task_id: str, *, review_policy="INDEPENDENT", migration_allowed=False):
    return TaskSpec(
        task_id=task_id,
        title=f"Task {task_id}",
        description="Runner test task",
        acceptance_criteria=["passes"],
        review_policy=review_policy,
        required_validation=["pytest tooling/build_coordinator/tests"],
        migration_allowed=migration_allowed,
    )


def _config(*, auto_push=False, remediation_cycles=2):
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("builder-b", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        max_remediation_cycles=remediation_cycles,
        auto_push_allowed=auto_push,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _config_with_conflict_budget(attempts: int):
    return RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        max_conflict_recovery_attempts=attempts,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )


def _runner(config=None, executors=None, git=None):
    return BuildRunner(
        SessionLocal,
        config or _config(),
        executors=executors,
        git=git if git is not None else FakeGit(),
    )


def _targeted_runner(task_id: str, config=None, executors=None, git=None):
    return BuildRunner(
        SessionLocal,
        config or _config(),
        executors=executors,
        git=git if git is not None else FakeGit(),
        target_task_ids={task_id},
    )


def _seed_priority(session, task_id: str, priority: int) -> None:
    record_event(
        session,
        EventInput(
            task_id=task_id,
            event_type="project.task_synced",
            actor="test",
            event_data={"revision": 1, "priority": priority, "action": "CREATED"},
        ),
    )


def test_merge_conflict_exhaustion_blocks_as_recovery_failed_with_evidence():
    runner = _runner(config=_config_with_conflict_budget(1))
    assessment = MechanicalMergeAssessment(
        current_main_sha="main-2",
        feature_remote_sha="feature-b1",
        merge_base="base-1",
        reviewed_sha_matches=True,
        conflict=True,
        conflict_paths=("src/app.py",),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("GH-78"))
        task = session.get(BuildTask, "GH-78")
        task.state = "REVIEWING"
        task.branch_name = "stagemesh/GH-78"
        task.waiting_input = {
            "conflict_recovery": {
                "attempts": 1,
                "max_attempts": 1,
                "recovery_worker": "builder-a",
                "recovery_provider": "local",
            }
        }
        result = orchestrator_module.RunnerCycleResult(mode="RUNNING")

        runner._handle_merge_conflict(session, "GH-78", assessment, result)
        session.flush()

        task = session.get(BuildTask, "GH-78")
        evidence = task.waiting_input["failure_evidence"]
        assert task.state == "BLOCKED"
        assert result.escalations == ["GH-78:MERGE_CONFLICT_RECOVERY_FAILED"]
        assert evidence["underlying_invariant"] == "MERGE_CONFLICT_RECOVERY_FAILED"
        assert evidence["original_reviewed_sha"] == "feature-b1"
        assert evidence["conflicting_current_main_sha"] == "main-2"
        assert evidence["conflict_paths"] == ["src/app.py"]
        assert evidence["max_attempts"] == 1
        assert evidence["conflict_recovery_worker"] == "builder-a"
        assert evidence["conflict_recovery_provider"] == "local"


def test_stale_review_for_pre_recovery_sha_is_not_accepted_for_integration():
    git = FakeGit(remote_feature_sha="feature-b2", main_sha="main-2")
    runner = _runner(config=_config(), git=git)
    with SessionLocal() as session:
        upsert_task(session, TaskSpec(**{**_task("GH-78").__dict__, "required_validation": []}))
        task = session.get(BuildTask, "GH-78")
        task.state = "REVIEWING"
        task.branch_name = "stagemesh/GH-78"
        session.add(
            BuildRunnerExecution(
                execution_id="review-b1",
                task_id="GH-78",
                role="REVIEWER",
                worker_id="reviewer-1",
                provider="local",
                adapter="fake",
                branch_name="stagemesh/GH-78",
                reviewed_feature_sha="feature-b1",
                status="SUCCEEDED",
                result_data={
                    "reviewed_feature_sha": "feature-b1",
                    "review": {
                        "verdict": "GREEN",
                        "ready_for_integration": True,
                        "required_remediation": [],
                    },
                },
            )
        )
        session.flush()
        result = orchestrator_module.RunnerCycleResult(mode="RUNNING")

        runner._dispatch_integration(session, result)
        session.flush()

        task = session.get(BuildTask, "GH-78")
        integrations = session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "INTEGRATION")
        ).all()
        assert integrations == []
        assert task.state == "REVIEW_READY"


def test_ready_task_dispatches_one_builder():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-READY"))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-READY")
        execution = session.scalar(select(BuildRunnerExecution))
        assert task.state == "CLAIMED"
        assert execution.role == "BUILDER"
        assert execution.prompt_hash


def test_targeted_ready_task_runs_and_unrelated_ready_task_is_untouched():
    with SessionLocal() as session:
        upsert_task(session, _task("TARGET"))
        upsert_task(session, _task("OTHER"))
        session.commit()

    result = _targeted_runner("TARGET").run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        assert session.get(BuildTask, "TARGET").state == "CLAIMED"
        assert session.get(BuildTask, "OTHER").state == "READY"
        assert session.scalar(select(BuildRunnerExecution)).task_id == "TARGET"


def test_targeted_run_ignores_higher_priority_unrelated_task():
    with SessionLocal() as session:
        upsert_task(session, _task("P0-OTHER"))
        upsert_task(session, _task("TARGET"))
        _seed_priority(session, "P0-OTHER", 0)
        _seed_priority(session, "TARGET", 100)
        session.commit()

    result = _targeted_runner("TARGET").run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        assert session.scalar(select(BuildRunnerExecution)).task_id == "TARGET"
        assert session.get(BuildTask, "P0-OTHER").state == "READY"


def test_targeted_missing_task_claims_nothing():
    with SessionLocal() as session:
        upsert_task(session, _task("OTHER"))
        session.commit()

    result = _targeted_runner("MISSING").run_once()

    assert result.launched == []
    with SessionLocal() as session:
        assert session.get(BuildTask, "OTHER").state == "READY"
        assert session.scalar(select(BuildRunnerExecution)) is None


def test_targeted_blocked_task_claims_nothing_and_preserves_dependency_check():
    with SessionLocal() as session:
        upsert_task(session, _task("DEP"))
        upsert_task(session, TaskSpec("TARGET", "Target", "d", ["ok"], dependencies=["DEP"]))
        upsert_task(session, _task("OTHER"))
        session.commit()

    result = _targeted_runner("TARGET").run_once()

    assert result.launched == []
    with SessionLocal() as session:
        assert session.get(BuildTask, "TARGET").state == "READY"
        assert session.get(BuildTask, "OTHER").state == "READY"
        assert session.scalar(select(BuildRunnerExecution)) is None


def test_targeted_unavailable_provider_fails_safely_without_general_backlog_fallback():
    config = RunnerConfig(
        workers=(WorkerConfig("builder-a", "BUILDER", adapter="fake", provider="down"),),
        providers={"down": ProviderConfig("down", availability="QUOTA_EXHAUSTED", consumption_mode="FALLBACK")},
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("TARGET"))
        upsert_task(session, _task("OTHER"))
        session.commit()

    result = _targeted_runner("TARGET", config=config).run_once()

    assert result.launched == []
    assert result.capacity_full is True
    with SessionLocal() as session:
        assert session.get(BuildTask, "TARGET").state == "READY"
        assert session.get(BuildTask, "OTHER").state == "READY"
        assert session.scalar(select(BuildRunnerExecution)) is None


def test_targeted_task_keeps_normal_review_and_integration_lifecycle():
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake"),
        ),
        auto_push_allowed=True,
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    executors = {
        "builder-a": FakeExecutor([
            ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "feature-sha"})
        ]),
        "reviewer-1": FakeExecutor([
            ExecutionObservation(
                "SUCCEEDED",
                result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
            )
        ]),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        upsert_task(
            session,
            TaskSpec(
                task_id="TARGET",
                title="Target",
                description="d",
                acceptance_criteria=["ok"],
                review_policy="INDEPENDENT",
            ),
        )
        upsert_task(session, _task("OTHER"))
        session.commit()

    runner = _targeted_runner("TARGET", config=config, executors=executors, git=FakeGit())
    for _ in range(6):
        runner.run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "TARGET").state == "DONE"
        assert session.get(BuildTask, "OTHER").state == "READY"
        roles = [
            row.role
            for row in session.scalars(
                select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "TARGET")
            )
        ]
        assert roles == ["BUILDER", "REVIEWER", "INTEGRATION"]
        assert session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "OTHER")
        ).all() == []


def test_runner_reload_keeps_live_executor_and_replaces_it_when_idle():
    old_executor = FakeExecutor()
    runner = _runner(
        config=RunnerConfig(
            workers=(WorkerConfig("builder-a", "BUILDER", adapter="fake"),),
            result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
        ),
        executors={"builder-a": old_executor},
    )
    updated = RunnerConfig(
        workers=(WorkerConfig("builder-a", "BUILDER", adapter="subprocess", command=("new-worker",)),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )

    runner.reload_config(updated, live_worker_ids={"builder-a"})
    assert runner._executors["builder-a"] is old_executor

    runner.reload_config(updated, live_worker_ids=set())
    assert "builder-a" not in runner._executors


def test_worker_saturation_is_backpressure_not_configuration_escalation():
    with SessionLocal() as session:
        for task_id in ("SAT-1", "SAT-2", "SAT-3", "SAT-4", "SAT-5"):
            upsert_task(session, _task(task_id))
        session.commit()

    config = RunnerConfig(
        workers=(WorkerConfig("builder-only", "BUILDER", adapter="fake"),),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    result = _runner(config=config).run_once()
    assert len(result.launched) == 1
    assert result.capacity_full is True
    assert not any("EXTERNAL_EXECUTOR_CONFIGURATION_REQUIRED" in item for item in result.escalations)
    with SessionLocal() as session:
        states = {row.task_id: row.state for row in session.scalars(select(BuildTask))}
        assert list(states.values()).count("CLAIMED") == 1
        assert list(states.values()).count("READY") == 4


def test_two_ready_tasks_dispatch_to_capacity_and_third_waits():
    with SessionLocal() as session:
        for task_id in ("RUN-A", "RUN-B", "RUN-C"):
            upsert_task(session, _task(task_id))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 2
    assert result.capacity_full is True
    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-C").state == "READY"


def test_migration_lane_rule_remains_coordinator_enforced():
    with SessionLocal() as session:
        upsert_task(session, _task("MIG-A", migration_allowed=True))
        upsert_task(session, _task("MIG-B", migration_allowed=True))
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        states = {task.task_id: task.state for task in session.scalars(select(BuildTask))}
        assert sorted(states.values()) == ["CLAIMED", "READY"]


def test_paused_and_draining_launch_no_new_builder():
    for mode in ("PAUSED", "DRAINING"):
        with SessionLocal() as session:
            upsert_task(session, _task(f"RUN-{mode}"))
            set_mode(session, mode)
            session.commit()

        result = _runner().run_once()

        assert result.launched == []
        with SessionLocal() as session:
            set_mode(session, "RUNNING")
            session.commit()


def test_review_ready_dispatches_independent_reviewer_not_builder():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-REVIEW"))
        claim_task(session, ClaimRequest("RUN-REVIEW", worker_id="builder-a"))
        transition_task(session, "RUN-REVIEW", "IN_PROGRESS")
        transition_task(session, "RUN-REVIEW", "VALIDATING")
        transition_task(session, "RUN-REVIEW", "REVIEW_READY")
        session.commit()

    result = _runner().run_once()

    assert len(result.launched) == 1
    with SessionLocal() as session:
        execution = session.scalar(select(BuildRunnerExecution))
        assert execution.role == "REVIEWER"
        assert execution.worker_id == "reviewer-1"


def test_green_review_dispatches_integration_and_push_policy_blocks_resumably():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "feature_sha": "abc123",
                        "review": {"verdict": "GREEN", "ready_for_integration": True},
                    },
                )
            ]
        ),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-GREEN"))
        claim_task(session, ClaimRequest("RUN-GREEN", worker_id="builder-a"))
        transition_task(session, "RUN-GREEN", "IN_PROGRESS")
        transition_task(session, "RUN-GREEN", "VALIDATING")
        transition_task(session, "RUN-GREEN", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()
    result = _runner(executors=executors).run_once()

    assert "RUN-GREEN:REMOTE_PUSH_APPROVAL_REQUIRED" in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-GREEN").state == "BLOCKED"


def test_green_with_notes_without_remediation_is_integration_eligible():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN_WITH_NOTES",
                            "findings": ["minor note"],
                            "required_remediation": [],
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        )
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-NOTES"))
        claim_task(session, ClaimRequest("RUN-NOTES", worker_id="builder-a"))
        transition_task(session, "RUN-NOTES", "IN_PROGRESS")
        transition_task(session, "RUN-NOTES", "VALIDATING")
        transition_task(session, "RUN-NOTES", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        execution = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "INTEGRATION")
        )
        assert execution is not None
        assert execution.result_data is not None


def test_integration_resume_context_failure_preserves_policy_error(monkeypatch):
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "GREEN",
                            "ready_for_integration": True,
                        }
                    },
                )
            ]
        )
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-CONTEXT-FAIL"))
        claim_task(session, ClaimRequest("RUN-CONTEXT-FAIL", worker_id="builder-a"))
        transition_task(session, "RUN-CONTEXT-FAIL", "IN_PROGRESS")
        transition_task(session, "RUN-CONTEXT-FAIL", "VALIDATING")
        transition_task(session, "RUN-CONTEXT-FAIL", "REVIEW_READY")
        session.commit()

    runner = _runner(executors=executors)
    runner.run_once()

    def fail_resume_context(session, task_id):
        raise CoordinatorPolicyError("resume context unavailable")

    monkeypatch.setattr(orchestrator_module, "get_resume_context", fail_resume_context)
    with pytest.raises(CoordinatorPolicyError, match="resume context unavailable"):
        runner.run_once()


def test_remediation_required_routes_to_rework_and_findings_are_checkpointed():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["fix test gap"],
                            "required_remediation": ["add regression test"],
                        }
                    },
                )
            ]
        )
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-REWORK"))
        claim_task(session, ClaimRequest("RUN-REWORK", worker_id="builder-a"))
        transition_task(session, "RUN-REWORK", "IN_PROGRESS")
        transition_task(session, "RUN-REWORK", "VALIDATING")
        transition_task(session, "RUN-REWORK", "REVIEW_READY")
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-REWORK").state == "CLAIMED"
        checkpoint = session.scalar(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "RUN-REWORK")
        )
        assert checkpoint.remaining_work == ["add regression test"]
        remediation = session.scalar(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role == "REMEDIATION")
        )
        assert remediation is not None


def test_rework_loop_count_is_bounded():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LIMIT"))
        session.add(
            BuildRunnerExecution(
                execution_id="old-remediation",
                task_id="RUN-LIMIT",
                role="REMEDIATION",
                worker_id="builder-a",
                adapter="fake",
                status="SUCCEEDED",
            )
        )
        claim_task(session, ClaimRequest("RUN-LIMIT", worker_id="builder-a"))
        transition_task(session, "RUN-LIMIT", "IN_PROGRESS")
        transition_task(session, "RUN-LIMIT", "VALIDATING")
        transition_task(session, "RUN-LIMIT", "REVIEW_READY")
        session.commit()

    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "REMEDIATION_REQUIRED"}},
                )
            ]
        )
    }
    _runner(config=_config(remediation_cycles=1), executors=executors).run_once()
    result = _runner(config=_config(remediation_cycles=1), executors=executors).run_once()

    assert "RUN-LIMIT:REMEDIATION_LIMIT_REACHED" in result.escalations


def test_same_finding_repeated_verbatim_still_converges_to_escalation():
    """A reviewer restating the exact same unresolved defect across cycles
    still escalates once its own attempt budget is exhausted (finding-aware
    convergence must not regress the case raw-cycle counting already
    handled correctly)."""
    task_id = "RUN-SAME-FINDING"
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["same defect"],
                            "required_remediation": ["fix it"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["same defect"],
                            "required_remediation": ["fix it"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["same defect"],
                            "required_remediation": ["fix it"],
                        }
                    },
                ),
            ]
        ),
        "builder-a": FakeExecutor(
            [
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-1"}),
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-2"}),
            ]
        ),
    }
    with SessionLocal() as session:
        upsert_task(session, TaskSpec(**{**_task(task_id).__dict__, "required_validation": []}))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    runner = _runner(config=_config(remediation_cycles=2), executors=executors)
    runner.run_once()  # launch review 1
    runner.run_once()  # review 1 -> REWORK_REQUIRED -> launch remediation 1
    runner.run_once()  # remediation 1 -> VALIDATING -> REVIEW_READY -> launch review 2
    result = runner.run_once()  # review 2 -> REWORK_REQUIRED again -> launch remediation 2
    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" not in result.escalations
    runner.run_once()  # remediation 2 -> REVIEW_READY -> launch review 3
    result = runner.run_once()  # review 3 -> escalate

    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "BLOCKED"
        event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.remediation_limit_reached")
        )
        assert event is not None
        open_findings = event.event_data["open_findings"]
        assert len(open_findings) == 1
        assert open_findings[0]["description"] == "same defect"
        assert open_findings[0]["attempts"] == 3


def test_new_finding_after_resolution_gets_its_own_budget_not_raw_count():
    """A finding that is fixed (and so drops out of the reviewer's findings)
    must not consume the remediation budget of an unrelated finding
    introduced afterward: convergence must be driven by which findings are
    actually still open, not by how many remediation cycles have run in
    total."""
    task_id = "RUN-FRESH-FINDING"
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["defect A"],
                            "required_remediation": ["fix A"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["defect B"],
                            "required_remediation": ["fix B"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["defect B"],
                            "required_remediation": ["fix B"],
                        }
                    },
                ),
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["defect B"],
                            "required_remediation": ["fix B"],
                        }
                    },
                ),
            ]
        ),
        "builder-a": FakeExecutor(
            [
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-1"}),
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-2"}),
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-3"}),
            ]
        ),
    }
    with SessionLocal() as session:
        upsert_task(session, TaskSpec(**{**_task(task_id).__dict__, "required_validation": []}))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    runner = _runner(config=_config(remediation_cycles=2), executors=executors)
    runner.run_once()  # launch review 1
    runner.run_once()  # review 1 (defect A) -> REWORK_REQUIRED -> launch remediation 1
    runner.run_once()  # remediation 1 -> REVIEW_READY -> launch review 2
    result = runner.run_once()  # review 2 (defect B, A resolved) -> must NOT escalate yet

    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" not in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "CLAIMED"
        # raw remediation cycle count already equals the cap here, but the
        # task keeps going because "defect B" has only been reported once
        assert runner._remediation_cycles(session, task_id) >= 2

    runner.run_once()  # remediation 2 -> REVIEW_READY -> launch review 3
    result = runner.run_once()  # review 3 (defect B again) -> still within its own budget
    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" not in result.escalations
    runner.run_once()  # remediation 3 -> REVIEW_READY -> launch review 4
    result = runner.run_once()  # review 4 (defect B again) -> escalate

    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "BLOCKED"
        event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == "runner.remediation_limit_reached")
        )
        assert event is not None
        open_findings = event.event_data["open_findings"]
        assert len(open_findings) == 1
        assert open_findings[0]["description"] == "defect B"


def test_findings_omitted_after_being_tracked_still_hits_the_raw_cap():
    """A reviewer that stops restating findings (but keeps returning
    REMEDIATION_REQUIRED with an empty `findings` array) must not be able to
    stall convergence forever: since there is no finding signal to reconcile,
    the raw remediation-cycle cap remains the safety net for those cycles."""
    task_id = "RUN-OMITTED-FINDINGS"
    empty_findings_review = ExecutionObservation(
        "SUCCEEDED",
        result_data={
            "review": {
                "verdict": "REMEDIATION_REQUIRED",
                "findings": [],
                "required_remediation": ["still broken, unspecified"],
            }
        },
    )
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={
                        "review": {
                            "verdict": "REMEDIATION_REQUIRED",
                            "findings": ["defect A"],
                            "required_remediation": ["fix A"],
                        }
                    },
                ),
                empty_findings_review,
                empty_findings_review,
            ]
        ),
        "builder-a": FakeExecutor(
            [
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-1"}),
                ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "remediated-sha-2"}),
            ]
        ),
    }
    with SessionLocal() as session:
        upsert_task(session, TaskSpec(**{**_task(task_id).__dict__, "required_validation": []}))
        claim_task(session, ClaimRequest(task_id, worker_id="builder-a"))
        transition_task(session, task_id, "IN_PROGRESS")
        transition_task(session, task_id, "VALIDATING")
        transition_task(session, task_id, "REVIEW_READY")
        session.commit()

    runner = _runner(config=_config(remediation_cycles=2), executors=executors)
    runner.run_once()  # launch review 1
    runner.run_once()  # review 1 (defect A) -> REWORK_REQUIRED -> launch remediation 1
    runner.run_once()  # remediation 1 -> REVIEW_READY -> launch review 2
    result = runner.run_once()  # review 2 (no findings restated; raw count 1 < cap 2)

    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" not in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "CLAIMED"

    runner.run_once()  # remediation 2 -> REVIEW_READY -> launch review 3
    result = runner.run_once()  # review 3 (no findings restated; raw count 2 >= cap 2)

    assert f"{task_id}:REMEDIATION_LIMIT_REACHED" in result.escalations
    with SessionLocal() as session:
        assert session.get(BuildTask, task_id).state == "BLOCKED"


def test_stale_builder_can_be_recovered_and_resumed_without_duplicate_launch():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-STALE"))
        claim = claim_task(session, ClaimRequest("RUN-STALE", worker_id="builder-a"))
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    result = _runner().run_once()

    assert "RUN-STALE" in result.recovered
    with SessionLocal() as session:
        recover_expired(session)
        task = session.get(BuildTask, "RUN-STALE")
        assert task.state in {"STALE", "CLAIMED"}


def test_failed_process_does_not_mark_task_done():
    executors = {"builder-a": FakeExecutor([ExecutionObservation("FAILED", exit_code=2)])}
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-FAIL"))
        session.commit()

    _runner(executors=executors).run_once()
    _runner(executors=executors).run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-FAIL").state == "CLAIMED"


def test_retryable_failure_relaunches_after_backoff_then_escalates_when_exhausted(monkeypatch):
    """RATE_LIMITED is retryable per the routing taxonomy (SM-002): each failure
    is retried with no human gate, the backoff grows and is bounded per attempt,
    every attempt is recorded on its own execution row with a checkpoint, and
    once max_execution_attempts is exhausted the task escalates with a typed
    reason instead of looping forever."""
    clock = {"now": utcnow()}
    monkeypatch.setattr(orchestrator_module, "_now", lambda: clock["now"])

    def rate_limited():
        return ExecutionObservation(
            "FAILED",
            exit_code=1,
            result_data={"provider_failure": "RATE_LIMITED", "schema_version": 1},
        )

    executors = {"builder-a": FakeExecutor([rate_limited(), rate_limited(), rate_limited()])}
    config = _config()
    assert config.max_execution_attempts == 3
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-RETRY"))
        session.commit()

    runner = _runner(config, executors=executors)
    backoffs = []
    for expected_attempt in (1, 2, 3):
        runner.run_once()  # launches a fresh attempt once eligible again
        runner.run_once()  # observes the RATE_LIMITED failure
        with SessionLocal() as session:
            executions = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == "RUN-RETRY")
                .order_by(BuildRunnerExecution.launched_at)
            ).all()
            assert len(executions) == expected_attempt, "each attempt is its own execution row"
            latest = executions[-1]
            assert latest.status == "LOST"
            assert latest.result_data["retry_attempt"] == expected_attempt
            assert latest.result_data["retryable_failure"] is True
            checkpoint_row = session.scalars(
                select(BuildTaskCheckpoint)
                .where(BuildTaskCheckpoint.task_id == "RUN-RETRY")
                .order_by(BuildTaskCheckpoint.created_at)
            ).all()[-1]
            assert f"attempt {expected_attempt}" in checkpoint_row.known_failures[0]
            task = session.get(BuildTask, "RUN-RETRY")
            if expected_attempt < 3:
                assert task.state != "BLOCKED", "not exhausted yet: no human gate"
            backoffs.append(latest.result_data["retry_backoff_seconds"])
        clock["now"] = clock["now"] + timedelta(seconds=backoffs[-1] + 1)

    assert backoffs == [600, 1200, 2400], "backoff grows per attempt and stays bounded"
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-RETRY")
        assert task.state == "BLOCKED"
        blocked_event = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "RUN-RETRY")
            .where(BuildTaskEvent.event_type == "task.transitioned")
            .where(BuildTaskEvent.to_state == "BLOCKED")
        ).all()[-1]
        assert blocked_event.event_data["reason"] == "EXECUTION_RETRY_LIMIT_REACHED"


def test_operator_retry_recovery_opens_new_durable_generation(monkeypatch):
    clock = {"now": utcnow()}
    monkeypatch.setattr(orchestrator_module, "_now", lambda: clock["now"])

    def rate_limited():
        return ExecutionObservation(
            "FAILED",
            exit_code=1,
            result_data={"provider_failure": "RATE_LIMITED", "schema_version": 1},
        )

    executors = {"builder-a": FakeExecutor([rate_limited(), rate_limited(), rate_limited(), rate_limited()])}
    config = _config()
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-RETRY-GEN"))
        session.commit()

    runner = _runner(config, executors=executors)
    for _ in range(config.max_execution_attempts):
        runner.run_once()
        runner.run_once()
        with SessionLocal() as session:
            latest = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == "RUN-RETRY-GEN")
                .order_by(BuildRunnerExecution.launched_at.desc())
            ).first()
            clock["now"] = clock["now"] + timedelta(seconds=latest.result_data["retry_backoff_seconds"] + 1)

    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-RETRY-GEN")
        assert task.state == "BLOCKED"
        recovered = recover_execution_retry_exhausted(session, "RUN-RETRY-GEN", actor="operator")
        assert recovered.state == "RESUMABLE"
        assert recovered.retry_generation == 1
        session.commit()

    runner.run_once()
    runner.run_once()
    with SessionLocal() as session:
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "RUN-RETRY-GEN")
            .order_by(BuildRunnerExecution.launched_at)
        ).all()
        latest = executions[-1]
        assert latest.result_data["retry_generation"] == 1
        assert latest.result_data["retry_attempt"] == 1
        assert session.get(BuildTask, "RUN-RETRY-GEN").state != "BLOCKED"


def test_lost_execution_releases_active_claim_to_stale_with_checkpoint():
    executors = {"builder-a": FakeExecutor([ExecutionObservation("LOST")])}
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST"))
        session.commit()

    _runner(executors=executors).run_once()
    result = _runner(executors=executors).run_once()

    assert "RUN-LOST" in result.recovered
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST")
        claims = session.scalars(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST")).all()
        execution = session.scalar(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "RUN-LOST"))
        checkpoint_row = session.scalar(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "RUN-LOST")
        )
        assert task.state == "CLAIMED"
        assert [claim.status for claim in claims].count("EXPIRED") == 1
        assert [claim.status for claim in claims].count("ACTIVE") == 1
        assert task.current_claim_id in {claim.claim_id for claim in claims if claim.status == "ACTIVE"}
        assert execution.status == "LOST"
        assert checkpoint_row.current_step == "builder execution lost"


def test_lost_execution_without_checkpoint_is_idempotently_recoverable_after_restart():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST-RESTART"))
        claim = claim_task(session, ClaimRequest("RUN-LOST-RESTART", worker_id="builder-a"))
        session.add(
            BuildRunnerExecution(
                execution_id="lost-after-restart",
                task_id="RUN-LOST-RESTART",
                role="BUILDER",
                worker_id="builder-a",
                adapter="fake",
                claim_id=str(claim.claim_id),
                status="LOST",
                completed_at=utcnow(),
                result_data={"reconciliation_state": "LOST"},
            )
        )
        session.commit()

    first = _runner().run_once()
    second = _runner().run_once()

    assert first.recovered == ["RUN-LOST-RESTART"]
    assert second.recovered == []
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST-RESTART")
        claims = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST-RESTART")
        ).all()
        active_claims = [claim for claim in claims if claim.status == "ACTIVE"]
        assert task.state == "CLAIMED"
        assert len(active_claims) == 1
        assert len([claim for claim in claims if claim.status == "EXPIRED"]) == 1


def test_lost_execution_with_live_successor_does_not_reclaim_active_claim():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-LOST-LIVE"))
        claim = claim_task(session, ClaimRequest("RUN-LOST-LIVE", worker_id="builder-a"))
        session.add_all(
            [
                BuildRunnerExecution(
                    execution_id="lost-old",
                    task_id="RUN-LOST-LIVE",
                    role="BUILDER",
                    worker_id="builder-a",
                    adapter="fake",
                    claim_id=str(claim.claim_id),
                    status="LOST",
                    completed_at=utcnow(),
                ),
                BuildRunnerExecution(
                    execution_id="live-new",
                    task_id="RUN-LOST-LIVE",
                    role="BUILDER",
                    worker_id="builder-a",
                    adapter="fake",
                    claim_id=str(claim.claim_id),
                    status="RUNNING",
                ),
            ]
        )
        session.commit()

    result = _runner().run_once()

    assert result.recovered == []
    with SessionLocal() as session:
        task = session.get(BuildTask, "RUN-LOST-LIVE")
        claim = session.scalar(select(BuildTaskClaim).where(BuildTaskClaim.task_id == "RUN-LOST-LIVE"))
        assert task.state == "CLAIMED"
        assert claim.status == "ACTIVE"


def test_restart_does_not_duplicate_existing_claims_or_launches():
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-RESTART"))
        session.commit()

    _runner().run_once()
    result = _runner().run_once()

    assert result.launched == []
    with SessionLocal() as session:
        assert session.scalar(select(BuildRunnerExecution).where(BuildRunnerExecution.task_id == "RUN-RESTART"))


def test_auto_push_allowed_completes_after_integration():
    executors = {
        "reviewer-1": FakeExecutor(
            [
                ExecutionObservation(
                    "SUCCEEDED",
                    result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
                )
            ]
        ),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        upsert_task(session, _task("RUN-DONE"))
        claim_task(session, ClaimRequest("RUN-DONE", worker_id="builder-a"))
        transition_task(session, "RUN-DONE", "IN_PROGRESS")
        transition_task(session, "RUN-DONE", "VALIDATING")
        transition_task(session, "RUN-DONE", "REVIEW_READY")
        session.commit()

    runner = _runner(config=_config(auto_push=True), executors=executors)
    runner.run_once()
    runner.run_once()
    runner.run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "RUN-DONE").state == "DONE"
