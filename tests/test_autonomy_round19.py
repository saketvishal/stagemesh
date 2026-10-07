"""Review round 19: unfinished check-run pages, indented FAILED lines, parenthesized locations."""

from __future__ import annotations

import re

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.dependencies import CIRollup
from stagemesh.autonomy.github_adapter import GitHubHostedCI

CAND, BASE = "c" * 40, "b" * 40


def _ci(pages: dict[int, tuple[list[dict], int | None]]) -> GitHubHostedCI:
    class Transport:
        def request(self, method, path, body=None):
            match = re.search(r"[?&]page=(\d+)", path)
            page = int(match.group(1)) if match else 1
            batch, total = pages[page]
            payload: dict = {"check_runs": batch}
            if total is not None:
                payload["total_count"] = total
            return 200, {}, payload

    return GitHubHostedCI("o", "r", Transport())


def _check(id_: int, name: str, conclusion: str, text: str = "") -> dict:
    return {
        "id": id_,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"text": text} if text else {},
        "started_at": "2026-01-01T00:00:00Z",
        "check_suite": {"id": id_},
    }


def test_a_short_page_does_not_hide_a_later_check_the_host_still_counts() -> None:
    page1 = [_check(i, f"g{i}", "success") for i in range(50)]
    page2 = [_check(50, "job-fail", "failure", "FAILED tests/test_x.py::test_x - AssertionError")]
    run = _ci({1: (page1, 51), 2: (page2, 51)}).run_for(CAND)
    assert run is not None and run.gates["job-fail"].conclusion is Conclusion.FAILURE
    assert _ci({1: (page1, 51), 2: (page2, 51)}).rollup(CAND) is CIRollup.FAILURE


def test_stopping_at_the_page_cap_is_not_a_green_rollup() -> None:
    pages = {n: ([_check(n * 100 + i, f"g{n}-{i}", "success") for i in range(100)], 1100) for n in range(1, 11)}
    pages[11] = ([_check(9999, "hidden-fail", "failure")], 1100)
    ci = _ci(pages)
    run = ci.run_for(CAND)
    assert run is not None and not run.complete and "hidden-fail" not in run.gates
    assert ci.rollup(CAND) is CIRollup.PENDING


def test_an_indented_failed_line_is_a_new_test_not_a_baseline_match() -> None:
    base_log = "error: build failed"
    candidate_log = base_log + "\n  FAILED tests/test_new.py::test_x - AssertionError"

    class Transport:
        def request(self, method, path, body=None):
            text = candidate_log if CAND in path else base_log
            run = _check(1, "unit", "failure", text)
            return 200, {}, {"total_count": 1, "check_runs": [run]}

    ci = GitHubHostedCI("o", "r", Transport())
    candidate = ci.run_for(CAND)
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, base_log)}, environment="github-actions")
    assert candidate is not None
    assert candidate.gates["unit"].failing_tests == ("tests/test_new.py::test_x",)
    assert diagnose_ci(candidate, base).gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_a_second_msvc_diagnostic_at_another_line_is_a_regression() -> None:
    base_log = r"src\app.c(10): error C2065: 'widget': undeclared identifier"
    candidate_log = base_log + "\n" + r"src\app.c(40): error C2065: 'widget': undeclared identifier"
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, base_log)}, environment="x")
    gate = GateOutcome("unit", Conclusion.FAILURE, candidate_log)
    assert diagnose_ci(HostedCIRun(CAND, {"unit": gate}, environment="x"), base).gates[0].klass is CIClass.CANDIDATE_REGRESSION
