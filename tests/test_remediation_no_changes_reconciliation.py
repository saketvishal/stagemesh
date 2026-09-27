"""Regression tests for Problem 4: an existing valid remediation commit must
be reusable.

When a task is REWORK_REQUIRED and a REMEDIATION worker is dispatched, the
correction the reviewer asked for may already be present on the task
branch (from an earlier, already-successful remediation attempt whose
claim was lost/expired before the coordinator could observe the success,
or from a worker that correctly recognized the fix was already there). A
remediation worker that truthfully reports "no changes" in that situation
must not be treated the same as one that made no progress at all: the
orchestrator must inspect the existing feature SHA on the task branch and,
when it already satisfies the requirement, proceed without manufacturing
a cosmetic commit. `NO_CHANGES_PRODUCED` must still apply, unweakened, to a
genuinely incomplete REMEDIATION attempt (no commits, no satisfied
acceptance evidence).

This exercises `BuildRunner._builder_succeeded()`, which already handles
both BUILDER and REMEDIATION roles identically for a reported
"the agent produced no changes on the task branch" blocker: it first tries
deterministic ALREADY_SATISFIED reconciliation (`_reconcile_no_changes_produced`,
already covered for the BUILDER role by
test_scenario_5_builder_zero_changes_when_already_satisfied in
test_runtime_stability_hardening.py), and -- when that is inconclusive --
falls back to checking whether the task's branch already has real commits
ahead of its base. Both paths were previously untested for the
REMEDIATION role specifically; these tests close that gap and pin the
existing-commit-reuse contract down as a regression test.
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from build_coordinator.db import Base
from build_coordinator.execution.results import parse_executor_result
from build_coordinator.models import BuildRunnerExecution, BuildTask, BuildTaskEvent
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.orchestrator import BuildRunner, RunnerCycleResult
from build_coordinator.runner.worktree import task_branch_name


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _setup_test_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", str(remote), str(repo))
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.name", "Remediation Reuse Test")
    _git(repo, "config", "user.email", "remediation-reuse@example.com")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    _git(repo, "push", "origin", "main")
    return repo


def _setup_runner(tmp_path: Path, repo: Path) -> tuple[sessionmaker, BuildRunner]:
    db_path = tmp_path / "coordinator.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    config = RunnerConfig(
        workers=[
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


def _no_changes_parsed(execution_id: str, task_id: str, feature_sha: str):
    return parse_executor_result(
        {
            "schema_version": 1,
            "role": "REMEDIATION",
            "feature_sha": feature_sha,
            "blockers": ["the agent produced no changes on the task branch"],
            "identity": {"provider": "test", "runtime": "fake"},
        },
        execution_id=execution_id,
        task_id=task_id,
        role="REMEDIATION",
        require_identity=False,
    )


def test_remediation_reuses_existing_corrective_commit_without_manufacturing_one(tmp_path: Path):
    """The remediation the reviewer asked for is already on the task branch
    (one real commit ahead of base). A remediation worker that truthfully
    reports no new changes must be trusted: the task proceeds using the
    existing feature SHA, and no additional (cosmetic) commit is created."""
    repo = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    branch = task_branch_name("GH-REMEDIATE-REUSE")
    _git(repo, "checkout", "-b", branch, "main")
    (repo / "fix.txt").write_text("the requested correction\n", encoding="utf-8")
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-m", "apply requested remediation")
    existing_feature_sha = _git(repo, "rev-parse", branch).stdout.strip()
    commit_count_before = _git(repo, "rev-list", "--count", branch).stdout.strip()
    _git(repo, "checkout", "main")

    with session_factory() as session:
        task = BuildTask(
            task_id="GH-REMEDIATE-REUSE",
            title="Remediation reuse task",
            description="desc",
            state="CLAIMED",
            review_policy="INDEPENDENT",
            acceptance_criteria=["fix applied"],
            required_validation=[],
            branch_name=branch,
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-remediate-1",
            task_id="GH-REMEDIATE-REUSE",
            role="REMEDIATION",
            worker_id="remediation-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            worktree_path=str(repo),
        )
        session.add(exec_row)
        session.commit()

        parsed = _no_changes_parsed("exec-remediate-1", "GH-REMEDIATE-REUSE", existing_feature_sha)

        result = RunnerCycleResult(mode="RUNNING")
        runner._builder_succeeded(session, exec_row, result, parsed)
        session.commit()

        refreshed = session.get(BuildTask, "GH-REMEDIATE-REUSE")
        # Proceeded past the no-changes report instead of retrying/blocking.
        assert refreshed.state in {"VALIDATING", "REVIEW_READY", "DONE"}
        assert f"GH-REMEDIATE-REUSE:NO_CHANGES_PRODUCED" not in result.escalations

    # No cosmetic/manufactured commit was added to the branch: exact same
    # commit count and tip as the pre-existing remediation.
    commit_count_after = _git(repo, "rev-list", "--count", branch).stdout.strip()
    tip_after = _git(repo, "rev-parse", branch).stdout.strip()
    assert commit_count_after == commit_count_before
    assert tip_after == existing_feature_sha


def test_remediation_no_changes_produced_is_not_weakened_for_genuinely_incomplete_task(tmp_path: Path):
    """A REMEDIATION worker that truthfully produces no changes, on a task
    branch with no work ahead of base and no deterministic satisfaction
    evidence, must still be retried/escalated via the normal
    NO_CHANGES_PRODUCED path -- the existing-commit-reuse fix above must
    not create a loophole that lets a genuinely incomplete task slip
    through."""
    repo = _setup_test_repo(tmp_path)
    session_factory, runner = _setup_runner(tmp_path, repo)

    branch = task_branch_name("GH-REMEDIATE-INCOMPLETE")
    _git(repo, "checkout", "-b", branch, "main")
    _git(repo, "checkout", "main")
    head_sha = _git(repo, "rev-parse", "main").stdout.strip()

    with session_factory() as session:
        task = BuildTask(
            task_id="GH-REMEDIATE-INCOMPLETE",
            title="Genuinely incomplete remediation task",
            description="desc",
            state="CLAIMED",
            review_policy="INDEPENDENT",
            acceptance_criteria=["fix applied and file contains the marker"],
            required_validation=[],
            branch_name=branch,
        )
        session.add(task)
        session.commit()

        exec_row = BuildRunnerExecution(
            execution_id="exec-remediate-2",
            task_id="GH-REMEDIATE-INCOMPLETE",
            role="REMEDIATION",
            worker_id="remediation-1",
            provider="test",
            adapter="fake",
            status="SUCCEEDED",
            worktree_path=str(repo),
        )
        session.add(exec_row)
        session.commit()

        parsed = _no_changes_parsed("exec-remediate-2", "GH-REMEDIATE-INCOMPLETE", head_sha)

        result = RunnerCycleResult(mode="RUNNING")
        runner._builder_succeeded(session, exec_row, result, parsed)
        session.commit()

        refreshed = session.get(BuildTask, "GH-REMEDIATE-INCOMPLETE")
        # Not silently accepted: retried with a fresh attempt (RESUMABLE)
        # rather than fast-forwarded to VALIDATING/REVIEW_READY/DONE the
        # way a truly-satisfied task is.
        assert refreshed.state == "RESUMABLE"
        assert refreshed.state not in {"VALIDATING", "REVIEW_READY", "DONE"}
        reconciliations = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-REMEDIATE-INCOMPLETE")
            .where(BuildTaskEvent.event_type == "runner.no_changes_reconciled")
        ).all()
        assert len(reconciliations) == 1
        assert reconciliations[0].event_data["outcome"] != "ALREADY_SATISFIED"
        no_change_events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == "GH-REMEDIATE-INCOMPLETE")
            .where(BuildTaskEvent.event_type == "runner.no_changes_produced")
        ).all()
        assert len(no_change_events) == 1

    # No commit was fabricated on the branch either.
    commit_count = _git(repo, "rev-list", "--count", branch).stdout.strip()
    assert commit_count == "1"
