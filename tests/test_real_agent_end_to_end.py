"""Real coding-agent end-to-end validation (SM-012).

Every parallel run before this task used a scripted stand-in worker
(``tests/_scripted_worker.py``). This module proves the SubprocessExecutor
adapter contract against a real, operator-installed coding-agent CLI:
prompt delivery, a structured result file, commits on the task branch, and
review/integration hand-off on a disposable scratch repository -- driven
entirely through StageMesh's own *managed* execution path, not by hand
constructing executors.

The live scenario (``test_real_agent_builder_reviewer_integration_on_scratch_repo``)
copies ``examples/public_dogfood/real_agent_end_to_end_demo.yaml`` into a
scratch repository's ``.stagemesh/project.yaml``, loads it with
``build_coordinator.project.definition.load_project`` +
``build_coordinator.project.runtime.build_runner_config`` (the same functions
StageMesh itself uses to turn worker templates into ``WorkerConfig``
objects), creates a task row with ``build_coordinator.service.upsert_task``,
and then drives the whole BUILDER -> REVIEWER -> INTEGRATION lifecycle by
repeatedly calling ``build_coordinator.runner.orchestrator.BuildRunner.run_once()``
against a real ``RealGit`` backend -- exactly the pattern used in
``tests/test_runner.py`` and ``tests/test_public_dogfood_acceptance.py``.
Nothing in this test hand-picks a worker id, an executor instance, or a
worktree path: ``BuildRunner`` resolves the worker for each role, provisions
its worktree via ``build_coordinator/runner/worktree.py:ensure_worktree``,
and launches/polls the real executor itself. BUILDER and REVIEWER resolve to
``adapter: subprocess`` (StageMesh's own agent wrapper, which drives the real
``claude`` CLI found on PATH). INTEGRATION resolves to StageMesh's own real
(not scripted) ``builtin-git`` executor.

INTEGRATION is deliberately NOT routed through a third coding-agent
subprocess. That was investigated for this task (see the "why not
`adapter: subprocess` for INTEGRATION" note in
``docs/evidence/REAL_AGENT_EXECUTION_ACCEPTANCE.md`` for the full citation
trail) and found to be actively unsafe with the current wrapper: nothing in
``build_coordinator/agents/wrapper.py::main`` special-cases the INTEGRATION
role -- ``role_key`` is only remapped for REMEDIATION, so INTEGRATION prompts
fall through to the generic builder prompt, which explicitly tells the agent
"Do NOT run git commit, push, checkout, reset, rebase or branch commands" and
then has the wrapper itself derive `feature_sha` from HEAD, never a
`merge_commit_sha`. Worse, `BuildRunner._integration_succeeded` only demands
`merge_commit_sha` when `execution.adapter == "builtin-git"`
(``build_coordinator/runner/orchestrator.py`` around line 1578); for any
other adapter it transitions the task straight to DONE once `auto_push_allowed`
policy is satisfied, with no check that any merge onto main ever happened.
Routing INTEGRATION through `adapter: subprocess` today would therefore mark
tasks DONE without a real merge -- a regression, not a stronger proof. Fixing
this would require editing `build_coordinator/agents/wrapper.py` and
`build_coordinator/runner/orchestrator.py`, which are out of this task's
allowed edit paths (`examples/`, `docs/`, `tests/` only). Given that, keeping
INTEGRATION on the real, non-scripted `builtin-git` executor -- which really
does execute `git merge --no-ff` against the scratch repository and only
reports `merge_commit_sha` when that merge actually happened -- is the
honest choice; it is a real execution against the real scratch repository,
just not a third LLM subprocess.

The scenario is opt-in: it only runs when a real, authenticated `claude` CLI
is discoverable on PATH, matching the same pattern documented for the Claude
Code runtime in CODEX_ACCEPTANCE.md ("Readiness on an operator machine still
requires `stagemesh agent setup` to complete a live headless probe."). It is
skipped everywhere else (CI, sandboxed task execution without a nested,
billed coding-agent CLI available) so this suite never silently depends on
live credentials.

The second requirement of SM-012 -- a malformed or missing result file must
be reported as a typed failure, never silently accepted -- is deterministic
and does not need a real agent, so it always runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
from build_coordinator.models import BuildRunnerExecution, BuildTask
from build_coordinator.project.definition import load_project
from build_coordinator.project.runtime import apply_project_environment, build_runner_config
from build_coordinator.runner.git_safety import RealGit
from build_coordinator.runner.orchestrator import BuildRunner
from build_coordinator.service import upsert_task
from build_coordinator.types import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = REPO_ROOT / "examples" / "public_dogfood" / "real_agent_end_to_end_demo.yaml"
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "real_agent_runs" / "SM-012"

_PROJECT_ENV_KEYS = (
    "BUILD_COORDINATOR_REPO_ROOT",
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS",
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_scratch_repo(root: Path) -> None:
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.name", "Scratch Repo", cwd=root)
    _git("config", "user.email", "scratch@example.invalid", cwd=root)
    (root / "README.md").write_text("scratch repo for SM-012 real-agent validation\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)


def _load_demo_project(scratch_repo: Path):
    """Materialize the example manifest as `<scratch_repo>/.stagemesh/project.yaml`.

    Copying the actual example file (not a hand-written stand-in) is what
    proves the manifest in examples/ is really loadable through the project
    mechanism, per SM-012's requirement that only project.yaml worker
    templates plus operator environment choose the command.
    """
    project_dir = scratch_repo / ".stagemesh"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.yaml").write_text(DEMO_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    return load_project(scratch_repo)


def _worker_for_role(config, role: str):
    for worker in config.workers:
        if worker.role == role:
            return worker
    raise AssertionError(f"no {role} worker resolved from project.yaml worker templates")


def _wait(executor: SubprocessExecutor, execution_id: str, *, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    observation = executor.poll(execution_id)
    while observation.status == "RUNNING":
        if time.monotonic() > deadline:
            raise AssertionError(f"execution {execution_id} did not finish within {timeout}s")
        time.sleep(0.02)
        observation = executor.poll(execution_id)
    return observation


def _record_evidence(role: str, execution: BuildRunnerExecution) -> Path:
    """Persist durable evidence for a single real, managed execution.

    Required by SM-012: provider, runtime, exit code, and the result file
    itself must be recorded durably (under docs/evidence/), not only
    asserted in-process or left under a pytest tmp_path that gets cleaned up.
    The values here come straight off the `BuildRunnerExecution` row that
    `BuildRunner` itself wrote -- not anything this test constructed.
    """
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    result_path = Path(execution.result_path) if execution.result_path else None
    record = {
        "role": role,
        "execution_id": execution.execution_id,
        "task_id": execution.task_id,
        "worker_id": execution.worker_id,
        "provider": execution.provider,
        "adapter": execution.adapter,
        "exit_code": execution.exit_code,
        "status": execution.status,
        "result_path": str(result_path) if result_path else None,
        "result_file_present": result_path.is_file() if result_path else False,
        "result_data": execution.result_data,
    }
    out_path = EVIDENCE_DIR / f"{role.lower()}-evidence.json"
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="a real, authenticated `claude` CLI must be on PATH to run the live SM-012 acceptance scenario",
)
def test_real_agent_builder_reviewer_integration_on_scratch_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)

    project = _load_demo_project(repo)

    # `apply_project_environment` mutates process-wide BUILD_COORDINATOR_* env
    # vars so RunnerConfig/BuildRunner/worktree provisioning resolve against
    # *this* scratch project, exactly as `stagemesh continue` does for a real
    # project. Snapshot and restore so this never leaks into other tests.
    previous_env = {key: os.environ.get(key) for key in _PROJECT_ENV_KEYS}
    try:
        apply_project_environment(project)
        config = build_runner_config(project, dry_run=False)

        builder_worker = next(w for w in config.workers if w.role == "BUILDER")
        reviewer_worker = next(w for w in config.workers if w.role == "REVIEWER")
        integration_worker = next(w for w in config.workers if w.role == "INTEGRATION")
        assert reviewer_worker.worker_id != builder_worker.worker_id, "reviewer must be an independent worker id"
        assert builder_worker.adapter == "subprocess"
        assert reviewer_worker.adapter == "subprocess"
        assert integration_worker.adapter == "builtin-git"

        # This test's own isolated coordinator DB (set up by tests/conftest.py)
        # is reused as-is: apply_project_environment only sets
        # BUILD_COORDINATOR_DATABASE_URL when it isn't already configured, so
        # the shared test-suite database keeps owning task/execution rows.
        Base.metadata.drop_all(bind=engine)
        initialize_schema()

        task_id = "SM-012-REAL"
        with SessionLocal() as session:
            upsert_task(
                session,
                TaskSpec(
                    task_id=task_id,
                    title="SM-012 real-agent smoke task",
                    description=(
                        "Create a file named NOTES.md at the repository root "
                        "containing exactly one line: validated"
                    ),
                    acceptance_criteria=[
                        "NOTES.md exists at the repository root",
                        "NOTES.md contains exactly one line: validated",
                    ],
                    review_policy="INDEPENDENT",
                ),
            )
            session.commit()

        runner = BuildRunner(SessionLocal, config=config, git=RealGit())

        deadline = time.monotonic() + 900.0
        terminal_states = {"DONE", "BLOCKED", "FAILED"}
        state = None
        while time.monotonic() < deadline:
            runner.run_once()
            with SessionLocal() as session:
                task = session.get(BuildTask, task_id)
                state = task.state if task is not None else None
            if state in terminal_states:
                break
            time.sleep(1.0)

        with SessionLocal() as session:
            task = session.get(BuildTask, task_id)
            executions = session.scalars(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == task_id)
                .order_by(BuildRunnerExecution.launched_at)
            ).all()
            # Detach for use after the session closes.
            session.expunge_all()

        for execution in executions:
            _record_evidence(execution.role, execution)

        by_role = {execution.role: execution for execution in executions}
        assert state == "DONE", (
            f"task did not reach DONE within the deadline (state={state}); "
            f"executions={[(e.role, e.status, e.human_escalation_type) for e in executions]}"
        )

        assert "BUILDER" in by_role and by_role["BUILDER"].status == "SUCCEEDED"
        assert "REVIEWER" in by_role and by_role["REVIEWER"].status == "SUCCEEDED"
        assert "INTEGRATION" in by_role and by_role["INTEGRATION"].status == "SUCCEEDED"
        assert by_role["BUILDER"].worker_id == builder_worker.worker_id
        assert by_role["REVIEWER"].worker_id == reviewer_worker.worker_id
        assert by_role["REVIEWER"].worker_id != by_role["BUILDER"].worker_id
        assert by_role["INTEGRATION"].worker_id == integration_worker.worker_id
        assert by_role["INTEGRATION"].adapter == "builtin-git"

        feature_sha = by_role["BUILDER"].result_data.get("feature_sha")
        assert feature_sha, by_role["BUILDER"].result_data
        merge_commit_sha = by_role["INTEGRATION"].result_data.get("merge_commit_sha")
        assert merge_commit_sha, by_role["INTEGRATION"].result_data

        main_log = _git("log", "--format=%H", project.main_ref, cwd=repo).split()
        assert feature_sha in main_log, "the builder's real commit must have been merged onto main"
        assert merge_commit_sha in main_log

        # NOTES.md is only guaranteed on the branch that was merged, not
        # necessarily on whichever worktree HEAD happens to be at rest; check
        # it out of the merge commit content instead of the working tree.
        show = subprocess.run(
            ["git", "show", f"{merge_commit_sha}:NOTES.md"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert show.returncode == 0, show.stderr
        assert show.stdout.strip() == "validated"
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_missing_result_file_from_real_agent_style_launch_is_typed_failure_not_silent_success(
    tmp_path: Path,
):
    """A CLI that exits 0 without writing a result file must fail closed."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)

    result_path = tmp_path / "result.json"
    executor = SubprocessExecutor([sys.executable, "-c", "pass"], log_dir=tmp_path / "logs")
    launch = ExecutionLaunch(
        task_id="SM-012-REAL",
        role="BUILDER",
        worker_id="real-agent-builder",
        provider="operator-configured",
        worktree_path=str(repo),
        branch_name="task/SM-012-REAL",
        prompt="never writes a result file",
        execution_id="exec-builder-missing",
        result_path=str(result_path),
    )
    handle = executor.launch(launch)
    observation = _wait(executor, handle.execution_id)

    assert not result_path.exists()
    assert observation.status == "FAILED"
    assert observation.result_data["failure_kind"] == "EXECUTOR_RESULT_INVALID_OR_MISSING"


def test_malformed_result_file_from_real_agent_style_launch_is_typed_failure_not_silent_success(
    tmp_path: Path,
):
    """A CLI that writes garbage instead of JSON must fail closed, not be accepted."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)

    result_path = tmp_path / "result.json"
    executor = SubprocessExecutor(
        [
            sys.executable,
            "-c",
            "import os; open(os.environ['BUILD_COORDINATOR_RESULT_PATH'], 'w').write('not json at all')",
        ],
        log_dir=tmp_path / "logs",
    )
    launch = ExecutionLaunch(
        task_id="SM-012-REAL",
        role="BUILDER",
        worker_id="real-agent-builder",
        provider="operator-configured",
        worktree_path=str(repo),
        branch_name="task/SM-012-REAL",
        prompt="writes a malformed result file",
        execution_id="exec-builder-malformed",
        result_path=str(result_path),
    )
    handle = executor.launch(launch)
    observation = _wait(executor, handle.execution_id)

    assert result_path.exists()
    assert observation.status == "FAILED"
    assert observation.result_data["failure_kind"] == "EXECUTOR_RESULT_INVALID_OR_MISSING"
    assert observation.result_data["detail"] == "subprocess wrote an invalid structured result file"
