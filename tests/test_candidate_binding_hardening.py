"""Hardening: baseline inheritance is task-scoped, ancestry-checked and fail-closed; integration only lands exactly-gated candidates."""
from __future__ import annotations

import json
from pathlib import Path

from test_parallel import Rig, ScriptedExecutor
from test_single_task_stale_rebase import TASK, _advance_until, _land_on_main, _single_task_coordinator, _tip

from stagemesh.contract_binding import contract_for_candidate
from stagemesh.contracts import changed_files, evaluate_contract
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage, TaskStatus
from stagemesh.git import GitWorkspace
from stagemesh.serialized_integration import SerializedIntegrator


def _git(rig: Rig, *args: str) -> str:
    return GitWorkspace(rig.project).run(*args).stdout.strip()


def _commit_on(worktree: Path, rel: str, content: str = "x\n") -> str:
    (worktree / rel).parent.mkdir(parents=True, exist_ok=True)
    (worktree / rel).write_text(content, encoding="utf-8")
    return GitWorkspace(worktree).commit_all(f"edit {rel}")


def _findings(rig: Rig, sha: str) -> list[dict]:
    bound = contract_for_candidate(rig.store, TASK, sha, rig.project)
    return list(evaluate_contract(rig.project, sha, bound.contract, baseline_sha=bound.baseline_sha, run_gates=False).findings)


def _rebased_task(tmp_path: Path):
    """A task whose candidate was rebased once onto a main that gained many unrelated files."""
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    original = rig.store.task_baseline(TASK)
    for index in range(15):
        tip = _land_on_main(rig, f"other/work{index}.txt", f"other {index}\n")
    coord.tick()
    rebased = rig.store.latest_candidate(TASK)["sha"]
    assert rig.store.contract_binding(TASK, rebased)["baseline_sha"] == tip
    return rig, executor, coord, original, tip, rebased


def _bind(rig: Rig, task_id: str, candidate: str, baseline: str) -> None:
    rig.store.bind_contract(task_id, candidate, baseline, 1, "d" * 64, "{}")


def _passed(rig: Rig, sha: str, kind: EvidenceKind) -> bool:
    binding = rig.store.contract_binding(TASK, sha)
    return binding is not None and rig.store.has_bound_evidence(TASK, sha, kind, binding["digest"], EvidenceStatus.PASSED)


def _candidate(rig: Rig, sha: str) -> None:
    rig.store.add_candidate(TASK, sha, "scripted", durable_handoff=True)


# ---- binding correctness -------------------------------------------------------------------------------------------------------


def test_sequential_remediation_commits_after_a_rebase_all_keep_the_rebased_base(tmp_path: Path) -> None:
    rig, executor, _coord, original, tip, _rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    for index in range(3):
        sha = _commit_on(worktree, f"out/A-fix{index}.txt")
        _candidate(rig, sha)
        bound = contract_for_candidate(rig.store, TASK, sha, rig.project)
        assert bound.baseline_sha == tip != original
        assert not any(p.startswith("other/") for p in changed_files(rig.project, sha, bound.baseline_sha))
    assert changed_files(rig.project, sha, tip) == ["out/A-fix0.txt", "out/A-fix1.txt", "out/A-fix2.txt", "out/A.txt"]


def test_second_rebase_as_main_advances_again_rebinds_to_the_newest_tip(tmp_path: Path) -> None:
    rig, executor, coord, original, first_tip, first = _rebased_task(tmp_path)
    _advance_until(rig, coord, Stage.INTEGRATE)
    second_tip = _land_on_main(rig, "other/later.txt", "later\n")
    coord.tick()
    second = rig.store.latest_candidate(TASK)["sha"]
    assert second != first
    assert rig.store.contract_binding(TASK, second)["baseline_sha"] == second_tip
    followup = _commit_on(executor.worktrees[TASK], "out/A-fix.txt")
    _candidate(rig, followup)
    bound = contract_for_candidate(rig.store, TASK, followup, rig.project)
    assert bound.baseline_sha == second_tip
    assert bound.baseline_sha not in {first_tip, original}
    assert changed_files(rig.project, followup, bound.baseline_sha) == ["out/A-fix.txt", "out/A.txt"]


def test_candidate_not_descended_from_the_rebased_binding_keeps_the_original_baseline(tmp_path: Path) -> None:
    rig, _executor, _coord, original, _tip, rebased = _rebased_task(tmp_path)
    sibling = _git(rig, "commit-tree", f"{rebased}^{{tree}}", "-p", original, "-m", "sibling")
    bound = contract_for_candidate(rig.store, TASK, sibling, rig.project)
    assert bound.baseline_sha == original
    assert any(p.startswith("other/") for p in changed_files(rig.project, sibling, bound.baseline_sha))  # over-reported, never hidden


def test_binding_whose_baseline_is_not_in_its_candidates_history_is_ignored(tmp_path: Path) -> None:
    rig, executor, _coord, _original, tip, rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    poisoned = _commit_on(worktree, "out/A-poison.txt")
    foreign_base = _git(rig, "commit-tree", f"{rebased}^{{tree}}", "-m", "unrelated root")
    _candidate(rig, poisoned)
    _bind(rig, TASK, poisoned, foreign_base)
    child = _commit_on(worktree, "out/A-child.txt")
    bound = contract_for_candidate(rig.store, TASK, child, rig.project)
    assert bound.baseline_sha == tip != foreign_base


def test_bindings_of_another_task_are_never_used(tmp_path: Path) -> None:
    rig, executor, _coord, _original, tip, rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    first = _commit_on(worktree, "out/A-first.txt")
    rig.store.upsert_task("other", source="local", source_id="OTHER-TASK")
    rig.store.add_candidate("OTHER-TASK", first, "scripted", durable_handoff=True)
    _bind(rig, "OTHER-TASK", first, rebased)  # would hide A.txt from the next diff if it were honoured
    child = _commit_on(worktree, "out/A-second.txt")
    bound = contract_for_candidate(rig.store, TASK, child, rig.project)
    assert bound.baseline_sha == tip
    assert "out/A.txt" in changed_files(rig.project, child, bound.baseline_sha)


def test_ambiguous_binding_history_fails_closed_to_the_original_baseline(tmp_path: Path) -> None:
    rig, _executor, _coord, original, tip, rebased = _rebased_task(tmp_path)
    tree = f"{rebased}^{{tree}}"
    left = _git(rig, "commit-tree", tree, "-p", rebased, "-m", "left")
    right = _git(rig, "commit-tree", tree, "-p", rebased, "-m", "right")
    older_main = _git(rig, "rev-parse", f"{tip}~1")
    _bind(rig, TASK, left, older_main)  # disagrees with the rebase binding (baseline tip) and is not a chain with its sibling
    _bind(rig, TASK, right, tip)
    merged = _git(rig, "commit-tree", tree, "-p", left, "-p", right, "-m", "merge")
    bound = contract_for_candidate(rig.store, TASK, merged, rig.project)
    assert bound.baseline_sha == original  # the oldest base only over-reports; a newer one could hide task-owned changes


def test_baseline_that_already_contains_the_tasks_own_work_is_never_inherited(tmp_path: Path) -> None:
    rig, executor, _coord, _original, tip, rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    first = _commit_on(worktree, "out/A-first.txt")
    _candidate(rig, first)
    second = _commit_on(worktree, "out/A-second.txt")
    _candidate(rig, second)
    _bind(rig, TASK, second, first)  # a valid ancestor, but it contains out/A.txt and out/A-first.txt: using it would hide them
    third = _commit_on(worktree, "out/A-third.txt")
    bound = contract_for_candidate(rig.store, TASK, third, rig.project)
    assert bound.baseline_sha == tip
    assert {"out/A.txt", "out/A-first.txt", "out/A-second.txt", "out/A-third.txt"} <= set(changed_files(rig.project, third, bound.baseline_sha))


def test_task_without_a_recorded_baseline_never_inherits(tmp_path: Path) -> None:
    rig, executor, _coord, _original, tip, rebased = _rebased_task(tmp_path)
    rig.store.conn.execute("DELETE FROM task_baselines WHERE task_id=?", (TASK,))
    rig.store.conn.commit()
    child = _commit_on(executor.worktrees[TASK], "out/A-child.txt")
    bound = contract_for_candidate(rig.store, TASK, child, rig.project)
    assert bound.baseline_sha == rebased  # legacy behaviour: the parent, which over-reports nothing it did not already bind


def test_frozen_contract_task_without_a_recorded_baseline_never_inherits(tmp_path: Path) -> None:
    rig, executor, _coord, original, _tip, rebased = _rebased_task(tmp_path)
    from stagemesh.contract_binding import bind_task_contract

    rig.store.conn.execute("DELETE FROM task_contracts WHERE task_id=?", (TASK,))
    bind_task_contract(rig.store, rig.project, TASK, original)  # the production path: freezes the contract file with the original baseline
    rig.store.conn.execute("DELETE FROM task_baselines WHERE task_id=?", (TASK,))
    rig.store.conn.commit()
    child = _commit_on(executor.worktrees[TASK], "out/A-child.txt")
    bound = contract_for_candidate(rig.store, TASK, child, rig.project)
    assert bound.baseline_sha == original  # the frozen baseline, not the rebased one


def test_unknown_ancestry_fails_closed_to_the_original_baseline(tmp_path: Path) -> None:
    rig, executor, _coord, original, _tip, _rebased = _rebased_task(tmp_path)
    missing = "1234567890abcdef1234567890abcdef12345678"  # a same-task candidate whose object git cannot find
    rig.store.add_candidate(TASK, missing, "scripted", durable_handoff=True)
    child = _commit_on(executor.worktrees[TASK], "out/A-child.txt")
    bound = contract_for_candidate(rig.store, TASK, child, rig.project)
    assert bound.baseline_sha == original


def test_provider_merging_main_into_the_worktree_is_over_reported_and_fails_scope(tmp_path: Path) -> None:
    rig, executor, _coord, _original, tip, _rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    newer = _land_on_main(rig, "other/merged-in.txt", "merged\n")
    GitWorkspace(worktree).run("merge", "--no-edit", newer)
    merged = GitWorkspace(worktree).head()
    _candidate(rig, merged)
    bound = contract_for_candidate(rig.store, TASK, merged, rig.project)
    assert bound.baseline_sha == tip
    assert "other/merged-in.txt" in changed_files(rig.project, merged, bound.baseline_sha)
    assert any(f["code"] == "outside_allowed_files" and f["path"] == "other/merged-in.txt" for f in _findings(rig, merged))


def test_provider_resetting_head_onto_main_cannot_hide_main_files(tmp_path: Path) -> None:
    rig, executor, _coord, original, _main, _rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    GitWorkspace(worktree).run("reset", "--hard", _tip(rig))
    sha = _commit_on(worktree, "out/A.txt", "rewritten\n")
    _candidate(rig, sha)
    bound = contract_for_candidate(rig.store, TASK, sha, rig.project)
    assert bound.baseline_sha == original
    assert any(f["code"] == "outside_allowed_files" and f["path"].startswith("other/") for f in _findings(rig, sha))


def test_untracked_and_unrelated_files_in_a_candidate_are_flagged_after_a_rebase(tmp_path: Path) -> None:
    rig, executor, _coord, _original, _tip, _rebased = _rebased_task(tmp_path)
    worktree = executor.worktrees[TASK]
    (worktree / "scratch").mkdir()
    (worktree / "scratch" / "notes.txt").write_text("untracked\n", encoding="utf-8")  # swept in by `git add -A`, like provider leftovers
    sha = GitWorkspace(worktree).commit_all("with leftovers")
    _candidate(rig, sha)
    assert any(f["code"] == "outside_allowed_files" and f["path"] == "scratch/notes.txt" for f in _findings(rig, sha))


# ---- integration correctness ---------------------------------------------------------------------------------------------------


def test_landed_candidate_delta_excludes_everything_main_gained_meanwhile(tmp_path: Path) -> None:
    rig, _executor, coord, _original, _main, _rebased = _rebased_task(tmp_path)
    _advance_until(rig, coord, Stage.DONE)
    final = rig.store.latest_candidate(TASK)["sha"]
    binding = rig.store.contract_binding(TASK, final)
    assert changed_files(rig.project, final, binding["baseline_sha"]) == ["out/A.txt"]
    assert _tip(rig) == final
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        assert _passed(rig, final, kind)


class _Overreaching(ScriptedExecutor):
    """Writes its contracted file plus an out-of-scope one."""

    def run(self, store, task_id, claim_id, project):  # type: ignore[no-untyped-def]
        from stagemesh.workspaces import prepare_task_workspace

        extra = prepare_task_workspace(project, task_id) / "unrelated" / "extra.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("extra\n", encoding="utf-8")
        return super().run(store, task_id, claim_id, project)


def test_scope_violation_is_not_laundered_by_a_rebase(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, _Overreaching(rig.files))
    _advance_until(rig, coord, Stage.VALIDATE)
    rig.store.advance_task(TASK, Stage.INTEGRATE)  # as if a stale approval had been trusted
    main_tip = _land_on_main(rig, "other/work.txt", "other\n")
    coord.tick()  # stale base: rebases, never integrates
    rebased = rig.store.latest_candidate(TASK)["sha"]
    assert _tip(rig) == main_tip
    assert not _passed(rig, rebased, EvidenceKind.INTEGRATION)
    for _ in range(6):
        coord.tick()
    assert _tip(rig) == main_tip  # the ref never took the over-reaching candidate
    assert not _passed(rig, rebased, EvidenceKind.VALIDATION)
    assert any(f["code"] == "outside_allowed_files" and f["path"] == "unrelated/extra.txt" for f in _findings(rig, rebased))
    assert rig.store.get_task(TASK)["status"] != TaskStatus.DONE


def test_new_candidate_has_no_evidence_and_cannot_integrate_on_its_predecessors(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    executor = ScriptedExecutor(rig.files)
    coord = _single_task_coordinator(rig, executor)
    _advance_until(rig, coord, Stage.INTEGRATE)
    approved = rig.store.latest_candidate(TASK)["sha"]
    assert _passed(rig, approved, EvidenceKind.VALIDATION) and _passed(rig, approved, EvidenceKind.REVIEW)
    newer = _commit_on(executor.worktrees[TASK], "out/A-late.txt")
    _candidate(rig, newer)
    contract_for_candidate(rig.store, TASK, newer, rig.project)
    before = _tip(rig)
    assert not _passed(rig, newer, EvidenceKind.VALIDATION) and not _passed(rig, newer, EvidenceKind.REVIEW)

    assert coord.integrator.integrate(rig.store, TASK, newer, rig.project) == EvidenceStatus.FAILED

    assert _tip(rig) == before
    codes = {
        f["code"]
        for row in rig.store.conn.execute(
            "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=?", (TASK, newer, EvidenceKind.INTEGRATION)
        )
        for f in json.loads(row["payload"])["findings"]
    }
    assert "missing_required_bound_evidence" in codes


def test_ref_does_not_advance_when_required_evidence_is_missing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, [TASK])
    coord = _single_task_coordinator(rig, ScriptedExecutor(rig.files))
    _advance_until(rig, coord, Stage.INTEGRATE)
    candidate = rig.store.latest_candidate(TASK)["sha"]
    before = _tip(rig)
    for kind in (EvidenceKind.REVIEW, EvidenceKind.VALIDATION):
        saved = [tuple(r) for r in rig.store.conn.execute("SELECT * FROM evidence WHERE task_id=? AND kind=?", (TASK, kind))]
        rig.store.conn.execute("DELETE FROM evidence WHERE task_id=? AND kind=?", (TASK, kind))
        rig.store.conn.commit()
        assert coord.integrator.integrate(rig.store, TASK, candidate, rig.project) == EvidenceStatus.FAILED
        assert _tip(rig) == before
        for row in saved:
            rig.store.conn.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        rig.store.conn.commit()
    strict = SerializedIntegrator(rig.ref, True, rig.lock)  # independent review required
    result = strict.integrate(rig.store, TASK, candidate, rig.project)
    assert result == EvidenceStatus.FAILED  # the rig's reviewer is not independent, so the strict gate must refuse
    assert _tip(rig) == before
    latest = rig.store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY rowid DESC LIMIT 1",
        (TASK, candidate, EvidenceKind.INTEGRATION),
    ).fetchone()
    message = " ".join(f["message"] for f in json.loads(latest["payload"])["findings"])
    assert "independent REVIEW" in message
