"""Review round 10: environment errors never hide new failures, reruns are real, post-merge verification understands concurrent changes."""

from __future__ import annotations

from pathlib import Path

from autonomy_support import TASK, commit, git, init_repo, new_store

from stagemesh.autonomy.ci_diagnosis import (
    CIClass,
    Conclusion,
    FakeHostedCI,
    GateOutcome,
    HostedCIRun,
    diagnose_ci,
    plan_ci_response,
)
from stagemesh.autonomy.decisions import Action, EscalationReason
from stagemesh.autonomy.gitfacts import GitFacts
from stagemesh.autonomy.merge_policy import verify_integration
from stagemesh.autonomy.supervisor import Supervisor

CAND, BASE = "c" * 40, "b" * 40
ENV = "ERROR: No matching distribution found for left-pad; requires python >=3.11"


def _run(sha: str, **gates: GateOutcome) -> HostedCIRun:
    return HostedCIRun(sha, dict(gates), environment="github-actions")


# --- an environment error shared with base cannot launder new failures -------------------------------------------------------------------------------


def test_new_failures_next_to_an_environment_error_that_base_also_has_are_a_regression() -> None:
    base = _run(BASE, unit=GateOutcome("unit", Conclusion.FAILURE, ENV))
    candidate = _run(CAND, unit=GateOutcome("unit", Conclusion.FAILURE, ENV + "\nerror: assertion failed in the new code path"))
    diagnosis = diagnose_ci(candidate, base)
    assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION
    assert diagnosis.merge_blockers()
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.REMEDIATE_CANDIDATE


def test_the_identical_environment_error_on_both_sides_is_just_a_baseline_failure() -> None:
    base = _run(BASE, unit=GateOutcome("unit", Conclusion.FAILURE, ENV))
    diagnosis = diagnose_ci(_run(CAND, unit=GateOutcome("unit", Conclusion.FAILURE, ENV)), base)
    assert diagnosis.gates[0].klass is CIClass.BASELINE_FAILURE and diagnosis.gates[0].evidence == "SIGNATURE"


# --- reruns are requested from the host, and the escalation says only what was actually done ----------------------------------------------------------


class RerunnableCI(FakeHostedCI):
    def __init__(self, runs, succeed: bool = True):
        super().__init__(runs)
        self.rerun_requests: list[tuple[str, list[str]]] = []
        self.succeed = succeed

    def rerun(self, sha, gates) -> bool:
        self.rerun_requests.append((sha, [g.name for g in gates]))
        return self.succeed


def _flaky_ci(cls=RerunnableCI, **kwargs):
    return cls([_run(CAND, unit=GateOutcome("unit", Conclusion.FAILURE, "fatal: 502 Bad Gateway fetching action")), _run(BASE, unit=GateOutcome("unit", Conclusion.SUCCESS))], **kwargs)


def test_an_infrastructure_failure_causes_a_real_rerun_request_then_a_truthful_escalation(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    ci = _flaky_ci()
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci)

    first = supervisor.assess_ci(TASK, CAND, BASE)
    assert first.action is Action.RERUN_CI and ci.rerun_requests == [(CAND, ["unit"])]

    second = supervisor.assess_ci(TASK, CAND, BASE)
    assert second.action is Action.ESCALATE_TO_FOUNDER and second.escalation.reason is EscalationReason.CI_FAILURE_UNRESOLVED
    assert any("rerun" in item for item in second.escalation.attempted)  # a rerun really was requested, so the claim is true
    assert len(ci.rerun_requests) == 1


def test_without_a_rerun_capability_the_escalation_does_not_claim_one(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=_flaky_ci(FakeHostedCI))

    decision = supervisor.assess_ci(TASK, CAND, BASE)

    assert decision.action is Action.ESCALATE_TO_FOUNDER
    assert not any("rerun" in item or "re-observed" in item for item in decision.escalation.attempted)


def test_a_refused_rerun_request_does_not_spend_the_budget_or_pretend(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    ci = _flaky_ci(succeed=False)
    decision = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci).assess_ci(TASK, CAND, BASE)
    assert decision.action is Action.ESCALATE_TO_FOUNDER and not any("rerun" in item for item in decision.escalation.attempted)


def test_the_github_adapter_reruns_failed_jobs_by_check_run_id() -> None:
    from stagemesh.autonomy.github_adapter import GitHubHostedCI

    calls = []

    class Transport:
        def request(self, method, path, body=None):
            calls.append((method, path))
            if method == "GET":
                runs = [{"id": 77, "name": "unit", "status": "completed", "conclusion": "failure", "output": {}, "started_at": "2026-01-01T00:00:00Z"}]
                return 200, {}, {"total_count": 1, "check_runs": runs}
            return 201, {}, {}

    ci = GitHubHostedCI("o", "r", Transport())
    run = ci.run_for(CAND)
    assert run.gates["unit"].ref == "77"
    assert ci.rerun(CAND, [run.gates["unit"]]) is True
    assert ("POST", "/repos/o/r/actions/jobs/77/rerun") in calls


def test_a_missing_gh_binary_is_not_a_crash(monkeypatch) -> None:
    import subprocess

    from stagemesh.autonomy import cli

    monkeypatch.delenv("STAGEMESH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing)
    assert cli._github_token() is None


# --- post-merge verification understands a clean concurrent change ------------------------------------------------------------------------------------


def _squash_with_concurrent_change(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"f.txt": "one\ntwo\nthree\n"}, "base")
    git(repo, "checkout", "-q", "-b", "candidate")
    candidate = commit(repo, {"f.txt": "ONE\ntwo\nthree\n"}, "candidate edits line 1")
    git(repo, "checkout", "-q", "main")
    main_before = commit(repo, {"f.txt": "one\ntwo\nTHREE\n"}, "someone else edits line 3 meanwhile")
    tree = git(repo, "merge-tree", "--write-tree", f"--merge-base={base}", main_before, candidate).splitlines()[0]
    squashed = git(repo, "commit-tree", tree, "-p", main_before, "-m", "squash merge of the candidate")
    git(repo, "update-ref", "refs/heads/main", squashed)
    return repo, base, candidate, squashed


def test_a_squash_that_merged_cleanly_with_concurrent_work_is_verified(tmp_path: Path) -> None:
    repo, base, candidate, squashed = _squash_with_concurrent_change(tmp_path)
    assert git(repo, "show", f"{squashed}:f.txt") == "ONE\ntwo\nTHREE"  # both changes are there; the blob is not the candidate's blob
    verdict = verify_integration(GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, merge_sha=squashed, task_id=TASK)
    assert verdict.verified and verdict.missing_content == []


def test_a_landing_that_dropped_the_candidates_change_escalates_instead_of_proposing_a_refresh(tmp_path: Path) -> None:
    repo, base, candidate, squashed = _squash_with_concurrent_change(tmp_path)
    lossy = commit(repo, {"f.txt": "one\ntwo\nTHREE\n"}, "a merge that silently dropped the candidate's change")
    verdict = verify_integration(GitFacts(repo), integration_ref="main", candidate_sha=candidate, baseline_sha=base, merge_sha=lossy, task_id=TASK)
    assert not verdict.verified and verdict.missing_content == ["f.txt"]
    decision = verdict.decision
    assert decision.action is Action.ESCALATE_TO_FOUNDER  # the work already landed: building a replacement would duplicate it
    assert decision.escalation.reason is EscalationReason.MERGED_CONTENT_NOT_VERIFIED
    assert "f.txt" in decision.escalation.smallest_decision and decision.escalation.smallest_decision.endswith("?")
