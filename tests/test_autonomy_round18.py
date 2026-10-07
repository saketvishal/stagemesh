"""Review round 18: no-test signatures, line numbers, and cross-suite reruns."""

from __future__ import annotations

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.github_adapter import GitHubHostedCI

CAND, BASE = "c" * 40, "b" * 40


def test_a_new_non_keyword_line_changes_a_gate_that_names_no_test() -> None:
    base_log = "error: build failed"
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, base_log)}, environment="x")
    candidate = GateOutcome("unit", Conclusion.FAILURE, base_log + "\nld: src/app.o: undefined reference to widget")
    diagnosis = diagnose_ci(HostedCIRun(CAND, {"unit": candidate}, environment="x"), base)
    assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_a_second_lint_at_a_different_line_is_not_the_same_baseline_failure() -> None:
    old = "FAILED tests/test_old.py::test_known - AssertionError"
    base_log = old + "\nsrc/app.py:1:1: F401 os imported but unused"
    candidate_log = old + "\nsrc/app.py:1:1: F401 os imported but unused\nsrc/app.py:50:1: F401 os imported but unused"
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, base_log, ("tests/test_old.py::test_known",))}, environment="x")
    gate = GateOutcome("unit", Conclusion.FAILURE, candidate_log, ("tests/test_old.py::test_known",))
    assert diagnose_ci(HostedCIRun(CAND, {"unit": gate}, environment="x"), base).gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_a_passing_rerun_in_one_suite_does_not_mark_another_suites_new_failure_flaky() -> None:
    def check(id_: int, text: str, suite: int, conclusion: str, started: str) -> dict:
        return {
            "id": id_,
            "name": "unit",
            "status": "completed",
            "conclusion": conclusion,
            "output": {"text": text},
            "started_at": started,
            "check_suite": {"id": suite},
        }

    runs = [
        check(1, "", 100, "success", "2026-01-01T00:00:00Z"),
        check(2, "FAILED tests/test_old.py::test_known - AssertionError", 100, "failure", "2026-01-01T00:05:00Z"),
        check(3, "FAILED tests/test_new.py::test_regressed - AssertionError", 200, "failure", "2026-01-01T00:06:00Z"),
    ]

    class Transport:
        def request(self, method, path, body=None):
            return 200, {}, {"total_count": len(runs), "check_runs": runs}

    run = GitHubHostedCI("o", "r", Transport()).run_for(CAND)
    assert run is not None
    base = HostedCIRun(
        BASE,
        {"unit": GateOutcome("unit", Conclusion.FAILURE, "FAILED tests/test_old.py::test_known - AssertionError", ("tests/test_old.py::test_known",))},
        environment="github-actions",
    )
    diagnosis = diagnose_ci(run, base)
    assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION
    assert "tests/test_new.py::test_regressed" in diagnosis.gates[0].new_failing_tests
