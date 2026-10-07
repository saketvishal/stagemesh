"""Capabilities 6 and 7: CI diagnosis (Scenarios E, F) and broken/fragile test detection (Scenario G)."""

from __future__ import annotations

import json
from pathlib import Path

from autonomy_support import TASK, decisions, new_store
from test_candidate_integrity import _coordinator, _project

from stagemesh.autonomy.ci_diagnosis import (
    PRODUCTION_INVARIANTS,
    CIClass,
    Conclusion,
    FakeHostedCI,
    GateOutcome,
    HostedCIRun,
    TestObservation,
    detect_test_defect,
    diagnose_ci,
    guard_remediation,
    observations_from_log,
    plan_ci_response,
)
from stagemesh.autonomy.decisions import Action, Condition
from stagemesh.autonomy.scope import TaskScope, deferred_items
from stagemesh.autonomy.supervisor import Supervisor
from stagemesh.workspaces import NO_IMPLEMENTATION_CHANGE

CAND, BASE = "c" * 40, "b" * 40

LINT_LOG = "ruff failed: src/stagemesh/x.py:10:5 E501 line too long (120 > 100) in /tmp/pytest-of-runner/pytest-17/work after 12.3s"
LINT_LOG_RERUN = "ruff failed: src/stagemesh/x.py:10:5 E501 line too long (120 > 100) in /tmp/pytest-of-runner/pytest-92/work after 9.8s"


def _run(sha: str, **gates: GateOutcome | Conclusion) -> HostedCIRun:
    return HostedCIRun(
        sha,
        {name: g if isinstance(g, GateOutcome) else GateOutcome(name, g) for name, g in gates.items()},
    )


def _failed(name: str, log: str = "", tests: tuple[str, ...] = (), **kwargs) -> GateOutcome:
    return GateOutcome(name, Conclusion.FAILURE, log, tests, **kwargs)


# --- Scenario E: base CI already red ----------------------------------------------------------------------------------------------------


def test_scenario_e_identical_failures_on_base_and_candidate_are_baseline_not_regression() -> None:
    candidate = _run(CAND, lint=_failed("lint", LINT_LOG), windows=_failed("windows", tests=("tests/test_a.py::test_x",)), unit=Conclusion.SUCCESS)
    base = _run(BASE, lint=_failed("lint", LINT_LOG_RERUN), windows=_failed("windows", tests=("tests/test_a.py::test_x",)), unit=Conclusion.SUCCESS)

    diagnosis = diagnose_ci(candidate, base)

    assert {g.gate: g.klass for g in diagnosis.gates} == {
        "lint": CIClass.BASELINE_FAILURE,  # shas, timings and temp paths in the log do not make it a new failure
        "windows": CIClass.BASELINE_FAILURE,
        "unit": CIClass.PASSED,
    }
    assert diagnosis.overall is CIClass.BASELINE_FAILURE
    decision = plan_ci_response(diagnosis, task_id=TASK)
    assert decision.condition is Condition.CI_BASELINE_FAILURE
    assert decision.action is Action.RECORD_BASELINE_FAILURE_AND_PROCEED
    assert decision.action is not Action.REMEDIATE_CANDIDATE and not decision.requires_human
    assert decision.detail["forbid_unrelated_ci_fixes"] is True
    assert diagnosis.merge_blockers(allow_baseline_failures=True) == []
    assert len(diagnosis.merge_blockers(allow_baseline_failures=False)) == 2  # policy can still demand green


def test_scenario_e_supervisor_compares_candidate_ci_with_base_ci_and_defers_baseline_failures(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("task", source_id=TASK)
    ci = FakeHostedCI([_run(CAND, lint=_failed("lint", LINT_LOG)), _run(BASE, lint=_failed("lint", LINT_LOG_RERUN))])
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci)
    scope = TaskScope("objective", allowed_files=("src/widget/**",))

    decision = supervisor.assess_ci(TASK, CAND, BASE, scope=scope)

    assert ci.requests == [CAND, BASE]  # base CI was consulted before concluding anything
    assert decision.condition is Condition.CI_BASELINE_FAILURE and decision.action is Action.RECORD_BASELINE_FAILURE_AND_PROCEED
    (deferred,) = deferred_items(store, TASK)
    assert "lint" in deferred.summary and deferred.source == "ci"  # recorded, not fixed as part of this task
    (record,) = decisions(store, TASK)
    assert record["shas"] == {"candidate": CAND, "base": BASE} and record["requires_human"] is False


# --- Scenario F: candidate introduces a new failure ----------------------------------------------------------------------------------------


def test_scenario_f_gate_passing_on_base_and_failing_on_candidate_is_blocked_and_remediated() -> None:
    candidate = _run(CAND, unit=_failed("unit", tests=("tests/test_widget.py::test_new",)), lint=_failed("lint", LINT_LOG))
    base = _run(BASE, unit=Conclusion.SUCCESS, lint=_failed("lint", LINT_LOG_RERUN))

    diagnosis = diagnose_ci(candidate, base)
    decision = plan_ci_response(diagnosis, task_id=TASK)

    regression = diagnosis.by_class(CIClass.CANDIDATE_REGRESSION)
    assert [g.gate for g in regression] == ["unit"]
    assert regression[0].new_failing_tests == ("tests/test_widget.py::test_new",)
    assert decision.condition is Condition.CI_CANDIDATE_REGRESSION
    assert decision.action is Action.REMEDIATE_CANDIDATE and not decision.requires_human
    assert decision.detail["remediate_gates"] == ["unit"]
    assert decision.detail["not_to_fix"] == ["lint"]  # the baseline failure is explicitly off limits
    assert diagnosis.merge_blockers() == ["unit: CANDIDATE_REGRESSION (the gate passes on base and fails on the candidate)"]


def test_scenario_f_new_failures_on_an_already_red_gate_are_still_a_regression() -> None:
    candidate = _run(CAND, unit=_failed("unit", tests=("t::old", "t::new")))
    base = _run(BASE, unit=_failed("unit", tests=("t::old",)))
    (gate,) = diagnose_ci(candidate, base).gates
    assert gate.klass is CIClass.CANDIDATE_REGRESSION and gate.new_failing_tests == ("t::new",)


def test_candidate_failing_a_subset_of_base_failures_is_baseline() -> None:
    candidate = _run(CAND, unit=_failed("unit", tests=("t::old",)))
    base = _run(BASE, unit=_failed("unit", tests=("t::old", "t::other")))
    (gate,) = diagnose_ci(candidate, base).gates
    assert gate.klass is CIClass.BASELINE_FAILURE


def test_candidate_is_never_called_broken_without_base_evidence(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("task", source_id=TASK)
    ci = FakeHostedCI([_run(CAND, unit=_failed("unit", "AssertionError: boom"))])  # base CI unavailable
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci)

    decision = supervisor.assess_ci(TASK, CAND, BASE)

    assert decision.condition is Condition.CI_GENUINE_UNKNOWN
    assert decision.action is Action.REQUEST_BASE_CI  # asks for base CI instead of remediating the candidate
    assert ci.requests == [CAND, BASE]


def test_infrastructure_failure_is_rerun_not_remediated() -> None:
    candidate = _run(CAND, unit=GateOutcome("unit", Conclusion.CANCELLED, "The runner has received a shutdown signal."))
    base = _run(BASE, unit=Conclusion.SUCCESS)
    diagnosis = diagnose_ci(candidate, base)
    assert diagnosis.overall is CIClass.INFRASTRUCTURE_FAILURE
    assert plan_ci_response(diagnosis, task_id=TASK, reruns_left=1).action is Action.RERUN_CI
    exhausted = plan_ci_response(diagnosis, task_id=TASK, reruns_left=0)  # bounded: no infinite reruns and no endless wait
    assert exhausted.action is Action.ESCALATE_TO_FOUNDER and exhausted.escalation.reason.value == "CI_FAILURE_UNRESOLVED"


def test_failure_that_passes_on_rerun_of_the_same_sha_is_a_fragile_test() -> None:
    candidate = _run(CAND, unit=_failed("unit", "flaky timeout", ("t::flaky",), rerun_conclusions=(Conclusion.SUCCESS,)))
    base = _run(BASE, unit=Conclusion.SUCCESS)
    (gate,) = diagnose_ci(candidate, base).gates
    assert gate.klass is CIClass.BROKEN_FRAGILE_TEST and "rerun" in gate.reason


def test_unsupported_environment_without_base_evidence_is_classified_but_never_tolerated() -> None:
    candidate = _run(CAND, macos=_failed("macos", "ERROR: Unsupported Python 3.8; requires python >=3.11"))
    diagnosis = diagnose_ci(candidate, None)
    (gate,) = diagnosis.gates
    assert gate.klass is CIClass.UNSUPPORTED_ENVIRONMENT and gate.evidence == "NO_BASE"
    decision = plan_ci_response(diagnosis, task_id=TASK)
    assert decision.condition is Condition.CI_UNSUPPORTED_ENVIRONMENT and decision.action is Action.REQUEST_BASE_CI
    assert diagnosis.merge_blockers()  # the candidate may have caused it (e.g. by raising requires-python): not tolerable without base proof


def test_an_environment_error_never_makes_a_changed_failure_tolerable() -> None:
    env_error = "ERROR: Unsupported Python 3.8; requires python >=3.11"
    base = _run(BASE, macos=_failed("macos", env_error + " (runner image 20240101)"))
    same = diagnose_ci(_run(CAND, macos=_failed("macos", env_error + " (runner image 20240202)")), base)
    assert same.gates[0].klass is CIClass.BASELINE_FAILURE and same.merge_blockers() == []  # identical once timings and ids are normalized
    changed = diagnose_ci(_run(CAND, macos=_failed("macos", "requires python >=3.12 not satisfied")), base)
    assert changed.gates[0].klass is CIClass.CANDIDATE_REGRESSION and changed.merge_blockers()


def test_candidate_that_turns_an_already_red_gate_into_an_environment_error_is_a_regression() -> None:
    """Independent-review finding: a candidate raising requires-python must not hide behind a gate that was already red."""
    base = _run(BASE, unit=_failed("unit", tests=("t::old",)))
    candidate = _run(CAND, unit=_failed("unit", "ERROR: Unsupported Python 3.8; requires python >=3.11"))
    diagnosis = diagnose_ci(candidate, base)
    (gate,) = diagnosis.gates
    assert gate.klass is CIClass.CANDIDATE_REGRESSION
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.REMEDIATE_CANDIDATE
    assert diagnosis.merge_blockers() and diagnosis.merge_blockers(allow_baseline_failures=True)


def test_dependency_base_pr_failure_is_attributed_to_the_dependency_not_the_candidate() -> None:
    candidate = _run(CAND, unit=_failed("unit", tests=("t::a",)))
    main = _run(BASE, unit=Conclusion.SUCCESS)
    dependency = _run("d" * 40, unit=_failed("unit", tests=("t::a",)))
    diagnosis = diagnose_ci(candidate, main, dependency=dependency)
    assert diagnosis.overall is CIClass.DEPENDENCY_BASE_PR_FAILURE
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.BLOCK_ON_DEPENDENCY


def test_pending_gates_wait() -> None:
    candidate = _run(CAND, unit=Conclusion.PENDING)
    decision = plan_ci_response(diagnose_ci(candidate, _run(BASE, unit=Conclusion.SUCCESS)), task_id=TASK)
    assert decision.condition is Condition.CI_PENDING and decision.action is Action.WAIT


def test_green_candidate_proceeds() -> None:
    decision = plan_ci_response(diagnose_ci(_run(CAND, unit=Conclusion.SUCCESS), _run(BASE, unit=Conclusion.SUCCESS)), task_id=TASK)
    assert decision.condition is Condition.CI_GREEN and decision.action is Action.PROCEED


# --- Scenario G: incorrect CI/test fixture (NO_IMPLEMENTATION_CHANGE) --------------------------------------------------------------------


def _observation_log(test_id: str, test_path: str) -> str:
    marker = {
        "test_id": test_id,
        "test_path": test_path,
        "expected": "success",
        "observed_failure_code": NO_IMPLEMENTATION_CHANGE,
        "provider_made_change": False,
    }
    return f"FAILED {test_id} - AssertionError\nSTAGEMESH_TEST_OBSERVATION {json.dumps(marker)}\n"


def test_scenario_g_production_really_returns_no_implementation_change_for_a_noop_provider(tmp_path: Path) -> None:
    """Ground truth for the fixture mismatch: the real coordinator/executor, not a mock, reports the failure code."""
    project, _ = _project(tmp_path)
    store, coordinator = _coordinator(tmp_path, project, "pass\n")  # a provider that changes nothing
    assert coordinator.tick() == 0  # production behaves correctly: no candidate, task stays in IMPLEMENT
    payload = json.loads(store.audit_events()[0]["payload"])
    assert payload["reason"] == NO_IMPLEMENTATION_CHANGE
    assert store.latest_candidate(TASK) is None


def test_scenario_g_fixture_expecting_success_from_a_noop_provider_is_diagnosed_as_a_test_defect(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    store.upsert_task("task", source_id=TASK)
    test_id = "tests/test_durability_fixture.py::test_noop_provider_candidate_is_durable"
    test_path = "tests/test_durability_fixture.py"
    ci = FakeHostedCI(
        [
            _run(CAND, unit=_failed("unit", _observation_log(test_id, test_path), (test_id,))),
            _run(BASE, unit=Conclusion.SUCCESS),  # base passes, so a naive diagnosis would blame the candidate
        ]
    )
    supervisor = Supervisor(store, tmp_path, integration_ref="main", hosted_ci=ci)
    scope = TaskScope("durability", allowed_files=("tests/**",), forbidden_files=())

    decision = supervisor.assess_ci(TASK, CAND, BASE, scope=scope, candidate_changed_files=(test_path,))

    assert decision.condition is Condition.CI_BROKEN_FRAGILE_TEST  # diagnosed as a test problem, not a production defect
    assert decision.action is Action.FIX_TEST_FIXTURE
    assert decision.detail["fix_tests"] == [test_path]
    protected = decision.detail["production_code_protected"]
    assert "src/stagemesh/workspaces.py" in protected and "src/stagemesh/execution.py" in protected
    (defect,) = supervisor.last_ci_diagnosis.defects
    assert NO_IMPLEMENTATION_CHANGE in defect.explanation and "made no change" in defect.explanation


def test_scenario_g_remediation_that_weakens_production_is_rejected_but_fixing_the_test_is_allowed() -> None:
    observation = TestObservation("t::noop", "tests/test_x.py", "success", NO_IMPLEMENTATION_CHANGE, False)
    defect = detect_test_defect(observation)
    assert defect is not None
    violations = guard_remediation(["src/stagemesh/workspaces.py", "tests/test_x.py"], [defect])
    assert len(violations) == 1 and "workspaces.py" in violations[0] and "NO_IMPLEMENTATION_CHANGE" in violations[0].upper()
    assert guard_remediation(["tests/test_x.py"], [defect]) == []


def test_scenario_g_invariant_owner_paths_exist_in_this_repository() -> None:
    root = Path(__file__).resolve().parents[1]
    for invariant in PRODUCTION_INVARIANTS.values():
        for path in invariant.owner_paths:
            assert (root / path).is_file(), f"{invariant.code} owner path moved: {path}"


def test_scenario_g_a_test_that_correctly_expects_failure_is_not_a_defect() -> None:
    assert detect_test_defect(TestObservation("t", "p", "failure", NO_IMPLEMENTATION_CHANGE, False)) is None
    assert detect_test_defect(TestObservation("t", "p", "success", NO_IMPLEMENTATION_CHANGE, True)) is None
    assert detect_test_defect(TestObservation("t", "p", "success", "some_other_failure", False)) is None


def test_scenario_g_defect_outside_the_task_scope_is_deferred_not_fixed() -> None:
    log = _observation_log("tests/test_other.py::test_noop", "tests/test_other.py")
    candidate = _run(CAND, unit=_failed("unit", log, ("tests/test_other.py::test_noop",)))
    diagnosis = diagnose_ci(candidate, _run(BASE, unit=Conclusion.SUCCESS), observations=observations_from_log(log), candidate_changed_files=("tests/test_other.py",))
    in_scope = plan_ci_response(diagnosis, task_id=TASK, scope=TaskScope("o", allowed_files=("tests/**",)))
    out_of_scope = plan_ci_response(diagnosis, task_id=TASK, scope=TaskScope("o", allowed_files=("src/widget/**",)))
    assert in_scope.action is Action.FIX_TEST_FIXTURE
    assert out_of_scope.action is Action.ESCALATE_TO_FOUNDER  # not fixed here, and not a silent dead end either: one specific question
    assert out_of_scope.escalation.reason.value == "CI_FAILURE_UNRESOLVED"
    assert out_of_scope.detail["deferred_because"] == "the broken test is outside this task's allowed files"


def test_malformed_observation_markers_are_ignored() -> None:
    assert observations_from_log("STAGEMESH_TEST_OBSERVATION {not json}\nSTAGEMESH_TEST_OBSERVATION {\"test_id\": \"x\"}") == []


def test_different_environments_are_never_compared_as_if_they_were_the_same() -> None:
    """Found by dogfooding: this branch's local CI vs main's hosted CI failed `invariants` at different assertions (this machine has a
    GitHub token set; hosted CI does not). That is not evidence about the candidate."""
    candidate = HostedCIRun(CAND, {"invariants": _failed("invariants", "AssertionError: assert config.github.configured is False")}, environment="local")
    base = HostedCIRun(BASE, {"invariants": _failed("invariants", "AssertionError: assert first == second == 3")}, environment="github-actions")

    diagnosis = diagnose_ci(candidate, base)
    (gate,) = diagnosis.gates

    assert gate.klass is CIClass.GENUINE_UNKNOWN and gate.evidence == "ENVIRONMENT_MISMATCH"
    assert plan_ci_response(diagnosis, task_id=TASK).action is Action.REQUEST_BASE_CI  # rerun base where the candidate ran
    assert diagnosis.notes and "different" not in diagnosis.notes[0] and "local" in diagnosis.notes[0]
    assert diagnosis.merge_blockers()  # not merge-ready, and not remediated either

    same_env = diagnose_ci(HostedCIRun(CAND, candidate.gates, environment="local"), HostedCIRun(BASE, base.gates, environment="local"))
    assert same_env.gates[0].klass is CIClass.CANDIDATE_REGRESSION  # in one environment the same logs are a real difference
    unlabeled = diagnose_ci(HostedCIRun(CAND, candidate.gates), HostedCIRun(BASE, base.gates))
    assert unlabeled.gates[0].klass is CIClass.CANDIDATE_REGRESSION  # unlabeled environments keep the previous behavior
