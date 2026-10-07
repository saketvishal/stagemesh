"""Review round 17: baseline logs must match beyond keywords, and same-named failing suites must not hide each other."""

from __future__ import annotations

from stagemesh.autonomy.ci_diagnosis import CIClass, Conclusion, GateOutcome, HostedCIRun, diagnose_ci
from stagemesh.autonomy.github_adapter import GitHubHostedCI

CAND, BASE = "c" * 40, "b" * 40


def test_a_lint_or_link_line_beside_a_known_red_test_is_not_baseline() -> None:
    old = "FAILED tests/test_old.py::test_known - AssertionError"
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, old, ("tests/test_old.py::test_known",))}, environment="x")
    for extra in (
        "src/app.py:1:1: F401 os imported but unused",
        "ld: src/app.o: undefined reference to widget",
    ):
        gate = GateOutcome("unit", Conclusion.FAILURE, old + "\n" + extra, ("tests/test_old.py::test_known",))
        diagnosis = diagnose_ci(HostedCIRun(CAND, {"unit": gate}, environment="x"), base)
        assert diagnosis.gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_two_failing_suites_with_the_same_check_name_keep_every_failing_test() -> None:
    def check(id_: int, text: str, suite: int) -> dict:
        return {
            "id": id_,
            "name": "unit",
            "status": "completed",
            "conclusion": "failure",
            "output": {"text": text},
            "started_at": f"2026-01-01T00:0{id_}:00Z",
            "check_suite": {"id": suite},
        }

    class Transport:
        def request(self, method, path, body=None):
            runs = [
                check(10, "FAILED tests/test_old.py::test_known - AssertionError", 100),
                check(11, "FAILED tests/test_new.py::test_regressed - AssertionError", 200),
            ]
            return 200, {}, {"total_count": len(runs), "check_runs": runs}

    run = GitHubHostedCI("o", "r", Transport()).run_for(CAND)
    assert run is not None
    tests = set(run.gates["unit"].failing_tests)
    assert tests == {"tests/test_old.py::test_known", "tests/test_new.py::test_regressed"}
    base = HostedCIRun(BASE, {"unit": GateOutcome("unit", Conclusion.FAILURE, "FAILED tests/test_old.py::test_known - AssertionError", ("tests/test_old.py::test_known",))}, environment="github-actions")
    assert diagnose_ci(run, base).gates[0].klass is CIClass.CANDIDATE_REGRESSION


def test_a_missing_total_count_does_not_drop_a_full_later_page() -> None:
    pages = {
        1: [{"id": i, "name": f"g{i}", "status": "completed", "conclusion": "success", "output": {}, "started_at": "2026-01-01T00:00:00Z", "check_suite": {"id": i}} for i in range(100)],
        2: [{"id": 100, "name": "g100", "status": "completed", "conclusion": "failure", "output": {"text": "FAILED tests/test_x.py::test_x - AssertionError"}, "started_at": "2026-01-01T00:00:00Z", "check_suite": {"id": 100}}],
    }

    class Transport:
        def request(self, method, path, body=None):
            page = 2 if "page=2" in path else 1
            return 200, {}, {"check_runs": pages[page]}

    run = GitHubHostedCI("o", "r", Transport()).run_for(CAND)
    assert run is not None and "g100" in run.gates and run.gates["g100"].conclusion is Conclusion.FAILURE
