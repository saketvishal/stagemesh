"""Capability 1 (candidate integrity) and capability 2 (external workspace mutation), including Scenario A."""

from __future__ import annotations

from pathlib import Path

import pytest
from autonomy_support import (
    HUMAN,
    STAGEMESH,
    TASK,
    add_passing_evidence,
    commit,
    decisions,
    git,
    init_repo,
    new_store,
    seed_candidate,
)

from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.provenance import (
    WorkspaceOwnership,
    check_ownership,
    claim_workspace,
    load_ownership,
    load_provenance,
    record_lineage,
)
from stagemesh.autonomy.supervisor import ExternalWorkspaceMutation, Supervisor
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n", "README.md": "readme\n"}, "base")
    worktree = repo / ".stagemesh" / "worktrees" / "wt"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    return repo, worktree, base


# --- capability 1: candidate integrity ----------------------------------------------------------------------------------------------


def test_evidence_authorizes_integration_only_for_the_exact_candidate(tmp_path: Path) -> None:
    _repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    first = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    seed_candidate(store, TASK, baseline=base, candidate=first)
    add_passing_evidence(store, TASK, first, baseline=base)

    provenance = load_provenance(store, TASK)
    assert (provenance.baseline_sha, provenance.candidate_sha) == (base, first)
    assert provenance.validation_sha == provenance.review_sha == first
    assert provenance.authorizes_integration()

    # remediation produces a new candidate: the old validation/review no longer authorize anything
    second = commit(worktree, {"src/widget.py": "W = 2\n"}, "remediate")
    store.add_candidate(TASK, second, "codex", durable_handoff=True)
    stale = load_provenance(store, TASK)
    assert stale.candidate_sha == second
    assert (stale.validation_sha, stale.review_sha) == (first, first)  # remembered, but bound to the old SHA
    assert not stale.authorizes_integration()
    assert any("validation is bound to" in problem for problem in stale.evidence_problems())

    add_passing_evidence(store, TASK, second, baseline=base)
    assert load_provenance(store, TASK).authorizes_integration()


def test_review_for_an_older_sha_cannot_stand_in_for_the_candidate(tmp_path: Path) -> None:
    _repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    first = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    second = commit(worktree, {"src/widget.py": "W = 2\n"}, "remediate")
    seed_candidate(store, TASK, baseline=base, candidate=first)
    add_passing_evidence(store, TASK, first, baseline=base)
    store.add_candidate(TASK, second, "codex", durable_handoff=True)
    store.add_evidence(TASK, second, EvidenceKind.VALIDATION, EvidenceStatus.PASSED, {})  # validated, never re-reviewed

    problems = load_provenance(store, TASK).evidence_problems()
    assert problems == [f"review is bound to {first[:7]}, not candidate {second[:7]}"]


def test_replacement_candidate_does_not_inherit_its_originals_evidence(tmp_path: Path) -> None:
    _repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    original = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    seed_candidate(store, TASK, baseline=base, candidate=original)
    add_passing_evidence(store, TASK, original, baseline=base)
    replacement = commit(worktree, {"src/widget.py": "W = 1\n", "src/extra.py": "E = 1\n"}, "retargeted")
    store.add_candidate(TASK, replacement, "codex", durable_handoff=True)
    record_lineage(store, TASK, original, replacement, "BASE_HISTORY_REWRITTEN")

    provenance = load_provenance(store, TASK)
    assert provenance.lineage == (original, replacement)
    assert provenance.original_candidate_sha == original
    assert not provenance.authorizes_integration()


def test_integration_sha_is_recorded_from_passed_integration_evidence(tmp_path: Path) -> None:
    _repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    sha = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    seed_candidate(store, TASK, baseline=base, candidate=sha)
    assert load_provenance(store, TASK).integration_sha is None
    store.add_evidence(TASK, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED, {"integration_ref_after": sha})
    assert load_provenance(store, TASK).integration_sha == sha


# --- capability 2 / Scenario A: unexpected second writer ----------------------------------------------------------------------------------


def test_scenario_a_second_writer_commit_is_detected_quarantined_and_never_adopted(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    store.set_task_baseline(TASK, base)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree, owner_execution_id="exec-1")

    foreign = commit(worktree, {"src/backdoor.py": "SECRET = 'x'\n"}, "sneaky", who=HUMAN)  # another process commits mid-run

    decision = supervisor.check_workspace(TASK)

    assert decision is not None
    assert decision.condition is Condition.EXTERNAL_WORKSPACE_MUTATION
    assert decision.action is Action.FAIL_CLOSED_QUARANTINE
    assert not decision.requires_human  # ordinary operational situation: no founder question
    assert decision.shas["expected_head"] == base and decision.shas["observed_head"] == foreign
    assert decision.shas["foreign_commit"] == foreign
    assert decision.detail["adopted"] is False
    assert decision.detail["evidence_for_mutated_state_authorizes_integration"] is False
    # the other writer's work is preserved, not lost, and not adopted
    quarantine_ref = decision.detail["quarantine_refs"]["worktree_snapshot"]
    assert "SECRET" in git(repo, "show", f"{quarantine_ref}:src/backdoor.py")
    # the owned workspace is restored to what StageMesh recorded, and no candidate was created from the foreign commit
    assert git(worktree, "rev-parse", "HEAD") == base
    assert not (worktree / "src" / "backdoor.py").exists()
    assert store.latest_candidate(TASK) is None
    # durable trace
    trace = decisions(store, TASK)
    assert len(trace) == 1 and trace[0]["trace"].startswith("EXTERNAL_WORKSPACE_MUTATION ")
    assert "action=FAIL_CLOSED_QUARANTINE" in trace[0]["trace"] and "human_escalation=false" in trace[0]["trace"]


def test_scenario_a_repeated_checks_do_not_flood_the_trace(tmp_path: Path) -> None:
    repo, worktree, _base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree, owner_execution_id="exec-1")
    execution = store.start_execution(task_id=TASK, claim_id=None, kind=ExecutionKind.IMPLEMENTATION)  # an execution owns it: never reset under it
    commit(worktree, {"src/backdoor.py": "x\n"}, "sneaky", who=HUMAN)

    first = supervisor.check_workspace(TASK)
    second = supervisor.check_workspace(TASK)

    assert first is not None and second is not None
    assert first.detail["workspace_restored_to_recorded_head"] is False  # execution still running: preserved, not reset
    assert len(decisions(store, TASK)) == 1
    assert execution


def test_scenario_a_second_writer_push_to_the_published_candidate_branch(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "push", "-q", "origin", f"{base}:refs/heads/feat/x")
    git(repo, "fetch", "-q", "origin")
    store = new_store(tmp_path)
    candidate = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    seed_candidate(store, TASK, baseline=base, candidate=candidate)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree, remote="origin", remote_branch="feat/x")

    other = tmp_path / "other-clone"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    git(other, "checkout", "-q", "-B", "feat/x", "origin/feat/x")
    pushed = commit(other, {"src/widget.py": "W = 666\n"}, "second writer", who=HUMAN)
    git(other, "push", "-q", "origin", "feat/x")

    decision = supervisor.check_workspace(TASK, fetch=True)

    assert decision is not None and decision.condition is Condition.EXTERNAL_WORKSPACE_MUTATION
    assert decision.detail["mutations"][0]["kind"] == "REMOTE_REF_MOVED"
    assert decision.detail["adopted"] is False
    assert git(repo, "rev-parse", decision.detail["quarantine_refs"]["REMOTE_REF_MOVED"]) == pushed
    replacement = decision.detail["quarantine_refs"]["replacement_branch"]
    assert git(repo, "rev-parse", replacement) == candidate  # clean replacement branch from the recorded candidate
    assert decision.detail["published_replacement_required"] is True
    assert git(remote, "rev-parse", "feat/x") == pushed  # StageMesh did not force-push or rewrite the remote
    assert load_ownership(store, TASK).expected_remote_tip == base  # expectation unchanged: the push was not accepted


def test_stagemesh_own_commit_is_an_owner_advance_not_a_mutation(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    mine = commit(worktree, {"src/widget.py": "W = 1\n"}, "StageMesh implementation", who=STAGEMESH)
    supervisor.candidate_committed(TASK, mine)  # StageMesh registers what it committed; trust is not inferred from an identity

    assert supervisor.check_workspace(TASK) is None
    assert load_ownership(store, TASK).expected_head == mine
    assert decisions(store, TASK) == []


def test_head_rewrite_is_a_mutation_even_by_a_trusted_identity(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    first = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    git(worktree, "reset", "--hard", "-q", base)  # history moved backwards under the owner
    commit(worktree, {"src/other.py": "O = 1\n"}, "different work")

    decision = supervisor.check_workspace(TASK)

    assert decision is not None
    assert decision.detail["mutations"][0]["kind"] == "HEAD_REWRITTEN"
    assert decision.shas["expected_head"] == first


def test_tracked_file_edit_is_a_mutation_only_while_no_execution_owns_the_worktree(tmp_path: Path) -> None:
    repo, worktree, _base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "src" / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
    (worktree / "scratch.txt").write_text("untracked agent scratch\n", encoding="utf-8")

    assert supervisor.check_workspace(TASK, execution_running=True) is None  # the agent is allowed to edit while it runs
    decision = supervisor.check_workspace(TASK, execution_running=False)
    assert decision is not None and decision.detail["mutations"][0]["kind"] == "TRACKED_FILES_MODIFIED"
    assert "VALUE = 999" in git(repo, "show", f"{decision.detail['quarantine_refs']['worktree_snapshot']}:src/app.py")
    assert (worktree / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_missing_worktree_fails_closed(tmp_path: Path) -> None:
    repo, worktree, _base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    ownership = claim_workspace(store, TASK, worktree)
    git(repo, "worktree", "remove", "--force", str(worktree))

    check = check_ownership(ownership)
    assert [m.kind for m in check.mutations] == ["WORKTREE_MISSING"]


def test_untrusted_commit_inside_the_candidate_taints_it_and_revokes_its_evidence(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    commit(worktree, {"src/widget.py": "W = 1\n"}, "mine", who=STAGEMESH)
    foreign = commit(worktree, {"src/backdoor.py": "x\n"}, "adopted by mistake", who=HUMAN)
    seed_candidate(store, TASK, baseline=base, candidate=foreign)
    add_passing_evidence(store, TASK, foreign, baseline=base)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "src" / "app.py").write_text("tampered\n", encoding="utf-8")  # trigger a mutation observation

    decision = supervisor.check_workspace(TASK, execution_running=False)

    assert decision is not None and decision.detail["candidate_tainted"] is True
    provenance = load_provenance(store, TASK)
    assert foreign in provenance.revoked
    assert not provenance.authorizes_integration()


def test_guard_blocks_the_coordinator_until_the_workspace_is_clean(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    sha = commit(worktree, {"src/widget.py": "W = 1\n"}, "implement")
    seed_candidate(store, TASK, baseline=base, candidate=sha, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, sha, baseline=base)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    assert supervisor.allow(Stage.INTEGRATE, TASK, sha) is True

    commit(worktree, {"src/backdoor.py": "x\n"}, "sneaky", who=HUMAN)
    assert supervisor.allow(Stage.INTEGRATE, TASK, sha) is False
    with pytest.raises(ExternalWorkspaceMutation) as raised:
        commit(worktree, {"src/backdoor2.py": "x\n"}, "sneaky again", who=HUMAN)
        supervisor.require_clean_workspace(TASK)
    assert raised.value.code == "EXTERNAL_WORKSPACE_MUTATION"


def test_ownership_round_trips_through_the_durable_store(tmp_path: Path) -> None:
    _repo, worktree, _base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    claimed = claim_workspace(store, TASK, worktree, owner_execution_id="exec-9")
    loaded = load_ownership(store, TASK)
    assert isinstance(loaded, WorkspaceOwnership) and loaded == claimed


def test_a_registered_ref_moved_by_a_trusted_identity_is_still_a_mutation(tmp_path: Path) -> None:
    """Independent-review finding: only HEAD may be advanced by trusted identity; candidate/remote refs must move by registration."""
    repo, worktree, base = _repo_with_worktree(tmp_path)
    git(repo, "branch", "feat/cand", base)
    store = new_store(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree, candidate_ref="refs/heads/feat/cand")
    git(repo, "checkout", "-q", "feat/cand")
    moved = commit(repo, {"src/other.py": "O = 1\n"}, "moved by another process using the StageMesh identity", who=STAGEMESH)
    git(repo, "checkout", "-q", "main")

    decision = supervisor.check_workspace(TASK)

    assert decision is not None and decision.detail["mutations"][0]["kind"] == "CANDIDATE_REF_MOVED"
    assert git(repo, "rev-parse", "refs/heads/feat/cand") == base  # restored to the registered tip (the moved commit is preserved)
    assert git(repo, "rev-parse", decision.detail["quarantine_refs"]["CANDIDATE_REF_MOVED"]) == moved
