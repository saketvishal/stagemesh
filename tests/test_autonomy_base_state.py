"""Capabilities 3 and 4: ordinary main advancement (Scenario B) and force-rewritten history (Scenario C)."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import (
    HUMAN,
    TASK,
    add_passing_evidence,
    commit,
    decisions,
    git,
    init_repo,
    new_store,
    seed_candidate,
    tree_of,
)

from stagemesh.autonomy import base_state as base
from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.provenance import load_provenance
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage


def _repo_with_candidate(tmp_path: Path, candidate_files: dict[str, str | None] | None = None) -> tuple[Path, str, str]:
    """main = base; a candidate branch adds the widget on top of base; main is left checked out at base."""
    repo = init_repo(tmp_path / "repo")
    base_sha = commit(repo, {"src/app.py": "VALUE = 1\n", "README.md": "readme\n"}, "base")
    git(repo, "checkout", "-q", "-b", "candidate")
    candidate = commit(repo, candidate_files or {"src/widget.py": "W = 1\n"}, "implement widget")
    git(repo, "checkout", "-q", "main")
    return repo, base_sha, candidate


def _task_under_review(tmp_path: Path, repo: Path, base_sha: str, candidate: str):
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base_sha, candidate=candidate, stage=Stage.REVIEW)
    add_passing_evidence(store, TASK, candidate, baseline=base_sha)
    return store, Supervisor(store, repo, integration_ref="main")


# --- Scenario B: ordinary main advancement ------------------------------------------------------------------------------------------------


def test_scenario_b_normal_main_advancement_refreshes_and_revalidates_without_the_founder(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    assert load_provenance(store, TASK).authorizes_integration()
    main_tip = commit(repo, {"docs/notes.md": "an unrelated normal commit\n"}, "unrelated work lands on main")

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None
    assert decision.condition is Condition.BASE_ADVANCED
    assert decision.action is Action.REFRESH_CANDIDATE
    assert not decision.requires_human
    replacement = decision.shas["replacement_candidate"]
    assert decision.shas["original_candidate"] == candidate and replacement != candidate
    # the replacement sits on the new tip and carries exactly the candidate's own change
    assert git(repo, "rev-parse", f"{replacement}^") == main_tip
    assert GitFacts(repo).changed_paths(main_tip, replacement) == ["src/widget.py"]
    assert git(repo, "show", f"{replacement}:docs/notes.md") == "an unrelated normal commit"
    # the original is preserved untouched; nothing was force-pushed or rewritten
    assert git(repo, "rev-parse", decision.detail["preserved_original_ref"]) == candidate
    assert git(repo, "rev-parse", "candidate") == candidate
    assert decision.detail["original_candidate_rewritten"] is False and decision.detail["force_push"] is False
    # evidence for the original no longer authorizes; the task is back at VALIDATE for the exact replacement
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert store.latest_candidate(TASK)["sha"] == replacement
    provenance = load_provenance(store, TASK)
    assert provenance.lineage == (candidate, replacement) and provenance.baseline_sha == main_tip
    assert not provenance.authorizes_integration()
    assert decision.detail["evidence_invalidated"] == ["VALIDATION", "REVIEW"]
    # explained in the durable trace
    (record,) = decisions(store, TASK)
    assert record["trace"].startswith("BASE_ADVANCED old_base=") and "action=REFRESH_CANDIDATE" in record["trace"]
    assert f"replacement_candidate={replacement[:7]}" in record["trace"]


def test_scenario_b_unchanged_base_needs_no_decision(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    assert supervisor.reconcile_base(TASK) is None
    assert decisions(store, TASK) == [] and store.get_task(TASK)["stage"] == Stage.REVIEW


def test_scenario_b_fresh_candidate_that_already_contains_the_new_tip_proceeds(tmp_path: Path) -> None:
    repo, base_sha, _ = _repo_with_candidate(tmp_path)
    main_tip = commit(repo, {"docs/notes.md": "x\n"}, "main moves")
    git(repo, "checkout", "-q", "-b", "fresh")
    fresh = commit(repo, {"src/widget.py": "W = 1\n"}, "built after main moved")
    git(repo, "checkout", "-q", "main")
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, fresh)

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.action is Action.PROCEED and decision.condition is Condition.BASE_ADVANCED
    assert store.latest_candidate(TASK)["sha"] == fresh
    assert main_tip


def test_scenario_b_candidate_already_on_main_is_left_alone(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    _store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    git(repo, "merge", "-q", "--ff-only", candidate)
    decision = supervisor.reconcile_base(TASK)
    assert decision is not None and decision.condition is Condition.CANDIDATE_ALREADY_INTEGRATED
    assert decision.action is Action.PROCEED


def test_scenario_b_conflict_is_reconstructed_once_then_escalates_with_a_specific_question(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path, {"src/app.py": "VALUE = 2\n"})
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    commit(repo, {"src/app.py": "VALUE = 3\n"}, "main changes the same line differently")

    first = supervisor.reconcile_base(TASK)
    assert first is not None and first.action is Action.RECONSTRUCT_ON_NEW_BASE and not first.requires_human
    assert "src/app.py" in first.observed["conflicts"]
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT  # a fresh attempt on the new base, with the conflict as its context
    assert store.open_findings_for_candidate(TASK, candidate)

    second = supervisor.reconcile_base(TASK)
    assert second is not None and second.action is Action.ESCALATE_TO_FOUNDER
    escalation = second.escalation
    assert escalation is not None and escalation.reason is EscalationReason.CONFLICT_REQUIRES_SEMANTIC_PRODUCT_DECISION
    assert len(escalation.attempted) >= 2 and "src/app.py" in escalation.why_undeterminable
    assert escalation.smallest_decision.endswith("?") and "rebase" not in escalation.smallest_decision.lower()


def test_scenario_b_mechanical_transplant_keeps_every_commit_and_the_original_author(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base_sha = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "candidate")
    commit(repo, {"src/a.py": "A = 1\n"}, "first commit", who=HUMAN)
    candidate = commit(repo, {"src/b.py": "B = 1\n"}, "second commit", who=HUMAN)
    git(repo, "checkout", "-q", "main")
    _store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    main_tip = commit(repo, {"docs/n.md": "n\n"}, "main moves")

    decision = supervisor.reconcile_base(TASK)

    replacement = decision.shas["replacement_candidate"]
    assert git(repo, "rev-list", "--count", f"{main_tip}..{replacement}") == "2"
    assert git(repo, "log", "-1", "--format=%an <%ae>", replacement) == f"{HUMAN[0]} <{HUMAN[1]}>"
    assert "StageMesh-Retargeted-From:" in git(repo, "log", "-1", "--format=%B", replacement)


# --- Scenario C: equivalent-tree history rewrite --------------------------------------------------------------------------------------------


def _rewrite_main_with_identical_tree(repo: Path, old_main: str) -> str:
    """Force-rewrite main to a new root commit whose tree is identical to the old tip's: different SHAs, different ancestry."""
    tree = tree_of(repo, old_main)
    rewritten = git(repo, "commit-tree", tree, "-m", "squashed history", who=HUMAN)
    git(repo, "update-ref", "refs/heads/main", rewritten)
    return rewritten


def test_scenario_c_equivalent_tree_history_rewrite_is_retargeted_with_proof_and_provenance(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    rewritten = _rewrite_main_with_identical_tree(repo, base_sha)
    assert rewritten != base_sha and tree_of(repo, rewritten) == tree_of(repo, base_sha)
    assert git(repo, "merge-base", "--is-ancestor", base_sha, rewritten, check=False) == ""  # ancestry really is different
    assert not GitFacts(repo).is_ancestor(base_sha, rewritten)

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None
    assert decision.condition is Condition.BASE_HISTORY_REWRITTEN
    assert decision.action is Action.CREATE_RETARGETED_CANDIDATE
    assert not decision.requires_human
    replacement = decision.shas["replacement_candidate"]
    proof = decision.detail["proof"]
    assert proof["base_trees_identical"] is True and proof["candidate_trees_identical"] is True and proof["proven"] is True
    assert tree_of(repo, replacement) == tree_of(repo, candidate)  # tree/content equivalence
    assert git(repo, "rev-parse", f"{replacement}^") == rewritten  # clean replacement on the rewritten base
    assert git(repo, "rev-parse", decision.detail["preserved_original_ref"]) == candidate  # original preserved, not rewritten
    assert git(repo, "rev-parse", "candidate") == candidate
    assert not GitFacts(repo).is_ancestor(candidate, replacement)
    # provenance original -> replacement, and nothing inherited: revalidate and rereview
    provenance = load_provenance(store, TASK)
    assert provenance.lineage == (candidate, replacement) and not provenance.authorizes_integration()
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    # the exact trace the founder would want to read
    (record,) = decisions(store, TASK)
    tokens = record["trace"].split()
    assert tokens[0] == "BASE_HISTORY_REWRITTEN"
    assert f"old_base={base_sha[:7]}" in tokens and f"new_base={rewritten[:7]}" in tokens and "tree_equivalent=true" in tokens
    assert f"original_candidate={candidate[:7]}" in tokens and f"replacement_candidate={replacement[:7]}" in tokens
    assert "action=CREATE_RETARGETED_CANDIDATE" in tokens and "human_escalation=false" in tokens


def test_scenario_c_rewrite_with_changed_content_is_distinguished_from_an_equivalent_one(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    tree_changed = commit(repo, {"README.md": "readme, but main was force-rewritten with a doc edit\n"}, "edit")
    changed_tree = tree_of(repo, tree_changed)
    rewritten = git(repo, "commit-tree", changed_tree, "-m", "rewritten with different content", who=HUMAN)
    git(repo, "update-ref", "refs/heads/main", rewritten)

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.condition is Condition.BASE_HISTORY_REWRITTEN_CONTENT_CHANGED
    assert decision.action is Action.CREATE_RETARGETED_CANDIDATE
    assert decision.observed["tree_equivalent"] == "false"
    assert decision.detail["proof"]["base_trees_identical"] is False
    assert decision.detail["proof"]["scope_preserved"] is True  # only the candidate's own change was transplanted
    replacement = decision.shas["replacement_candidate"]
    assert GitFacts(repo).changed_paths(rewritten, replacement) == ["src/widget.py"]
    assert not load_provenance(store, TASK).authorizes_integration()


def test_scenario_c_never_force_pushes_or_rewrites_the_original_branch(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    _store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    before = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    _rewrite_main_with_identical_tree(repo, base_sha)
    after_rewrite = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")

    supervisor.reconcile_base(TASK)

    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads") == after_rewrite
    assert "candidate " + candidate in before
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/stagemesh").splitlines()
    assert any(ref.startswith("refs/stagemesh/preserved/") for ref in refs)
    assert any(ref.startswith("refs/stagemesh/candidates/") for ref in refs)


def test_scenario_c_rewrite_that_conflicts_is_reconstructed_not_force_adopted(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path, {"src/app.py": "VALUE = 2\n"})
    _store, supervisor = _task_under_review(tmp_path, repo, base_sha, candidate)
    edited = commit(repo, {"src/app.py": "VALUE = 9\n"}, "main edit")
    rewritten = git(repo, "commit-tree", tree_of(repo, edited), "-m", "rewritten", who=HUMAN)
    git(repo, "update-ref", "refs/heads/main", rewritten)

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.condition is Condition.BASE_HISTORY_REWRITTEN_CONTENT_CHANGED
    assert decision.action is Action.RECONSTRUCT_ON_NEW_BASE
    assert git(repo, "rev-parse", f"refs/stagemesh/preserved/{_key()}/{candidate[:12]}") == candidate


def test_unrecoverable_old_base_escalates_with_the_three_required_parts(tmp_path: Path) -> None:
    repo, _base_sha, candidate = _repo_with_candidate(tmp_path)
    store = new_store(tmp_path)
    ghost = "f" * 40  # a base that no longer exists in the object store
    seed_candidate(store, TASK, baseline=ghost, candidate=candidate, stage=Stage.REVIEW)
    supervisor = Supervisor(store, repo, integration_ref="main")
    commit(repo, {"docs/n.md": "n\n"}, "main moves")

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.condition is Condition.BASE_PROVENANCE_UNRECOVERABLE
    assert decision.action is Action.ESCALATE_TO_FOUNDER
    escalation = decision.escalation
    assert escalation is not None and escalation.reason is EscalationReason.BASE_PROVENANCE_UNRECOVERABLE
    assert escalation.attempted and escalation.why_undeterminable and escalation.smallest_decision


def test_equivalence_proof_is_not_proven_when_the_replacement_tree_differs(tmp_path: Path) -> None:
    repo, base_sha, candidate = _repo_with_candidate(tmp_path)
    facts = GitFacts(repo)
    rewritten = git(repo, "commit-tree", tree_of(repo, base_sha), "-m", "same tree", who=HUMAN)
    wrong = git(repo, "commit-tree", tree_of(repo, candidate), "-p", rewritten, "-m", "extra", who=HUMAN)
    git(repo, "checkout", "-q", "-b", "drifted", wrong)
    drifted = commit(repo, {"src/extra.py": "E = 1\n"}, "drift")
    proof = base.prove_equivalence(facts, candidate=candidate, old_base=base_sha, replacement=drifted, new_base=rewritten)
    assert proof.base_trees_identical and not proof.candidate_trees_identical and not proof.proven


def _key() -> str:
    from stagemesh.workspaces import _task_key

    return _task_key(TASK)
