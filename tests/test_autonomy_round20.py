"""Review round 20: an unfinished check list is not a pass, and other location spellings stay distinct."""

from __future__ import annotations

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci, plan_ci_response

CAND, BASE = "c" * 40, "b" * 40


def test_an_incomplete_check_list_of_successes_does_not_proceed() -> None:
    candidate = HostedCIRun(CAND, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, complete=False, environment="x")
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.SUCCESS)}, environment="x")
    diagnosis = diagnose_ci(candidate, base)
    assert diagnosis.overall is CIClass.PENDING
    assert diagnosis.merge_blockers(allow_baseline_failures=True, baseline_requires_detail=True)
    assert plan_ci_response(diagnosis, task_id="T-1").action.value != "PROCEED"


def test_gcc_and_msvc_column_locations_are_not_collapsed() -> None:
    pairs = (
        ("src/app.c:10: error: widget undeclared", "src/app.c:40: error: widget undeclared"),
        (r"src\app.c(10,1): error C2065: 'widget'", r"src\app.c(40,1): error C2065: 'widget'"),
    )
    for base_log, extra in pairs:
        base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, base_log)}, environment="x")
        gate = GateOutcome("unit", Conclusion.FAILURE, base_log + "\n" + extra)
        klass = diagnose_ci(HostedCIRun(CAND, {"unit": gate}, environment="x"), base).gates[0].klass
        assert klass is CIClass.CANDIDATE_REGRESSION
