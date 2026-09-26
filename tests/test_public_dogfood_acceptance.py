from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution import ExecutionObservation, FakeExecutor
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.policy import CoordinatorPolicyError
from build_coordinator.project.commands import _global_capacity_batches
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.git_safety import FakeGit
from build_coordinator.runner.models import RunnerConfig, WorkerConfig
from build_coordinator.runner.worktree import cleanup_task_branch
from build_coordinator.service import (
    ClaimRequest,
    claim_review,
    claim_task,
    checkpoint,
    get_resume_context,
    recover_expired,
    transition_task,
    upsert_task,
    utcnow,
)
from build_coordinator.types import CheckpointInput, TaskSpec


REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = REPO_ROOT / "docs" / "dogfood" / "acceptance-suite.yaml"
DEMO_ROOT = REPO_ROOT / "examples" / "public_dogfood"

REQUIRED_REQUIREMENTS = {
    "parallelism",
    "review",
    "validation",
    "provider fallback",
    "controlled interruption and recovery",
    "cleanup",
    "global invocation",
    "GitHub delivery",
    "single-agent public demo",
    "staged agents public demo",
    "cross-provider recovery",
    "high-risk governance",
    "multi-project execution",
}

FORBIDDEN_TEXT = re.compile(
    r"(?i)(caventra|C:\\|[A-Za-z]:[\\/]|(?:token|secret|api[_-]?key|password)\s*[:=]\s*\S+)"
)


@pytest.fixture(autouse=True)
def isolate_runner_artifacts(tmp_path, monkeypatch):
    result_dir = tmp_path / "results"
    result_dir.mkdir()
    monkeypatch.setenv("BUILD_COORDINATOR_RESULT_DIR", str(result_dir))


@pytest.fixture(autouse=True)
def clean_db():
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


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def _task(task_id: str, **kwargs) -> TaskSpec:
    values = dict(
        task_id=task_id,
        title=f"Task {task_id}",
        description="public dogfood acceptance test task",
        acceptance_criteria=["passes"],
        review_policy="INDEPENDENT",
    )
    values.update(kwargs)
    return TaskSpec(**values)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Existing guardrails (kept, still valid).
# ---------------------------------------------------------------------------


def test_public_dogfood_suite_covers_gh50_acceptance_surface():
    suite = _load_yaml(SUITE_PATH)
    scenarios = suite["scenarios"]
    covered = {requirement for scenario in scenarios for requirement in scenario["requirements"]}

    assert REQUIRED_REQUIREMENTS <= covered
    assert suite["repeatability"] == {
        "database": "sqlite",
        "providers": "fake-or-scripted",
        "network_required": False,
        "cleanup_required": True,
    }


def test_public_dogfood_scenarios_reference_existing_demo_manifests():
    suite = _load_yaml(SUITE_PATH)

    for scenario in suite["scenarios"]:
        demo = REPO_ROOT / scenario["demo"]
        assert demo.exists(), scenario
        manifest = _load_yaml(demo)
        assert manifest["demo_id"] == scenario["id"]
        assert "evidence" in manifest or "expected" in manifest


def test_public_dogfood_assets_are_public_safe():
    paths = [SUITE_PATH, REPO_ROOT / "docs" / "dogfood" / "README.md"]
    paths.extend(DEMO_ROOT.glob("*.yaml"))
    paths.append(DEMO_ROOT / "README.md")

    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in FORBIDDEN_TEXT.finditer(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert not offenders, offenders


def test_public_alpha_claims_match_accepted_evidence():
    """Public docs should claim only accepted post-alpha evidence and keep
    unproven delivery/runtime support explicit."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    roadmap = (REPO_ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    evidence = (REPO_ROOT / "docs" / "evidence" / "CODEX_ACCEPTANCE.md").read_text(
        encoding="utf-8"
    )
    public_claims = "\n".join([readme, roadmap, evidence])

    for required in (
        "Claude Code worker execution",
        "Provider failover",
        "Cross-provider recovery",
        "Cross-provider independent review",
        "Concurrent execution",
        "Cleanup after integration",
        "Deterministic validation",
        "Global invocation",
    ):
        assert required in public_claims

    assert "GitHub delivery | **DRY-RUN ONLY**" in readme
    assert "Antigravity IDE runtime | **NOT PROVEN / UNSUPPORTED**" in readme
    assert "SELF_HOSTING_PROVEN" in evidence and "not claimed" in evidence.lower()
    assert "cross-provider independent final verification is\n> still pending" not in readme
    assert "Cross-provider independent review execution when provider runtimes are available" not in roadmap


# ---------------------------------------------------------------------------
# New behavioral coverage: actually exercise the described scenarios.
# ---------------------------------------------------------------------------


def test_public_dogfood_command_outlines_only_reference_registered_cli_commands():
    """Every `stagemesh ...` line in command_outline must be a real, currently
    registered CLI invocation (verified against `--help` output), or a plain
    prose fallback describing a non-CLI mechanism."""
    suite = _load_yaml(SUITE_PATH)
    known_top_level = {
        "init",
        "agent",
        "doctor",
        "upgrade",
        "project",
        "continue",
        "run",
        "claim",
        "review-claim",
        "heartbeat",
        "checkpoint",
        "objective",
        "workers",
        "events",
        "routing",
        "watcher",
        "status",
        "list",
        "pause",
        "drain",
        "resume",
        "recover-expired",
        "block",
        "fail",
        "request-input",
        "provide-input",
        "recover-review-environment",
        "recover-execution-retry",
    }

    for scenario in suite["scenarios"]:
        for line in scenario["command_outline"]:
            if not line.startswith("stagemesh "):
                # Prose description of a non-CLI mechanism (e.g. automatic cleanup).
                continue
            tokens = line[len("stagemesh ") :].split()
            first = tokens[0]
            if first.startswith('"') or first.startswith("Continue"):
                # Documented natural-language phrase form of `continue`.
                continue
            assert first in known_top_level, f"unregistered CLI command in {scenario['id']}: {line}"

    # Spot-check the exact fixed lines that used to be invalid.
    single = next(s for s in suite["scenarios"] if s["id"] == "single-agent-demo")
    assert any(
        "objective create" in line and "--title" in line for line in single["command_outline"]
    )
    assert not any("cleanup --dry-run" in line for line in single["command_outline"])

    staged = next(s for s in suite["scenarios"] if s["id"] == "staged-independent-review")
    assert not any(line.startswith("stagemesh github ") for line in staged["command_outline"])
    assert any("--github" in line and "--dry-run" in line for line in staged["command_outline"])


def test_objective_create_command_outline_actually_parses_and_runs(tmp_path):
    """Prove `stagemesh objective create ... --title ... --review-policy ...`
    (the documented single-agent-demo command_outline line) really parses and
    creates a task, using a disposable sqlite DB - no network, no credentials."""
    db_path = tmp_path / "acceptance.db"
    env = dict(os.environ)
    env["BUILD_COORDINATOR_DATABASE_URL"] = f"sqlite:///{db_path}"
    env.pop("BUILD_COORDINATOR_RESULT_DIR", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build_coordinator",
            "objective",
            "create",
            "DEMO-001",
            "--title",
            "Demo public dogfood task",
            "--review-policy",
            "SELF",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = result.stdout.strip()
    assert '"task_id": "DEMO-001"' in payload
    assert '"state": "READY"' in payload

    # Confirm the previously-invalid invocation (missing --title) is rejected,
    # matching the SystemExit guard in build_coordinator/cli.py::_objective_create.
    bad = subprocess.run(
        [
            sys.executable,
            "-m",
            "build_coordinator",
            "objective",
            "create",
            "DEMO-BAD",
            "--review-policy",
            "SELF",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0
    assert "--title is required" in bad.stderr


def test_single_agent_demo_reaches_done_with_matching_identity_and_cleanup(tmp_path):
    """Single-agent demo: run a builder-capable FakeExecutor worker through a
    full builder/reviewer/integration cycle to DONE, assert the structured
    execution result identity matches configuration, and confirm the real
    cleanup helper actually removes the integrated task branch."""
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-a", "BUILDER", adapter="fake", provider="provider-a"),
            WorkerConfig("reviewer-1", "REVIEWER", adapter="fake", provider="provider-a"),
            WorkerConfig("integration-1", "INTEGRATION", adapter="fake", provider="provider-a"),
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
        upsert_task(session, _task("DEMO-001", review_policy="SELF"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    for _ in range(6):
        runner.run_once()

    with SessionLocal() as session:
        task = session.get(BuildTask, "DEMO-001")
        assert task.state == "DONE"
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "DEMO-001")
            .order_by(BuildRunnerExecution.execution_id)
        ).all()
        by_role = {execution.role: execution for execution in executions}

    assert by_role["BUILDER"].worker_id == "builder-a"
    assert by_role["BUILDER"].provider == "provider-a"
    assert by_role["BUILDER"].status == "SUCCEEDED"
    assert by_role["INTEGRATION"].worker_id == "integration-1"
    assert by_role["INTEGRATION"].status == "SUCCEEDED"

    # Real cleanup mechanism: a `stagemesh/<task_id>` branch fully merged into
    # main is removed; one that is not fully integrated is refused.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "branch", "-M", "main")

    _git(repo, "checkout", "-b", "stagemesh/DEMO-001")
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature work")
    feature_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "-m", "integrate", "stagemesh/DEMO-001")

    removed, detail = cleanup_task_branch(
        repo, "stagemesh/DEMO-001", main_ref="main", reviewed_sha=feature_sha
    )
    assert removed, detail
    branches = _git(repo, "branch", "--list", "stagemesh/DEMO-001")
    assert branches == ""

    # A branch that is not yet integrated must be refused, never force-deleted.
    _git(repo, "checkout", "-b", "stagemesh/DEMO-002")
    (repo / "other.txt").write_text("wip\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "not integrated yet")
    unmerged_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    refused, reason = cleanup_task_branch(
        repo, "stagemesh/DEMO-002", main_ref="main", reviewed_sha=unmerged_sha
    )
    assert refused is False
    assert "not fully integrated" in reason
    assert _git(repo, "branch", "--list", "stagemesh/DEMO-002") != ""


def test_staged_independent_review_uses_distinct_workers_and_matched_sha():
    """Staged/independent review: builder and reviewer are different worker
    ids under an INDEPENDENT review_policy, and the reviewed SHA is captured
    and matches before integration proceeds."""
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
            ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "feature-sha-2"})
        ]),
        "reviewer-1": FakeExecutor([
            ExecutionObservation(
                "SUCCEEDED",
                result_data={
                    "reviewed_feature_sha": "feature-sha-2",
                    "review": {"verdict": "GREEN", "ready_for_integration": True},
                },
            )
        ]),
        "integration-1": FakeExecutor([ExecutionObservation("SUCCEEDED")]),
    }
    with SessionLocal() as session:
        upsert_task(session, _task("DEMO-002", review_policy="INDEPENDENT"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    for _ in range(6):
        runner.run_once()

    with SessionLocal() as session:
        assert session.get(BuildTask, "DEMO-002").state == "DONE"
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "DEMO-002")
            .order_by(BuildRunnerExecution.execution_id)
        ).all()
        by_role = {execution.role: execution for execution in executions}

    assert by_role["BUILDER"].worker_id != by_role["REVIEWER"].worker_id
    assert by_role["REVIEWER"].reviewed_feature_sha == "feature-sha-2"


def test_high_risk_governance_rejects_self_review():
    """High-risk governance: a review policy requiring independence rejects
    the implementer from also claiming the reviewer role (self-review)."""
    with SessionLocal() as session:
        upsert_task(session, _task("DEMO-GOV", review_policy="TWO_REVIEWERS", risk_level="HIGH"))
        claim_task(session, ClaimRequest("DEMO-GOV", worker_id="builder-a"))
        for state in ("IN_PROGRESS", "VALIDATING", "REVIEW_READY"):
            transition_task(session, "DEMO-GOV", state)
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(CoordinatorPolicyError):
            claim_review(session, ClaimRequest("DEMO-GOV", worker_id="builder-a"))

    # A distinct worker is allowed to claim the review.
    with SessionLocal() as session:
        review_claim = claim_review(session, ClaimRequest("DEMO-GOV", worker_id="reviewer-1"))
        assert review_claim.worker_id == "reviewer-1"


def test_provider_fallback_recovery_preserves_checkpoint_and_history():
    """Provider fallback / controlled interruption+recovery: an active claim
    is expired via recover_expired and a replacement provider/worker can
    resume, with checkpoint and event history surviving the handoff."""
    executors = {
        "builder-provider-a": FakeExecutor([
            ExecutionObservation("FAILED", result_data={"provider_failure": "UNAVAILABLE"})
        ]),
        "builder-provider-b": FakeExecutor([
            ExecutionObservation("SUCCEEDED", result_data={"feature_sha": "feature-sha-3"})
        ]),
    }
    config = RunnerConfig(
        workers=(
            WorkerConfig("builder-provider-a", "BUILDER", adapter="fake", provider="provider-a"),
            WorkerConfig("builder-provider-b", "BUILDER", adapter="fake", provider="provider-b"),
        ),
        result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"),
    )
    with SessionLocal() as session:
        upsert_task(session, _task("DEMO-FALLBACK"))
        session.commit()

    runner = BuildRunner(SessionLocal, config=config, executors=executors, git=FakeGit())
    launch1 = runner.run_once()
    assert len(launch1.launched) == 1

    with SessionLocal() as session:
        claim = session.scalar(
            select(BuildTaskClaim)
            .where(BuildTaskClaim.task_id == "DEMO-FALLBACK")
            .where(BuildTaskClaim.worker_id == "builder-provider-a")
            .where(BuildTaskClaim.status == "ACTIVE")
        )
        assert claim is not None
        checkpoint(
            session,
            claim.claim_id,
            worker_id="builder-provider-a",
            data=CheckpointInput(
                current_step="implemented reusable helper",
                completed_work=["demo_helper.py"],
                remaining_work=["run validation"],
                files_changed=["demo_helper.py"],
                current_head_sha="checkpoint-sha-3",
            ),
        )
        session.commit()

    recovered = runner.run_once()  # observes FAILED, recovers claim
    assert recovered.observed == ["DEMO-FALLBACK"]

    with SessionLocal() as session:
        resume_context = get_resume_context(session, "DEMO-FALLBACK")
        assert resume_context.previous_worker_id == "builder-provider-a"
        assert resume_context.completed_work == ("demo_helper.py",)
        assert resume_context.remaining_work == ("run validation",)

    launch2 = runner.run_once()  # dispatches remaining candidate: builder-provider-b
    assert len(launch2.launched) == 1

    runner.run_once()  # observes builder-provider-b SUCCEEDED

    with SessionLocal() as session:
        executions = session.scalars(
            select(BuildRunnerExecution)
            .where(BuildRunnerExecution.task_id == "DEMO-FALLBACK")
            .order_by(BuildRunnerExecution.execution_id)
        ).all()
        events = session.scalars(
            select(BuildTaskEvent).where(BuildTaskEvent.task_id == "DEMO-FALLBACK")
        ).all()
        checkpoints = session.scalars(
            select(BuildTaskCheckpoint).where(BuildTaskCheckpoint.task_id == "DEMO-FALLBACK")
        ).all()

    assert [execution.worker_id for execution in executions] == [
        "builder-provider-a",
        "builder-provider-b",
    ]
    assert executions[1].status == "SUCCEEDED"
    assert [(row.worker_id, row.current_step) for row in checkpoints] == [
        ("builder-provider-a", "implemented reusable helper")
    ]
    assert checkpoints[0].completed_work == ["demo_helper.py"]
    assert checkpoints[0].current_head_sha == "checkpoint-sha-3"
    assert len(events) > 0  # history is preserved, not wiped by the handoff


def test_parallelism_two_live_builder_claims_with_capacity():
    """Parallelism: with capacity >1 and two independently claimable tasks,
    two builder claims can be live/launched in the same cycle."""
    workers = (WorkerConfig("builder-1", "BUILDER", adapter="fake", max_concurrency=2),)
    with SessionLocal() as session:
        upsert_task(session, _task("PAR-1"))
        upsert_task(session, _task("PAR-2"))
        session.commit()

    config = RunnerConfig(workers=workers, result_dir=os.getenv("BUILD_COORDINATOR_RESULT_DIR"))
    runner = BuildRunner(SessionLocal, config=config, git=FakeGit())
    result = runner.run_once()
    assert len(result.launched) == 2

    with SessionLocal() as session:
        active = session.scalars(
            select(BuildTaskClaim).where(BuildTaskClaim.status == "ACTIVE")
        ).all()
        assert len(active) == 2


def test_global_capacity_batches_splits_bounded_capacity_across_projects():
    """Multi-project / global invocation: `_global_capacity_batches` (used by
    `stagemesh continue --all --capacity N`) allocates bounded capacity across
    registered project backlogs, one slot per project per batch."""

    class _FakeProject:
        def __init__(self, project_id: str, concurrency: int):
            self.project_id = project_id
            self.concurrency = concurrency

    projects = [_FakeProject("proj-a", 2), _FakeProject("proj-b", 1)]

    batches = _global_capacity_batches(projects, capacity=2)
    assert batches == [{"proj-a": 1, "proj-b": 1}]

    batches_unbounded = _global_capacity_batches(projects, capacity=None)
    assert batches_unbounded == [{"proj-a": 2, "proj-b": 1}]

    with pytest.raises(Exception):
        _global_capacity_batches(projects, capacity=0)


def test_github_outbound_dry_run_stays_local_no_network():
    """GitHub delivery dry-run: `_optional_task_source(force=True, dry_run=True)`
    is the mechanism behind `stagemesh continue --once --github --dry-run`.
    With no GitHub task-source configured for the disposable demo project it
    must degrade to "no source" without attempting any network call."""
    from build_coordinator.project.commands import _optional_task_source
    from build_coordinator.project.definition import ProjectDefinition

    project = ProjectDefinition(
        root=Path("."),
        project_id="demo-project",
        name="demo-project",
        task_sources={},
        concurrency=1,
    )

    source, diagnostics = _optional_task_source(project, force=True, dry_run=True)

    assert source is None
    assert isinstance(diagnostics, list)
