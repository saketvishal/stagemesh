"""The GitHub connector boundary (recorded transport) and the supervised PR merge flow.

Payload shapes below are copied from real responses of the StageMesh repository (PR #161, and the check runs of main's commit
970efeb), so the adapter is exercised against what GitHub actually returns, without any network access.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
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

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, diagnose_ci
from stagemesh.autonomy.decisions import Action, Condition, EscalationReason, autonomy_streak
from stagemesh.autonomy.dependencies import CIRollup, FakePullRequests, PRState, PullRequest
from stagemesh.autonomy.github_adapter import (
    GitHubAdapterError,
    GitHubAuthorizationError,
    GitHubHostedCI,
    GitHubPullRequests,
    GitHubRateLimited,
)
from stagemesh.autonomy.merge_policy import IntegrationPolicy
from stagemesh.autonomy.provenance import load_provenance
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.domain import Stage

OWNER, REPO = "saketvishal", "stagemesh"
ROOT = f"/repos/{OWNER}/{REPO}"
HEAD161 = "c5f2ec784ece2faa1618679f8c4e99ad1ea782da"
MAIN = "970efeb2364f7abcd4a0f22e0e8410276136a584"

REAL_PR_161 = {  # a merged PR as GitHub returns it (trimmed to the fields the adapter reads)
    "number": 161,
    "state": "closed",
    "merged": True,
    "mergeable": None,
    "mergeable_state": "unknown",
    "merge_commit_sha": "141d756661f2e52213f0c8ac91bca80c9a11efde",
    "head": {"sha": HEAD161, "ref": "fix/continue-unrelated-blocked-tasks"},
    "base": {"ref": "main"},
}
REAL_MAIN_CHECK_RUNS = {  # main's hosted CI at 970efeb: both gates red, and the check-run API carries no log text
    "total_count": 2,
    "check_runs": [
        {"name": "linux", "status": "completed", "conclusion": "failure", "output": {"title": None, "summary": None, "text": None}},
        {"name": "windows", "status": "completed", "conclusion": "failure", "output": {"title": None, "summary": None, "text": None}},
    ],
}


class RecordedTransport:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict, object]]):
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        return self.routes.get((method, path), (404, {}, {"message": "Not Found"}))


def _checks(sha: str) -> str:
    return f"{ROOT}/commits/{sha}/check-runs?per_page=100"


def _client(routes) -> tuple[GitHubPullRequests, RecordedTransport]:
    transport = RecordedTransport(routes)
    return GitHubPullRequests(OWNER, REPO, transport), transport


# --- adapter against recorded real payloads ------------------------------------------------------------------------------------------------


def test_merged_pull_request_maps_state_and_merge_commit() -> None:
    client, transport = _client({("GET", f"{ROOT}/pulls/161"): (200, {}, REAL_PR_161)})
    pr = client.get(161)
    assert pr == PullRequest(161, HEAD161, "fix/continue-unrelated-blocked-tasks", "main", PRState.MERGED, CIRollup.SUCCESS, None, "141d756661f2e52213f0c8ac91bca80c9a11efde")
    assert transport.calls == [("GET", f"{ROOT}/pulls/161", None)]  # a merged PR needs no CI lookup


def test_open_pull_request_derives_ci_rollup_and_mergeability() -> None:
    open_pr = {**REAL_PR_161, "state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean", "merge_commit_sha": "x" * 40}
    green = {"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success", "output": {}}]}
    client, _ = _client({("GET", f"{ROOT}/pulls/7"): (200, {}, open_pr), ("GET", _checks(HEAD161)): (200, {}, green)})
    pr = client.get(7)
    assert pr.state is PRState.OPEN and pr.ci is CIRollup.SUCCESS and pr.mergeable is True and pr.merge_commit_sha is None
    for check_runs, expected in (
        ({"check_runs": [{"name": "unit", "status": "completed", "conclusion": "failure", "output": {}}]}, CIRollup.FAILURE),
        ({"check_runs": [{"name": "unit", "status": "in_progress", "conclusion": None, "output": {}}]}, CIRollup.PENDING),
        ({"check_runs": []}, CIRollup.PENDING),
    ):
        client, _ = _client({("GET", f"{ROOT}/pulls/7"): (200, {}, open_pr), ("GET", _checks(HEAD161)): (200, {}, check_runs)})
        assert client.get(7).ci is expected


def test_dirty_pull_request_is_not_mergeable_and_closed_unmerged_is_closed() -> None:
    dirty = {**REAL_PR_161, "state": "open", "merged": False, "mergeable": None, "mergeable_state": "dirty"}
    client, _ = _client({("GET", f"{ROOT}/pulls/7"): (200, {}, dirty), ("GET", _checks(HEAD161)): (200, {}, {"check_runs": []})})
    assert client.get(7).mergeable is False
    closed = {**REAL_PR_161, "merged": False}
    client, _ = _client({("GET", f"{ROOT}/pulls/8"): (200, {}, closed)})
    assert client.get(8).state is PRState.CLOSED


def test_missing_pull_request_is_none_not_an_error() -> None:
    client, _ = _client({})
    assert client.get(999) is None


def test_set_base_sends_the_retarget_request() -> None:
    retargeted = {**REAL_PR_161, "state": "open", "merged": False, "mergeable": True, "base": {"ref": "main"}}
    client, transport = _client({("PATCH", f"{ROOT}/pulls/2"): (200, {}, retargeted), ("GET", _checks(HEAD161)): (200, {}, {"check_runs": []})})
    assert client.set_base(2, "main").base_ref == "main"
    assert transport.calls[0] == ("PATCH", f"{ROOT}/pulls/2", {"base": "main"})


def test_merge_is_pinned_to_the_validated_head_sha() -> None:
    client, transport = _client({("PUT", f"{ROOT}/pulls/2/merge"): (200, {}, {"merged": True, "sha": "m" * 40, "message": "Pull Request successfully merged"})})
    outcome = client.merge(2, "a" * 40, "squash")
    assert outcome.merged and outcome.sha == "m" * 40
    assert transport.calls == [("PUT", f"{ROOT}/pulls/2/merge", {"sha": "a" * 40, "merge_method": "squash"})]
    with pytest.raises(ValueError):
        client.merge(2, "a" * 40, "fast-forward")


def test_merge_refused_because_the_head_moved_is_reported_as_head_sha_mismatch() -> None:
    client, _ = _client({("PUT", f"{ROOT}/pulls/2/merge"): (409, {}, {"message": "Head branch was modified. Review and try the merge again."})})
    outcome = client.merge(2, "a" * 40)
    assert not outcome.merged and outcome.reason == "head_sha_mismatch"
    not_mergeable, _ = _client({("PUT", f"{ROOT}/pulls/2/merge"): (405, {}, {"message": "Pull Request is not mergeable"})})
    assert not_mergeable.merge(2, "a" * 40).reason.startswith("not_mergeable")


def test_authorization_and_rate_limits_are_different_errors() -> None:
    unauthorized, _ = _client({("GET", f"{ROOT}/pulls/1"): (401, {}, {"message": "Bad credentials"})})
    with pytest.raises(GitHubAuthorizationError):
        unauthorized.get(1)
    forbidden, _ = _client({("GET", f"{ROOT}/pulls/1"): (403, {}, {"message": "Resource not accessible by personal access token"})})
    with pytest.raises(GitHubAuthorizationError):
        forbidden.get(1)
    limited, _ = _client({("GET", f"{ROOT}/pulls/1"): (403, {"X-RateLimit-Remaining": "0", "Retry-After": "42"}, {"message": "API rate limit exceeded"})})
    with pytest.raises(GitHubRateLimited) as raised:
        limited.get(1)
    assert raised.value.retry_after == 42
    broken, _ = _client({("GET", f"{ROOT}/pulls/1"): (500, {}, {"message": "boom"})})
    with pytest.raises(GitHubAdapterError):
        broken.get(1)


def test_hosted_ci_reads_check_runs_including_pending_and_unknown_conclusions() -> None:
    payload = {
        "check_runs": [
            {"name": "linux", "status": "completed", "conclusion": "success", "output": {}},
            {"name": "windows", "status": "queued", "conclusion": None, "output": {}},
            {"name": "lint", "status": "completed", "conclusion": "timed_out", "output": {}},
            {"name": "docs", "status": "completed", "conclusion": "neutral", "output": {}},
            {"name": "unit", "status": "completed", "conclusion": "failure", "output": {"summary": "FAILED tests/test_a.py::test_x - assert 1\nFAILED tests/test_b.py::test_y", "title": "2 failed"}},
        ]
    }
    ci = GitHubHostedCI(OWNER, REPO, RecordedTransport({("GET", _checks(MAIN)): (200, {}, payload)}))
    run = ci.run_for(MAIN)
    assert run is not None and not run.complete
    assert {n: g.conclusion for n, g in run.gates.items()} == {
        "linux": Conclusion.SUCCESS, "windows": Conclusion.PENDING, "lint": Conclusion.TIMED_OUT, "docs": Conclusion.SKIPPED, "unit": Conclusion.FAILURE,
    }
    assert run.gates["unit"].failing_tests == ("tests/test_a.py::test_x", "tests/test_b.py::test_y")
    assert ci.run_for("0" * 40) is None


# --- the real base-CI-already-red incident, replayed from the recorded payload -----------------------------------------------------------------------


def test_scenario_e_real_incident_main_red_on_both_gates_is_baseline_for_a_candidate_failing_the_same_gates() -> None:
    ci = GitHubHostedCI(OWNER, REPO, RecordedTransport({("GET", _checks(MAIN)): (200, {}, REAL_MAIN_CHECK_RUNS), ("GET", _checks("c" * 40)): (200, {}, REAL_MAIN_CHECK_RUNS)}))
    diagnosis = diagnose_ci(ci.run_for("c" * 40), ci.run_for(MAIN))

    assert {g.gate: g.klass for g in diagnosis.gates} == {"linux": CIClass.BASELINE_FAILURE, "windows": CIClass.BASELINE_FAILURE}
    assert {g.evidence for g in diagnosis.gates} == {"GATE_LEVEL"}  # honest about how weak the comparison is: conclusions only
    assert diagnosis.merge_blockers() == []
    assert len(diagnosis.merge_blockers(baseline_requires_detail=True)) == 2  # a stricter policy can refuse gate-level evidence
    assert IntegrationPolicy(baseline_requires_detail=True).baseline_requires_detail


def test_scenario_f_real_shape_candidate_failing_a_gate_that_passes_on_base_is_a_regression() -> None:
    passing = {"check_runs": [{"name": "linux", "status": "completed", "conclusion": "success", "output": {}}, {"name": "windows", "status": "completed", "conclusion": "success", "output": {}}]}
    ci = GitHubHostedCI(OWNER, REPO, RecordedTransport({("GET", _checks(MAIN)): (200, {}, passing), ("GET", _checks("c" * 40)): (200, {}, REAL_MAIN_CHECK_RUNS)}))
    diagnosis = diagnose_ci(ci.run_for("c" * 40), ci.run_for(MAIN))
    assert {g.klass for g in diagnosis.gates} == {CIClass.CANDIDATE_REGRESSION}


# --- the supervised PR merge flow ------------------------------------------------------------------------------------------------------------------


def _merge_rig(tmp_path: Path, *, lossy: bool = False):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    git(repo, "checkout", "-q", "-b", "feat/widget")
    candidate = commit(repo, {"src/widget.py": "W = 1\n"}, "widget")
    git(repo, "checkout", "-q", "main")
    store = new_store(tmp_path)
    seed_candidate(store, TASK, baseline=base, candidate=candidate, stage=Stage.INTEGRATE)
    add_passing_evidence(store, TASK, candidate, baseline=base)

    def land(pr: PullRequest, method: str) -> str:
        tree = f"{candidate}^{{tree}}" if not lossy else f"{base}^{{tree}}"
        squashed = git(repo, "commit-tree", tree, "-p", "main", "-m", f"{method} PR #{pr.number}", who=HUMAN)
        git(repo, "update-ref", "refs/heads/main", squashed)
        return squashed

    prs = FakePullRequests([PullRequest(5, candidate, "feat/widget", "main", PRState.OPEN, CIRollup.SUCCESS, True)], on_merge=land)
    supervisor = Supervisor(store, repo, integration_ref="refs/heads/main", pull_requests=prs)
    ok = _green(candidate, base)
    return repo, store, prs, supervisor, base, candidate, ok


def _green(candidate: str, base: str):
    from stagemesh.autonomy.ci_diagnosis import GateOutcome, HostedCIRun

    gate = GateOutcome("unit", Conclusion.SUCCESS)
    return diagnose_ci(HostedCIRun(candidate, {"unit": gate}), HostedCIRun(base, {"unit": gate}))


def test_ready_pull_request_is_merged_verified_marked_done_and_counted_toward_the_streak(tmp_path: Path) -> None:
    repo, store, prs, supervisor, _base, candidate, ci = _merge_rig(tmp_path)

    decision = supervisor.merge_when_ready(TASK, 5, ci=ci)

    assert decision.action is Action.MERGE and decision.detail["head_pinned"] is True and decision.detail["task_stage"] == "DONE"
    assert prs.calls[-1] == ("merge", 5, candidate)  # pinned to the exact validated head
    main = git(repo, "rev-parse", "main")
    assert decision.shas["integration"] == main and main != candidate  # squash: new commit, same content
    assert store.get_task(TASK)["stage"] == Stage.DONE
    provenance = load_provenance(store, TASK)
    assert provenance.integration_sha == main
    assert supervisor.finish_task(TASK, notes="first hands-off task")
    assert autonomy_streak(store)["streak"] == 1
    assert [d["condition"] for d in decisions(store, TASK)][-2:] == ["POST_MERGE_VERIFIED", "MERGE_POLICY_SATISFIED"]


def test_streak_refuses_to_count_a_task_that_is_not_verified_done(tmp_path: Path) -> None:
    _repo, store, _prs, supervisor, _base, _candidate, _ci = _merge_rig(tmp_path)
    assert supervisor.finish_task(TASK) is False and autonomy_streak(store)["streak"] == 0


def test_policy_unsatisfied_means_no_merge_request_is_ever_sent(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, candidate, _ci = _merge_rig(tmp_path)
    from stagemesh.autonomy.ci_diagnosis import GateOutcome, HostedCIRun

    red = diagnose_ci(
        HostedCIRun(candidate, {"unit": GateOutcome("unit", Conclusion.FAILURE, "", ("t::new",))}),
        HostedCIRun(base, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}),
    )
    decision = supervisor.merge_when_ready(TASK, 5, ci=red)
    assert decision.action is Action.REMEDIATE_CANDIDATE
    assert all(call[0] != "merge" for call in prs.calls) and git(repo, "rev-parse", "main") == base
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE


def test_pr_head_that_differs_from_the_validated_candidate_is_never_merged(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, candidate, ci = _merge_rig(tmp_path)
    foreign = commit(repo, {"src/backdoor.py": "x\n"}, "second writer pushed to the PR branch", who=HUMAN)  # reachable object
    git(repo, "reset", "--hard", "-q", base)
    prs.update(PullRequest(5, foreign, "feat/widget", "main", PRState.OPEN, CIRollup.SUCCESS, True))

    decision = supervisor.merge_when_ready(TASK, 5, ci=ci)

    assert decision.condition is Condition.EXTERNAL_WORKSPACE_MUTATION and decision.action is Action.FAIL_CLOSED_QUARANTINE
    assert decision.observed["mutation"] == "PR_HEAD_MOVED" and decision.detail["merged"] is False
    assert all(call[0] != "merge" for call in prs.calls)
    assert git(repo, "rev-parse", decision.detail["replacement_branch"]) == candidate  # clean replacement from the validated candidate
    assert git(repo, "rev-parse", decision.detail["quarantine_ref"]) == foreign  # the other writer's work is preserved
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE


def test_head_that_moves_between_the_check_and_the_merge_is_refused_by_the_host(tmp_path: Path) -> None:
    repo, store, prs, supervisor, base, candidate, ci = _merge_rig(tmp_path)

    class Racy(FakePullRequests):
        def get(self, number):  # the head moves right after StageMesh looked at it
            pr = super().get(number)
            self.prs[number] = PullRequest(number, "e" * 40, pr.head_ref, pr.base_ref, pr.state, pr.ci, pr.mergeable)
            return pr

    racy = Racy(prs.prs.values(), on_merge=prs.on_merge)
    supervisor.pull_requests = racy

    decision = supervisor.merge_when_ready(TASK, 5, ci=ci)

    assert ("merge", 5, candidate) in racy.calls  # the merge was attempted, pinned to the validated SHA...
    assert decision.condition is Condition.EXTERNAL_WORKSPACE_MUTATION  # ...and refused: reported, nothing merged
    assert git(repo, "rev-parse", "main") == base and store.get_task(TASK)["stage"] == Stage.INTEGRATE


def test_landing_that_dropped_content_is_not_done(tmp_path: Path) -> None:
    _repo, store, _prs, supervisor, _base, _candidate, ci = _merge_rig(tmp_path, lossy=True)
    decision = supervisor.merge_when_ready(TASK, 5, ci=ci)
    assert decision.condition is Condition.POST_MERGE_VERIFICATION_FAILED and decision.action is Action.REFRESH_CANDIDATE
    assert store.get_task(TASK)["stage"] == Stage.INTEGRATE and load_provenance(store, TASK).integration_sha is None


def test_unauthorized_github_is_a_typed_specific_escalation_and_rate_limit_is_just_a_wait(tmp_path: Path) -> None:
    _repo, _store, _prs, supervisor, _base, _candidate, ci = _merge_rig(tmp_path)

    class Denied(FakePullRequests):
        def get(self, number):
            raise GitHubAuthorizationError(403, "Resource not accessible by personal access token")

    supervisor.pull_requests = Denied()
    decision = supervisor.merge_when_ready(TASK, 5, ci=ci)
    escalation = decision.escalation
    assert escalation is not None and escalation.reason is EscalationReason.EXTERNAL_SYSTEM_AUTHORIZATION_REQUIRED
    assert "pull request #5" in escalation.smallest_decision and escalation.smallest_decision.endswith("?")

    class Limited(FakePullRequests):
        def get(self, number):
            raise GitHubRateLimited(403, "API rate limit exceeded", 30)

    supervisor.pull_requests = Limited()
    waiting = supervisor.merge_when_ready(TASK, 5, ci=ci)
    assert waiting.action is Action.WAIT and not waiting.requires_human and waiting.detail["retry_after_seconds"] == 30


# --- live connector tests: opt in with STAGEMESH_LIVE_GITHUB=1 (strictly read-only GET requests; nothing is ever written) ---------------------------------


@pytest.mark.skipif(os.environ.get("STAGEMESH_LIVE_GITHUB") != "1", reason="live GitHub boundary tests are opt-in (STAGEMESH_LIVE_GITHUB=1)")
def test_live_boundary_reads_a_real_merged_pull_request_and_real_check_runs() -> None:
    from stagemesh.github import UrlLibGitHubTransport

    transport = UrlLibGitHubTransport(os.environ.get("GITHUB_TOKEN"))
    prs = GitHubPullRequests(OWNER, REPO, transport)
    pr = prs.get(161)
    assert pr is not None and pr.state is PRState.MERGED and pr.merge_commit_sha == "141d756661f2e52213f0c8ac91bca80c9a11efde"
    assert pr.head_ref == "fix/continue-unrelated-blocked-tasks" and pr.base_ref == "main"
    run = GitHubHostedCI(OWNER, REPO, transport).run_for(MAIN)
    assert run is not None and set(run.gates) >= {"linux", "windows"} and run.complete
    assert prs.get(10**9) is None
