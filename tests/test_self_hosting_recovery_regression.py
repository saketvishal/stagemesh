from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from build_coordinator.db import Base
from build_coordinator.events import record_event
from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.git_integrator import GitIntegrationExecutor, IntegrationStop
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.orchestrator import BuildRunner, RunnerCycleResult
from build_coordinator.runner.worktree import (
    _checked_out_elsewhere,
    _preserve,
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
)
from build_coordinator.types import ClaimRequest, EventInput


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
    _git(repo, "config", "user.name", "Operator Test")
    _git(repo, "config", "user.email", "operator@example.com")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "origin", "main")
    return repo, remote


def _setup_runner(tmp_path: Path, repo: Path, workers: list[WorkerConfig] | None = None) -> tuple[sessionmaker, BuildRunner]:
    db_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    config = RunnerConfig(
        workers=workers or [
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


# 1. Canonical repo clean + task worktree dirty with valid task-owned changes
def test_canonical_repo_clean_task_worktree_dirty_with_task_work(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    task_wt = tmp_path / "worktrees" / "task-1"
    ensure_worktree(task_wt, repo_root=repo, branch_name="stagemesh/GH-10", base_sha="main", allowed_roots=[str(tmp_path)])

    # Make uncommitted changes in task worktree
    (task_wt / "feature.py").write_text("print('feature')\n", encoding="utf-8")
    assert _git(task_wt, "status", "--porcelain").stdout.strip() != ""
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # Canonical repo integration check passes without tripping WORKING_CHECKOUT_DIRTY
    executor = GitIntegrationExecutor(main_ref="main")
    feature_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    executor._advance(task_wt, "main", feature_sha, feature_sha)
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# 2. Canonical repo accidentally dirtied by StageMesh
def test_canonical_repo_accidentally_dirtied_by_stagemesh(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    # StageMesh accidentally creates task files in canonical repo
    (repo / "build_coordinator").mkdir(parents=True, exist_ok=True)
    (repo / "build_coordinator" / "new_feature.py").write_text("# new task work\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain").stdout.strip() != ""

    # Reconcile displaced work into task lineage (GH-45)
    reconciled = reconcile_displaced_task_work(repo, task_id="GH-45")
    assert reconciled is True
    # Canonical repo must now be clean
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # The branch stagemesh/GH-45 must have the commit
    branch_status = _git(repo, "log", "-n", "1", "--oneline", "stagemesh/GH-45")
    assert branch_status.returncode == 0
    assert "GH-45" in branch_status.stdout


# 3. Task worktree restart/resume with legitimate uncommitted task work
def test_task_worktree_restart_preserves_uncommitted_work(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    task_wt = tmp_path / "worktrees" / "builder-1"
    prepare_task_workspace(task_wt, repo_root=repo, branch_name="stagemesh/GH-20", base_ref="main", resume=False, allowed_roots=[str(tmp_path)])

    # Worker left uncommitted WIP
    (task_wt / "wip.py").write_text("# in progress work\n", encoding="utf-8")

    # Resume must preserve uncommitted changes onto resume_branch
    prepare_task_workspace(task_wt, repo_root=repo, branch_name="stagemesh/GH-20", base_ref="main", resume=True, allowed_roots=[str(tmp_path)])
    log = _git(task_wt, "log", "-n", "1", "--oneline")
    assert "checkpoint" in log.stdout.lower() or "preserve" in log.stdout.lower()
    assert (task_wt / "wip.py").exists()


# 4. Unknown operator-created dirty files are preserved and fail safely
def test_unknown_operator_work_preserved_safely(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    # Operator created unknown untracked file
    (repo / "operator_notes.secret").write_text("secret human data\n", encoding="utf-8")

    # reconcile_displaced_task_work must refuse to blindly discard unknown non-stagemesh files
    assert reconcile_displaced_task_work(repo) is False
    assert (repo / "operator_notes.secret").exists()

    # preserve_unknown_operator_work stashes it safely
    stash_msg = preserve_unknown_operator_work(repo, reason="test")
    assert stash_msg is not None
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    stash_list = _git(repo, "stash", "list").stdout
    assert stash_msg in stash_list


# 5. Stale worktree ownership reconciliation
def test_stale_worktree_ownership_reconciliation(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    wt1 = tmp_path / "worktrees" / "wt1"
    wt2 = tmp_path / "worktrees" / "wt2"
    branch = "stagemesh/GH-30"

    ensure_worktree(wt1, repo_root=repo, branch_name=branch, base_sha="main", allowed_roots=[str(tmp_path)])
    # Check that wt1 holds branch
    assert _checked_out_elsewhere(repo, branch) == wt1

    # Now prepare wt2 for the same branch with resume
    prepare_task_workspace(wt2, repo_root=repo, branch_name=branch, base_ref="main", resume=True, allowed_roots=[str(tmp_path)])
    # wt1 should be detached, wt2 now owns the branch
    wt1_head = _git(wt1, "status").stdout
    assert "HEAD detached" in wt1_head or "detached" in wt1_head


# 6. Wrong branch/worktree association
def test_wrong_branch_worktree_association(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    wt = tmp_path / "worktrees" / "reused-wt"
    # Previously on branch A
    ensure_worktree(wt, repo_root=repo, branch_name="stagemesh/GH-40", base_sha="main", allowed_roots=[str(tmp_path)])
    assert "stagemesh/GH-40" in _git(wt, "branch", "--show-current").stdout

    # Now assign to branch B
    prepare_task_workspace(wt, repo_root=repo, branch_name="stagemesh/GH-41", base_ref="main", resume=False, allowed_roots=[str(tmp_path)])
    assert "stagemesh/GH-41" in _git(wt, "branch", "--show-current").stdout


# 7. Missing or changed reviewed SHA
def test_missing_or_changed_reviewed_sha(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-50", title="Test SHA", description="desc", state="REVIEWING", branch_name="stagemesh/GH-50")
        session.add(task)
        # Execution missing reviewed_feature_sha
        exec_row = BuildRunnerExecution(
            execution_id="exec-rev-50",
            task_id="GH-50",
            role="REVIEWER",
            worker_id="reviewer-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            completed_at=datetime.now(UTC),
            result_data={"review": {"verdict": "GREEN", "ready_for_integration": True}},
        )
        session.add(exec_row)
        session.commit()

        # Runner cycle dispatch integration
        cycle_result = RunnerCycleResult(mode="AUTONOMOUS")
        runner._dispatch_integration(session, cycle_result)
        assert "GH-50:MISSING_REVIEWED_SHA" in cycle_result.escalations

        # Verify task has structured failure evidence
        task_ref = session.get(BuildTask, "GH-50")
        assert task_ref.state == "BLOCKED"
        evidence = task_ref.waiting_input.get("failure_evidence")
        assert evidence is not None
        assert evidence["underlying_invariant"] == "REVIEWED_SHA_CONTRACT"
        assert evidence["recovery_classification"] == "RECOVERABLE_GIT_STATE"


# 8. Git safety failure reports specific evidence
def test_git_safety_failure_reports_specific_evidence(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-60", title="Safety Failure", description="desc", state="READY")
        session.add(task)
        session.commit()

        worker = WorkerConfig(
            worker_id="b-1",
            role="BUILDER",
            provider="test",
            adapter="subprocess",
            worktree_path=str(tmp_path / "nonexistent-wt"),
        )
        runner._block_task(
            session,
            task.task_id,
            "GIT_SAFETY_FAILURE",
            invariant="GIT_SAFETY_FAILURE",
            worker=worker,
            branch="stagemesh/GH-60",
            worktree=worker.worktree_path,
            error="fatal: not a valid repository",
            recovery_classification="RECOVERABLE_GIT_STATE",
        )
        session.commit()

        task_ref = session.get(BuildTask, "GH-60")
        assert task_ref.state == "BLOCKED"
        ev = task_ref.waiting_input["failure_evidence"]
        assert ev["underlying_invariant"] == "GIT_SAFETY_FAILURE"
        assert ev["worker"] == "b-1"
        assert ev["recovery_classification"] == "RECOVERABLE_GIT_STATE"
        assert "not a valid repository" in ev["original_error"]


# 9. Recoverable dirty state does not become generic COORDINATOR_INVARIANT_FAILURE
def test_recoverable_dirty_state_does_not_become_generic_invariant_failure(tmp_path: Path):
    repo, _ = _setup_test_repo(tmp_path)
    task_wt = tmp_path / "worktrees" / "task-wt"
    ensure_worktree(task_wt, repo_root=repo, branch_name="stagemesh/GH-70", base_sha="main", allowed_roots=[str(tmp_path)])

    # Dirty canonical checkout
    (repo / "uncommitted_operator.py").write_text("# manual edit\n", encoding="utf-8")

    executor = GitIntegrationExecutor(main_ref="main")
    with pytest.raises(IntegrationStop) as exc_info:
        executor._advance(task_wt, "main", "dummy_sha", "old_sha")

    assert exc_info.value.escalation == "WORKING_CHECKOUT_DIRTY"
    assert exc_info.value.escalation != "COORDINATOR_INVARIANT_FAILURE"


# 10. Restart does not duplicate workers or discard changes
def test_restart_does_not_duplicate_workers_or_discard_changes(tmp_path: Path):
    from datetime import timedelta

    repo, _ = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(task_id="GH-80", title="Restart Test", description="desc", state="READY")
        session.add(task)
        session.commit()

        claim = claim_task(
            session,
            ClaimRequest(
                task_id="GH-80",
                worker_id="builder-worker-1",
                provider="test",
                branch_name="stagemesh/GH-80",
                worktree_path=str(tmp_path / "worktrees" / "b1"),
            ),
        )
        exec_row = BuildRunnerExecution(
            execution_id="exec-80",
            task_id="GH-80",
            claim_id=claim.claim_id,
            role="BUILDER",
            worker_id="builder-worker-1",
            provider="test",
            adapter="subprocess",
            status="RUNNING",
        )
        session.add(exec_row)
        session.commit()

        # Simulate coordinator restart / stale reconciliation by expiring claim lease
        claim_ref = session.get(BuildTaskClaim, claim.claim_id)
        claim_ref.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
        session.commit()

        reconciled = reconcile_stale_executions(session)
        assert len(reconciled) == 1
        assert reconciled[0].status == "TERMINATED"
        assert reconciled[0].result_data["reconciliation_state"] == "STALE_CLAIM"


# 11. Multi-project / worktree isolation remains intact
def test_multi_project_worktree_isolation(tmp_path: Path):
    repo1, _ = _setup_test_repo(tmp_path / "proj1")
    repo2, _ = _setup_test_repo(tmp_path / "proj2")

    wt1 = tmp_path / "wt1"
    wt2 = tmp_path / "wt2"

    ensure_worktree(wt1, repo_root=repo1, branch_name="stagemesh/P1-01", base_sha="main", allowed_roots=[str(tmp_path)])
    ensure_worktree(wt2, repo_root=repo2, branch_name="stagemesh/P2-01", base_sha="main", allowed_roots=[str(tmp_path)])

    (wt1 / "p1.txt").write_text("p1 data\n", encoding="utf-8")
    (wt2 / "p2.txt").write_text("p2 data\n", encoding="utf-8")

    assert not (wt1 / "p2.txt").exists()
    assert not (wt2 / "p1.txt").exists()
    assert _git(repo1, "status", "--porcelain").stdout.strip() == ""
    assert _git(repo2, "status", "--porcelain").stdout.strip() == ""


def test_retry_limit_reached_recovery_from_transient_failures(tmp_path: Path):
    """GH-78 / GH-60 regression: when EXECUTION_RETRY_LIMIT_REACHED was caused by
    transient infrastructure/provider failures or process exits, _recover_diagnosed_blockers
    must automatically advance retry_generation and recover the task to READY."""
    repo_root, _ = _setup_test_repo(tmp_path / "repo")
    session_factory, runner = _setup_runner(tmp_path, repo_root)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-78",
            title="Provider rate limit blocked task",
            description="A test task",
            state="READY",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        # Simulate task getting blocked with EXECUTION_RETRY_LIMIT_REACHED
        transition_task(session, "GH-78", "BLOCKED", actor="runner", reason="EXECUTION_RETRY_LIMIT_REACHED")
        # Add execution rows representing transient provider rate limits
        for i in range(3):
            exec_row = BuildRunnerExecution(
                execution_id=f"exec-gh78-{i}",
                task_id="GH-78",
                role="BUILDER",
                worker_id="builder-claude-1",
                provider="anthropic",
                adapter="claude",
                status="LOST",
                result_data={
                    "reconciliation_state": "PROVIDER_FAILED",
                    "provider_failure": "RATE_LIMITED",
                    "retry_generation": 0,
                },
                completed_at=datetime.now(UTC),
            )
            session.add(exec_row)
        session.commit()

        result = RunnerCycleResult(mode="RUNNING")
        runner._recover_diagnosed_blockers(session, result)
        session.commit()

        refreshed = session.get(BuildTask, "GH-78")
        assert refreshed.state == "READY"
        assert refreshed.retry_generation == 1
        assert "GH-78" in result.recovered

        # Check blocker recovered event
        event = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-78")
            .where(BuildTaskEvent.event_type == "runner.blocker_recovered")
        )
        assert event is not None
        assert event.event_data["reason"] == "EXECUTION_RETRY_LIMIT_REACHED"
        assert event.event_data["recovery_type"] == "INFRASTRUCTURE_RETRY_RECOVERY"


def test_reviewer_loss_does_not_consume_implementation_retry_budget(tmp_path: Path):
    """Phase 7 & 15: A lost or failed reviewer must never consume the task's
    implementation retry budget or block the task with EXECUTION_RETRY_LIMIT_REACHED."""
    repo_root, _ = _setup_test_repo(tmp_path / "repo")
    session_factory, runner = _setup_runner(tmp_path, repo_root)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-75",
            title="Task under review",
            description="A test task",
            state="REVIEW_READY",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        # Reviewer execution fails
        rev_exec = BuildRunnerExecution(
            execution_id="exec-rev-1",
            task_id="GH-75",
            role="REVIEWER",
            worker_id="reviewer-1",
            provider="anthropic",
            adapter="claude",
            status="RUNNING",
        )
        session.add(rev_exec)
        session.commit()

        from build_coordinator.execution.base import ExecutionObservation
        obs = ExecutionObservation(status="FAILED", exit_code=1)
        handled = runner._recoverable_failure(
            session,
            rev_exec,
            {"provider_failure": "UNAVAILABLE"},
            obs,
        )
        session.commit()

        assert handled is True
        assert rev_exec.status == "LOST"
        refreshed = session.get(BuildTask, "GH-75")
        # Task remains in REVIEW_READY, NOT BLOCKED!
        assert refreshed.state == "REVIEW_READY"


def test_provider_rate_limit_does_not_consume_implementation_budget(tmp_path: Path):
    """Phase 7 & 15: Transient provider failure (RATE_LIMITED) routes for cooldown
    and leaves task in READY without consuming implementation retry limit."""
    repo_root, _ = _setup_test_repo(tmp_path / "repo")
    session_factory, runner = _setup_runner(tmp_path, repo_root)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-RATE-1",
            title="Builder rate limit task",
            description="A test task",
            state="READY",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        build_exec = BuildRunnerExecution(
            execution_id="exec-rate-1",
            task_id="GH-RATE-1",
            role="BUILDER",
            worker_id="builder-grok",
            provider="grok",
            adapter="grok",
            status="RUNNING",
        )
        session.add(build_exec)
        session.commit()

        from build_coordinator.execution.base import ExecutionObservation
        obs = ExecutionObservation(status="FAILED", exit_code=1)
        handled = runner._recoverable_failure(
            session,
            build_exec,
            {"provider_failure": "RATE_LIMITED", "detail": "Rate limit exceeded"},
            obs,
        )
        session.commit()

        assert handled is True
        assert build_exec.status == "LOST"
        refreshed = session.get(BuildTask, "GH-RATE-1")
        assert refreshed.state == "READY"

        # Check provider failure event recorded
        ev = session.scalar(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-RATE-1")
            .where(BuildTaskEvent.event_type == "runner.provider_failure")
        )
        assert ev is not None
        assert ev.event_data["failure"] == "RATE_LIMITED"


def test_builder_no_changes_retries_when_attempts_remain(tmp_path: Path):
    """GH-55 regression: When builder produces 0 changes and acceptance criteria not met,
    if attempts remain, it must retry (transitioning to READY) rather than instantly
    blocking with COORDINATOR_INVARIANT_FAILURE."""
    repo_root, _ = _setup_test_repo(tmp_path / "repo")
    session_factory, runner = _setup_runner(tmp_path, repo_root)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-55-TEST",
            title="Unsatisfied task",
            description="A test task",
            state="CLAIMED",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-55-1",
            task_id="GH-55-TEST",
            role="BUILDER",
            worker_id="builder-claude-1",
            provider="anthropic",
            adapter="claude",
            status="SUCCEEDED",
        )
        session.add(exec_row)
        session.commit()

        from build_coordinator.execution.results import parse_executor_result
        parsed = parse_executor_result(
            {
                "schema_version": 1,
                "role": "BUILDER",
                "feature_sha": "abc1234",
                "blockers": ["the agent produced no changes on the task branch"],
                "identity": {"provider": "anthropic", "runtime": "claude"},
            },
            execution_id="exec-55-1",
            task_id="GH-55-TEST",
            role="BUILDER",
            require_identity=False,
        )

        result = RunnerCycleResult(mode="RUNNING")
        runner._builder_succeeded(session, exec_row, result, parsed)
        session.commit()

        refreshed = session.get(BuildTask, "GH-55-TEST")
        # Attempt 1 of 3: Should be RESUMABLE to retry with alternate worker, NOT blocked!
        assert refreshed.state == "RESUMABLE"
        assert "GH-55-TEST:NO_CHANGES_PRODUCED" not in result.escalations


def test_recover_diagnosed_blocker_for_zero_change_with_remaining_attempts(tmp_path: Path):
    """GH-55 recovery regression: If task was blocked with COORDINATOR_INVARIANT_FAILURE
    due to zero changes on attempt 1, _recover_diagnosed_blockers recovers it to READY."""
    repo_root, _ = _setup_test_repo(tmp_path / "repo")
    session_factory, runner = _setup_runner(tmp_path, repo_root)

    with session_factory() as session:
        ensure_state(session)
        task = BuildTask(
            task_id="GH-55-REC",
            title="Zero change blocked task",
            description="A test task",
            state="READY",
            review_policy="INDEPENDENT",
            retry_generation=0,
        )
        session.add(task)
        session.commit()

        transition_task(session, "GH-55-REC", "BLOCKED", actor="runner", reason="COORDINATOR_INVARIANT_FAILURE")
        # 1 previous execution recorded
        exec_row = BuildRunnerExecution(
            execution_id="exec-55-rec-1",
            task_id="GH-55-REC",
            role="BUILDER",
            worker_id="builder-claude-1",
            provider="anthropic",
            adapter="claude",
            status="SUCCEEDED",
        )
        session.add(exec_row)
        session.commit()

        result = RunnerCycleResult(mode="RUNNING")
        runner._recover_diagnosed_blockers(session, result)
        session.commit()

        refreshed = session.get(BuildTask, "GH-55-REC")
        assert refreshed.state == "READY"
        assert "GH-55-REC" in result.recovered
