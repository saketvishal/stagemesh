"""Ordinary provider trouble (no progress, a tampered workspace) is retried automatically within a budget; nothing needs `retry-task`."""
from __future__ import annotations

import time
from pathlib import Path

from test_parallel import Rig as ParallelRig
from test_parallel import ScriptedExecutor
from test_provider_pool import TASK
from test_provider_pool import Rig as PoolRig

from stagemesh.audit import record_audit
from stagemesh.blocked_recovery import (
    AUTO_RETRY_EVENT,
    auto_retries_used,
    auto_retry_blocked_provider_failures,
    exhaustion_message,
)
from stagemesh.cli import _format_recovered
from stagemesh.coordinator import Coordinator
from stagemesh.domain import ExecutionKind, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.integration import Integrator
from stagemesh.persistence import Store
from stagemesh.provider_pool import IMPLEMENT, REVIEW, PooledExecutor
from stagemesh.review import Reviewer
from stagemesh.run_ready import run_ready
from stagemesh.workspace_guard import EXTERNAL_WORKSPACE_MUTATION
from stagemesh.workspaces import prepare_task_workspace, task_workspace


def _block_no_progress(store: Store, task_id: str, stage: str = "IMPLEMENT") -> None:
    """The #248 pattern: the coordinator's repeated-no-progress stop (diagnosis_stop + block)."""
    store.block_task(task_id)
    record_audit(store, "task.diagnosis_stop", {"task_id": task_id, "category": "provider_no_progress", "stage": stage, "remediation_attempts": 0})
    record_audit(store, "task.remediation_exhausted", {"task_id": task_id, "stage": stage, "reason": "repeated_failure_diagnosed"})


def _block_mutation(store: Store, task_id: str, stage: str = "IMPLEMENT") -> None:
    """The #240 pattern: a provider modified tracked files in its workspace; fail closed, block."""
    store.block_task(task_id)
    record_audit(store, "task.blocked", {"task_id": task_id, "reason": EXTERNAL_WORKSPACE_MUTATION, "stage": stage})


def _advance_to(store: Store, task_id: str, stage: Stage) -> None:
    store.advance_task(task_id, stage)


def test_provider_no_progress_block_is_retried_automatically_and_reported(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    _block_no_progress(rig.store, "A")

    recovered = auto_retry_blocked_provider_failures(rig.store, rig.project)

    assert [e["task_id"] for e in recovered] == ["A"] and recovered[0]["auto_recovery"] == "provider_no_progress_retry"
    assert rig.store.get_task("A")["status"] == TaskStatus.OPEN
    assert rig.store.conn.execute("SELECT COUNT(*) FROM audit_events WHERE event_type=?", (AUTO_RETRY_EVENT,)).fetchone()[0] == 1
    line = _format_recovered(recovered[0])
    assert "retrying automatically (attempt 1 of 2)" in line and "no usable change" in line


def test_the_retry_budget_is_bounded_then_a_human_is_told_exactly_what_is_needed(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    for attempt in (1, 2):
        _block_no_progress(rig.store, "A")
        time.sleep(0.01)
        assert len(auto_retry_blocked_provider_failures(rig.store, rig.project)) == 1, attempt
    _block_no_progress(rig.store, "A")

    assert auto_retry_blocked_provider_failures(rig.store, rig.project) == []  # budget spent: no third automatic retry
    assert rig.store.get_task("A")["status"] == TaskStatus.BLOCKED
    message = exhaustion_message(rig.store, "A")
    assert message is not None and "2 automatic retries" in message and "needs a human" in message
    assert "retry-task --task A" in message

    assert rig.store.unblock_task("A")  # the operator's explicit retry grants a fresh budget
    assert auto_retries_used(rig.store, "A") == 0
    _block_no_progress(rig.store, "A")
    assert len(auto_retry_blocked_provider_failures(rig.store, rig.project)) == 1


def test_a_tampered_workspace_is_preserved_in_quarantine_and_the_task_moves_to_a_fresh_worktree(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    old = prepare_task_workspace(rig.project, "A")
    (old / "README.md").write_text("tampered by a provider\n", encoding="utf-8")  # TRACKED_FILES_MODIFIED
    _block_mutation(rig.store, "A")

    recovered = auto_retry_blocked_provider_failures(rig.store, rig.project)

    entry = recovered[0]
    assert entry["auto_recovery"] == "workspace_mutation_retry" and rig.store.get_task("A")["status"] == TaskStatus.OPEN
    ref = entry["quarantine_ref"]
    preserved = GitWorkspace(rig.project).run("show", f"{ref}:README.md").stdout
    assert preserved.strip() == "tampered by a provider"  # the exact tampered state is kept
    assert (old / "README.md").read_text(encoding="utf-8").strip() == "tampered by a provider"  # and the old worktree is not deleted
    assert task_workspace(rig.project, "A") != old and entry["new_workspace"] == str(task_workspace(rig.project, "A"))


def test_failures_that_need_a_human_decision_are_never_retried_automatically(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A", "B", "C"])
    for task in ("A", "B", "C"):
        _advance_to(rig.store, task, Stage.IMPLEMENT)
    rig.store.block_task("A")
    record_audit(rig.store, "task.diagnosis_stop", {"task_id": "A", "category": "contract_scope", "stage": "VALIDATE"})
    _block_mutation(rig.store, "B", stage="INTEGRATE")  # tampering after the candidate exists is not the implementation case
    _block_no_progress(rig.store, "C")
    rig.store.conn.execute("UPDATE tasks SET stage='REVIEW' WHERE id='C'")  # a later-stage block is someone else's recovery
    rig.store.conn.commit()

    assert auto_retry_blocked_provider_failures(rig.store, rig.project) == []
    assert {rig.store.get_task(t)["status"] for t in ("A", "B", "C")} == {TaskStatus.BLOCKED}


def test_a_task_with_a_live_claim_or_execution_is_not_taken_over(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    _block_no_progress(rig.store, "A")
    rig.store.start_execution(task_id="A", claim_id=None, kind=ExecutionKind.IMPLEMENTATION)

    assert auto_retry_blocked_provider_failures(rig.store, rig.project) == []
    assert rig.store.get_task("A")["status"] == TaskStatus.BLOCKED


def test_continue_on_a_blocked_no_progress_task_recovers_and_completes_without_retry_task(tmp_path: Path) -> None:
    rig = PoolRig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    _advance_to(rig.store, TASK, Stage.IMPLEMENT)
    _block_no_progress(rig.store, TASK)  # `continue --task` used to refuse here with "use retry-task"

    def make(target):
        return Coordinator(
            rig.store, rig.project, executor=PooledExecutor(rig.pool), reviewer=Reviewer(require_independent=True, review_pool=rig.pool),
            integrator=Integrator(integration_ref="refs/heads/integration", require_independent_review=True),
            require_independent_review=True, target=target,
        )

    summary = run_ready(rig.store, rig.project, make, task_id=TASK, max_steps=20, auto_plan=False)

    assert summary.recovered and summary.recovered[0]["auto_recovery"] == "provider_no_progress_retry"
    assert summary.stop_reason == "DONE", summary.to_dict()


def test_continue_when_the_budget_is_spent_refuses_with_a_plain_human_instruction(tmp_path: Path) -> None:
    rig = PoolRig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    _advance_to(rig.store, TASK, Stage.IMPLEMENT)
    for _ in range(2):
        _block_no_progress(rig.store, TASK)
        time.sleep(0.01)
        auto_retry_blocked_provider_failures(rig.store, rig.project, task_id=TASK)
    _block_no_progress(rig.store, TASK)

    summary = run_ready(rig.store, rig.project, lambda target: None, task_id=TASK, max_steps=5, auto_plan=False)  # type: ignore[arg-type]

    assert summary.stop_reason == "REFUSED:task_blocked"
    assert "after 2 automatic retries" in summary.message and "needs a human" in summary.message


def test_queue_run_retries_the_blocked_task_announces_it_and_still_finishes_other_work(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A", "B"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    _block_no_progress(rig.store, "A")
    said: list[str] = []
    runner = rig.runner(ScriptedExecutor(rig.files), emit=lambda task, text: said.append(f"{task}: {text}"))

    summary = runner.run()

    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}, summary.to_dict()
    assert any(item.get("auto_recovery") == "provider_no_progress_retry" for item in summary.recovered)
    assert any("automatic recovery" in line and "attempt 1 of 2" in line for line in said), said


def test_queue_run_completes_a_task_that_was_blocked_by_a_tampered_workspace_on_the_fresh_worktree(tmp_path: Path) -> None:
    rig = ParallelRig(tmp_path, ["A", "B"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    old = prepare_task_workspace(rig.project, "A")
    (old / "README.md").write_text("tampered by a provider\n", encoding="utf-8")
    _block_mutation(rig.store, "A")
    executor = ScriptedExecutor(rig.files)

    summary = rig.runner(executor).run()

    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE"}, summary.to_dict()
    assert executor.worktrees["A"] != old  # the retry ran on the new generation, not on the tampered workspace
    assert (old / "README.md").read_text(encoding="utf-8").strip() == "tampered by a provider"  # which is still there to inspect
    assert "out/A.txt" in rig.tree() and "tampered" not in GitWorkspace(rig.project).run("show", f"{rig.ref}:README.md").stdout


def test_a_task_that_blocked_earlier_in_this_run_is_held_until_the_other_work_is_done(tmp_path: Path) -> None:
    from stagemesh.blocked_recovery import can_auto_retry

    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    _block_no_progress(rig.store, "A")

    assert can_auto_retry(rig.store, "A")
    assert auto_retry_blocked_provider_failures(rig.store, rig.project, exclude={"A"}) == []  # held: it just blocked in this run
    assert rig.store.get_task("A")["status"] == TaskStatus.BLOCKED
    assert len(auto_retry_blocked_provider_failures(rig.store, rig.project)) == 1  # released for the end-of-run retry round

    for _ in range(2):
        _block_no_progress(rig.store, "A")
        time.sleep(0.01)
        auto_retry_blocked_provider_failures(rig.store, rig.project)
    _block_no_progress(rig.store, "A")
    assert not can_auto_retry(rig.store, "A")  # budget spent: the run reports it and stops retrying


def test_an_automatically_retried_task_is_not_skipped_by_selection_as_stale(tmp_path: Path) -> None:
    """Found by the live dogfood run: the retry unblocked the task but selection still skipped it as 'stale failed state'."""
    from stagemesh.task_selection import stale_failure

    rig = ParallelRig(tmp_path, ["A"])
    _advance_to(rig.store, "A", Stage.IMPLEMENT)
    record_audit(rig.store, "task.implementation_unsuccessful", {
        "task_id": "A", "reason": "all_implementation_providers_exhausted: grok: quota_rate_limit; codex: no_implementation_change",
    })
    _block_no_progress(rig.store, "A")
    assert stale_failure(rig.store, rig.store.get_task("A")).startswith("recent provider pool exhaustion")

    time.sleep(0.01)
    assert auto_retry_blocked_provider_failures(rig.store, rig.project)

    assert stale_failure(rig.store, rig.store.get_task("A")) is None  # StageMesh chose to retry it: it is selectable again


def test_an_automatically_reintegrated_task_is_not_skipped_as_latest_integration_failed(tmp_path: Path) -> None:
    from stagemesh.domain import EvidenceKind, EvidenceStatus
    from stagemesh.recovery import auto_reintegrate_blocked_runtime_failures
    from stagemesh.task_selection import stale_failure

    rig = ParallelRig(tmp_path, ["A"])
    rig.store.add_candidate("A", "c" * 40, "codex", durable_handoff=True)
    rig.store.advance_task("A", Stage.INTEGRATE)
    rig.store.add_evidence("A", "c" * 40, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED,
                           {"contract_hash": "h", "findings": [{"code": "integration_rebase_unavailable", "severity": "error", "message": "x"}]})
    rig.store.block_task("A")
    assert stale_failure(rig.store, rig.store.get_task("A")) == "latest integration failed"

    time.sleep(0.01)
    assert auto_reintegrate_blocked_runtime_failures(rig.store, rig.project)

    assert stale_failure(rig.store, rig.store.get_task("A")) is None


def test_a_retried_integration_that_rebases_the_candidate_is_not_blocked_by_the_old_shas_failure(tmp_path: Path) -> None:
    """Found by the live dogfood run: the retry rebased the candidate, then the old SHA's FAILED evidence blocked the task again."""
    from stagemesh.concurrency import IntegrationLock
    from stagemesh.contract_binding import contract_for_candidate
    from stagemesh.domain import EvidenceKind, EvidenceStatus
    from stagemesh.serialized_integration import SerializedIntegrator

    rig = PoolRig(tmp_path, {"provider-a": "ok", "provider-b": "ok"}, pools={IMPLEMENT: ("provider-a",), REVIEW: ("provider-b",)})
    rig.coordinator.integrator = SerializedIntegrator(
        "refs/heads/integration", True, IntegrationLock(rig.project / ".stagemesh" / "integration.lock"), max_rebases=2
    )
    rig.tick(4)  # PLAN, IMPLEMENT, VALIDATE, REVIEW -> the task waits at INTEGRATE
    assert rig.stage == Stage.INTEGRATE
    sha = rig.store.latest_candidate(TASK)["sha"]
    digest = contract_for_candidate(rig.store, TASK, sha, rig.project).digest
    rig.store.add_evidence(TASK, sha, EvidenceKind.INTEGRATION, EvidenceStatus.FAILED, {
        "contract_hash": digest, "findings": [{"code": "integration_rebase_unavailable", "severity": "error", "message": "git: Filename too long"}],
    })
    rig.git.run("checkout", "-q", "integration")  # another task landed: the ref moved, so the candidate needs a rebase
    (rig.project / "other.txt").write_text("landed elsewhere\n", encoding="utf-8")
    rig.git.commit_all("another change lands")
    rig.git.run("checkout", "-q", "-")

    rig.tick(1)

    task = rig.store.get_task(TASK)
    assert task["status"] == TaskStatus.OPEN and task["stage"] == Stage.VALIDATE, dict(task)
    assert rig.store.latest_candidate(TASK)["sha"] != sha
    assert not rig.store.conn.execute("SELECT 1 FROM audit_events WHERE event_type='task.remediation_exhausted'").fetchone()
    rig.tick(6)
    assert rig.stage == Stage.DONE  # re-validated, re-reviewed on the rebased SHA and integrated
