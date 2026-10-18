from __future__ import annotations

import pytest
from test_parallel import Rig, ScriptedExecutor
from test_single_task_stale_rebase import (
    TASK, _advance_until, _executions, _land_on_main, _single_task_coordinator, _tip,
)

from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage
from stagemesh.run_ready import RunSummary, drive_task


@pytest.mark.parametrize("stage", [Stage.VALIDATE, Stage.REVIEW])
def test_refreshes_already_stale_candidate_before_expensive_checks(tmp_path, stage):
    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, stage)
    old = rig.store.latest_candidate(TASK)["sha"]
    before = (_executions(rig, ExecutionKind.VALIDATION), _executions(rig, ExecutionKind.REVIEW))
    tip = _land_on_main(rig, "other/task.txt", "another task landed\n")
    assert coord.tick() == 1
    new = rig.store.latest_candidate(TASK)["sha"]
    assert new != old and rig.store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert _tip(rig) == tip
    assert before == (_executions(rig, ExecutionKind.VALIDATION), _executions(rig, ExecutionKind.REVIEW))
    binding = rig.store.contract_binding(TASK, new)
    assert binding["baseline_sha"] == tip
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert not rig.store.has_bound_evidence(TASK, new, kind, binding["digest"], EvidenceStatus.PASSED)
    _advance_until(rig, coord, Stage.DONE)
    assert _executions(rig, ExecutionKind.IMPLEMENTATION) == 1
    assert _executions(rig, ExecutionKind.REVIEW) == 1


def test_two_base_moves_finish_within_original_eight_step_budget(tmp_path):
    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, Stage.IMPLEMENT)
    moved = []
    def after_step(step):
        if not moved and step["previous"]["stage"] == Stage.IMPLEMENT:
            moved.append(_land_on_main(rig, "other/first.txt", "first\n"))
        elif len(moved) == 1 and step["previous"]["stage"] == Stage.REVIEW:
            moved.append(_land_on_main(rig, "other/second.txt", "second\n"))
    summary = RunSummary(True, "UNSET", task_id=TASK)
    drive_task(rig.store, rig.project, coord, TASK, summary, max_steps=8, on_step=after_step)
    assert len(moved) == 2
    assert summary.stop_reason == "DONE", summary.to_dict()
    assert len(summary.steps) == 8
    assert _executions(rig, ExecutionKind.IMPLEMENTATION) == 1
    assert _executions(rig, ExecutionKind.VALIDATION) == 2
    assert _executions(rig, ExecutionKind.REVIEW) == 2


def test_early_refresh_respects_existing_refresh_budget(tmp_path):
    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, Stage.VALIDATE)
    original = rig.store.latest_candidate(TASK)["sha"]
    tip = _land_on_main(rig, "other/task.txt", "moved\n")
    coord.integrator.max_rebases = 0
    assert not coord.integrator.refresh_before_verification(rig.store, TASK, original, rig.project)
    assert rig.store.latest_candidate(TASK)["sha"] == original
    assert _tip(rig) == tip


@pytest.mark.parametrize("stage", [Stage.VALIDATE, Stage.REVIEW])
def test_supervised_early_refresh_keeps_workspace_and_evidence_exact(tmp_path, stage):
    from autonomy_support import TASK as task, commit, git
    from test_autonomy_lifecycle import REF, _coordinator, _project, _task_at_integrate

    from stagemesh.autonomy.supervisor import Supervisor
    from stagemesh.workspaces import prepare_task_workspace

    project, store, base = _project(tmp_path)
    original = _task_at_integrate(store, project, base)
    store.advance_task(task, stage)
    worktree = prepare_task_workspace(project, task)
    git(worktree, "checkout", "--detach", original)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(task, worktree)
    tip = commit(project, {"docs/notes.md": "another task\n"}, "base advances")
    coord = _coordinator(store, project, supervisor)
    assert coord.tick() == 1
    replacement = store.latest_candidate(task)["sha"]
    assert replacement != original
    assert git(worktree, "rev-parse", "HEAD") == replacement
    assert supervisor.check_workspace(task) is None
    assert store.get_task(task)["stage"] == Stage.VALIDATE
    binding = store.contract_binding(task, replacement)
    assert binding["baseline_sha"] == tip
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert not store.has_bound_evidence(task, replacement, kind, binding["digest"], EvidenceStatus.PASSED)


@pytest.mark.parametrize("stage", [Stage.VALIDATE, Stage.REVIEW])
@pytest.mark.parametrize("failure", ["ordinary", "exhausted", "judgment"])
def test_restart_failure_is_handled_before_any_early_refresh(tmp_path, monkeypatch, stage, failure):
    from stagemesh.contract_binding import contract_for_candidate

    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, stage)
    original = rig.store.latest_candidate(TASK)["sha"]
    bound = contract_for_candidate(rig.store, TASK, original, rig.project)
    kind = EvidenceKind.VALIDATION if stage is Stage.VALIDATE else EvidenceKind.REVIEW
    rig.store.add_evidence(TASK, original, kind, EvidenceStatus.FAILED, bound.evidence_payload())
    message = "requires a product decision" if failure == "judgment" else "repair this defect"
    rig.store.upsert_finding("restart-finding", TASK, original, "error", message)
    if failure == "exhausted":
        coord.remediation_policy.max_attempts = 1
        rig.store.add_task_remediation(TASK, str(stage), original)
    _land_on_main(rig, "other/task.txt", "new base\n")
    monkeypatch.setattr(coord.validator, "validate", lambda *args: None)
    monkeypatch.setattr(coord.reviewer, "review", lambda *args: None)
    monkeypatch.setattr(coord, "_diagnose", lambda *args: None)
    stopped = []
    def exhausted(task_id, sha, failed_stage, findings, diagnosis, cause, **kwargs):
        stopped.append((sha, failed_stage, cause))
        rig.store.block_task(task_id)
        return 1
    monkeypatch.setattr(coord, "_on_remediation_exhausted", exhausted)
    assert coord.tick() == 1
    assert rig.store.latest_candidate(TASK)["sha"] == original
    assert rig.store.open_findings_for_candidate(TASK, original)
    if failure == "ordinary":
        assert not stopped
        assert rig.store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    else:
        assert stopped == [(original, stage, "judgment_required" if failure == "judgment" else "remediation_budget_exhausted")]
