"""Tests for #65: work-conserving scheduling while external CI is pending.

Covers the policy.py state-machine change (AWAITING_EXTERNAL_CI) and
build_coordinator.runner.ci_reconciliation's SHA-pinned, non-blocking
reconciliation.
"""

from __future__ import annotations

import pytest

from build_coordinator.db import DatabaseLifecycle
from build_coordinator.models import BuildRunnerExecution, BuildTask
from build_coordinator.policy import CLAIMABLE_STATES, VALID_TRANSITIONS, can_transition
from build_coordinator.runner.ci_reconciliation import poll_external_ci, reconcile_awaiting_ci
from build_coordinator.service import transition_task, upsert_task
from build_coordinator.types import TaskSpec


# --------------------------------------------------------------- policy


def test_awaiting_external_ci_is_not_claimable():
    assert "AWAITING_EXTERNAL_CI" not in CLAIMABLE_STATES


def test_integrating_can_move_to_awaiting_external_ci():
    assert can_transition("INTEGRATING", "AWAITING_EXTERNAL_CI")


@pytest.mark.parametrize("to_state", ["DONE", "REWORK_REQUIRED", "BLOCKED"])
def test_awaiting_external_ci_resolves_to_expected_states(to_state):
    assert can_transition("AWAITING_EXTERNAL_CI", to_state)


def test_awaiting_external_ci_cannot_be_claimed_directly():
    # Only DONE / REWORK_REQUIRED / BLOCKED are reachable -- never CLAIMED.
    assert "CLAIMED" not in VALID_TRANSITIONS["AWAITING_EXTERNAL_CI"]


# --------------------------------------------------------------- poll_external_ci


class _FakeClient:
    def __init__(self, checks_by_sha: dict[str, list[dict]]):
        self._checks_by_sha = checks_by_sha
        self.calls: list[tuple[str, str]] = []

    def check_runs_for_sha(self, *, repo: str, sha: str):
        self.calls.append((repo, sha))
        return self._checks_by_sha.get(sha, [])


class _ErrorClient:
    def check_runs_for_sha(self, *, repo: str, sha: str):
        raise RuntimeError("gh: authentication required")


def test_no_checks_reported_yet_is_pending():
    client = _FakeClient({})
    obs = poll_external_ci(repo="org/repo", sha="abc123", client=client)
    assert obs.status == "PENDING"


def test_in_progress_check_is_pending():
    client = _FakeClient({"abc123": [{"status": "in_progress", "conclusion": None}]})
    obs = poll_external_ci(repo="org/repo", sha="abc123", client=client)
    assert obs.status == "PENDING"


def test_all_success_is_success():
    client = _FakeClient(
        {"abc123": [{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "neutral"}]}
    )
    obs = poll_external_ci(repo="org/repo", sha="abc123", client=client)
    assert obs.status == "SUCCESS"


def test_any_failure_is_failure():
    client = _FakeClient(
        {"abc123": [{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "failure"}]}
    )
    obs = poll_external_ci(repo="org/repo", sha="abc123", client=client)
    assert obs.status == "FAILURE"


def test_unreachable_gh_is_reported_not_swallowed_as_pending():
    obs = poll_external_ci(repo="org/repo", sha="abc123", client=_ErrorClient())
    assert obs.status == "UNREACHABLE"
    assert "authentication" in obs.detail


# --------------------------------------------------------------- reconcile_awaiting_ci


@pytest.fixture
def db(tmp_path):
    lifecycle = DatabaseLifecycle(f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}", data_dir=tmp_path)
    lifecycle.initialize_schema()
    with lifecycle.session() as session:
        yield session
    lifecycle.dispose()


def _make_task_awaiting_ci(session, task_id: str, sha: str) -> BuildTask:
    upsert_task(
        session,
        TaskSpec(
            task_id=task_id,
            title=f"Task {task_id}",
            description="demo",
            acceptance_criteria=["done"],
        ),
    )
    for to_state in ("CLAIMED", "IN_PROGRESS", "VALIDATING", "REVIEW_READY", "REVIEWING", "INTEGRATING"):
        transition_task(session, task_id, to_state, actor="test")
    execution = BuildRunnerExecution(
        task_id=task_id,
        role="INTEGRATION",
        worker_id="integration-1",
        provider="stagemesh",
        adapter="fake",
        status="SUCCEEDED",
        result_data={"merge_commit_sha": sha},
    )
    session.add(execution)
    session.flush()
    transition_task(session, task_id, "AWAITING_EXTERNAL_CI", actor="test")
    return session.get(BuildTask, task_id)


def test_pending_ci_leaves_task_in_awaiting_state_without_blocking(db):
    task = _make_task_awaiting_ci(db, "T-1", "sha-pending")
    client = _FakeClient({"sha-pending": [{"status": "in_progress", "conclusion": None}]})
    outcomes = reconcile_awaiting_ci(db, repo="org/repo", client=client)
    db.flush()
    assert outcomes == [{"task_id": "T-1", "status": "PENDING", "detail": "one or more checks still running"}]
    assert db.get(BuildTask, "T-1").state == "AWAITING_EXTERNAL_CI"


def test_success_ci_reconciles_task_to_done(db):
    _make_task_awaiting_ci(db, "T-2", "sha-good")
    client = _FakeClient({"sha-good": [{"status": "completed", "conclusion": "success"}]})
    reconcile_awaiting_ci(db, repo="org/repo", client=client)
    assert db.get(BuildTask, "T-2").state == "DONE"


def test_success_ci_records_project_delivery_evidence_before_done(db):
    _make_task_awaiting_ci(db, "T-2E", "sha-good")
    client = _FakeClient({"sha-good": [{"status": "completed", "conclusion": "success"}]})
    calls = []

    reconcile_awaiting_ci(
        db,
        repo="org/repo",
        client=client,
        delivery_evidence_recorder=lambda task_id, sha: calls.append((task_id, sha)) or {"status": "COMMITTED"},
    )

    assert calls == [("T-2E", "sha-good")]
    assert db.get(BuildTask, "T-2E").state == "DONE"


def test_success_ci_fails_closed_when_project_delivery_evidence_cannot_persist(db):
    _make_task_awaiting_ci(db, "T-2F", "sha-good")
    client = _FakeClient({"sha-good": [{"status": "completed", "conclusion": "success"}]})

    outcomes = reconcile_awaiting_ci(
        db,
        repo="org/repo",
        client=client,
        delivery_evidence_recorder=lambda task_id, sha: {"status": "COMMIT_FAILED", "detail": "read-only repo"},
    )

    assert outcomes == [
        {
            "task_id": "T-2F",
            "status": "UNREACHABLE",
            "detail": "project delivery evidence failed: read-only repo",
        }
    ]
    assert db.get(BuildTask, "T-2F").state == "BLOCKED"


def test_failure_ci_routes_to_rework(db):
    _make_task_awaiting_ci(db, "T-3", "sha-bad")
    client = _FakeClient({"sha-bad": [{"status": "completed", "conclusion": "failure"}]})
    reconcile_awaiting_ci(db, repo="org/repo", client=client)
    assert db.get(BuildTask, "T-3").state == "REWORK_REQUIRED"


def test_unreachable_gh_blocks_only_after_max_consecutive_errors(db):
    _make_task_awaiting_ci(db, "T-4", "sha-x")
    client = _ErrorClient()
    for _ in range(4):
        reconcile_awaiting_ci(db, repo="org/repo", client=client, max_consecutive_errors=5)
        assert db.get(BuildTask, "T-4").state == "AWAITING_EXTERNAL_CI"
    reconcile_awaiting_ci(db, repo="org/repo", client=client, max_consecutive_errors=5)
    assert db.get(BuildTask, "T-4").state == "BLOCKED"


def test_stale_sha_does_not_satisfy_a_different_pending_push(db):
    """A later re-integration under the same task records a new SHA; CI
    results must be checked against the exact recorded SHA, never a stale
    or substituted one."""
    task = _make_task_awaiting_ci(db, "T-5", "sha-first")
    # Only the second, later SHA has a result recorded; the first has none.
    client = _FakeClient({"sha-second": [{"status": "completed", "conclusion": "success"}]})
    outcomes = reconcile_awaiting_ci(db, repo="org/repo", client=client)
    # Polled against sha-first (the recorded SHA), found nothing -> PENDING,
    # never silently treated as satisfied by sha-second's result.
    assert outcomes[0]["status"] == "PENDING"
    assert db.get(BuildTask, "T-5").state == "AWAITING_EXTERNAL_CI"


# --------------------------------------------------------------- project.yaml wiring


def test_project_yaml_external_ci_config_is_parsed_and_defaults_off(tmp_path):
    """The config-plumbing path most likely to silently break: project.yaml
    -> ProjectDefinition -> RunnerConfig. Default (no `external_ci` key at
    all) must stay disabled -- this is a strictly additive feature."""
    import yaml

    from build_coordinator.project.definition import load_project, register_project
    from build_coordinator.project.runtime import build_runner_config

    root = tmp_path / "repo"
    (root / ".stagemesh" / "tasks").mkdir(parents=True)
    definition = {
        "schema_version": 1,
        "id": "fixture",
        "name": "Fixture",
        "execution": {"concurrency": 1, "reviewers": 1, "default_review_policy": "NONE"},
    }
    (root / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(yaml.safe_dump({"tasks": []}), encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)

    project = load_project(root)
    assert project.external_ci_enabled is False
    assert project.external_ci_repo is None

    config = build_runner_config(project, dry_run=True)
    assert config.external_ci_enabled is False
    assert config.external_ci_repo is None


def test_project_yaml_external_ci_config_enabled_is_parsed_through_to_runner_config(tmp_path):
    import subprocess

    import yaml

    from build_coordinator.project.definition import load_project
    from build_coordinator.project.runtime import build_runner_config

    root = tmp_path / "repo"
    (root / ".stagemesh" / "tasks").mkdir(parents=True)
    definition = {
        "schema_version": 1,
        "id": "fixture",
        "name": "Fixture",
        "execution": {
            "concurrency": 1,
            "reviewers": 1,
            "default_review_policy": "NONE",
            "external_ci": {"enabled": True, "repo": "org/repo", "max_consecutive_errors": 3},
        },
    }
    (root / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(yaml.safe_dump({"tasks": []}), encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)

    project = load_project(root)
    assert project.external_ci_enabled is True
    assert project.external_ci_repo == "org/repo"
    assert project.external_ci_max_consecutive_errors == 3

    config = build_runner_config(project, dry_run=True)
    assert config.external_ci_enabled is True
    assert config.external_ci_repo == "org/repo"
    assert config.external_ci_max_consecutive_errors == 3


def test_project_yaml_external_ci_enabled_without_repo_is_rejected(tmp_path):
    import subprocess

    import yaml

    from build_coordinator.project.definition import ProjectError, load_project

    root = tmp_path / "repo"
    (root / ".stagemesh" / "tasks").mkdir(parents=True)
    definition = {
        "schema_version": 1,
        "id": "fixture",
        "name": "Fixture",
        "execution": {
            "concurrency": 1,
            "reviewers": 1,
            "default_review_policy": "NONE",
            "external_ci": {"enabled": True},
        },
    }
    (root / ".stagemesh" / "project.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    (root / ".stagemesh" / "tasks" / "backlog.yaml").write_text(yaml.safe_dump({"tasks": []}), encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True)

    with pytest.raises(ProjectError, match="external_ci"):
        load_project(root)


def test_independent_ready_task_is_unaffected_by_a_task_awaiting_ci(db):
    """The core work-conserving guarantee: a task in AWAITING_EXTERNAL_CI
    does not appear in CLAIMABLE_STATES, so it cannot hold up an
    independent READY task's own claimability."""
    from datetime import UTC, datetime

    from build_coordinator.claims import task_is_claimable

    awaiting = _make_task_awaiting_ci(db, "T-6", "sha-pending-2")
    upsert_task(
        db,
        TaskSpec(task_id="T-7", title="Independent task", description="demo", acceptance_criteria=["done"]),
    )
    ready = db.get(BuildTask, "T-7")
    now = datetime.now(UTC)

    assert task_is_claimable(db, ready, now)
    assert not task_is_claimable(db, awaiting, now)
