"""Regression tests: candidate isolation across main advancement, rebases, simultaneous tasks and failed integrations."""
from __future__ import annotations

import json
from pathlib import Path

from test_parallel import Rig, ScriptedExecutor
from test_single_task_stale_rebase import TASK, _advance_until, _land_on_main, _single_task_coordinator, _tip

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.contracts import changed_files
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.git import GitWorkspace


def _follow_up_candidate(rig: Rig, worktree: Path, rel: str) -> str:
    """A remediation-style commit on top of the task's current (rebased) candidate, in the task's own worktree."""
    (worktree / rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree / rel).write_text("fix\n", encoding="utf-8")
    sha = GitWorkspace(worktree).commit_all("remediation")
    rig.store.add_candidate(TASK, sha, "scripted", durable_handoff=True)
    return sha


def test_candidate_after_rebase_is_diffed_against_the_rebased_base_not_the_stale_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    original_baseline = rig.store.task_baseline(TASK)
    for index in range(20):  # main advances by far more files than the task's contract allows
        tip = _land_on_main(rig, f"other/work{index}.txt", f"other {index}\n")
    coord.tick()  # stale -> rebase onto tip
    rebased = rig.store.latest_candidate(TASK)["sha"]
    assert rig.store.contract_binding(TASK, rebased)["baseline_sha"] == tip

    followup = _follow_up_candidate(rig, executor.worktrees[TASK], "out/A-fix.txt")
    bound = contract_for_candidate(rig.store, TASK, followup, rig.project)

    assert bound.baseline_sha == tip != original_baseline
    assert changed_files(rig.project, followup, bound.baseline_sha) == ["out/A-fix.txt", "out/A.txt"]
    assert not any(path.startswith("other/") for path in changed_files(rig.project, followup, bound.baseline_sha))


def test_unrelated_edit_after_rebase_is_still_rejected_by_the_file_scope(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    _land_on_main(rig, "other/work.txt", "other\n")
    coord.tick()
    followup = _follow_up_candidate(rig, executor.worktrees[TASK], "unrelated/sneaky.txt")

    from stagemesh.contracts import evaluate_contract

    bound = contract_for_candidate(rig.store, TASK, followup, rig.project)
    evaluation = evaluate_contract(rig.project, followup, bound.contract, baseline_sha=bound.baseline_sha, run_gates=False)
    assert any("unrelated/sneaky.txt" in json.dumps(f) for f in evaluation.findings)
    assert not any("other/work.txt" in json.dumps(f) for f in evaluation.findings)


def test_unrebased_candidate_keeps_the_frozen_baseline(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    followup = _follow_up_candidate(rig, executor.worktrees[TASK], "out/A-fix.txt")
    bound = contract_for_candidate(rig.store, TASK, followup, rig.project)
    assert bound.baseline_sha == rig.store.task_baseline(TASK)


def test_failed_integration_never_marks_task_done_and_preserves_candidate_and_worktree(tmp_path: Path) -> None:
    files = {TASK: ("shared.txt", "from A\n")}
    rig = Rig(tmp_path, [TASK], files=files)
    executor = ScriptedExecutor(files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    candidate = rig.store.latest_candidate(TASK)["sha"]
    tip = _land_on_main(rig, "shared.txt", "from elsewhere\n")
    for _ in range(8):
        coord.tick()

    assert _tip(rig) == tip
    assert rig.store.get_task(TASK)["status"] != TaskStatus.DONE
    assert not rig.store.has_evidence(TASK, candidate, EvidenceKind.INTEGRATION) or not rig.store.has_bound_evidence(
        TASK, candidate, EvidenceKind.INTEGRATION, rig.store.contract_binding(TASK, candidate)["digest"], EvidenceStatus.PASSED
    )
    worktree = executor.worktrees[TASK]
    assert GitWorkspace(worktree).head() == candidate  # the valuable change is preserved, not reset
    assert GitWorkspace(rig.project).run("cat-file", "-e", f"{candidate}^{{commit}}", check=False).returncode == 0
    diagnostic = next(
        f
        for row in rig.store.conn.execute("SELECT payload FROM evidence WHERE task_id=? AND kind=?", (TASK, EvidenceKind.INTEGRATION))
        for f in json.loads(row["payload"]).get("findings", [])
        if f["code"] == "integration_rebase_conflict"
    )
    assert "shared.txt" in diagnostic["message"] and candidate in diagnostic["message"] and tip in diagnostic["message"]


def test_simultaneous_tasks_integrate_serially_with_isolated_candidates(tmp_path: Path) -> None:
    ids = ["A", "B", "C"]
    rig = Rig(tmp_path, ids)
    executor = ScriptedExecutor(rig.files)
    summary = rig.runner(executor, concurrency=3).run()

    assert rig.outcomes(summary) == {"A": "DONE", "B": "DONE", "C": "DONE"}, summary.to_dict()
    assert {f"out/{t}.txt" for t in ids} <= rig.tree()
    for task_id in ids:
        for row in rig.store.conn.execute("SELECT sha FROM candidates WHERE task_id=?", (task_id,)):
            binding = rig.store.contract_binding(task_id, row["sha"])
            assert binding is not None
            assert changed_files(rig.project, row["sha"], binding["baseline_sha"]) == [f"out/{task_id}.txt"]
    # the ref only ever moved by fast-forward: every landed candidate is an ancestor of the final tip
    tip = _tip(rig)
    for task_id in ids:
        sha = rig.store.latest_candidate(task_id)["sha"]
        assert GitWorkspace(rig.project).run("merge-base", "--is-ancestor", sha, tip, check=False).returncode == 0
