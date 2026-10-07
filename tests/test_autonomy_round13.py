"""Review round 13: same-named checks, repeated failures, closed stack parents, suggestions with severity, environment mismatch both ways."""

from __future__ import annotations

from autonomy_support import TASK

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci, plan_ci_response
from stagemesh.autonomy.decisions import Action, Condition, EscalationReason
from stagemesh.autonomy.dependencies import CIRollup, PRState, PullRequest, evaluate_dependencies, implicit_dependencies
from stagemesh.autonomy.github_adapter import GitHubHostedCI
from stagemesh.autonomy.review_adapter import findings_from_response
from stagemesh.autonomy.review_policy import FindingClass, ReviewFindingInput, classify_finding
from stagemesh.autonomy.scope import TaskScope

CAND, BASE = "c" * 40, "b" * 40
SCOPE = TaskScope("o", allowed_files=("docs/**",))


def _runs(*runs: dict):
    class Transport:
        def request(self, method, path, body=None):
            return 200, {}, {"total_count": len(runs), "check_runs": list(runs)}

    return GitHubHostedCI("o", "r", Transport()).run_for(CAND)


def _check(id_: int, name: str, conclusion: str, suite: int, started: str):
    return {"id": id_, "name": name, "status": "completed", "conclusion": conclusion, "output": {}, "started_at": started, "check_suite": {"id": suite}}


# --- same-named checks from different workflows are different gates, not reruns ---------------------------------------------------------------------


def test_a_later_green_check_with_the_same_name_from_another_suite_cannot_hide_an_earlier_red_one() -> None:
    run = _runs(
        _check(1, "build", "failure", suite=100, started="2026-01-01T00:00:00Z"),
        _check(2, "build", "success", suite=200, started="2026-01-01T00:10:00Z"),  # another workflow reporting the same check name, later
    )
    assert run.gates["build"].conclusion is Conclusion.FAILURE  # the worst of the independent suites decides


def test_a_pending_suite_keeps_the_gate_pending_and_reruns_within_a_suite_still_collapse() -> None:
    pending = _runs(
        {**_check(1, "build", "success", suite=100, started="2026-01-01T00:00:00Z")},
        {"id": 2, "name": "build", "status": "in_progress", "conclusion": None, "output": {}, "started_at": "2026-01-01T00:10:00Z", "check_suite": {"id": 200}},
    )
    assert pending.gates["build"].conclusion is Conclusion.PENDING and not pending.complete
    rerun = _runs(
        _check(1, "unit", "failure", suite=100, started="2026-01-01T00:00:00Z"),
        _check(2, "unit", "success", suite=100, started="2026-01-01T00:10:00Z"),  # the same suite: a genuine rerun that passed
    )
    assert rerun.gates["unit"].conclusion is Conclusion.SUCCESS and rerun.gates["unit"].rerun_conclusions == (Conclusion.FAILURE,)


# --- a failure that repeats on the identical SHA with a green base is the candidate ----------------------------------------------------------------------------


def test_a_repeated_failure_on_the_same_sha_is_a_regression_even_with_an_infrastructure_looking_log() -> None:
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")
    first = GateOutcome("unit", Conclusion.FAILURE, "could not resolve host while the candidate's new code calls out")
    assert diagnose_ci(HostedCIRun(CAND, {"unit": first}, environment="x"), base).gates[0].klass is CIClass.INFRASTRUCTURE_FAILURE  # first look
    again = GateOutcome("unit", Conclusion.FAILURE, first.log, rerun_conclusions=(Conclusion.FAILURE,))
    diagnosis = diagnose_ci(HostedCIRun(CAND, {"unit": again}, environment="x"), base)
    assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.REMEDIATE_CANDIDATE


# --- stacks on a parent that was closed without landing ----------------------------------------------------------------------------------------------------


def test_a_pr_stacked_on_a_closed_parent_escalates_instead_of_waiting_forever() -> None:
    parent = PullRequest(1, "a" * 40, "feat/one", "main", PRState.CLOSED, CIRollup.SUCCESS, True)
    child = PullRequest(2, "b" * 40, "feat/two", "feat/one", PRState.OPEN, CIRollup.SUCCESS, True)
    assert {(e.pr, e.depends_on) for e in implicit_dependencies({1: parent, 2: child})} == {(2, 1)}
    assessment = evaluate_dependencies(2, {1: parent, 2: child}, [], integration_ref="main")
    assert assessment.decision.condition is Condition.DEPENDENCY_CLOSED_WITHOUT_LANDING
    assert assessment.decision.escalation.reason is EscalationReason.DEPENDENCY_CLOSED_WITHOUT_LANDING


def test_the_newest_pr_owning_a_head_branch_is_the_parent() -> None:
    old = PullRequest(1, "a" * 40, "feat/one", "main", PRState.CLOSED, CIRollup.SUCCESS, True)
    new = PullRequest(3, "c" * 40, "feat/one", "main", PRState.OPEN, CIRollup.SUCCESS, True)
    child = PullRequest(2, "b" * 40, "feat/two", "feat/one", PRState.OPEN, CIRollup.SUCCESS, True)
    assert {(e.pr, e.depends_on) for e in implicit_dependencies({1: old, 2: child, 3: new})} == {(2, 3)}


# --- suggestions keep their severity ---------------------------------------------------------------------------------------------------------------------------


def test_a_suggestion_with_a_severe_label_is_not_deferred_but_one_without_a_severity_is() -> None:
    (severe,) = findings_from_response([{"severity": "critical", "message": "x", "category": "suggestion", "path": "docs/a.md"}])
    assert classify_finding(severe, SCOPE).klass is FindingClass.BLOCKING_IN_SCOPE
    (mild,) = findings_from_response([{"severity": "info", "message": "x", "category": "suggestion", "path": "docs/a.md"}])
    assert classify_finding(mild, SCOPE).klass is FindingClass.UNRELATED_SUGGESTION
    (bare,) = findings_from_response([{"message": "consider a cache", "category": "suggestion"}])
    assert classify_finding(bare, SCOPE).klass is FindingClass.UNRELATED_SUGGESTION
    (unlabelled,) = findings_from_response([{"message": "a real problem with no label at all", "path": "docs/a.md"}])
    assert classify_finding(unlabelled, SCOPE).klass is FindingClass.BLOCKING_IN_SCOPE
    assert ReviewFindingInput("m").is_blocking


# --- a different environment is never a valid comparison, whether base passed or failed -----------------------------------------------------------------------


def test_a_candidate_failure_against_a_base_that_passed_elsewhere_is_unknown_not_a_regression() -> None:
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="linux")
    candidate = HostedCIRun(CAND, {"unit": GateOutcome("unit", Conclusion.FAILURE, "windows only: path separator")}, environment="windows")
    diagnosis = diagnose_ci(candidate, base)
    assert diagnosis.gates[0].klass is CIClass.GENUINE_UNKNOWN and diagnosis.gates[0].evidence == "ENVIRONMENT_MISMATCH"
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.REQUEST_BASE_CI  # ask for base CI in the candidate's environment
    assert diagnosis.merge_blockers()
