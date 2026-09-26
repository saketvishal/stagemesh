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
scratch repository's ``.stagemesh/project.yaml`` (substituting this
machine's real Python interpreter and the path to
``tests/_real_agent_integration_worker.py`` for the manifest's literal
``PYTHON_EXECUTABLE``/``INTEGRATION_AGENT_SCRIPT`` placeholders -- see the
comment in that manifest for why this is a plain path substitution, not
``${VAR}`` shell-style expansion), loads it with
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
and launches/polls the real executor itself.

All three roles resolve to ``adapter: subprocess`` and are real coding-agent
CLI executions. BUILDER and REVIEWER go through StageMesh's own agent
wrapper (``build_coordinator/agents/wrapper.py``), which drives the real
``claude`` CLI found on PATH. INTEGRATION goes through
``tests/_real_agent_integration_worker.py`` instead of that wrapper, because
``wrapper.py`` never special-cases the INTEGRATION role -- ``role_key`` is
only remapped for REMEDIATION, so its generic builder prompt tells the agent
not to touch git history at all, and its result derivation reports a
`feature_sha` from HEAD, never a `merge_commit_sha`. Editing
`build_coordinator/agents/wrapper.py` is outside this task's allowed edit
paths (`examples/`, `docs/`, `tests/` only), so the INTEGRATION worker
template instead points straight at an INTEGRATION-aware operator script
that drives the real `claude` CLI with permission to run
`git checkout`/`git merge --no-ff`, then -- following the same "an agent's
self-report is never the lifecycle result" discipline as `wrapper.py` --
independently verifies from git state that the merge really happened before
ever reporting `status: SUCCEEDED`. Because the adapter is `subprocess`
rather than `builtin-git`, `BuildRunner._integration_succeeded`
(`build_coordinator/runner/orchestrator.py`) requires `auto_push_allowed`
before completing the task; this test sets
`BUILD_COORDINATOR_AUTO_PUSH_ALLOWED=true` for the scratch scenario (the
scratch repo has no upstream remote configured, so nothing is actually
pushed anywhere) and independently re-verifies from `git log` that the
reported `merge_commit_sha` really is reachable from `main`, so the test's
own assertions -- not just the orchestrator's transition -- are the proof.

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
INTEGRATION_AGENT_SCRIPT = REPO_ROOT / "tests" / "_real_agent_integration_worker.py"
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "real_agent_runs" / "SM-012"

_PROJECT_ENV_KEYS = (
    "BUILD_COORDINATOR_REPO_ROOT",
    "BUILD_COORDINATOR_DATA_DIR",
    "BUILD_COORDINATOR_MAX_ACTIVE_BUILDERS",
    "BUILD_COORDINATOR_DATABASE_URL",
    "BUILD_COORDINATOR_ALLOWED_WORKSPACE_ROOTS",
    "BUILD_COORDINATOR_AUTO_PUSH_ALLOWED",
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
    manifest = DEMO_MANIFEST.read_text(encoding="utf-8")
    manifest = manifest.replace("PYTHON_EXECUTABLE", Path(sys.executable).as_posix())
    manifest = manifest.replace("INTEGRATION_AGENT_SCRIPT", INTEGRATION_AGENT_SCRIPT.as_posix())
    (project_dir / "project.yaml").write_text(manifest, encoding="utf-8")
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


def _runtime_from_execution_result(execution: BuildRunnerExecution, command: tuple[str, ...]) -> str:
    if isinstance(execution.result_data, dict) and execution.result_data.get("runtime"):
        return str(execution.result_data["runtime"])
    if "--runtime" in command:
        runtime_index = command.index("--runtime") + 1
        if runtime_index < len(command):
            return command[runtime_index]
    return "unknown"


def _record_evidence(role: str, execution: BuildRunnerExecution, command: tuple[str, ...]) -> Path:
    """Persist durable evidence for a single real, managed execution.

    Required by SM-012: provider, runtime, subprocess command, exit code, and
    the result file itself must be recorded durably (under docs/evidence/),
    not only asserted in-process or left under a pytest tmp_path that gets
    cleaned up. The status/provider/adapter/exit_code values come straight
    off the `BuildRunnerExecution` row that `BuildRunner` itself wrote; the
    command comes from the same `WorkerConfig` the orchestrator resolved and
    launched (execution rows do not persist argv, so it is captured here
    from the worker the test already holds a reference to, not invented).
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
        "runtime": _runtime_from_execution_result(execution, command),
        "command": list(command),
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
        # INTEGRATION resolves to `adapter: subprocess`, not `builtin-git`, so
        # `BuildRunner._integration_succeeded` requires auto-push approval
        # before completing the task. The scratch repo has no upstream
        # remote configured, so this never actually pushes anything anywhere
        # -- it only lets the orchestrator transition a real subprocess
        # integration to DONE, matching the policy a real project with a
        # third-party INTEGRATION worker would need to set.
        os.environ["BUILD_COORDINATOR_AUTO_PUSH_ALLOWED"] = "true"
        apply_project_environment(project)
        config = build_runner_config(project, dry_run=False)

        builder_worker = next(w for w in config.workers if w.role == "BUILDER")
        reviewer_worker = next(w for w in config.workers if w.role == "REVIEWER")
        integration_worker = next(w for w in config.workers if w.role == "INTEGRATION")
        assert reviewer_worker.worker_id != builder_worker.worker_id, "reviewer must be an independent worker id"
        assert builder_worker.adapter == "subprocess"
        assert reviewer_worker.adapter == "subprocess"
        assert integration_worker.adapter == "subprocess"
        assert integration_worker.command, "INTEGRATION must launch a real subprocess command, not an empty argv"
        assert integration_worker.command[-1] == INTEGRATION_AGENT_SCRIPT.as_posix()

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

        workers_by_role = {
            "BUILDER": builder_worker,
            "REVIEWER": reviewer_worker,
            "INTEGRATION": integration_worker,
        }
        for execution in executions:
            worker = workers_by_role.get(execution.role)
            command = tuple(worker.command) if worker is not None else ()
            _record_evidence(execution.role, execution, command)

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
        assert by_role["INTEGRATION"].adapter == "subprocess"
        assert by_role["INTEGRATION"].exit_code == 0

        evidence = {
            role: json.loads((EVIDENCE_DIR / f"{role.lower()}-evidence.json").read_text(encoding="utf-8"))
            for role in ("BUILDER", "REVIEWER", "INTEGRATION")
        }
        for role, record in evidence.items():
            assert record["provider"] == "anthropic"
            assert record["adapter"] == "subprocess"
            assert record["runtime"] == "claude"
            assert record["exit_code"] == 0
            assert record["result_file_present"] is True
            assert record["result_data"]["status"] == "SUCCEEDED"
            assert record["command"], f"{role} evidence must preserve the resolved worker command"

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
