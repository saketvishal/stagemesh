"""Hardening from independent review rounds: registration-based trust, streak integrity, per-task trace, CI misclassification,
post-merge verification, fail-closed configuration, delivery edges, scope of blocking review findings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from autonomy_support import HUMAN, STAGEMESH, TASK, add_passing_evidence, commit, decisions, git, init_repo, new_store, seed_candidate
from test_autonomy_lifecycle import CONTRACT, REF, _coordinator, _project, _task_at_integrate

from stagemesh.autonomy.ci_diagnosis import (
    CIClass,
    Conclusion,
    GateOutcome,
    HostedCIRun,
    diagnose_ci,
    plan_ci_response,
)
from stagemesh.autonomy.decisions import (
    Action,
    AutonomyDecision,
    Condition,
    EscalationReason,
    TaskOutcome,
    autonomy_streak,
    decision_trace,
    record_decision,
    record_task_outcome,
)
from stagemesh.autonomy.isolation import check_isolation
from stagemesh.autonomy.provenance import claim_workspace, load_ownership
from stagemesh.autonomy.review_policy import ReviewFindingInput, ReviewReport, assess_review
from stagemesh.autonomy.scope import TaskScope
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.coordinator import Coordinator
from stagemesh.domain import EvidenceKind, EvidenceStatus, Stage
from stagemesh.execution import SubprocessExecutor

CAND, BASE = "c" * 40, "b" * 40


def _repo_with_worktree(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = commit(repo, {"src/app.py": "VALUE = 1\n"}, "base")
    worktree = repo / ".stagemesh" / "worktrees" / "wt"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    return repo, worktree, base


# --- trust is by registration, not by a self-declared committer identity ---------------------------------------------------------------------


def test_a_commit_made_with_the_stagemesh_identity_but_never_registered_is_a_mutation(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    commit(worktree, {"src/backdoor.py": "x\n"}, "second writer using the worktree's own configured identity", who=STAGEMESH)

    decision = supervisor.check_workspace(TASK)

    assert decision is not None and decision.detail["mutations"][0]["kind"] == "HEAD_MOVED"
    assert git(worktree, "rev-parse", "HEAD") == base


def test_a_registered_candidate_commit_is_not_a_mutation(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    mine = commit(worktree, {"src/widget.py": "W = 1\n"}, "StageMesh candidate", who=STAGEMESH)

    supervisor.candidate_committed(TASK, mine)

    assert supervisor.check_workspace(TASK) is None and load_ownership(store, TASK).expected_head == mine


def test_registration_requires_the_worktree_to_actually_be_at_that_commit(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    other = commit(repo, {"x.txt": "x\n"}, "elsewhere")
    supervisor.candidate_committed(TASK, other)  # not what the worktree has: ignored
    assert load_ownership(store, TASK).expected_head == base


def test_a_provider_that_commits_itself_is_trusted_only_when_its_identity_is_listed(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    supervisor = Supervisor(store, repo, integration_ref="main")
    claim_workspace(store, TASK, worktree, trusted_committer_emails=("codex@provider.example",))
    mine = commit(worktree, {"src/widget.py": "W = 1\n"}, "provider commit", who=("Codex", "codex@provider.example"))
    assert supervisor.check_workspace(TASK) is None and load_ownership(store, TASK).expected_head == mine
    commit(worktree, {"src/evil.py": "x\n"}, "someone else", who=HUMAN)
    assert supervisor.check_workspace(TASK) is not None


def test_end_to_end_a_second_writer_using_the_worktrees_own_git_identity_is_detected(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    store.upsert_task("add the widget", source_id=TASK)
    store.advance_task(TASK, Stage.IMPLEMENT)
    script = tmp_path / "provider.py"
    script.write_text(
        "\n".join(
            [
                "import subprocess, pathlib",
                "pathlib.Path('src/backdoor.py').write_text('x = 1' + chr(10))",
                "subprocess.run(['git', 'add', '-A'], check=True)",
                "subprocess.run(['git', 'commit', '-qm', 'plain commit with the configured identity'], check=True)",
            ]
        ),
        encoding="utf-8",
    )
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor, executor=SubprocessExecutor([sys.executable, str(script)], name="codex"))

    assert coordinator.tick() == 0

    assert store.latest_candidate(TASK) is None
    assert [d["condition"] for d in decisions(store, TASK)] == ["EXTERNAL_WORKSPACE_MUTATION"]


def test_a_candidate_stacked_on_an_unregistered_foreign_commit_is_tainted_and_loses_its_evidence(tmp_path: Path) -> None:
    repo, worktree, base = _repo_with_worktree(tmp_path)
    store = new_store(tmp_path)
    foreign = commit(worktree, {"src/backdoor.py": "x\n"}, "adopted by mistake", who=HUMAN)
    candidate = commit(worktree, {"src/widget.py": "W = 1\n"}, "StageMesh commit on top", who=STAGEMESH)
    seed_candidate(store, TASK, baseline=base, candidate=candidate)
    add_passing_evidence(store, TASK, candidate, baseline=base)
    supervisor = Supervisor(store, repo, integration_ref="main")
    supervisor.claim_workspace(TASK, worktree)
    (worktree / "src" / "app.py").write_text("tampered\n", encoding="utf-8")

    decision = supervisor.check_workspace(TASK, execution_running=False)

    assert decision is not None and decision.detail["candidate_tainted"] is True and foreign
    from stagemesh.autonomy.provenance import load_provenance

    assert not load_provenance(store, TASK).authorizes_integration()


# --- streak integrity --------------------------------------------------------------------------------------------------------------------------


def test_the_streak_counts_each_task_once_and_a_failed_task_breaks_it(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    for _ in range(12):
        record_task_outcome(store, TaskOutcome("T-1", True))  # the same task, recorded again and again
    assert autonomy_streak(store)["streak"] == 1 and autonomy_streak(store)["tasks_recorded"] == 1
    record_task_outcome(store, TaskOutcome("T-2", True))
    record_task_outcome(store, TaskOutcome("T-3", False))  # failed with no escalation and no intervention
    assert autonomy_streak(store)["streak"] == 0
    record_task_outcome(store, TaskOutcome("T-4", True))
    record_task_outcome(store, TaskOutcome("T-5", False, escalations=("PRODUCT_REQUIREMENT_AMBIGUOUS",)))  # waiting on a real decision: neutral
    assert autonomy_streak(store)["streak"] == 1


def test_the_latest_outcome_of_a_task_wins(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    record_task_outcome(store, TaskOutcome("T-1", True))
    record_task_outcome(store, TaskOutcome("T-1", True, operational_interventions=2, notes="founder had to fix the branch"))
    assert autonomy_streak(store)["streak"] == 0


def test_finish_task_is_idempotent_and_record_outcome_refuses_an_unverified_completion(tmp_path: Path, capsys) -> None:
    from stagemesh.cli import main

    project, store, base = _project(tmp_path)
    original = _task_at_integrate(store, project, base)
    supervisor = Supervisor(store, project, integration_ref=REF)
    assert supervisor.finish_task(TASK) is False  # not DONE yet
    store.close()
    assert main(["--project", str(project), "autonomy", "record-outcome", "--task", TASK, "--completed"]) == 2
    assert "not verified" in capsys.readouterr().out
    assert original


def test_finish_task_records_a_verified_done_once(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    _task_at_integrate(store, project, base)
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)
    for _ in range(8):
        coordinator.tick()
    assert store.get_task(TASK)["stage"] == Stage.DONE
    assert supervisor.finish_task(TASK) and supervisor.finish_task(TASK) and supervisor.finish_task(TASK)
    assert autonomy_streak(store)["tasks_recorded"] == 1


# --- the per-task trace is not truncated by other tasks' decisions ----------------------------------------------------------------------------


def test_per_task_trace_survives_more_than_500_decisions_of_other_tasks(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    record_decision(store, AutonomyDecision(Condition.BASE_ADVANCED, "p", Action.RECONSTRUCT_ON_NEW_BASE, TASK, {"n": "first"}))
    for n in range(600):
        record_decision(store, AutonomyDecision(Condition.CI_GREEN, "p", Action.PROCEED, "OTHER", {"n": str(n)}))
    assert [d["action"] for d in decision_trace(store, TASK)] == ["RECONSTRUCT_ON_NEW_BASE"]
    supervisor = Supervisor(store, tmp_path, integration_ref="main")
    assert supervisor._reconstruct_count(TASK) == 1  # the reconstruct budget cannot silently reset


def test_trace_markers_slice_a_task_trace_exactly(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    supervisor = Supervisor(store, tmp_path, integration_ref="main")
    supervisor.record(AutonomyDecision(Condition.CI_GREEN, "p", Action.PROCEED, TASK, {"n": "before"}))
    marker = supervisor.trace_marker()
    for n in range(600):
        record_decision(store, AutonomyDecision(Condition.CI_GREEN, "p", Action.PROCEED, "OTHER", {"n": str(n)}))
    supervisor.record(AutonomyDecision(Condition.CI_PENDING, "p", Action.WAIT, TASK, {"n": "after"}))
    assert [d["condition"] for d in supervisor.trace_after(TASK, marker)] == ["CI_PENDING"]


# --- CI: timeouts and cancellations are not automatically infrastructure --------------------------------------------------------------------------


def _run(sha: str, **gates: GateOutcome) -> HostedCIRun:
    return HostedCIRun(sha, dict(gates), environment="github-actions")


def test_a_timeout_without_an_infrastructure_signature_is_unknown_then_a_regression_when_it_repeats() -> None:
    base = _run(BASE, unit=GateOutcome("unit", Conclusion.SUCCESS))
    first = diagnose_ci(_run(CAND, unit=GateOutcome("unit", Conclusion.TIMED_OUT)), base)
    assert first.gates[0].klass is CIClass.GENUINE_UNKNOWN
    assert plan_ci_response(first, task_id=TASK, reruns_left=1).action is Action.RERUN_CI
    again = diagnose_ci(_run(CAND, unit=GateOutcome("unit", Conclusion.TIMED_OUT, rerun_conclusions=(Conclusion.TIMED_OUT,))), base)
    assert again.gates[0].klass is CIClass.CANDIDATE_REGRESSION  # it hangs on the same SHA twice: that is the candidate
    assert plan_ci_response(again, task_id=TASK).action is Action.REMEDIATE_CANDIDATE


def test_a_real_infrastructure_signature_is_rerun_once_then_escalated_with_a_specific_question() -> None:
    base = _run(BASE, unit=GateOutcome("unit", Conclusion.SUCCESS))
    diagnosis = diagnose_ci(_run(CAND, unit=GateOutcome("unit", Conclusion.FAILURE, "fatal: 502 Bad Gateway fetching action")), base)
    assert diagnosis.gates[0].klass is CIClass.INFRASTRUCTURE_FAILURE
    assert plan_ci_response(diagnosis, task_id=TASK, reruns_left=1).action is Action.RERUN_CI
    exhausted = plan_ci_response(diagnosis, task_id=TASK, reruns_left=0)
    assert exhausted.action is Action.ESCALATE_TO_FOUNDER and exhausted.escalation.reason is EscalationReason.CI_FAILURE_UNRESOLVED
    assert "unit" in exhausted.escalation.smallest_decision and exhausted.escalation.smallest_decision.endswith("?")


def test_the_rerun_budget_is_durable_across_observations(tmp_path: Path) -> None:
    from stagemesh.autonomy.ci_diagnosis import FakeHostedCI

    store = new_store(tmp_path)
    store.upsert_task("t", source_id=TASK)
    ci = FakeHostedCI([_run(CAND, unit=GateOutcome("unit", Conclusion.FAILURE, "502 Bad Gateway")), _run(BASE, unit=GateOutcome("unit", Conclusion.SUCCESS))])
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci)
    assert supervisor.assess_ci(TASK, CAND, BASE).action is Action.RERUN_CI
    second = supervisor.assess_ci(TASK, CAND, BASE)
    assert second.action is Action.ESCALATE_TO_FOUNDER  # the budget did not reset: it ends in a typed escalation, not an endless wait


def test_check_runs_attempts_of_the_same_gate_are_read_as_reruns() -> None:
    from stagemesh.autonomy.github_adapter import GitHubHostedCI

    class Transport:
        def request(self, method, path, body=None):
            runs = [
                {"id": 2, "name": "unit", "status": "completed", "conclusion": "timed_out", "output": {}, "started_at": "2026-01-01T00:10:00Z"},
                {"id": 1, "name": "unit", "status": "completed", "conclusion": "timed_out", "output": {}, "started_at": "2026-01-01T00:00:00Z"},
            ]
            return 200, {}, {"check_runs": runs}

    run = GitHubHostedCI("o", "r", Transport()).run_for(CAND)
    assert run.gates["unit"].rerun_conclusions == (Conclusion.TIMED_OUT,)


# --- merge facts must describe this candidate -----------------------------------------------------------------------------------------------------


def test_a_ci_diagnosis_for_another_sha_or_with_no_gates_never_satisfies_the_ci_check() -> None:
    from stagemesh.autonomy.base_state import BaseState
    from stagemesh.autonomy.merge_policy import IntegrationPolicy, MergeFacts
    from stagemesh.autonomy.provenance import CandidateProvenance

    ok = GateOutcome("unit", Conclusion.SUCCESS)
    prov = CandidateProvenance(TASK, BASE, CAND, CAND, CAND, None)
    fresh = BaseState(Condition.BASE_UNCHANGED, BASE, BASE, CAND)

    def verdict(ci):
        return IntegrationPolicy().evaluate(MergeFacts(provenance=prov, base=fresh, review_independent=True, ci=ci, mergeable=True), task_id=TASK)

    assert verdict(diagnose_ci(_run(CAND, unit=ok), _run(BASE, unit=ok))).may_merge
    stale = verdict(diagnose_ci(_run("d" * 40, unit=ok), _run(BASE, unit=ok)))
    assert not stale.may_merge and "another candidate" in stale.unsatisfied[0].reason
    empty = verdict(diagnose_ci(None, None, candidate_sha=CAND))
    assert not empty.may_merge and "no CI gates" in empty.unsatisfied[0].reason


# --- post-merge verification does not depend on a moving tip -----------------------------------------------------------------------------------


def test_done_is_still_verified_when_main_moves_on_and_touches_the_same_files(tmp_path: Path) -> None:
    project, store, base = _project(tmp_path)
    candidate = _task_at_integrate(store, project, base)
    supervisor = Supervisor(store, project, integration_ref=REF)
    coordinator = _coordinator(store, project, supervisor)
    for _ in range(8):
        coordinator.tick()
    assert store.get_task(TASK)["stage"] == Stage.DONE and candidate
    commit(project, {"src/widget.py": "W = 2  # a later, unrelated task edits the same file\n"}, "later work")

    verdict = supervisor.verify_integration(TASK, store.latest_candidate(TASK)["sha"])

    assert verdict.verified  # judged at the commit that landed it, not at today's tip


# --- fail-closed configuration and typed delivery errors --------------------------------------------------------------------------------------------


def test_a_corrupt_isolation_file_fails_closed_instead_of_forgetting_the_forbidden_list(tmp_path: Path) -> None:
    mine = init_repo(tmp_path / "mine")
    commit(mine, {"README.md": "x\n"}, "init")
    (mine / ".stagemesh").mkdir()
    (mine / ".stagemesh" / "isolation.json").write_text("{not json", encoding="utf-8")
    report = check_isolation(mine)
    assert "UNREADABLE_ISOLATION_CONFIG" in {f.code for f in report.findings} and not report.isolated
    (mine / ".stagemesh" / "isolation.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert "UNREADABLE_ISOLATION_CONFIG" in {f.code for f in check_isolation(mine).findings}
    (mine / ".stagemesh" / "isolation.json").write_text(json.dumps({"forbidden_checkouts": []}), encoding="utf-8")
    assert "UNREADABLE_ISOLATION_CONFIG" not in {f.code for f in check_isolation(mine).findings}


def test_a_blocking_finding_on_a_file_the_candidate_itself_changed_is_never_waved_through() -> None:
    scope = TaskScope("o", allowed_files=("docs/**",))
    finding = ReviewFindingInput("this change to src/legacy.py is broken", "major", "src/legacy.py")
    report = ReviewReport("a" * 40, "claude", "codex", (finding,))
    waved = assess_review(report, candidate_sha="a" * 40, scope=scope, task_id=TASK)
    assert waved.approved  # out of scope and untouched by the candidate: deferred
    caught = assess_review(report, candidate_sha="a" * 40, scope=scope, task_id=TASK, candidate_changed_files=("src/legacy.py",))
    assert not caught.approved and caught.remediate == [finding]  # the candidate edited it: this is its defect, not someone else's
