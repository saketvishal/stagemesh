"""Real coding-agent end-to-end validation (SM-012).

Every parallel run before this task used a scripted stand-in worker
(``tests/_scripted_worker.py``). This module proves the SubprocessExecutor
adapter contract against a real, operator-supplied coding-agent CLI:
prompt delivery, a structured result file, commits on the task branch, and
review/integration hand-off on a disposable scratch repository.

The live scenario (``test_real_agent_builder_reviewer_integration_on_scratch_repo``)
is opt-in: it only runs when an operator points
``BUILD_COORDINATOR_REAL_AGENT_CLI`` at a real, authenticated coding-agent
CLI installed on the machine. It is skipped everywhere else (CI, sandboxed
task execution without shell/network access to spawn another billed agent)
so this suite never silently depends on live credentials. Only the worker
templates declared here plus that operator environment variable choose the
command; there is no per-run manual worktree or worker choice.

The second requirement of SM-012 -- a malformed or missing result file must
be reported as a typed failure, never silently accepted -- is deterministic
and does not need a real agent, so it always runs.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from build_coordinator.execution.base import ExecutionLaunch
from build_coordinator.execution.subprocess_executor import SubprocessExecutor

REAL_AGENT_CLI_ENV = "BUILD_COORDINATOR_REAL_AGENT_CLI"


def _init_scratch_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Scratch Repo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "scratch@example.invalid"], cwd=root, check=True)
    (root / "README.md").write_text("scratch repo for SM-012 real-agent validation\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def _launch_for_role(
    *,
    task_id: str,
    role: str,
    worker_id: str,
    worktree: Path,
    result_path: Path,
    prompt: str,
    reviewed_feature_sha: str | None = None,
) -> ExecutionLaunch:
    return ExecutionLaunch(
        task_id=task_id,
        role=role,
        worker_id=worker_id,
        provider="operator-configured",
        worktree_path=str(worktree),
        branch_name=f"task/{task_id}",
        prompt=prompt,
        execution_id=f"exec-{role.lower()}-{uuid4().hex[:8]}",
        result_path=str(result_path),
        reviewed_feature_sha=reviewed_feature_sha,
    )


def _record_evidence(evidence_dir: Path, role: str, observation, result_path: Path) -> Path:
    """Persist durable evidence for a single real-agent execution.

    Required by SM-012: provider, runtime, exit code, and the result file
    itself must be recorded, not just asserted in-process.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "role": role,
        "provider": "operator-configured",
        "runtime": os.environ.get(REAL_AGENT_CLI_ENV, ""),
        "exit_code": observation.exit_code,
        "status": observation.status,
        "result_path": str(result_path),
        "result_file_present": result_path.is_file(),
        "result_data": observation.result_data,
    }
    out_path = evidence_dir / f"{role.lower()}-evidence.json"
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


@pytest.mark.skipif(
    not os.environ.get(REAL_AGENT_CLI_ENV),
    reason=(
        f"set {REAL_AGENT_CLI_ENV} to a real, authenticated coding-agent CLI "
        "command to run the live SM-012 acceptance scenario"
    ),
)
def test_real_agent_builder_reviewer_integration_on_scratch_repo(tmp_path: Path):
    command = shlex.split(os.environ[REAL_AGENT_CLI_ENV])
    assert shutil.which(command[0]) is not None, f"{command[0]!r} is not on PATH"

    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "task/SM-012-REAL"], cwd=repo, check=True)

    evidence_dir = tmp_path / "evidence"
    log_dir = tmp_path / "logs"
    executor = SubprocessExecutor(command, log_dir=log_dir)

    builder_worker = "real-agent-builder"
    reviewer_worker = "real-agent-reviewer"
    integrator_worker = "real-agent-integrator"

    # --- BUILDER ---
    builder_result_path = tmp_path / "builder-result.json"
    builder_launch = _launch_for_role(
        task_id="SM-012-REAL",
        role="BUILDER",
        worker_id=builder_worker,
        worktree=repo,
        result_path=builder_result_path,
        prompt=(
            "Append a single line 'validated' to NOTES.md, commit it on the "
            "current branch, then write the required structured result JSON."
        ),
    )
    handle = executor.launch(builder_launch)
    observation = executor.poll(handle.execution_id)
    while observation.status == "RUNNING":
        observation = executor.poll(handle.execution_id)
    _record_evidence(evidence_dir, "BUILDER", observation, builder_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    assert builder_result_path.is_file()
    builder_result = json.loads(builder_result_path.read_text(encoding="utf-8"))
    assert builder_result["role"] == "BUILDER"
    feature_sha = builder_result["feature_sha"]

    commit_log = subprocess.run(
        ["git", "log", "--format=%H"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.split()
    assert feature_sha in commit_log, "builder must produce a real commit on the task branch"

    # --- REVIEWER (independent worker id) ---
    assert reviewer_worker != builder_worker
    reviewer_result_path = tmp_path / "reviewer-result.json"
    reviewer_launch = _launch_for_role(
        task_id="SM-012-REAL",
        role="REVIEWER",
        worker_id=reviewer_worker,
        worktree=repo,
        result_path=reviewer_result_path,
        prompt="Review the current branch and write the required structured result JSON.",
        reviewed_feature_sha=feature_sha,
    )
    handle = executor.launch(reviewer_launch)
    observation = executor.poll(handle.execution_id)
    while observation.status == "RUNNING":
        observation = executor.poll(handle.execution_id)
    _record_evidence(evidence_dir, "REVIEWER", observation, reviewer_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    reviewer_result = json.loads(reviewer_result_path.read_text(encoding="utf-8"))
    assert reviewer_result["reviewed_feature_sha"] == feature_sha
    assert reviewer_result["ready_for_integration"] is True

    # --- INTEGRATION ---
    integrator_result_path = tmp_path / "integration-result.json"
    integrator_launch = _launch_for_role(
        task_id="SM-012-REAL",
        role="INTEGRATION",
        worker_id=integrator_worker,
        worktree=repo,
        result_path=integrator_result_path,
        prompt="Merge the reviewed branch into main and write the required structured result JSON.",
        reviewed_feature_sha=feature_sha,
    )
    handle = executor.launch(integrator_launch)
    observation = executor.poll(handle.execution_id)
    while observation.status == "RUNNING":
        observation = executor.poll(handle.execution_id)
    _record_evidence(evidence_dir, "INTEGRATION", observation, integrator_result_path)

    assert observation.status == "SUCCEEDED", observation.result_data
    integration_result = json.loads(integrator_result_path.read_text(encoding="utf-8"))
    assert integration_result["reviewed_feature_sha"] == feature_sha
    assert integration_result["feature_sha"] == feature_sha


def test_missing_result_file_from_real_agent_style_launch_is_typed_failure_not_silent_success(
    tmp_path: Path,
):
    """A CLI that exits 0 without writing a result file must fail closed."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    _init_scratch_repo(repo)

    result_path = tmp_path / "result.json"
    executor = SubprocessExecutor([sys.executable, "-c", "pass"], log_dir=tmp_path / "logs")
    launch = _launch_for_role(
        task_id="SM-012-REAL",
        role="BUILDER",
        worker_id="real-agent-builder",
        worktree=repo,
        result_path=result_path,
        prompt="never writes a result file",
    )
    handle = executor.launch(launch)
    observation = executor.poll(handle.execution_id)

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
    launch = _launch_for_role(
        task_id="SM-012-REAL",
        role="BUILDER",
        worker_id="real-agent-builder",
        worktree=repo,
        result_path=result_path,
        prompt="writes a malformed result file",
    )
    handle = executor.launch(launch)
    observation = executor.poll(handle.execution_id)

    assert result_path.exists()
    assert observation.status == "FAILED"
    assert observation.result_data["failure_kind"] == "EXECUTOR_RESULT_INVALID_OR_MISSING"
    assert observation.result_data["detail"] == "subprocess wrote an invalid structured result file"
