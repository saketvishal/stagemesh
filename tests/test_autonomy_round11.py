"""Review round 11: untracked files, observable landings, retarget names, 409 meanings, unresolved fragile tests."""

from __future__ import annotations

import sys
from pathlib import Path

from autonomy_support import TASK, add_passing_evidence, commit, git, init_repo, new_store, seed_candidate
from test_autonomy_lifecycle import REF, _coordinator, _project

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci, plan_ci_response
from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.dependencies import CIRollup, FakePullRequests, PRState, PullRequest
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage
from stagemesh.execution import SubprocessExecutor

CAND, BASE = "c" * 40, "b" * 40


def _worktree(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    worktree = repo / ".stagemesh" / "worktrees" / "wt"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    return repo, worktree, base


# --- untracked files are part of what the owner left --------------------------------------------------------------------------------------------------


def test_an_untracked_file_dropped_into_an_idle_worktree_is_a_mutation_and_is_not_swept_into_the_candidate(tmp_path: Path) -> None:
    repo, worktree, base = _worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "planted.py").write_text("SECRET = 'second writer'\n", encoding="utf-8")

    decision = supervisor.check_workspace(TASK, execution_running=False)

    assert decision is not None and decision.detail["mutations"][0]["kind"] == "TRACKED_FILES_MODIFIED"
    assert not (worktree / "planted.py").exists()  # restored away...
    assert "SECRET" in git(repo, "show", f"{decision.detail['quarantine_refs']['worktree_snapshot']}:planted.py")  # ...but preserved


def test_the_owners_own_untracked_leftovers_are_not_a_mutation_but_later_changes_to_them_are(tmp_path: Path) -> None:
    repo, worktree, base = _worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "scratch.py").write_text("x = 1\n", encoding="utf-8")  # the running provider creates a file
    supervisor.execution_finished(TASK)  # its execution ends: this is the owner's, now part of the expectation
    assert supervisor.check_workspace(TASK, execution_running=False) is None

    (worktree / "scratch.py").write_text("x = 2  # edited by someone else while idle\n", encoding="utf-8")
    assert supervisor.check_workspace(TASK, execution_running=False) is not None


def test_end_to_end_an_untracked_file_planted_between_attempts_never_reaches_a_candidate(tmp_path: Path) -> None:
    from stagemesh.workspaces import task_workspace

    project, store, base = _project(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    script = tmp_path / "provider.py"
    script.write_text(
        "\n".join(
            [
                "import pathlib, sys",
                "marker = pathlib.Path('../attempt.marker')",
                "first = not marker.exists()",
                "marker.write_text('x')",
                "if first:",
                "    sys.exit(3)  # crash without changing anything",
                "pathlib.Path('src/widget.py').write_text('W = 1' + chr(10))",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor, executor=SubprocessExecutor([sys.executable, str(script)], name="codex"))
    assert coordinator.tick() == 0
    (task_workspace(project, TASK) / "planted.py").write_text("SECRET = 1\n", encoding="utf-8")  # a second writer, between the attempts

    assert coordinator.tick() == 0  # refused: the mutation was caught at hand-over
    assert coordinator.tick() == 1  # the next attempt runs from the restored, clean worktree
    candidate = store.latest_candidate(TASK)["sha"]
    assert "planted.py" not in git(project, "ls-tree", "-r", "--name-only", candidate)


# --- a landing that is not yet observable is a wait, and a merged PR can be verified on retry ------------------------------------------------------------------


def _merge_rig(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "feat/widget")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "push", "-q", "origin", "feat/widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    return repo, remote, store, base, candidate


def _green(candidate: str, base: str):
    gate = GateOutcome("unit", Conclusion.SUCCESS)
    return diagnose_ci(HostedCIRun(candidate, {"unit": gate}, environment="x"), HostedCIRun(base, {"unit": gate}, environment="x"))


def test_a_merged_pr_whose_landing_is_not_yet_visible_waits_and_a_retry_verifies_it(tmp_path: Path) -> None:
    repo, remote, store, base, candidate = _merge_rig(tmp_path)
    landed: dict[str, str] = {}

    def land(pr, method):
        landed["sha"] = git(remote, "commit-tree", f"{candidate}^{{tree}}", "-p", base, "-m", "squash")
        return landed["sha"]  # the host says it merged, but main is NOT updated yet (replication lag)

    prs = FakePullRequests([PullRequest(5, candidate, "feat/widget", "main", PRState.OPEN, CIRollup.SUCCESS, True)], on_merge=land)
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", pull_requests=prs)

    waiting = supervisor.merge_when_ready(TASK, 5, ci=_green(candidate, base), remote="origin")

    assert waiting.action is Action.WAIT and not waiting.requires_human  # not an escalation, and not "revert it?"
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE
    git(remote, "update-ref", "refs/heads/main", landed["sha"])  # the ref catches up

    done = supervisor.merge_when_ready(TASK, 5, ci=_green(candidate, base), remote="origin")  # retried: the PR is already merged

    assert done.action is Action.MERGE and store.get_task(TASK)["stage"] == Stage.DONE
    assert [c for c in prs.calls if c[0] == "merge"] == [("merge", 5, candidate)]  # merged exactly once


# --- smaller correctness points ----------------------------------------------------------------------------------------------------------------------


def test_stack_retargeting_names_the_branch_not_the_tracking_ref(tmp_path: Path) -> None:
    repo, remote, store, base, candidate = _merge_rig(tmp_path)
    prs = FakePullRequests(
        [
            PullRequest(1, "a" * 40, "feat/one", "main", PRState.MERGED, CIRollup.SUCCESS, True, "a" * 40),
            PullRequest(2, candidate, "feat/widget", "feat/one", PRState.OPEN, CIRollup.SUCCESS, True),
        ]
    )
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", pull_requests=prs)
    supervisor.declare_dependency(TASK, 2, 1)
    supervisor.assess_dependencies(TASK, 2)
    assert ("set_base", 2, "main") in prs.calls
    assert not any(c[2].startswith("refs/") for c in prs.calls if c[0] == "set_base")


def test_a_409_because_the_base_moved_is_not_a_head_mutation() -> None:
    from stagemesh.autonomy.github_adapter import GitHubPullRequests

    def outcome(message):
        class T:
            def request(self, method, path, body=None):
                return 409, {}, {"message": message}

        return GitHubPullRequests("o", "r", T()).merge(1, "a" * 40)

    assert outcome("Head branch was modified. Review and try the merge again.").reason == "head_sha_mismatch"
    assert outcome("Base branch was modified. Review and try the merge again.").reason == "base_modified"
    assert outcome("something else entirely").reason.startswith("409")


def test_a_base_modified_refusal_waits_instead_of_recording_a_mutation(tmp_path: Path) -> None:
    from stagemesh.autonomy.dependencies import MergeOutcome

    repo, remote, store, base, candidate = _merge_rig(tmp_path)

    class BaseMoved(FakePullRequests):
        def merge(self, number, expected_head_sha, method="squash"):
            self.calls.append(("merge", number, expected_head_sha))
            return MergeOutcome(False, None, "base_modified")

    prs = BaseMoved([PullRequest(5, candidate, "feat/widget", "main", PRState.OPEN, CIRollup.SUCCESS, True)])
    supervisor = Supervisor(store, repo, integration_ref="refs/remotes/origin/main", pull_requests=prs)
    decision = supervisor.merge_when_ready(TASK, 5, ci=_green(candidate, base), remote="origin")
    assert decision.action is Action.WAIT and decision.condition is not Condition.EXTERNAL_WORKSPACE_MUTATION
    assert not git(repo, "for-each-ref", "refs/heads/pr-*")  # no spurious replacement branch


def test_a_fragile_test_verdict_with_no_budget_left_ends_in_a_typed_escalation() -> None:
    gate = GateOutcome("unit", Conclusion.FAILURE, "flaky", ("t::flaky",), rerun_conclusions=(Conclusion.SUCCESS,))
    diagnosis = diagnose_ci(
        HostedCIRun(CAND, {"unit": gate}, environment="x"), HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")
    )
    assert diagnosis.gates[0].klass is CIClass.BROKEN_FRAGILE_TEST
    assert plan_ci_response(diagnosis, task_id=TASK, reruns_left=1).action is Action.RERUN_CI
    spent = plan_ci_response(diagnosis, task_id=TASK, reruns_left=0)
    assert spent.action is Action.ESCALATE_TO_FOUNDER and spent.escalation.reason is EscalationReason.CI_FAILURE_UNRESOLVED
