"""Capability 5: PR dependencies and stacked PRs (Scenarios D and J)."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import (
    add_passing_evidence,
    commit,
    decisions,
    git,
    init_repo,
    new_store,
    seed_candidate,
)

from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.dependencies import (
    CIRollup,
    FakePullRequests,
    PRDependency,
    PRState,
    PullRequest,
    evaluate_dependencies,
    find_cycle,
    implicit_dependencies,
)
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.provenance import load_provenance
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage

T1, T2 = "TASK-1", "TASK-2"


def _pr(number: int, head: str, base: str = "main", state: PRState = PRState.OPEN, ci: CIRollup = CIRollup.SUCCESS, mergeable: bool | None = True, sha: str = "a") -> PullRequest:
    return PullRequest(number, sha * 40, head, base, state, ci, mergeable)


# --- pure policy -------------------------------------------------------------------------------------------------------------------------


def test_downstream_pr_blocks_while_its_dependency_is_open() -> None:
    prs = {1: _pr(1, "feat/one"), 2: _pr(2, "feat/two", base="feat/one", sha="b")}
    assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1)])
    assert assessment.blocked and assessment.blocked_on == [1] and not assessment.red
    assert assessment.decision.condition is Condition.DEPENDENCY_PENDING and assessment.decision.action is Action.BLOCK_ON_DEPENDENCY
    assert not assessment.decision.requires_human


def test_stacking_is_inferred_from_base_branches_without_any_declaration() -> None:
    prs = {1: _pr(1, "feat/one"), 2: _pr(2, "feat/two", base="feat/one", sha="b")}
    assert implicit_dependencies(prs) == {PRDependency(2, 1)}
    assert evaluate_dependencies(2, prs).blocked_on == [1]


def test_dependency_that_landed_resumes_the_downstream_pr_with_refresh_and_retarget() -> None:
    prs = {1: _pr(1, "feat/one", state=PRState.MERGED), 2: _pr(2, "feat/two", base="feat/one", sha="b")}
    assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1)], was_blocked=True)
    assert assessment.can_resume and assessment.refresh_required and assessment.retarget_base_to == "main"
    assert assessment.decision.condition is Condition.DEPENDENCY_LANDED
    assert assessment.decision.detail["revalidate"] is True and assessment.decision.detail["rereview"] is True


def test_red_or_unmergeable_dependency_blocks_and_names_what_needs_remediation() -> None:
    for dep in (_pr(1, "feat/one", ci=CIRollup.FAILURE), _pr(1, "feat/one", mergeable=False)):
        prs = {1: dep, 2: _pr(2, "feat/two", base="feat/one", sha="b")}
        assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1)])
        assert assessment.blocked and assessment.red == [1]
        assert assessment.decision.condition is Condition.DEPENDENCY_RED
        assert assessment.decision.detail["remediate_dependencies"] == [1]
        assert assessment.decision.detail["resume"] == "automatic once every dependency has landed"


def test_unknown_dependency_state_fails_closed() -> None:
    prs = {2: _pr(2, "feat/two", base="feat/one", sha="b"), 1: None}
    assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1)])
    assert assessment.blocked and assessment.blocked_on == [1]


def test_every_dependency_must_land_before_resuming() -> None:
    prs = {
        1: _pr(1, "a", state=PRState.MERGED),
        3: _pr(3, "c"),
        2: _pr(2, "b", base="a", sha="b"),
    }
    assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1), PRDependency(2, 3)])
    assert assessment.blocked and assessment.blocked_on == [3] and assessment.landed == [1]


def test_dependency_closed_without_landing_escalates_with_a_specific_decision() -> None:
    prs = {1: _pr(1, "feat/one", state=PRState.CLOSED), 2: _pr(2, "feat/two", base="feat/one", sha="b")}
    assessment = evaluate_dependencies(2, prs, [PRDependency(2, 1)])
    escalation = assessment.decision.escalation
    assert escalation is not None and escalation.reason is EscalationReason.DEPENDENCY_CLOSED_WITHOUT_LANDING
    assert "#1" in escalation.smallest_decision and "#2" in escalation.smallest_decision and escalation.smallest_decision.endswith("?")


def test_dependency_cycle_is_detected_and_escalated() -> None:
    edges = [PRDependency(1, 2), PRDependency(2, 3), PRDependency(3, 1)]
    assert find_cycle(edges) is not None and find_cycle([PRDependency(2, 1)]) is None
    prs = {1: _pr(1, "a"), 2: _pr(2, "b"), 3: _pr(3, "c")}
    decision = evaluate_dependencies(1, prs, edges).decision
    assert decision.escalation is not None and decision.escalation.reason is EscalationReason.DEPENDENCY_CYCLE


def test_pr_without_dependencies_proceeds() -> None:
    decision = evaluate_dependencies(1, {1: _pr(1, "feat/one")}).decision
    assert decision.action is Action.PROCEED


# --- Scenario D: stacked PRs with real git -----------------------------------------------------------------------------------------------


def _stack(tmp_path: Path):
    """main -> PR1 branch (feat/one) -> PR2 branch (feat/two); PR2's candidate sits on PR1's head."""
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "feat/one")
    one = commit(repo, {"src/one.py": "ONE = 1\n"}, "PR1 work")
    git(repo, "checkout", "-q", "-b", "feat/two")
    two = commit(repo, {"src/two.py": "TWO = 2\n"}, "PR2 work")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, T2, baseline=one, candidate=two, stage=Stage.REVIEW)  # PR2 was built on PR1's head
    add_passing_evidence(store, T2, two, baseline=one)
    prs = FakePullRequests(
        [
            PullRequest(1, one, "feat/one", "main", PRState.OPEN, CIRollup.SUCCESS, True),
            PullRequest(2, two, "feat/two", "feat/one", PRState.OPEN, CIRollup.SUCCESS, True),
        ]
    )
    supervisor = Supervisor(store, repo, integration_ref="main", pull_requests=prs)
    supervisor.declare_dependency(T2, 2, 1)
    return repo, store, prs, supervisor, base, one, two


def test_scenario_d_pr2_pauses_and_resumes_automatically_after_pr1_is_squash_merged(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, one, two = _stack(tmp_path)

    blocked = supervisor.assess_dependencies(T2, 2)
    assert blocked.blocked and blocked.decision.action is Action.BLOCK_ON_DEPENDENCY and blocked.blocked_on == [1]
    assert store.latest_candidate(T2)["sha"] == two and store.get_task(T2)["stage"] == Stage.REVIEW  # nothing moved
    assert supervisor.assess_dependencies(T2, 2).blocked  # re-evaluation is stable and does not flood the trace
    assert [d["action"] for d in decisions(store, T2)] == ["BLOCK_ON_DEPENDENCY"]

    # PR1 is squash-merged: main gets a NEW commit with PR1's tree, so PR2's recorded base (PR1's head) is not an ancestor of main
    squashed = git(repo, "commit-tree", f"{one}^{{tree}}", "-p", base, "-m", "PR1 (squash)")
    git(repo, "update-ref", "refs/heads/main", squashed)
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, squashed))
    assert not GitFacts(repo).is_ancestor(one, squashed)

    resumed = supervisor.assess_dependencies(T2, 2)  # no founder prompt: the supervisor notices the landing by itself

    assert resumed.can_resume and resumed.decision.condition is Condition.DEPENDENCY_LANDED
    assert ("set_base", 2, "main") in prs.calls  # the PR is retargeted off the merged branch
    replacement = store.latest_candidate(T2)["sha"]
    assert replacement != two
    assert git(repo, "rev-parse", f"{replacement}^") == squashed  # only PR2's own commit was transplanted onto the new main
    assert GitFacts(repo).changed_paths(squashed, replacement) == ["src/two.py"]
    assert store.get_task(T2)["stage"] == Stage.VALIDATE  # validation and review run again on the exact replacement
    assert not load_provenance(store, T2).authorizes_integration()
    assert git(repo, "rev-parse", "feat/two") == two  # the original PR2 branch was never rewritten
    actions = [d["action"] for d in decisions(store, T2)]
    assert actions[0] == "BLOCK_ON_DEPENDENCY" and actions[1] == "RESUME_AFTER_DEPENDENCY"
    assert "REFRESH_CANDIDATE" in actions or "CREATE_RETARGETED_CANDIDATE" in actions


def test_scenario_d_pr1_landing_by_merge_commit_also_resumes_pr2(tmp_path: Path) -> None:
    repo, store, prs, supervisor, _base, one, two = _stack(tmp_path)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge PR1", "feat/one")
    merged = git(repo, "rev-parse", "main")
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, merged))
    assert supervisor.assess_dependencies(T2, 2).blocked is False  # first look already sees it landed (was not blocked before)
    replacement = store.latest_candidate(T2)["sha"]
    assert replacement != two and GitFacts(repo).is_ancestor(merged, replacement)


# --- Scenario J: unresolved dependency ---------------------------------------------------------------------------------------------------------


def test_scenario_j_red_dependency_blocks_downstream_and_resumes_when_it_is_fixed_and_landed(tmp_path: Path) -> None:
    repo, store, prs, supervisor, _base, one, two = _stack(tmp_path)
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.OPEN, CIRollup.FAILURE, True))

    red = supervisor.assess_dependencies(T2, 2)
    assert red.blocked and red.red == [1] and red.decision.condition is Condition.DEPENDENCY_RED
    assert not red.decision.requires_human
    assert store.get_task(T2)["stage"] == Stage.REVIEW  # the downstream task blocked itself; nothing was rewritten

    prs.update(PullRequest(1, one, "feat/one", "main", PRState.OPEN, CIRollup.SUCCESS, False))  # green but unmergeable (conflicts)
    assert supervisor.assess_dependencies(T2, 2).red == [1]

    prs.update(PullRequest(1, one, "feat/one", "main", PRState.OPEN, CIRollup.SUCCESS, True))
    pending = supervisor.assess_dependencies(T2, 2)
    assert pending.blocked and not pending.red and pending.decision.condition is Condition.DEPENDENCY_PENDING

    git(repo, "merge", "-q", "--ff-only", "feat/one")
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, one))
    resumed = supervisor.assess_dependencies(T2, 2)
    assert resumed.can_resume
    # identical consecutive decisions are not re-recorded, so the trace reads as the sequence of distinct situations
    assert [d["condition"] for d in decisions(store, T2)][:3] == ["DEPENDENCY_RED", "DEPENDENCY_PENDING", "DEPENDENCY_LANDED"]
    # PR1 fast-forwarded, so PR2's candidate already sits exactly on the new main: its evidence stays valid and it simply continues
    assert ("set_base", 2, "main") in prs.calls
    assert store.get_task(T2)["stage"] == Stage.REVIEW and store.latest_candidate(T2)["sha"] == two
    assert load_provenance(store, T2).authorizes_integration()


# --- review finding: stacking must be discovered from the PR's base branch, not only from declarations --------------------------------------------


def test_an_undeclared_stack_is_discovered_from_the_base_branch_and_blocks(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, one, two = _stack(tmp_path)
    store.conn.execute("DELETE FROM audit_events WHERE event_type='autonomy.pr_dependency'")  # nobody declared "PR2 depends on PR1"
    store.conn.commit()

    blocked = supervisor.assess_dependencies(T2, 2)

    assert blocked.blocked and blocked.blocked_on == [1]  # found by asking the host which PR owns feat/one
    assert ("find_by_head", 0, "feat/one") in prs.calls
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.OPEN, CIRollup.FAILURE, True))
    assert supervisor.assess_dependencies(T2, 2).red == [1]


def test_an_undeclared_stack_resumes_after_the_parent_lands(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, one, two = _stack(tmp_path)
    store.conn.execute("DELETE FROM audit_events WHERE event_type='autonomy.pr_dependency'")
    store.conn.commit()
    assert supervisor.assess_dependencies(T2, 2).blocked
    git(repo, "merge", "-q", "--ff-only", "feat/one")
    prs.update(PullRequest(1, one, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, one))

    resumed = supervisor.assess_dependencies(T2, 2)

    assert resumed.can_resume and ("set_base", 2, "main") in prs.calls


def test_a_pr_based_on_the_integration_branch_costs_no_parent_lookup(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, one, two = _stack(tmp_path)
    store.conn.execute("DELETE FROM audit_events WHERE event_type='autonomy.pr_dependency'")
    store.conn.commit()
    assessment = supervisor.assess_dependencies(T1_UNSTACKED := "TASK-9", 1)
    assert assessment.decision.action is Action.PROCEED and T1_UNSTACKED
    assert not any(call[0] == "find_by_head" for call in prs.calls)
