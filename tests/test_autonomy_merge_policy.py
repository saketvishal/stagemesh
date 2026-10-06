"""Capability 10: merge policy and post-merge verification."""

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
)

from stagemesh.autonomy.base_state import BaseState
from stagemesh.autonomy.ci_diagnosis import Conclusion, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.dependencies import (
    CIRollup,
    PRDependency,
    PRState,
    PullRequest,
    evaluate_dependencies,
)
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.merge_policy import (
    IntegrationPolicy,
    MergeFacts,
    PostMergeCheck,
    verify_integration,
)
from stagemesh.autonomy.provenance import CandidateProvenance, Mutation, load_provenance
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage

C, B = "c" * 40, "b" * 40


def _prov(**kw) -> CandidateProvenance:
    base = dict(task_id=TASK, baseline_sha=B, candidate_sha=C, validation_sha=C, review_sha=C, integration_sha=None)
    base.update(kw)
    return CandidateProvenance(**base)


def _fresh() -> BaseState:
    return BaseState(Condition.BASE_UNCHANGED, B, B, C)


def _green():
    run = HostedCIRun(C, {"unit": GateOutcome("unit", Conclusion.SUCCESS)})
    return diagnose_ci(run, HostedCIRun(B, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}))


def _facts(**kw) -> MergeFacts:
    base = dict(provenance=_prov(), base=_fresh(), review_independent=True, ci=_green(), mergeable=True)
    base.update(kw)
    return MergeFacts(**base)


def _verdict(**kw):
    policy = kw.pop("policy", IntegrationPolicy())
    return policy.evaluate(_facts(**kw), task_id=TASK)


def test_merge_only_when_every_condition_holds() -> None:
    verdict = _verdict()
    assert verdict.may_merge and verdict.decision.action is Action.MERGE
    assert verdict.decision.condition is Condition.MERGE_POLICY_SATISFIED and len(verdict.checks) == 8
    assert verdict.unsatisfied == []


def test_each_unsatisfied_condition_blocks_with_a_specific_action() -> None:
    dep_pr = {1: PullRequest(1, "a" * 40, "x", "main", PRState.OPEN, CIRollup.PENDING, True), 2: PullRequest(2, C, "y", "x")}
    cases = {
        "no_unexpected_workspace_mutation": (dict(mutations=[Mutation("HEAD_MOVED", B, C)]), Action.FAIL_CLOSED_QUARANTINE),
        "exact_candidate_evidence": (dict(provenance=_prov(review_sha="d" * 40)), Action.REVOKE_EVIDENCE_REQUIRE_REVALIDATION),
        "candidate_fresh": (dict(base=BaseState(Condition.BASE_ADVANCED, B, "e" * 40, C, candidate_stale=True)), Action.REFRESH_CANDIDATE),
        "dependencies_landed": (dict(dependencies=evaluate_dependencies(2, dep_pr, [PRDependency(2, 1)])), Action.BLOCK_ON_DEPENDENCY),
        "no_unresolved_blocking_findings": (dict(unresolved_blocking_findings=2), Action.REMEDIATE_CANDIDATE),
        "independent_review": (dict(review_independent=False), Action.REQUIRE_INDEPENDENT_REVIEW),
        "mergeable": (dict(mergeable=False), Action.REFRESH_CANDIDATE),
    }
    for name, (override, action) in cases.items():
        verdict = _verdict(**override)
        assert not verdict.may_merge, name
        assert [c.name for c in verdict.unsatisfied] == [name], name
        assert verdict.decision.action is action, name


def test_rewritten_base_requires_a_retargeted_candidate_not_a_plain_refresh() -> None:
    rewritten = BaseState(Condition.BASE_HISTORY_REWRITTEN, B, "e" * 40, C, tree_equivalent=True, candidate_stale=True)
    verdict = _verdict(base=rewritten)
    assert verdict.decision.action is Action.CREATE_RETARGETED_CANDIDATE and verdict.decision.condition is Condition.BASE_HISTORY_REWRITTEN


def test_unknown_base_state_is_not_fresh() -> None:
    assert not _verdict(base=None).may_merge


def test_precedence_puts_external_mutation_before_everything_else() -> None:
    verdict = _verdict(mutations=[Mutation("HEAD_MOVED", B, C)], review_independent=False, mergeable=False, unresolved_blocking_findings=3)
    assert verdict.decision.action is Action.FAIL_CLOSED_QUARANTINE
    assert len(verdict.unsatisfied) == 4  # all reported, one decides


def test_ci_failing_only_on_base_does_not_block_unless_policy_demands_green() -> None:
    red = GateOutcome("unit", Conclusion.FAILURE, "", ("t::a",))
    ci = diagnose_ci(HostedCIRun(C, {"unit": red}), HostedCIRun(B, {"unit": red}))
    tolerant = _verdict(ci=ci)
    assert tolerant.may_merge and tolerant.decision.detail["baseline_ci_failures"] == ["unit"]  # recorded in the trace
    strict = _verdict(ci=ci, policy=IntegrationPolicy(allow_baseline_ci_failures=False))
    assert not strict.may_merge


def test_ci_regression_pending_and_missing_all_block() -> None:
    bad = diagnose_ci(HostedCIRun(C, {"unit": GateOutcome("unit", Conclusion.FAILURE, "", ("t::new",))}), HostedCIRun(B, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}))
    assert _verdict(ci=bad).decision.action is Action.REMEDIATE_CANDIDATE
    pending = diagnose_ci(HostedCIRun(C, {"unit": GateOutcome("unit", Conclusion.PENDING)}), HostedCIRun(B, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}))
    assert _verdict(ci=pending).decision.action is Action.WAIT
    assert _verdict(ci=None).decision.condition is Condition.CI_PENDING


def test_unknown_mergeability_waits_unless_policy_allows() -> None:
    assert _verdict(mergeable=None).decision.action is Action.WAIT
    assert _verdict(mergeable=None, policy=IntegrationPolicy(require_mergeable=False)).may_merge


# --- through the supervisor with a real store and git ----------------------------------------------------------------------------------------------


def test_supervisor_evaluates_merge_from_durable_state(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "candidate")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    supervisor = Supervisor(store, repo, integration_ref="main")
    ci = _ci_for(candidate, base)

    ok = supervisor.evaluate_merge(TASK, ci=ci, mergeable=True)
    assert ok.may_merge and ok.decision.shas["candidate"] == candidate and ok.decision.shas["review"] == candidate

    commit(repo, {"docs/x.md": "x\n"}, "main moves")  # base advanced: the candidate is no longer fresh
    stale = supervisor.evaluate_merge(TASK, ci=ci, mergeable=True)
    assert not stale.may_merge and stale.decision.action is Action.REFRESH_CANDIDATE

    not_independent = Supervisor(store, repo, integration_ref="main")
    store.conn.execute("DELETE FROM evidence WHERE kind='REVIEW'")
    add_passing_evidence(store, TASK, candidate, baseline=base, independent=False)
    git(repo, "reset", "--hard", "-q", base)
    verdict = not_independent.evaluate_merge(TASK, ci=ci, mergeable=True)
    assert [c.name for c in verdict.unsatisfied] == ["independent_review"]


def _ci_for(candidate: str, base: str):
    ok = GateOutcome("unit", Conclusion.SUCCESS)
    return diagnose_ci(HostedCIRun(candidate, {"unit": ok}), HostedCIRun(base, {"unit": ok}))


# --- post-merge verification ------------------------------------------------------------------------------------------------------------------


def _merge_setup(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n", "README.md": "r\n"}, "base")
    git(repo, "checkout", "-q", "-b", "candidate")
    candidate = commit(repo, {"src/widget.py": "W = 1\n", "README.md": None}, "widget and remove readme")
    git(repo, "checkout", "-q", "main")
    return repo, base, candidate


def test_fast_forward_landing_is_verified_and_records_the_resulting_main_sha(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    git(repo, "merge", "-q", "--ff-only", candidate)
    ran: list[str] = []
    verdict = verify_integration(
        GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, task_id=TASK,
        post_merge_checks=[lambda: (ran.append("smoke"), PostMergeCheck("smoke", True))[1]],
    )
    assert verdict.verified and verdict.may_mark_done and verdict.integration_sha == candidate and ran == ["smoke"]
    assert verdict.decision.action is Action.MARK_DONE and verdict.decision.shas["integration"] == candidate


def test_squash_landing_is_verified_by_content_not_ancestry(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    squashed = git(repo, "commit-tree", f"{candidate}^{{tree}}", "-p", base, "-m", "squash", who=HUMAN)
    git(repo, "update-ref", "refs/heads/main", squashed)
    assert not GitFacts(repo).is_ancestor(candidate, squashed)
    verdict = verify_integration(GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, merge_sha=squashed, task_id=TASK)
    assert verdict.verified and verdict.integration_sha == squashed


def test_content_that_did_not_land_is_not_done(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    lossy = commit(repo, {"src/widget.py": "W = 0  # merge dropped the change\n"}, "bad merge")
    ran: list[str] = []
    verdict = verify_integration(
        GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, merge_sha=lossy, task_id=TASK,
        post_merge_checks=[lambda: (ran.append("never"), PostMergeCheck("smoke", True))[1]],
    )
    assert not verdict.verified and "src/widget.py" in verdict.missing_content and "README.md" in verdict.missing_content
    assert ran == []  # no point running post-merge checks on content that is not there
    assert verdict.decision.action is Action.REFRESH_CANDIDATE and verdict.decision.detail["task_not_done"] is True


def test_failing_post_merge_check_keeps_the_task_open(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    git(repo, "merge", "-q", "--ff-only", candidate)
    verdict = verify_integration(
        GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, task_id=TASK,
        post_merge_checks=[lambda: PostMergeCheck("main-smoke", False, "tests red on main after merge")],
    )
    assert not verdict.may_mark_done
    assert verdict.decision.condition is Condition.POST_MERGE_VERIFICATION_FAILED and verdict.decision.action is Action.REMEDIATE_CANDIDATE
    assert verdict.decision.observed["failed"] == "main-smoke"


def test_unresolvable_integration_ref_is_not_done(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    verdict = verify_integration(GitFacts(repo), integration_ref="refs/heads/nope", candidate_sha=candidate, baseline_sha=base, task_id=TASK)
    assert not verdict.verified and verdict.integration_sha is None


def test_supervisor_records_the_integration_sha_in_provenance_only_after_verification(tmp_path: Path) -> None:
    repo, base, candidate = _merge_setup(tmp_path)
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    supervisor = Supervisor(store, repo, integration_ref="main", post_merge_checks=[lambda: PostMergeCheck("smoke", True)])
    assert not supervisor.integration_verified(TASK, candidate)  # not on main yet
    assert [d for d in decisions(store, TASK) if d["condition"] == "POST_MERGE_VERIFICATION_FAILED"]
    git(repo, "merge", "-q", "--ff-only", candidate)
    assert supervisor.integration_verified(TASK, candidate)
    assert decisions(store, TASK)[-1]["condition"] == "POST_MERGE_VERIFIED"
    assert load_provenance(store, TASK).candidate_sha == candidate
