"""Review round 12: unknown severities block, explicit verdicts win, landed means landed on the integration branch, ordinary states are not questions."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import TASK, add_passing_evidence, commit, git, init_repo, new_store, seed_candidate

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.dependencies import CIRollup, PRDependency, PRState, PullRequest, evaluate_dependencies
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.review_adapter import findings_from_response
from stagemesh.autonomy.review_policy import FindingClass, ReviewFindingInput, ReviewReport, assess_review, classify_finding
from stagemesh.autonomy.scope import TaskScope
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage

SCOPE = TaskScope("o", allowed_files=("docs/**",))
SHA = "a" * 40


# --- review severities ------------------------------------------------------------------------------------------------------------------------------


def test_an_unrecognised_severity_label_is_blocking_never_a_free_pass() -> None:
    for label in ("medium", "severe", "p0", "fatal", "moderate", "SQL-INJECTION", ""):
        (finding,) = findings_from_response([{"severity": label, "message": "real defect", "path": "docs/a.md"}])
        assert finding.is_blocking, label
        assert classify_finding(finding, SCOPE).klass is FindingClass.BLOCKING_IN_SCOPE
        assessment = assess_review(ReviewReport(SHA, "claude", "codex", (finding,)), candidate_sha=SHA, scope=SCOPE, task_id=TASK)
        assert not assessment.approved and assessment.remediate == [finding]


def test_only_known_non_blocking_labels_or_an_explicit_verdict_defer_a_finding() -> None:
    for label in ("nit", "minor", "info", "suggestion", "style", "low", "warning", "note", "trivial"):
        (finding,) = findings_from_response([{"severity": label, "message": "x", "path": "docs/a.md"}])
        assert not finding.is_blocking, label
    (explicit,) = findings_from_response([{"severity": "medium", "message": "x", "blocking": False}])
    assert not explicit.is_blocking


def test_an_explicit_blocking_verdict_beats_a_category() -> None:
    suggestion = ReviewFindingInput("critical bug mislabelled as a suggestion", "critical", "docs/a.md", blocking=True, category="suggestion")
    assert classify_finding(suggestion, SCOPE).klass is FindingClass.BLOCKING_IN_SCOPE
    out_of_scope_test = ReviewFindingInput("this test is wrong and blocks", "error", "tests/test_x.py", blocking=True, category="test_defect")
    assert classify_finding(out_of_scope_test, SCOPE).klass is FindingClass.BLOCKING_OUT_OF_SCOPE
    plain = ReviewFindingInput("just an idea", "info", "docs/a.md", category="suggestion")
    assert classify_finding(plain, SCOPE).klass is FindingClass.UNRELATED_SUGGESTION


def test_the_merge_policy_counts_open_findings_with_the_same_rule(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "c")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    store.upsert_finding("f1", TASK, candidate, "medium", "an unrecognised severity must still block")
    gate = GateOutcome("unit", Conclusion.SUCCESS)
    ci = diagnose_ci(HostedCIRun(candidate, {"unit": gate}, environment="x"), HostedCIRun(base, {"unit": gate}, environment="x"))
    verdict = Supervisor(store, repo, integration_ref="main").evaluate_merge(TASK, ci=ci, mergeable=True)
    assert "no_unresolved_blocking_findings" in [c.name for c in verdict.unsatisfied]


# --- dependencies ---------------------------------------------------------------------------------------------------------------------------------------


def test_a_dependency_merged_into_another_branch_has_not_landed_on_the_integration_branch() -> None:
    parent = PullRequest(1, "a" * 40, "feat/one", "feat/zero", PRState.MERGED, CIRollup.SUCCESS, True, "m" * 40)  # merged, but into feat/zero
    child = PullRequest(2, "b" * 40, "feat/two", "feat/one", PRState.OPEN, CIRollup.SUCCESS, True)
    assessment = evaluate_dependencies(2, {1: parent, 2: child}, [PRDependency(2, 1)], integration_ref="main")
    assert assessment.blocked and assessment.blocked_on == [1] and not assessment.refresh_required
    landed = PullRequest(1, "a" * 40, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, "m" * 40)
    assert evaluate_dependencies(2, {1: landed, 2: child}, [PRDependency(2, 1)], integration_ref="main").refresh_required


# --- ordinary states are not founder questions --------------------------------------------------------------------------------------------------------


def test_a_candidate_whose_every_commit_is_already_on_the_base_is_not_a_conflict(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "cand")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"src/widget.py": "W = 1\n"}, "the same change landed independently")  # same content, different commit
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    decision = Supervisor(store, repo, integration_ref="main").reconcile_base(TASK)
    assert decision is not None and not decision.requires_human
    assert decision.condition is Condition.CANDIDATE_ALREADY_INTEGRATED and decision.action is Action.PROCEED
    assert decision.detail["content_already_on_base"] is True


def test_changed_paths_include_the_old_name_of_a_rename(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"old_name.py": "X = 1\n" * 20}, "base")
    git(repo, "mv", "old_name.py", "new_name.py")
    git(repo, "commit", "-q", "-m", "rename")
    tip = git(repo, "rev-parse", "HEAD")
    assert GitFacts(repo).changed_paths(base, tip) == ["new_name.py", "old_name.py"]


def test_other_host_errors_during_delivery_are_a_typed_wait(tmp_path: Path) -> None:
    from stagemesh.autonomy.ci_diagnosis import FakeHostedCI
    from stagemesh.autonomy.delivery import deliver
    from stagemesh.autonomy.dependencies import FakePullRequests
    from stagemesh.autonomy.github_adapter import GitHubAdapterError

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "cand")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)

    class Broken(FakePullRequests):
        def open_pr(self, *args, **kwargs):
            raise GitHubAdapterError(422, "Validation Failed")

    prs = Broken()
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", hosted_ci=FakeHostedCI([]), pull_requests=prs)
    report = deliver(supervisor, TASK, remote="origin", base="main", pulls=prs, ci=FakeHostedCI([]), title="t")

    assert report.status in {"NOT_PUBLISHED", "PUBLISHED_WAITING"} and "422" in report.recommendation
    assert not any("human_escalation=true" in line for line in report.decisions)


# --- environment mismatch is never evidence of a baseline ---------------------------------------------------------------------------------------------


def test_a_matching_failure_in_a_different_environment_is_not_a_baseline() -> None:
    log = "error: the same visible failure"
    base = HostedCIRun("b" * 40, {"unit": GateOutcome("unit", Conclusion.FAILURE, log)}, environment="github-actions")
    candidate = HostedCIRun("c" * 40, {"unit": GateOutcome("unit", Conclusion.FAILURE, log)}, environment="local")
    diagnosis = diagnose_ci(candidate, base)
    assert diagnosis.gates[0].klass is CIClass.GENUINE_UNKNOWN and diagnosis.gates[0].evidence == "ENVIRONMENT_MISMATCH"
    assert diagnosis.merge_blockers()
