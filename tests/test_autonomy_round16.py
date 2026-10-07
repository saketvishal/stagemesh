"""Review round 16: create-only push, preserve before reset, idempotent re-runs, baseline means every failure line."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import TASK, add_passing_evidence, commit, git, init_repo, new_store, seed_candidate
from test_autonomy_lifecycle import CONTRACT, REF, _project

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, FakeHostedCI, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.decisions import Action
from stagemesh.autonomy.delivery import branch_name, deliver
from stagemesh.autonomy.dependencies import FakePullRequests
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage
from stagemesh.workspaces import _task_key, prepare_task_workspace


def _delivery_rig(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "cand")
    first = commit(repo, {"src/widget.py": "W = 1\n"}, "first step")
    candidate = commit(repo, {"src/widget.py": "W = 2\n"}, "second step")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    prs = FakePullRequests()
    prs.default_mergeable = True
    return repo, remote, store, prs, base, first, candidate


def _ok(sha: str) -> HostedCIRun:
    return HostedCIRun(sha, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")


def _deliver(supervisor, prs, ci):
    return deliver(supervisor, TASK, remote="origin", base="main", pulls=prs, ci=ci, title="t", clock=lambda: 0.0, sleep=lambda s: None)


# --- delivery is create-only ----------------------------------------------------------------------------------------------------------------------------------


def test_an_existing_remote_branch_that_could_be_fast_forwarded_is_never_moved(tmp_path: Path) -> None:
    repo, remote, store, prs, base, first, candidate = _delivery_rig(tmp_path)
    branch = branch_name(TASK, candidate)
    git(repo, "push", "-q", "origin", f"{first}:refs/heads/{branch}")  # an ancestor of the candidate already sits on that branch name
    ci = FakeHostedCI([_ok(candidate), _ok(base)])

    report = _deliver(Supervisor(store, repo, integration_ref="refs/remotes/origin/main", hosted_ci=ci, pull_requests=prs), prs, ci)

    assert report.status == "NOT_PUBLISHED" and "not overwritten" in report.recommendation
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == first  # fast-forwardable, yet untouched
    assert not prs.prs


def test_redelivering_the_same_sha_is_idempotent(tmp_path: Path) -> None:
    repo, remote, store, prs, base, first, candidate = _delivery_rig(tmp_path)
    ci = FakeHostedCI([_ok(candidate), _ok(base)])
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", hosted_ci=ci, pull_requests=prs)
    assert _deliver(supervisor, prs, ci).status == "MERGE_READY"
    again = _deliver(supervisor, prs, ci)
    assert again.status == "MERGE_READY" and git(remote, "rev-parse", f"refs/heads/{branch_name(TASK, candidate)}") == candidate


# --- re-running a pass never spends a budget twice ----------------------------------------------------------------------------------------------------------------


def test_delivering_again_while_remediation_is_in_flight_does_not_spend_the_budget(tmp_path: Path) -> None:
    repo, remote, store, prs, base, first, candidate = _delivery_rig(tmp_path)
    red = HostedCIRun(candidate, {"unit": GateOutcome("unit", Conclusion.FAILURE, "", ("t::new",))}, environment="x")
    ci = FakeHostedCI([red, _ok(base)])
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", hosted_ci=ci, pull_requests=prs)

    first_pass = _deliver(supervisor, prs, ci)
    assert first_pass.status == "PUBLISHED_REMEDIATING" and store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    spent = store.task_remediation_count(TASK, "INTEGRATE")

    for _ in range(4):  # an ordinary re-run while the agent is still working
        again = _deliver(supervisor, prs, ci)
        assert again.status == "AWAITING_REMEDIATION"
    assert store.task_remediation_count(TASK, "INTEGRATE") == spent and store.get_task(TASK)["status"] != "BLOCKED"


def test_reconciling_again_while_a_reconstruct_is_in_flight_does_nothing(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    git(project, "checkout", "-q", "-b", "candidate")
    candidate = commit(project, {"src/app.py": "VALUE = 2\n"}, "change app")
    git(project, "checkout", "-q", "main")
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE, contract=CONTRACT)
    add_passing_evidence(store, TASK, candidate, baseline=base, contract=CONTRACT)
    commit(project, {"src/app.py": "VALUE = 3\n"}, "main changes the same line")
    supervisor = Supervisor(store, project, integration_ref=REF)

    assert supervisor.reconcile_base(TASK).action is Action.RECONSTRUCT_ON_NEW_BASE
    findings = len(store.open_findings_for_candidate(TASK, candidate))
    for _ in range(3):
        assert supervisor.reconcile_base(TASK) is None  # the rebuild has not run yet: nothing to decide, nobody to ask
    assert len(store.open_findings_for_candidate(TASK, candidate)) == findings and store.task_remediation_count(TASK, "INTEGRATE") == 1


# --- a hard reset preserves whatever is in the worktree first ----------------------------------------------------------------------------------------------


def test_reconstruct_preserves_uncommitted_work_before_it_resets_the_worktree(tmp_path: Path) -> None:
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
    (worktree / "planted.py").write_text("PRECIOUS = 1\n", encoding="utf-8")  # uncommitted and unrecorded
    commit(project, {"src/app.py": "VALUE = 3\n"}, "main changes the same line")

    supervisor.reconcile_base(TASK)

    assert not (worktree / "planted.py").exists()
    refs = git(project, "for-each-ref", "--format=%(refname)", f"refs/stagemesh/quarantine/{_task_key(TASK)}").splitlines()
    assert any("PRECIOUS" in git(project, "show", f"{ref}:planted.py", check=False) for ref in refs)  # preserved before it was removed


# --- baseline means the same set of failures, not the same named tests ----------------------------------------------------------------------------------------------------------


def test_a_new_non_test_failure_next_to_a_known_red_test_is_a_regression() -> None:
    old = "FAILED tests/test_old.py::test_known - AssertionError"
    base = HostedCIRun("b" * 40, {"unit": GateOutcome("unit", Conclusion.FAILURE, old, ("tests/test_old.py::test_known",))}, environment="x")
    worse = GateOutcome("unit", Conclusion.FAILURE, old + "\nERROR tests/test_new.py - ImportError: cannot import name widget", ("tests/test_old.py::test_known",))
    diagnosis = diagnose_ci(HostedCIRun("c" * 40, {"unit": worse}, environment="x"), base)
    assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION
    same = GateOutcome("unit", Conclusion.FAILURE, old, ("tests/test_old.py::test_known",))
    assert diagnose_ci(HostedCIRun("c" * 40, {"unit": same}, environment="x"), base).gates[0].klass is CIClass.BASELINE_FAILURE
    subset_noise = GateOutcome("unit", Conclusion.FAILURE, "FAILED tests/test_old.py::test_known - AssertionError", ("tests/test_old.py::test_known",))
    assert diagnose_ci(HostedCIRun("c" * 40, {"unit": subset_noise}, environment="x"), base).gates[0].klass is CIClass.BASELINE_FAILURE
