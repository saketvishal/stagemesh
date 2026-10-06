"""Delivery: publish a validated, reviewed candidate as a branch and PR, observe hosted CI, decide merge-readiness (merge stays off)."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import TASK, add_passing_evidence, commit, git, init_repo, new_store, seed_candidate

from stagemesh.autonomy.ci_diagnosis import Conclusion, GateOutcome, HostedCIRun
from stagemesh.autonomy.delivery import branch_name, deliver, write_report
from stagemesh.autonomy.dependencies import FakePullRequests
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage

TITLE = "Improve diagnose output with failed gate stdout excerpts"
BASE_REF = "refs/remotes/origin/main"


class ScriptedCI:
    """Hosted CI whose answers change per call: `script[sha]` is a list consumed one entry per `run_for` (the last one repeats)."""

    def __init__(self, script: dict[str, list[HostedCIRun | None]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def run_for(self, sha: str) -> HostedCIRun | None:
        self.calls.append(sha)
        entries = self.script.get(sha, [None])
        return entries.pop(0) if len(entries) > 1 else entries[0]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _ok(sha: str, **extra: Conclusion) -> HostedCIRun:
    gates = {"unit": GateOutcome("unit", Conclusion.SUCCESS), **{n: GateOutcome(n, c) for n, c in extra.items()}}
    return HostedCIRun(sha, gates, environment="github-actions")


def _rig(tmp_path: Path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "checkout", "-q", "-b", "candidate")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "implement")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    prs = FakePullRequests()
    prs.default_mergeable = True
    return repo, remote, store, prs, base, candidate


def _supervisor(store, repo, prs, ci):
    return Supervisor(store, repo, integration_ref=BASE_REF, hosted_ci=ci, pull_requests=prs)


def _deliver(supervisor, prs, ci, clock=None, **kwargs):
    clock = clock or Clock()
    return deliver(supervisor, TASK, remote="origin", base="main", pulls=prs, ci=ci, title=TITLE, clock=clock, sleep=clock.sleep, **kwargs)


def test_ready_candidate_is_published_observed_and_recommended_without_merging(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    ci = ScriptedCI({candidate: [None, _ok(candidate, lint=Conclusion.PENDING), _ok(candidate)], base: [_ok(base)]})
    ci.script[candidate][1] = HostedCIRun(candidate, {"unit": GateOutcome("unit", Conclusion.PENDING)}, complete=False)
    clock = Clock()

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci, clock)

    assert report.status == "MERGE_READY" and not report.merge_performed
    branch = branch_name(TASK, candidate)
    assert git(remote, "rev-parse", f"refs/heads/{branch}") == candidate  # the exact validated SHA, nothing else, was pushed
    assert git(remote, "rev-parse", "refs/heads/main") == base  # main untouched
    (pr,) = prs.prs.values()
    assert pr.head_ref == branch and pr.base_ref == "main" and pr.head_sha == candidate
    title, body = prs.bodies[pr.number]
    assert title == TITLE and candidate in body and "independent=True" in body and "Automatic merge is disabled" in body
    assert all(call[0] != "merge" for call in prs.calls)
    assert clock.sleeps == [20.0, 20.0]  # it waited for CI through the injected clock, never really sleeping
    assert report.ci["overall"] == "PASSED" and report.unsatisfied == []
    assert any(line.startswith("CI_GREEN") for line in report.decisions) and any(line.startswith("MERGE_POLICY_SATISFIED") for line in report.decisions)
    path = write_report(repo, report)
    assert "MERGE_READY" in path.read_text(encoding="utf-8") and path.with_suffix(".json").is_file()


def test_delivering_again_updates_the_same_pr_instead_of_opening_another(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    ci = ScriptedCI({candidate: [_ok(candidate)], base: [_ok(base)]})
    supervisor = _supervisor(store, repo, prs, ci)
    _deliver(supervisor, prs, ci)
    second = _deliver(supervisor, prs, ci)

    assert second.status == "MERGE_READY" and len(prs.prs) == 1
    assert [c[0] for c in prs.calls if c[0] in {"open_pr", "edit"}] == ["open_pr", "edit"]


def test_a_moved_base_refreshes_the_candidate_and_publishes_nothing(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    work = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(work))
    commit(work, {"docs/n.md": "n\n"}, "someone else lands work")
    git(work, "push", "-q", "origin", "main")
    ci = ScriptedCI({})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert report.status == "NEEDS_REVALIDATION" and report.candidate_sha != candidate
    assert git(remote, "branch", "--list", "stagemesh/*") == "" and not prs.prs  # a stale candidate is never published
    assert store.get_task(TASK)["stage"] == Stage.VALIDATE
    assert ci.calls == []


def test_ci_regression_on_the_published_pr_sends_the_task_back_for_scoped_remediation(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    red = HostedCIRun(candidate, {"unit": GateOutcome("unit", Conclusion.FAILURE, "", ("t::new",)), "lint": GateOutcome("lint", Conclusion.FAILURE, "E501")}, environment="github-actions")
    base_run = HostedCIRun(base, {"unit": GateOutcome("unit", Conclusion.SUCCESS), "lint": GateOutcome("lint", Conclusion.FAILURE, "E501")}, environment="github-actions")
    ci = ScriptedCI({candidate: [red], base: [base_run]})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert report.status == "PUBLISHED_REMEDIATING" and not report.merge_performed
    assert store.get_task(TASK)["stage"] == Stage.IMPLEMENT
    (finding,) = store.open_findings_for_candidate(TASK, candidate)
    assert "unit" in finding["message"] and "Do not touch gates that already fail on base: lint" in finding["message"]


def test_ci_that_does_not_finish_in_time_is_a_wait_not_a_failure(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    pending = HostedCIRun(candidate, {"unit": GateOutcome("unit", Conclusion.PENDING)}, complete=False)
    ci = ScriptedCI({candidate: [pending], base: [_ok(base)]})
    clock = Clock()

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci, clock, wait_seconds=60, poll_seconds=20)

    assert report.status == "PUBLISHED_WAITING" and clock.now == 60 and len(clock.sleeps) == 3
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE


def test_evidence_that_does_not_authorize_the_candidate_publishes_nothing(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    store.conn.execute("DELETE FROM evidence WHERE kind='REVIEW'")
    ci = ScriptedCI({})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert report.status == "NOT_PUBLISHED" and "does not authorize" in report.recommendation
    assert git(remote, "branch", "--list", "stagemesh/*") == "" and not prs.prs


def test_an_existing_remote_branch_at_a_different_commit_is_never_overwritten(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    git(repo, "checkout", "-q", "-b", "squatter", base)
    other = commit(repo, {"x.txt": "someone else's work\n"}, "squatter")
    git(repo, "checkout", "-q", "main")
    git(repo, "push", "-q", "origin", f"{other}:refs/heads/{branch_name(TASK, candidate)}")
    ci = ScriptedCI({})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert report.status == "NOT_PUBLISHED" and "not overwritten" in report.recommendation
    assert git(remote, "rev-parse", f"refs/heads/{branch_name(TASK, candidate)}") == other
    assert any("REMOTE_BRANCH_DIFFERS" in line for line in report.decisions) and not prs.prs


def test_baseline_red_gates_without_log_detail_are_not_merge_ready_by_default(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    red = lambda sha: HostedCIRun(sha, {"linux": GateOutcome("linux", Conclusion.FAILURE), "windows": GateOutcome("windows", Conclusion.FAILURE)}, environment="github-actions")  # noqa: E731
    ci = ScriptedCI({candidate: [red(candidate)], base: [red(base)]})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert report.status == "PUBLISHED_NOT_READY" and any(item.startswith("ci:") for item in report.unsatisfied)
    assert all(g["evidence"] == "GATE_LEVEL" for g in report.ci["gates"])
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE  # not remediated: nothing is wrong with the candidate that code could fix


def test_unknown_mergeability_is_waited_for_not_assumed(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    prs.default_mergeable = None
    ci = ScriptedCI({candidate: [_ok(candidate)], base: [_ok(base)]})
    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci, wait_seconds=40, poll_seconds=20)
    assert report.status == "PUBLISHED_NOT_READY" and any(item.startswith("mergeable:") for item in report.unsatisfied)


def test_merge_only_happens_when_explicitly_enabled_and_the_policy_is_satisfied(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    ci = ScriptedCI({candidate: [_ok(candidate)], base: [_ok(base)]})

    def land(pr, method):  # the host squash-merges: main gets a new commit with the candidate's tree
        squashed = git(remote, "commit-tree", f"{candidate}^{{tree}}", "-p", base, "-m", "squash")  # made on the host
        git(remote, "update-ref", "refs/heads/main", squashed)
        return squashed

    prs.on_merge = land
    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci, merge=True)

    assert report.status == "MERGED" and report.merge_performed, report.recommendation
    assert ("merge", 1, candidate) in prs.calls  # pinned to the validated head
    assert store.get_task(TASK)["stage"] == Stage.DONE


def test_host_authorization_failure_is_a_typed_escalation_not_a_crash(tmp_path: Path) -> None:
    from stagemesh.autonomy.github_adapter import GitHubAuthorizationError

    repo, remote, store, prs, base, candidate = _rig(tmp_path)

    class Denied(FakePullRequests):
        def open_pr(self, *args, **kwargs):
            raise GitHubAuthorizationError(403, "Resource not accessible by personal access token")

    ci = ScriptedCI({})
    denied = Denied()
    report = _deliver(_supervisor(store, repo, denied, ci), denied, ci)

    assert report.status == "ESCALATED" and "EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED" in report.recommendation
    assert any("escalation=EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED" in line for line in report.decisions)


def test_rate_limit_is_a_wait_not_an_escalation(tmp_path: Path) -> None:
    from stagemesh.autonomy.github_adapter import GitHubRateLimited

    repo, remote, store, prs, base, candidate = _rig(tmp_path)

    class Limited(FakePullRequests):
        def find_open(self, head_ref):
            raise GitHubRateLimited(403, "API rate limit exceeded", 30)

    limited = Limited()
    ci = ScriptedCI({})
    report = _deliver(_supervisor(store, repo, limited, ci), limited, ci)
    assert report.status in {"NOT_PUBLISHED", "PUBLISHED_WAITING"} and "rate limit" in report.recommendation
    assert not any("human_escalation=true" in line for line in report.decisions)


def test_push_refusals_are_classified_authorization_versus_branch_conflict() -> None:
    from stagemesh.autonomy.delivery import _PUSH_DENIED

    for denied in (
        "remote: Permission to saketvishal/stagemesh.git denied to someone.",
        "fatal: Authentication failed for 'https://github.com/x/y.git/'",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "remote: error: 403 forbidden",
    ):
        assert _PUSH_DENIED.search(denied), denied
    for conflict in (
        " ! [rejected]        abc -> stagemesh/t-abc1234 (non-fast-forward)",
        "error: failed to push some refs; Updates were rejected because the tip of your current branch is behind",
    ):
        assert not _PUSH_DENIED.search(conflict), conflict


def test_cli_deliver_refuses_without_the_supervisor_enabled(tmp_path: Path, capsys) -> None:
    import json

    from stagemesh.cli import main

    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    store.close()
    (repo / ".stagemesh").mkdir(exist_ok=True)
    assert main(["--project", str(repo), "autonomy", "deliver", "--task", TASK]) == 2
    assert "not enabled" in capsys.readouterr().out
    (repo / ".stagemesh" / "autonomy.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    code = main(["--project", str(repo), "autonomy", "deliver", "--task", TASK])
    assert code in {2, 3}  # no GitHub remote / not isolated here: it refuses cleanly instead of publishing


def test_an_existing_pr_with_the_wrong_base_is_retargeted_before_anything_else(tmp_path: Path) -> None:
    """Independent-review finding: a reused PR's base must be verified, never assumed."""
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    from stagemesh.autonomy.dependencies import CIRollup, PRState, PullRequest

    prs.update(PullRequest(7, candidate, branch_name(TASK, candidate), "some-other-branch", PRState.OPEN, CIRollup.SUCCESS, True))
    ci = ScriptedCI({candidate: [_ok(candidate)], base: [_ok(base)]})

    report = _deliver(_supervisor(store, repo, prs, ci), prs, ci)

    assert ("set_base", 7, "main") in prs.calls and prs.prs[7].base_ref == "main"
    assert report.status == "MERGE_READY" and report.pr_number == 7


def test_merge_refuses_a_pr_whose_base_is_not_the_integration_branch(tmp_path: Path) -> None:
    repo, remote, store, prs, base, candidate = _rig(tmp_path)
    from stagemesh.autonomy.dependencies import CIRollup, PRState, PullRequest
    from stagemesh.autonomy.decisions import Action

    prs.update(PullRequest(7, candidate, "feat/x", "release-branch", PRState.OPEN, CIRollup.SUCCESS, True))
    ci = ScriptedCI({candidate: [_ok(candidate)], base: [_ok(base)]})
    supervisor = _supervisor(store, repo, prs, ci)
    from stagemesh.autonomy.ci_diagnosis import diagnose_ci

    diagnosis = diagnose_ci(_ok(candidate), _ok(base))
    decision = supervisor.merge_when_ready(TASK, 7, ci=diagnosis)

    assert decision.action is not Action.MERGE
    assert all(call[0] != "merge" for call in prs.calls)  # never sent to a branch other than the one policy was evaluated against
    assert "base" in decision.trace_line().lower() or "base" in str(decision.detail).lower() or decision.observed
