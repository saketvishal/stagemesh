"""Review round 15: reconstruct really starts from the new base, unchanged-base freshness, drift during refresh, delivery dependencies, merged-PR shortcut."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import TASK, add_passing_evidence, commit, git, init_repo, new_store, seed_candidate
from test_autonomy_lifecycle import CONTRACT, REF, _project

from stagemesh.autonomy.base_state import classify_base
from stagemesh.autonomy.ci_diagnosis import Conclusion, FakeHostedCI, GateOutcome, HostedCIRun
from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.delivery import deliver
from stagemesh.autonomy.dependencies import CIRollup, FakePullRequests, PRState, PullRequest
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage
from stagemesh.workspaces import prepare_task_workspace


# --- reconstruct starts from the new base ---------------------------------------------------------------------------------------------------------------------------


def test_a_reconstruct_puts_the_owned_worktree_on_the_new_base(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/app.py": "VALUE = 2\n"}, "change app")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    new_tip = commit(project, {"src/app.py": "VALUE = 3\n"}, "main changes the same line")

    decision = supervisor.reconcile_base(TASK)

    assert decision is not None and decision.action is Action.RECONSTRUCT_ON_NEW_BASE
    assert git(worktree, "rev-parse", "HEAD") == new_tip  # the re-implementation starts from the current base, not on the old candidate
    check = supervisor.check_workspace(TASK, execution_running=False)
    assert check is None, check.detail["mutations"]  # and StageMesh's own move is not a mutation
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT and store.task_baseline(TASK) == new_tip


# --- an unchanged base does not make a non-descendant candidate fresh ------------------------------------------------------------------------------------------


def test_a_candidate_that_does_not_descend_from_the_unchanged_base_is_stale(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    recorded = commit(repo, {"a.txt": "a\n"}, "recorded base")
    elsewhere = git(repo, "commit-tree", f"{recorded}^{{tree}}", "-m", "built on an unrelated root, not on the base")  # no ancestry in common
    state = classify_base(GitFacts(repo), candidate=elsewhere, old_base=recorded, new_base=recorded)
    assert state.candidate_stale and state.condition is Condition.BASE_ADVANCED
    fresh = classify_base(GitFacts(repo), candidate=recorded, old_base=recorded, new_base=recorded)
    assert fresh.condition in {Condition.BASE_UNCHANGED, Condition.CANDIDATE_ALREADY_INTEGRATED}


# --- a refresh never records an external untracked file as the owner's ----------------------------------------------------------------------------------------------


def test_a_refresh_leaves_a_worktree_alone_when_an_untracked_file_appeared(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/widget.py": "W = 1\n"}, "widget")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    worktree = prepare_task_workspace(project, TASK)
    git(worktree, "checkout", "-q", "--detach", candidate)
    supervisor = Supervisor(store, project, integration_ref=REF)
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "planted.py").write_text("SECRET = 1\n", encoding="utf-8")  # a second writer, just before the refresh
    commit(project, {"docs/n.md": "n\n"}, "main advances")

    supervisor.reconcile_base(TASK)

    assert git(worktree, "rev-parse", "HEAD") == candidate  # not moved
    decision = supervisor.check_workspace(TASK, execution_running=False)  # the next check still sees the plant as a mutation
    assert decision is not None and decision.detail["mutations"][0]["kind"] == "TRACKED_FILES_MODIFIED"


# --- delivery respects declared dependencies ---------------------------------------------------------------------------------------------------------------------------


def test_delivery_never_recommends_a_pr_whose_declared_dependency_is_unresolved(tmp_path: Path) -> None:
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
    prs = FakePullRequests([PullRequest(9, "9" * 40, "feat/dependency", "main", PRState.OPEN, CIRollup.PENDING, True)])
    prs.default_mergeable = True
    gate = GateOutcome("unit", Conclusion.SUCCESS)
    ci = FakeHostedCI([HostedCIRun(candidate, {"unit": gate}, environment="x"), HostedCIRun(base, {"unit": gate}, environment="x")])
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", hosted_ci=ci, pull_requests=prs)
    supervisor.declare_dependency(TASK, 10, 9)  # the PR this delivery opens will be #10, and it depends on #9

    report = deliver(supervisor, TASK, remote="origin", base="main", pulls=prs, ci=ci, title="t", clock=lambda: 0.0, sleep=lambda s: None)

    assert report.pr_number == 10 and report.status == "PUBLISHED_NOT_READY"
    assert any(item.startswith("dependencies_landed") for item in report.unsatisfied)


# --- the already-merged shortcut is not a loophole -------------------------------------------------------------------------------------------------------------------


def _merged_rig(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "cand")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    return repo, store, base, candidate


def test_a_pr_a_human_merged_without_evidence_is_not_marked_done(tmp_path: Path) -> None:
    repo, store, base, candidate = _merged_rig(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)  # no validation or review evidence at all
    prs = FakePullRequests([PullRequest(5, candidate, "cand", "main", PRState.MERGED, CIRollup.SUCCESS, True, candidate)])
    supervisor = Supervisor(store, repo, integration_ref="main", pull_requests=prs)
    decision = supervisor.merge_when_ready(TASK, 5)
    assert decision.action is not Action.MERGE and store.get_task(TASK)["stage"] == Stage.INTEGRATE


def test_a_pr_merged_into_another_branch_is_not_verified_against_the_integration_ref(tmp_path: Path) -> None:
    repo, store, base, candidate = _merged_rig(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    prs = FakePullRequests([PullRequest(5, candidate, "cand", "release", PRState.MERGED, CIRollup.SUCCESS, True, candidate)])
    supervisor = Supervisor(store, repo, integration_ref="main", pull_requests=prs)
    decision = supervisor.merge_when_ready(TASK, 5)
    assert decision.action is Action.WAIT and not decision.requires_human and store.get_task(TASK)["stage"] == Stage.INTEGRATE
