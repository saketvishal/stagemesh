"""Scenario K (unknown process identity) and Scenario L (destructive git operations)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from autonomy_support import TASK, commit, decisions, git, init_repo, new_store

from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.recovery_policy import (
    GitOperation,
    GitOperationRequest,
    RecoveryPolicy,
    UnknownIdentityStrategy,
    decide_execution_recovery,
    decide_git_operation,
)
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, ExecutionStatus, Stage, TaskStatus
from stagemesh.execution import SubprocessExecutor
from stagemesh.process_identity import popen_identity
from stagemesh.workspaces import prepare_task_workspace, task_workspace

# --- Scenario K: UNKNOWN identity is never treated as dead -------------------------------------------------------------------------------------


def test_scenario_k_unknown_identity_is_never_released_and_defaults_to_fencing() -> None:
    decision = decide_execution_recovery(TASK, "exec-1", "UNKNOWN", age_seconds=10_000)
    assert decision.condition is Condition.EXECUTION_IDENTITY_UNKNOWN
    assert decision.action is Action.FENCE_AND_REPLACE_EXECUTION  # explicit recovery policy, not "assume it is dead"
    assert decision.action is not Action.RELEASE_DEAD_EXECUTION and not decision.requires_human
    assert decision.detail["never_treated_as_dead"] is True
    assert decision.detail["old_worktree_preserved"] is True and decision.detail["old_output_untrusted"] is True


def test_scenario_k_hold_policy_fails_closed_forever() -> None:
    policy = RecoveryPolicy(UnknownIdentityStrategy.HOLD)
    for age in (0, 1, 10**9):
        assert decide_execution_recovery(TASK, "e", "UNKNOWN", age_seconds=age, policy=policy).action is Action.HOLD_FAIL_CLOSED


def test_scenario_k_fence_policy_waits_out_the_hold_window_first() -> None:
    policy = RecoveryPolicy(UnknownIdentityStrategy.FENCE, hold_seconds=300)
    assert decide_execution_recovery(TASK, "e", "UNKNOWN", age_seconds=299, policy=policy).action is Action.HOLD_FAIL_CLOSED
    assert decide_execution_recovery(TASK, "e", "UNKNOWN", age_seconds=300, policy=policy).action is Action.FENCE_AND_REPLACE_EXECUTION


def test_live_waits_and_only_provably_dead_is_released() -> None:
    assert decide_execution_recovery(TASK, "e", "LIVE").action is Action.WAIT
    assert decide_execution_recovery(TASK, "e", "DEAD").action is Action.RELEASE_DEAD_EXECUTION
    assert decide_execution_recovery(TASK, "e", "SOMETHING-ELSE").action is Action.FENCE_AND_REPLACE_EXECUTION  # unrecognized == unknown


def _repo_with_claimed_task(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    git(repo, "config", "user.email", "stagemesh@example.invalid")
    git(repo, "config", "user.name", "StageMesh")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    store = new_store(tmp_path)
    store.upsert_task("task", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    return repo, store, base


def _running_execution(store, pid=None, **identity):
    claim = store.acquire_claim(TASK, "worker-1")
    return store.start_execution(task_id=TASK, claim_id=claim, kind=ExecutionKind.IMPLEMENTATION, pid=pid, **identity)


def _status(store, execution_id: str) -> str:
    return store.conn.execute("SELECT status FROM executions WHERE id=?", (execution_id,)).fetchone()[0]


def test_scenario_k_assess_does_not_touch_an_unknown_execution(tmp_path: Path) -> None:
    repo, store, _base = _repo_with_claimed_task(tmp_path)
    execution = _running_execution(store)  # no identity recorded: UNKNOWN
    supervisor = Supervisor(store, repo, integration_ref="main")

    decision = supervisor.assess_execution(TASK, execution, age_seconds=10_000)

    assert decision.action is Action.FENCE_AND_REPLACE_EXECUTION
    assert _status(store, execution) == "RUNNING" and store.has_active_claim(TASK)  # deciding is not acting
    Coordinator(store, repo).recover()  # the existing recovery path also leaves it alone
    assert _status(store, execution) == "RUNNING"


def test_scenario_k_fencing_preserves_the_old_worktree_and_continues_on_a_replacement(tmp_path: Path) -> None:
    repo, store, base = _repo_with_claimed_task(tmp_path)
    old = prepare_task_workspace(repo, TASK)
    (old / "work-in-progress.txt").write_text("the unknown process may still be writing this\n", encoding="utf-8")
    execution = _running_execution(store)
    supervisor = Supervisor(store, repo, integration_ref="main")

    decision = supervisor.recover_execution(TASK, execution, age_seconds=600)

    assert decision.action is Action.FENCE_AND_REPLACE_EXECUTION
    assert _status(store, execution) == ExecutionStatus.UNKNOWN  # never marked dead, failed or orphaned
    assert not store.has_active_claim(TASK) and store.get_task(TASK)["status"] == TaskStatus.OPEN
    assert (old / "work-in-progress.txt").read_text(encoding="utf-8").startswith("the unknown process")  # untouched
    new = task_workspace(repo, TASK)
    assert new != old and decision.detail["new_worktree"] == str(new) and decision.detail["old_worktree"] == str(old)
    assert git(new, "rev-parse", "HEAD") == base and not (new / "work-in-progress.txt").exists()
    assert git(repo, "show", f"{decision.detail['preserved_old_state_ref']}:work-in-progress.txt").startswith("the unknown process")
    assert prepare_task_workspace(repo, TASK) == new  # every executor now resolves the replacement
    assert decisions(store, TASK)[-1]["detail"]["fenced_execution_status"] == "UNKNOWN"


def test_scenario_k_task_completes_a_candidate_on_the_replacement_while_the_fenced_process_keeps_writing(tmp_path: Path) -> None:
    repo, store, _base = _repo_with_claimed_task(tmp_path)
    old = prepare_task_workspace(repo, TASK)
    execution = _running_execution(store)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.recover_execution(TASK, execution)

    (old / "src" / "app.py").write_text("VALUE = 'written by the fenced process'\n", encoding="utf-8")  # it is still alive and writing
    script = tmp_path / "provider.py"
    script.write_text("from pathlib import Path\nPath('src/widget.py').write_text('W = 1\\n')\n", encoding="utf-8")
    contracts = repo / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / f"{TASK}.json").write_text('{"objective": "widget", "allowed_files": ["src/**"]}', encoding="utf-8")
    coordinator = Coordinator(store, repo, executor=SubprocessExecutor([sys.executable, str(script)], name="codex"))

    assert coordinator.tick() == 1

    candidate = store.latest_candidate(TASK)["sha"]
    assert git(repo, "show", f"{candidate}:src/app.py") == "VALUE = 1"  # the fenced process's edit is nowhere in the candidate
    assert git(repo, "cat-file", "-t", f"{candidate}:src/widget.py") == "blob"


def test_scenario_k_provably_dead_execution_is_released_and_a_live_one_is_not(tmp_path: Path) -> None:
    repo, store, _base = _repo_with_claimed_task(tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = popen_identity(proc)
    execution = _running_execution(store, identity.pid, process_create_time=identity.create_time, boot_id=identity.boot_id, executable=identity.executable)
    supervisor = Supervisor(store, repo, integration_ref="main")
    try:
        live = supervisor.recover_execution(TASK, execution)
        assert live.action is Action.WAIT and _status(store, execution) == "RUNNING"
    finally:
        proc.kill()
        proc.wait()

    dead = supervisor.recover_execution(TASK, execution)

    assert dead.condition is Condition.EXECUTION_IDENTITY_DEAD and dead.action is Action.RELEASE_DEAD_EXECUTION
    assert _status(store, execution) == "FAILED" and not store.has_active_claim(TASK)


def test_scenario_k_current_process_is_live(tmp_path: Path) -> None:
    from stagemesh.process_identity import process_identity

    repo, store, _base = _repo_with_claimed_task(tmp_path)
    me = process_identity(os.getpid())
    assert me is not None
    execution = _running_execution(store, me.pid, process_create_time=me.create_time, boot_id=me.boot_id, executable=me.executable)
    assert Supervisor(store, repo, integration_ref="main").assess_execution(TASK, execution).action is Action.WAIT


# --- Scenario L: destructive git operations ------------------------------------------------------------------------------------------------------


def test_scenario_l_force_push_of_a_candidate_branch_becomes_a_replacement_branch() -> None:
    request = GitOperationRequest(GitOperation.FORCE_PUSH, "refs/heads/feat/widget", "rebase onto rewritten main", "a" * 40)
    decision = decide_git_operation(request, task_id=TASK)
    assert decision.condition is Condition.DESTRUCTIVE_GIT_OPERATION_REQUESTED
    assert decision.action is Action.USE_REPLACEMENT_BRANCH and not decision.requires_human
    assert decision.detail["replacement_branch"] == "feat/widget-sm-aaaaaaa"
    assert decision.detail["destructive_operation_executed"] is False


def test_scenario_l_history_rewrite_of_the_integration_ref_has_no_safe_alternative_and_escalates() -> None:
    request = GitOperationRequest(GitOperation.FORCE_PUSH, "refs/heads/main", "remove a leaked file from history", "b" * 40)
    decision = decide_git_operation(request, task_id=TASK)
    escalation = decision.escalation
    assert decision.action is Action.ESCALATE_TO_FOUNDER
    assert escalation is not None and escalation.reason is EscalationReason.DESTRUCTIVE_OPERATION_HAS_NO_SAFE_ALTERNATIVE
    assert len(escalation.attempted) == 2
    assert "refs/heads/main" in escalation.smallest_decision and "leaked file" in escalation.smallest_decision
    for op in (GitOperation.HISTORY_REWRITE, GitOperation.BRANCH_DELETE, GitOperation.RESET_HARD):
        assert decide_git_operation(GitOperationRequest(op, "refs/heads/main", "x", "b" * 40), task_id=TASK).requires_human


def test_scenario_l_reset_or_clean_with_unpreserved_work_snapshots_first() -> None:
    for op in (GitOperation.RESET_HARD, GitOperation.CLEAN):
        decision = decide_git_operation(GitOperationRequest(op, "refs/heads/feat/x", "start over", "c" * 40, has_unpreserved_work=True), task_id=TASK)
        assert decision.action is Action.PRESERVE_THEN_PROCEED and decision.detail["destructive_operation_executed"] is False
        clean = decide_git_operation(GitOperationRequest(op, "refs/heads/feat/x", "start over", "c" * 40, has_unpreserved_work=False), task_id=TASK)
        assert clean.action is Action.PROCEED  # nothing would be lost


def test_scenario_l_supervisor_preserves_the_original_before_any_destructive_workflow_proceeds(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"a.txt": "a\n"}, "base")
    git(repo, "checkout", "-q", "-b", "feat/x")
    tip = commit(repo, {"b.txt": "b\n"}, "work")
    store = new_store(tmp_path)
    supervisor = Supervisor(store, repo, integration_ref="main")

    decision = supervisor.assess_git_operation(TASK, GitOperationRequest(GitOperation.FORCE_PUSH, "refs/heads/feat/x", "tidy history", tip))

    assert decision.action is Action.USE_REPLACEMENT_BRANCH
    assert git(repo, "rev-parse", decision.detail["preserved_ref"]) == tip  # provenance preserved
    assert git(repo, "rev-parse", "feat/x") == tip and base  # nothing was rewritten
    assert decisions(store, TASK)[0]["trace"].endswith("human_escalation=false")


def test_scenario_k_coordinator_with_the_supervisor_no_longer_stalls_on_an_unknown_execution(tmp_path: Path) -> None:
    repo, store, _base = _repo_with_claimed_task(tmp_path)
    old = prepare_task_workspace(repo, TASK)
    execution = _running_execution(store)  # a RUNNING implementation execution with no recorded identity
    script = tmp_path / "provider.py"
    script.write_text("from pathlib import Path\nPath('src/widget.py').write_text('W = 1\\n')\n", encoding="utf-8")
    contracts = repo / ".stagemesh" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / f"{TASK}.json").write_text('{"objective": "widget", "allowed_files": ["src/**"]}', encoding="utf-8")
    executor = SubprocessExecutor([sys.executable, str(script)], name="codex")

    unsupervised = Coordinator(store, repo, executor=executor)
    assert unsupervised.tick() == 0 and _status(store, execution) == "RUNNING"  # legacy behavior: waits forever, never guesses

    supervisor = Supervisor(store, repo, integration_ref="main")
    supervised = Coordinator(store, repo, executor=executor, guard=supervisor)
    assert supervised.tick() == 1  # fenced under the explicit policy, then a candidate is produced on the replacement

    assert _status(store, execution) == ExecutionStatus.UNKNOWN
    assert task_workspace(repo, TASK) != old and store.latest_candidate(TASK) is not None
    assert any(d["action"] == "FENCE_AND_REPLACE_EXECUTION" for d in decisions(store, TASK))
