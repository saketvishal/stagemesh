"""Real coding-agent end-to-end validation (SM-012).

Every parallel run before this task used a scripted stand-in worker
(``tests/_scripted_worker.py``). This module proves the SubprocessExecutor
adapter contract against a real, operator-installed coding-agent CLI:
prompt delivery, a structured result file, commits on the task branch, and
review/integration hand-off on a disposable scratch repository.

The live scenario (``test_real_agent_builder_reviewer_integration_on_scratch_repo``)
routes entirely through the real project mechanism -- it copies
``examples/public_dogfood/real_agent_end_to_end_demo.yaml`` into a scratch
repository's ``.stagemesh/project.yaml`` and loads it with
``build_coordinator.project.definition.load_project`` +
``build_coordinator.project.runtime.build_runner_config``, the same functions
StageMesh itself uses to turn worker templates into ``WorkerConfig`` objects.
Nothing here hand-picks a worker id, an executor command, or a worktree: the
resolved ``WorkerConfig.command`` (StageMesh's own agent wrapper, which in
turn drives the real ``claude`` CLI found on PATH) is what gets executed for
BUILDER and REVIEWER. INTEGRATION resolves to StageMesh's own real (not
scripted) git integrator -- merging an already-reviewed commit is mechanical
by design and does not use a model -- so it drives a real `git merge --no-ff`
against the scratch repository instead of a third coding-agent subprocess.

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
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.git_integrator import GitIntegrationExecutor
from build_coordinator.execution.subprocess_executor import SubprocessExecutor
from build_coordinator.project.definition import load_project
from build_coordinator.project.runtime import build_runner_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_MANIFEST = REPO_ROOT / "examples" / "public_dogfood" / "real_agent_end_to_end_demo.yaml"
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence" / "real_agent_runs" / "SM-012"

REVIEWER_VERDICT_INSTRUCTIONS = (
    "\n\nEnd your reply with ONE fenced ```json block, exactly this shape:\n"
    '{"verdict": "GREEN", "findings": [], "ready_for_integration": true}\n'
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


def _run_role(
    *,
    executor: SubprocessExecutor,
    task_id: str,
    role: str,
    worker_id: str,
    worktree: Path,
    result_path: Path,
    prompt: str,
    extra_env: dict[str, str],
    reviewed_feature_sha: str | None = None,
):
    launch = ExecutionLaunch(
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        provider="claude",
        worktree_path=str(worktree),
        branch_name=f"task/{task_id}",
        prompt=prompt,
        execution_id=f"exec-{role.lower()}-{task_id}",
        result_path=str(result_path),
        reviewed_feature_sha=reviewed_feature_sha,
        extra_env=extra_env,
    )
    handle = executor.launch(launch)
    return _wait(executor, handle.execution_id, timeout=300.0)


def _record_evidence(role: str, worker, observation, result_path: Path) -> Path:
    """Persist durable evidence for a single real-agent execution.

    Required by SM-012: provider, runtime, exit code, and the result file
    itself must be recorded durably (under docs/evidence/), not only
    asserted in-process or left under a pytest tmp_path that gets cleaned up.
    """
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "role": role,
        "worker_id": worker.worker_id,
        "provider": worker.provider,
        "runtime": worker.runtime,
        "command": list(worker.command),
        "exit_code": observation.exit_code,
        "status": observation.status,
        "result_path": str(result_path),
        "result_file_present": result_path.is_file(),
        "result_data": observation.result_data,
    }
    out_path = EVIDENCE_DIR / f"{role.lower()}-evidence.json"
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="a real, authenticated `claude` CLI must be on PATH to run the live SM-012 acceptance scenario",
)
def test_real_agent_builder_reviewer_integration_on_scratch_repo(tmp_path: Path):
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)

    project = _load_demo_project(repo)
    config = build_runner_config(project, dry_run=False)

    builder_worker = _worker_for_role(config, "BUILDER")
    reviewer_worker = _worker_for_role(config, "REVIEWER")
    integration_worker = _worker_for_role(config, "INTEGRATION")
    assert reviewer_worker.worker_id != builder_worker.worker_id, "reviewer must be an independent worker id"
    assert builder_worker.adapter == "subprocess"
    assert reviewer_worker.adapter == "subprocess"

    log_dir = tmp_path / "logs"
    task_id = "SM-012-REAL"

    _git("checkout", "-q", "-b", f"task/{task_id}", cwd=repo)

    # --- BUILDER ---
    builder_executor = SubprocessExecutor(list(builder_worker.command), log_dir=log_dir)
    builder_result_path = tmp_path / "builder-result.json"
    observation = _run_role(
        executor=builder_executor,
        task_id=task_id,
        role="BUILDER",
        worker_id=builder_worker.worker_id,
        worktree=repo,
        result_path=builder_result_path,
        prompt=(
            "Create a file named NOTES.md containing exactly one line: validated\n"
            "Do not run git commit, checkout, reset or branch commands yourself."
        ),
        extra_env=dict(builder_worker.env),
    )
    _record_evidence("BUILDER", builder_worker, observation, builder_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    assert builder_result_path.is_file()
    builder_result = json.loads(builder_result_path.read_text(encoding="utf-8"))
    assert builder_result["role"] == "BUILDER"
    feature_sha = builder_result["feature_sha"]

    commit_log = _git("log", "--format=%H", cwd=repo).split()
    assert feature_sha in commit_log, "builder must produce a real commit on the task branch"
    assert (repo / "NOTES.md").read_text(encoding="utf-8").strip() == "validated"

    # --- REVIEWER (independent worker id) ---
    reviewer_executor = SubprocessExecutor(list(reviewer_worker.command), log_dir=log_dir)
    reviewer_result_path = tmp_path / "reviewer-result.json"
    observation = _run_role(
        executor=reviewer_executor,
        task_id=task_id,
        role="REVIEWER",
        worker_id=reviewer_worker.worker_id,
        worktree=repo,
        result_path=reviewer_result_path,
        prompt=(
            f"Review commit {feature_sha}, which is checked out (detached) in this worktree. "
            "It should add NOTES.md containing the line 'validated'. Judge whether that is correct."
            + REVIEWER_VERDICT_INSTRUCTIONS
        ),
        extra_env=dict(reviewer_worker.env),
        reviewed_feature_sha=feature_sha,
    )
    _record_evidence("REVIEWER", reviewer_worker, observation, reviewer_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    reviewer_result = json.loads(reviewer_result_path.read_text(encoding="utf-8"))
    assert reviewer_result["reviewed_feature_sha"] == feature_sha
    assert reviewer_result["ready_for_integration"] is True

    # --- INTEGRATION ---
    # INTEGRATION is StageMesh's own deterministic merge step (the
    # SCM_OPERATOR role): merging an already-reviewed commit is mechanical
    # and does not need a model, so `build_runner_config` resolves the
    # `adapter: builtin-git` template to `GitIntegrationExecutor`, not a
    # coding-agent subprocess. This is still a real execution against the
    # real scratch repository (a real `git merge --no-ff` that advances
    # `main`), driven by the same project.yaml worker template as builder
    # and reviewer, not a scripted stand-in.
    assert integration_worker.adapter == "builtin-git"
    integration_executor = GitIntegrationExecutor(main_ref=project.main_ref)
    integration_result_path = tmp_path / "integration-result.json"
    integration_launch = ExecutionLaunch(
        task_id=task_id,
        role="INTEGRATION",
        worker_id=integration_worker.worker_id,
        provider=integration_worker.provider,
        worktree_path=str(repo),
        branch_name=f"task/{task_id}",
        prompt="",
        execution_id=f"exec-integration-{task_id}",
        result_path=str(integration_result_path),
        reviewed_feature_sha=feature_sha,
    )
    handle = integration_executor.launch(integration_launch)
    observation = integration_executor.poll(handle.execution_id)
    _record_evidence("INTEGRATION", integration_worker, observation, integration_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    assert integration_result_path.is_file()
    integration_result = json.loads(integration_result_path.read_text(encoding="utf-8"))
    assert integration_result["reviewed_feature_sha"] == feature_sha
    assert integration_result["merge_commit_sha"]
    main_log = _git("log", "--format=%H", "main", cwd=repo).split()
    assert feature_sha in main_log, "integration must merge the reviewed commit onto main"
    assert integration_result["merge_commit_sha"] in main_log
    assert (repo / "NOTES.md").is_file()


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
