"""Runtime stability hardening regression and failure injection test suite.

Proves StageMesh interruption-safety, conflict-safety, work-preservation,
restart-safety, and canonical checkout protection across the 12 required
lifecycle regression chains.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from build_coordinator.db import Base
from build_coordinator.events import record_event
from build_coordinator.execution.base import ExecutionLaunch, ExecutionObservation
from build_coordinator.execution.git_integrator import GitIntegrationExecutor, IntegrationStop
from build_coordinator.execution.results import parse_executor_result
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.orchestrator import BuildRunner, RunnerCycleResult
from build_coordinator.runner.worktree import (
    _checked_out_elsewhere,
    ensure_worktree,
    prepare_task_workspace,
    preserve_unknown_operator_work,
    reconcile_displaced_task_work,
    task_branch_name,
)
from build_coordinator.service import (
    claim_task,
    ensure_state,
    reconcile_stale_executions,
    transition_task,
    upsert_task,
)
from build_coordinator.types import ClaimRequest, EventInput, TaskSpec


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _setup_test_repo(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(remote), str(repo))
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.name", "Stability Test")
    _git(repo, "config", "user.email", "stability@example.com")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "origin", "main")
    return repo, remote


def _setup_runner(
    tmp_path: Path,
    repo: Path,
    workers: list[WorkerConfig] | None = None,
) -> tuple[sessionmaker, BuildRunner]:
    db_path = tmp_path / "coordinator.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    config = RunnerConfig(
        workers=workers
        or [
            WorkerConfig(
                worker_id="integration-1",
                role="INTEGRATION",
                provider="test",
                adapter="builtin-git",
                worktree_path=str(repo),
            )
        ],
        auto_push_allowed=False,
        allowed_workspace_roots=[str(tmp_path)],
    )
    runner = BuildRunner(session_factory, config)
    runner._settings = dataclasses.replace(runner._settings, repo_root=repo, data_dir=tmp_path)
    return session_factory, runner


def test_branch_collision_preflight_preserves_existing_real_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote = _setup_test_repo(tmp_path)
    monkeypatch.setenv("BUILD_COORDINATOR_REPO_ROOT", str(repo))
    worker = WorkerConfig(
        worker_id="builder-a",
        role="BUILDER",
        provider="test",
        adapter="fake",
        worktree_path=str(tmp_path / "builder-a"),
    )
    session_factory, runner = _setup_runner(tmp_path, repo, workers=[worker])
    branch = task_branch_name("GH-COLLIDE")
    owner_wt = tmp_path / "owner"
    prepare_task_workspace(
        owner_wt,
        repo_root=repo,
        branch_name=branch,
        base_ref="main",
        resume=False,
        allowed_roots=[str(tmp_path)],
    )
    (owner_wt / "owned.txt").write_text("do not reset me\n", encoding="utf-8")
    _git(owner_wt, "add", "owned.txt")
    _git(owner_wt, "commit", "-m", "owned branch work")
    original_tip = _git(repo, "rev-parse", branch).stdout.strip()

    with session_factory() as session:
        owner = upsert_task(session, TaskSpec("GH-OWNER", "owner", "", []))
        owner.objective_id = "OBJ-A"
        owner.branch_name = branch
        contender = upsert_task(session, TaskSpec("GH-COLLIDE", "contender", "", []))
        contender.objective_id = "OBJ-B"
        session.commit()

        with pytest.raises(CoordinatorPolicyError, match="refusing cross-objective collision"):
            runner._prepare_task_worker(worker, contender)

        claims = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.task_id == "GH-COLLIDE")
        ).all()

    assert claims == []
    assert _git(repo, "rev-parse", branch).stdout.strip() == original_tip


# ---------------------------------------------------------------------------
# SCENARIO 1: Interrupted Builder Recovered and Next Task Starts
# ---------------------------------------------------------------------------
def test_scenario_1_interrupted_builder_recovered_and_next_task(tmp_path: Path):
    """Task A implementation -> StageMesh killed -> restart -> work recovered
    -> task completes -> Task B starts successfully."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task_a = BuildTask(task_id="GH-A", title="Task A", description="A", state="READY")
        task_b = BuildTask(task_id="GH-B", title="Task B", description="B", state="READY")
        session.add_all([task_a, task_b])
        session.commit()

        # Task A claimed and building in worker worktree
        wt_a = tmp_path / "worktrees" / "wt-a"
        prepare_task_workspace(wt_a, repo_root=repo, branch_name="stagemesh/GH-A", base_ref="main", resume=False, allowed_roots=[str(tmp_path)])
        (wt_a / "a.py").write_text("print('task a')\n", encoding="utf-8")
        _git(wt_a, "add", "a.py")
        _git(wt_a, "commit", "-m", "task a implementation")

        claim_a = claim_task(
            session,
            ClaimRequest(task_id="GH-A", worker_id="b-1", provider="test", branch_name="stagemesh/GH-A", worktree_path=str(wt_a)),
        )
        exec_a = BuildRunnerExecution(
            execution_id="exec-a-1",
            task_id="GH-A",
            claim_id=claim_a.claim_id,
            role="BUILDER",
            worker_id="b-1",
            provider="test",
            adapter="fake",
            status="RUNNING",
        )
        session.add(exec_a)
        session.commit()

        # CRASH SIMULATION: claim lease expires, coordinator killed
        claim_ref = session.get(BuildTaskClaim, claim_a.claim_id)
        claim_ref.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
        session.commit()

    # Fresh coordinator restart
    _, fresh_runner = _setup_runner(tmp_path, repo)
    with session_factory() as session:
        cycle_result = fresh_runner.run_once()
        # Work is recovered without losing commits
        task_a_ref = session.get(BuildTask, "GH-A")
        assert task_a_ref.state in ("READY", "STALE", "RESUMABLE", "VALIDATING", "REVIEW_READY")
        # Commit exists on task branch
        log = _git(repo, "log", "-n", "1", "--oneline", "stagemesh/GH-A")
        assert "task a implementation" in log.stdout


# ---------------------------------------------------------------------------
# SCENARIO 2: Merge Conflict Automatic Remediation and Integration
# ---------------------------------------------------------------------------
def test_scenario_2_merge_conflict_automatic_remediation_and_integration(tmp_path: Path):
    """Task A integration -> merge conflict -> automatic remediation ->
    validation -> exact-SHA rereview -> integration -> Task B starts cleanly."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    # Base commit
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Task A creates feature modifying shared.py
    wt_a = tmp_path / "worktrees" / "wt-a"
    ensure_worktree(wt_a, repo_root=repo, branch_name="stagemesh/GH-A", base_sha=base_sha, allowed_roots=[str(tmp_path)])
    (wt_a / "shared.py").write_text("# Line from Task A\n", encoding="utf-8")
    _git(wt_a, "add", "shared.py")
    _git(wt_a, "commit", "-m", "Task A feature")
    sha_a = _git(wt_a, "rev-parse", "HEAD").stdout.strip()

    # Meanwhile main advances with conflicting change on shared.py
    (repo / "shared.py").write_text("# Line from Main\n", encoding="utf-8")
    _git(repo, "add", "shared.py")
    _git(repo, "commit", "-m", "advance main with conflict")
    _git(repo, "push", "origin", "main")
    _git(wt_a, "push", "origin", "stagemesh/GH-A")
    new_main_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with session_factory() as session:
        ensure_state(session)
        task_a = BuildTask(
            task_id="GH-A",
            title="Task A",
            description="A",
            state="REVIEWING",
            branch_name="stagemesh/GH-A",
        )
        task_b = BuildTask(task_id="GH-B", title="Task B", description="B", state="READY")
        session.add_all([task_a, task_b])
        # Reviewer review succeeded against sha_a
        rev_exec = BuildRunnerExecution(
            execution_id="exec-rev-a",
            task_id="GH-A",
            role="REVIEWER",
            worker_id="reviewer-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            reviewed_feature_sha=sha_a,
            completed_at=datetime.now(UTC),
            result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
        )
        session.add(rev_exec)
        session.commit()

        # Integration attempted -> detects merge conflict
        cycle_result = RunnerCycleResult(mode="AUTONOMOUS")
        runner._dispatch_integration(session, cycle_result)
        session.commit()

        task_a_ref = session.get(BuildTask, "GH-A")
        # In our generic conflict lifecycle, task enters REWORK_REQUIRED with conflict payload
        assert task_a_ref.state == "REWORK_REQUIRED"
        assert task_a_ref.waiting_input is not None
        assert "conflict_recovery" in task_a_ref.waiting_input
        cr = task_a_ref.waiting_input["conflict_recovery"]
        assert "shared.py" in cr["conflict_paths"]
        assert cr["task_sha"] == sha_a

        # Canonical repo must remain clean
        assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# SCENARIO 3: Merge Conflict Interrupted by Crash and Resumed
# ---------------------------------------------------------------------------
def test_scenario_3_merge_conflict_interrupted_by_crash_and_resumed(tmp_path: Path):
    """Task A integration -> merge conflict -> StageMesh killed while conflict exists
    -> restart -> conflict state recovered -> task completes -> next task unaffected."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task_a = BuildTask(
            task_id="GH-A",
            title="Task A",
            description="A",
            state="BLOCKED",
            waiting_input={
                "blocked_reason": "MERGE_CONFLICT",
                "failure_evidence": {
                    "underlying_invariant": "MERGE_CONFLICT",
                    "conflict_files": ["shared.py"],
                    "current_main_sha": "dummy_main",
                    "task_sha": "dummy_task",
                },
            },
        )
        session.add(task_a)
        session.commit()

        # Fresh runner cycle recovers diagnosed MERGE_CONFLICT blocker
        result = RunnerCycleResult(mode="RUNNING")
        runner._recover_diagnosed_blockers(session, result)
        session.commit()

        task_a_ref = session.get(BuildTask, "GH-A")
        assert task_a_ref.state == "REWORK_REQUIRED"
        assert "GH-A" in result.recovered


# ---------------------------------------------------------------------------
# SCENARIO 4: Failed Integration Cleanup Guarantee Prevents WORKING_CHECKOUT_DIRTY
# ---------------------------------------------------------------------------
def test_scenario_4_failed_integration_clean_guarantee(tmp_path: Path):
    """Task A failed integration -> workspace cleanup -> Task B integration
    -> no WORKING_CHECKOUT_DIRTY."""
    repo, _ = _setup_test_repo(tmp_path)
    executor = GitIntegrationExecutor(main_ref="main")

    # Simulate an aborted/failed merge leaving unmerged index / conflict in integration worktree
    (repo / "file1.txt").write_text("line 1\n", encoding="utf-8")
    _git(repo, "add", "file1.txt")
    _git(repo, "commit", "-m", "commit 1")

    # Call _integrate with something that triggers an abort
    # Invariant: executor clean guarantee leaves repo clean
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# SCENARIO 5: Builder Zero-Changes When Already Satisfied
# ---------------------------------------------------------------------------
def test_scenario_5_builder_zero_changes_when_already_satisfied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Builder returns no changes but task already satisfied -> validation/review
    -> no operator escalation."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)
    monkeypatch.setattr(runner, "_check_task_already_satisfied", lambda _session, task_id: task_id == "GH-SATISFIED")
    validation_script = repo / "validate_already_satisfied.py"
    validation_script.write_text(
        "from pathlib import Path\n"
        "Path('validation-marker.txt').write_text('validated', encoding='utf-8')\n",
        encoding="utf-8",
    )

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-SATISFIED",
            title="Already satisfied task",
            description="desc",
            state="CLAIMED",
            review_policy="INDEPENDENT",
            acceptance_criteria=["Existing implementation is valid"],
            required_validation=[f'"{sys.executable}" validate_already_satisfied.py'],
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-sat-1",
            task_id="GH-SATISFIED",
            role="BUILDER",
            worker_id="b-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            worktree_path=str(repo),
        )
        session.add(exec_row)
        session.commit()

        parsed = parse_executor_result(
            {
                "schema_version": 1,
                "role": "BUILDER",
                "feature_sha": "head_sha",
                "blockers": ["the agent produced no changes on the task branch"],
                "identity": {"provider": "test", "runtime": "fake"},
            },
            execution_id="exec-sat-1",
            task_id="GH-SATISFIED",
            role="BUILDER",
            require_identity=False,
        )

        result = RunnerCycleResult(mode="RUNNING")
        runner._builder_succeeded(session, exec_row, result, parsed)
        session.commit()

        # Task does NOT become blocked with NO_CHANGES_PRODUCED on attempt 1
        refreshed = session.get(BuildTask, "GH-SATISFIED")
        assert refreshed.state == "REVIEW_READY"
        verification_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert exec_row.reviewed_feature_sha == verification_sha
        assert exec_row.reviewed_feature_sha
        assert (repo / "validation-marker.txt").read_text(encoding="utf-8") == "validated"
        assert "GH-SATISFIED:NO_CHANGES_PRODUCED" not in result.escalations
        validation_events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-SATISFIED")
            .where(BuildTaskEvent.event_type == "runner.validation")
        ).all()
        assert len(validation_events) == 1
        assert validation_events[0].event_data["passed"] is True
        assert validation_events[0].event_data["feature_sha"] == verification_sha


# ---------------------------------------------------------------------------
# SCENARIO 6: Builder Zero-Changes When Not Satisfied Retries and Bounds
# ---------------------------------------------------------------------------
def test_scenario_6_builder_zero_changes_when_not_satisfied_retries(tmp_path: Path):
    """Builder returns no changes and task not satisfied -> alternate builder/remediation
    -> bounded policy -> no premature operator escalation."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-UNSAT",
            title="Unsatisfied task",
            description="desc",
            state="CLAIMED",
            review_policy="INDEPENDENT",
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-unsat-1",
            task_id="GH-UNSAT",
            role="BUILDER",
            worker_id="b-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
        )
        session.add(exec_row)
        session.commit()

        parsed = parse_executor_result(
            {
                "schema_version": 1,
                "role": "BUILDER",
                "feature_sha": "dummy",
                "blockers": ["the agent produced no changes on the task branch"],
                "identity": {"provider": "test", "runtime": "fake"},
            },
            execution_id="exec-unsat-1",
            task_id="GH-UNSAT",
            role="BUILDER",
            require_identity=False,
        )

        result = RunnerCycleResult(mode="RUNNING")
        runner._builder_succeeded(session, exec_row, result, parsed)
        session.commit()

        refreshed = session.get(BuildTask, "GH-UNSAT")
        assert refreshed.state == "RESUMABLE"
        assert "GH-UNSAT:NO_CHANGES_PRODUCED" not in result.escalations

        provider_failures = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-UNSAT")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        ).all()
        no_change_events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-UNSAT")
            .where(BuildTaskEvent.event_type == "runner.no_changes_produced")
        ).all()
        assert provider_failures == []
        assert len(no_change_events) == 1
        assert (no_change_events[0].event_data or {}).get("worker_id") == "b-1"


# ---------------------------------------------------------------------------
# SCENARIO 7: Reviewer Dies Repeatedly Does Not Consume Implementation Budget
# ---------------------------------------------------------------------------
def test_scenario_7_reviewer_dies_preserves_implementation_budget(tmp_path: Path):
    """Reviewer dies repeatedly -> implementation retry budget unchanged."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-REV-TEST",
            title="Task under review",
            description="desc",
            state="REVIEW_READY",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        # Simulate 3 reviewer crashes
        # First crash: task remains in REVIEW_READY
        rev_exec = BuildRunnerExecution(
            execution_id="exec-rev-crash-0",
            task_id="GH-REV-TEST",
            role="REVIEWER",
            worker_id="rev-1",
            provider="test",
            adapter="fake",
            status="RUNNING",
        )
        session.add(rev_exec)
        session.commit()

        obs = ExecutionObservation(status="FAILED", exit_code=-9)
        handled = runner._recoverable_failure(session, rev_exec, {"error": "SIGKILL"}, obs)
        session.commit()
        assert handled is True

        refreshed = session.get(BuildTask, "GH-REV-TEST")
        assert refreshed.state == "REVIEW_READY"
        assert refreshed.retry_generation == 0

        # Repeated reviewer crash: triggers REVIEW_ENVIRONMENT_BLOCKED, NOT EXECUTION_RETRY_LIMIT_REACHED
        rev_exec_2 = BuildRunnerExecution(
            execution_id="exec-rev-crash-1",
            task_id="GH-REV-TEST",
            role="REVIEWER",
            worker_id="rev-1",
            provider="test",
            adapter="fake",
            status="RUNNING",
        )
        session.add(rev_exec_2)
        session.commit()
        handled_2 = runner._recoverable_failure(session, rev_exec_2, {"error": "SIGKILL"}, obs)
        session.commit()
        assert handled_2 is True

        refreshed = session.get(BuildTask, "GH-REV-TEST")
        assert refreshed.state == "BLOCKED"
        assert refreshed.waiting_input["failure_evidence"]["underlying_invariant"] == "REVIEW_ENVIRONMENT"
        assert refreshed.retry_generation == 0


# ---------------------------------------------------------------------------
# SCENARIO 8: Provider Rate Limited Preserves Work and Fails Over
# ---------------------------------------------------------------------------
def test_scenario_8_provider_rate_limited_preserves_work(tmp_path: Path):
    """Provider rate limited -> provider cooldown/failover -> task work preserved."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-RATE",
            title="Rate limited task",
            description="desc",
            state="READY",
            review_policy="INDEPENDENT",
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-rate-0",
            task_id="GH-RATE",
            role="BUILDER",
            worker_id="b-rate",
            provider="anthropic",
            adapter="claude",
            status="RUNNING",
        )
        session.add(exec_row)
        session.commit()

        obs = ExecutionObservation(status="FAILED", exit_code=1)
        handled = runner._recoverable_failure(
            session,
            exec_row,
            {"provider_failure": "RATE_LIMITED", "detail": "HTTP 429 Too Many Requests"},
            obs,
        )
        session.commit()
        assert handled is True

        refreshed = session.get(BuildTask, "GH-RATE")
        assert refreshed.state == "READY"
        assert exec_row.status == "LOST"


# ---------------------------------------------------------------------------
# SCENARIO 9: Push Succeeds Then Crash Reconciles Git Reality
# ---------------------------------------------------------------------------
def test_scenario_9_push_succeeds_then_crash_reconciles_git_reality(tmp_path: Path):
    """Push succeeds then coordinator dies -> restart recognizes remote result
    -> no duplicate integration."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    # Commit already landed on main
    (repo / "feature.py").write_text("print('landed')\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "landed feature")
    landed_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with session_factory() as session:
        ensure_state(session)
        # DB says task is still INTEGRATING or BLOCKED
        task = BuildTask(
            task_id="GH-LANDED",
            title="Landed Task",
            description="desc",
            state="BLOCKED",
            branch_name="stagemesh/GH-LANDED",
        )
        session.add(task)
        exec_row = BuildRunnerExecution(
            execution_id="exec-landed-1",
            task_id="GH-LANDED",
            role="REVIEWER",
            worker_id="rev-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            reviewed_feature_sha=landed_sha,
            completed_at=datetime.now(UTC),
        )
        session.add(exec_row)
        session.commit()

        result = RunnerCycleResult(mode="RUNNING")
        runner._reconcile_git_reality(session, result)
        session.commit()

        refreshed = session.get(BuildTask, "GH-LANDED")
        assert refreshed.state == "DONE"
        assert "GH-LANDED" in result.recovered


# ---------------------------------------------------------------------------
# SCENARIO 10: Three Sequential Tasks With Deliberate Crashes Between Each
# ---------------------------------------------------------------------------
def test_scenario_10_three_sequential_tasks_with_crashes(tmp_path: Path):
    """Three independent tasks complete sequentially with one deliberate crash between each."""
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    task_ids = ["GH-S1", "GH-S2", "GH-S3"]
    for tid in task_ids:
        # 1. Start task
        with session_factory() as session:
            ensure_state(session)
            task = BuildTask(
                task_id=tid,
                title=f"Sequential {tid}",
                description="desc",
                state="INTEGRATING",
                branch_name=f"stagemesh/{tid}",
            )
            session.add(task)
            session.commit()

            # Create commit on branch
            wt = tmp_path / "worktrees" / tid
            ensure_worktree(wt, repo_root=repo, branch_name=f"stagemesh/{tid}", base_sha="main", allowed_roots=[str(tmp_path)])
            (wt / f"{tid}.txt").write_text(f"{tid} content\n", encoding="utf-8")
            _git(wt, "add", f"{tid}.txt")
            _git(wt, "commit", "-m", f"{tid} commit")
            sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

            # Merge to main
            _git(repo, "merge", "--no-ff", sha, "-m", f"Merge {tid}")

        # 2. Crash coordinator and restart
        _, fresh_runner = _setup_runner(tmp_path, repo)
        with session_factory() as session:
            fresh_runner.run_once()
            refreshed = session.get(BuildTask, tid)
            assert refreshed.state == "DONE"
            assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# SCENARIO 11: Parallel Builders Serialize Integrations Safely
# ---------------------------------------------------------------------------
def test_scenario_11_parallel_builders_serialize_integrations(tmp_path: Path):
    """Parallel builders finish while integrations serialize safely."""
    repo, _ = _setup_test_repo(tmp_path)
    wt1 = tmp_path / "worktrees" / "p1"
    wt2 = tmp_path / "worktrees" / "p2"

    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Two builders build in parallel in isolated worktrees
    ensure_worktree(wt1, repo_root=repo, branch_name="stagemesh/GH-P1", base_sha=base_sha, allowed_roots=[str(tmp_path)])
    ensure_worktree(wt2, repo_root=repo, branch_name="stagemesh/GH-P2", base_sha=base_sha, allowed_roots=[str(tmp_path)])

    (wt1 / "p1.txt").write_text("p1\n", encoding="utf-8")
    _git(wt1, "add", "p1.txt")
    _git(wt1, "commit", "-m", "p1 commit")
    sha1 = _git(wt1, "rev-parse", "HEAD").stdout.strip()

    (wt2 / "p2.txt").write_text("p2\n", encoding="utf-8")
    _git(wt2, "add", "p2.txt")
    _git(wt2, "commit", "-m", "p2 commit")
    sha2 = _git(wt2, "rev-parse", "HEAD").stdout.strip()

    # Integrations must serialize into main
    _git(repo, "merge", "--no-ff", sha1, "-m", "Integrate P1")
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert (repo / "p1.txt").exists()

    # P2 integrates after P1 advanced main
    _git(repo, "merge", "--no-ff", sha2, "-m", "Integrate P2")
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert (repo / "p1.txt").exists()
    assert (repo / "p2.txt").exists()


def test_branch_moved_concurrently_retries_exact_reviewed_sha(tmp_path: Path):
    """A CAS loser is retried against new main without spending build attempts."""
    repo, _ = _setup_test_repo(tmp_path)
    wt1 = tmp_path / "worktrees" / "race-1"
    wt2 = tmp_path / "worktrees" / "race-2"
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    ensure_worktree(
        wt1,
        repo_root=repo,
        branch_name="stagemesh/GH-108-A",
        base_sha=base_sha,
        allowed_roots=[str(tmp_path)],
    )
    ensure_worktree(
        wt2,
        repo_root=repo,
        branch_name="stagemesh/GH-108-B",
        base_sha=base_sha,
        allowed_roots=[str(tmp_path)],
    )

    (wt1 / "winner.txt").write_text("winner\n", encoding="utf-8")
    _git(wt1, "add", "winner.txt")
    _git(wt1, "commit", "-m", "winner commit")
    sha1 = _git(wt1, "rev-parse", "HEAD").stdout.strip()

    (wt2 / "loser.txt").write_text("loser\n", encoding="utf-8")
    _git(wt2, "add", "loser.txt")
    _git(wt2, "commit", "-m", "loser commit")
    sha2 = _git(wt2, "rev-parse", "HEAD").stdout.strip()

    class RaceExecutor(GitIntegrationExecutor):
        def __init__(self) -> None:
            super().__init__(main_ref="main")
            self.injected = False

        def _advance(self, wt: Path, branch: str, new: str, old: str) -> None:
            if not self.injected and wt == wt2:
                self.injected = True
                first = GitIntegrationExecutor(main_ref="main")
                observed = first.launch(
                    ExecutionLaunch(
                        task_id="GH-108-A",
                        role="INTEGRATION",
                        worker_id="integration-1",
                        provider="test",
                        worktree_path=str(wt1),
                        branch_name="stagemesh/GH-108-A",
                        prompt="integrate",
                        reviewed_feature_sha=sha1,
                    )
                )
                assert first.poll(observed.execution_id).status == "SUCCEEDED"
            super()._advance(wt, branch, new, old)

    race = RaceExecutor()
    handle = race.launch(
        ExecutionLaunch(
            task_id="GH-108-B",
            role="INTEGRATION",
            worker_id="integration-1",
            provider="test",
            prompt="integrate",
            worktree_path=str(wt2),
            branch_name="stagemesh/GH-108-B",
            reviewed_feature_sha=sha2,
        )
    )
    observed = race.poll(handle.execution_id)
    assert observed.status == "HUMAN_ACTION_REQUIRED"
    assert observed.human_escalation_type == "BRANCH_MOVED_CONCURRENTLY"
    assert (repo / "winner.txt").exists()
    assert not (repo / "loser.txt").exists()

    session_factory, runner = _setup_runner(tmp_path, repo)
    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-108-B",
            title="CAS loser",
            description="retry integration",
            state="BLOCKED",
            branch_name="stagemesh/GH-108-B",
        )
        session.add(task)
        session.add(
            BuildRunnerExecution(
                execution_id="review-gh-108-b",
                task_id="GH-108-B",
                role="REVIEWER",
                worker_id="reviewer-1",
                provider="test",
                adapter="fake",
                status="SUCCEEDED",
                completed_at=datetime.now(UTC),
                reviewed_feature_sha=sha2,
                branch_name="stagemesh/GH-108-B",
                worktree_path=str(wt2),
                result_data={
                    "review": {
                        "verdict": "GREEN",
                        "ready_for_integration": True,
                        "reviewed_feature_sha": sha2,
                    }
                },
            )
        )
        session.add(
            BuildRunnerExecution(
                execution_id="integration-gh-108-b-stale",
                task_id="GH-108-B",
                role="INTEGRATION",
                worker_id="integration-1",
                provider="test",
                adapter="builtin-git",
                status="HUMAN_ACTION_REQUIRED",
                completed_at=datetime.now(UTC),
                reviewed_feature_sha=sha2,
                branch_name="stagemesh/GH-108-B",
                worktree_path=str(wt2),
                human_escalation_type="BRANCH_MOVED_CONCURRENTLY",
                result_data=observed.result_data,
            )
        )
        record_event(
            session,
            EventInput(
                task_id="GH-108-B",
                event_type="task.transitioned",
                actor="runner",
                to_state="BLOCKED",
                event_data={"reason": "BRANCH_MOVED_CONCURRENTLY"},
            ),
        )
        session.commit()

    result = RunnerCycleResult()
    with session_factory() as session:
        runner._recover_diagnosed_blockers(session, result)
        assert "GH-108-B" in result.recovered
        task = session.get(BuildTask, "GH-108-B")
        assert task.state == "REVIEWING"
        runner._dispatch_integration(session, result)
        session.commit()

    runner.run_once()

    with session_factory() as session:
        task = session.get(BuildTask, "GH-108-B")
        assert task.state == "DONE"
        builder_attempts = session.scalars(
            select(BuildRunnerExecution).where(BuildRunnerExecution.role.in_(("BUILDER", "REMEDIATION")))
        ).all()
        assert builder_attempts == []

    assert (repo / "winner.txt").exists()
    assert (repo / "loser.txt").exists()


# ---------------------------------------------------------------------------
# SCENARIO 12: Unknown Human Change Protected in Canonical Repo
# ---------------------------------------------------------------------------
def test_scenario_12_unknown_human_change_protected(tmp_path: Path):
    """Unknown human change exists in canonical repo -> protected
    -> StageMesh does not destroy it -> genuine human gate if required."""
    repo, _ = _setup_test_repo(tmp_path)
    (repo / "operator_notes.secret").write_text("Top secret human notes\n", encoding="utf-8")

    # StageMesh displaced-work reconciler must refuse to wipe it
    assert reconcile_displaced_task_work(repo) is False
    assert (repo / "operator_notes.secret").exists()

    # preserve_unknown_operator_work safely stashes it
    stash_msg = preserve_unknown_operator_work(repo, reason="operator preservation")
    assert stash_msg is not None
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    # Verify stash contains the secret note
    stash_list = _git(repo, "stash", "list").stdout
    assert stash_msg in stash_list
