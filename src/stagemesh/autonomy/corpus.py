"""The incident corpus and the capability readiness model: one source of truth for docs/founder-hands-off.md.

Each scenario lists the regression tests that make it permanent, tagged `success` (the situation is resolved without the founder) or
`fail_closed` (the unsafe variant is refused or escalated). `tests/test_autonomy_corpus.py` verifies that every referenced test
exists, that every scenario has both kinds, and that the canonical document states the same numbers as this module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .decisions import REQUIRED_STREAK


@dataclass(frozen=True)
class ScenarioTest:
    ref: str  # tests/<file>.py::<test name>
    kind: str  # "success" | "fail_closed"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    capability: str
    tests: tuple[ScenarioTest, ...]
    milestone_one: bool = False


def _t(ref: str, kind: str) -> ScenarioTest:
    return ScenarioTest(ref, kind)


P, B, CI, R, D, RC, G, L = (
    "tests/test_autonomy_provenance.py::",
    "tests/test_autonomy_base_state.py::",
    "tests/test_autonomy_ci.py::",
    "tests/test_autonomy_review.py::",
    "tests/test_autonomy_dependencies.py::",
    "tests/test_autonomy_recovery.py::",
    "tests/test_autonomy_github.py::",
    "tests/test_autonomy_lifecycle.py::",
)

INCIDENT_CORPUS: tuple[Scenario, ...] = (
    Scenario("A", "Unexpected second writer", "external workspace mutation", (
        _t(L + "test_scenario_a_end_to_end_second_writer_commit_is_never_adopted_into_the_candidate", "fail_closed"),
        _t(P + "test_scenario_a_second_writer_commit_is_detected_quarantined_and_never_adopted", "fail_closed"),
        _t(P + "test_scenario_a_second_writer_push_to_the_published_candidate_branch", "fail_closed"),
        _t(P + "test_stagemesh_own_commit_is_an_owner_advance_not_a_mutation", "success"),
    ), True),
    Scenario("B", "Normal main advancement", "ordinary main advancement", (
        _t(L + "test_scenario_b_end_to_end_stale_candidate_is_refreshed_reevidenced_and_integrated", "success"),
        _t(B + "test_scenario_b_normal_main_advancement_refreshes_and_revalidates_without_the_founder", "success"),
        _t(B + "test_scenario_b_conflict_is_reconstructed_once_then_escalates_with_a_specific_question", "fail_closed"),
    ), True),
    Scenario("C", "Equivalent-tree history rewrite", "history rewrite detection", (
        _t(L + "test_scenario_c_end_to_end_equivalent_tree_rewrite_is_retargeted_reevidenced_and_integrated", "success"),
        _t(B + "test_scenario_c_equivalent_tree_history_rewrite_is_retargeted_with_proof_and_provenance", "success"),
        _t(B + "test_scenario_c_rewrite_that_conflicts_is_reconstructed_not_force_adopted", "fail_closed"),
        _t(B + "test_equivalence_proof_is_not_proven_when_the_replacement_tree_differs", "fail_closed"),
    ), True),
    Scenario("D", "Stacked PR", "PR dependency handling", (
        _t(D + "test_scenario_d_pr2_pauses_and_resumes_automatically_after_pr1_is_squash_merged", "success"),
        _t(D + "test_dependency_closed_without_landing_escalates_with_a_specific_decision", "fail_closed"),
    )),
    Scenario("E", "Base CI already red", "CI diagnosis", (
        _t(L + "test_scenario_e_end_to_end_base_already_red_does_not_block_integration", "success"),
        _t(G + "test_scenario_e_real_incident_main_red_on_both_gates_is_baseline_for_a_candidate_failing_the_same_gates", "success"),
        _t(CI + "test_candidate_is_never_called_broken_without_base_evidence", "fail_closed"),
    ), True),
    Scenario("F", "Candidate introduces a new failure", "CI diagnosis", (
        _t(L + "test_scenario_f_end_to_end_new_failure_is_remediated_without_touching_baseline_failures", "success"),
        _t(L + "test_scenario_f_remediation_budget_exhaustion_is_a_typed_specific_escalation", "fail_closed"),
    ), True),
    Scenario("G", "Incorrect CI/test fixture (NO_IMPLEMENTATION_CHANGE)", "broken/fragile test detection", (
        _t(CI + "test_scenario_g_fixture_expecting_success_from_a_noop_provider_is_diagnosed_as_a_test_defect", "success"),
        _t(CI + "test_scenario_g_production_really_returns_no_implementation_change_for_a_noop_provider", "success"),
        _t(CI + "test_scenario_g_remediation_that_weakens_production_is_rejected_but_fixing_the_test_is_allowed", "fail_closed"),
    )),
    Scenario("H", "Review causes remediation", "independent review lifecycle", (
        _t(R + "test_scenario_h_real_defect_triggers_remediation_new_sha_validation_and_independent_rereview", "success"),
        _t(R + "test_review_bound_to_another_sha_is_stale_and_requires_rereview", "fail_closed"),
    )),
    Scenario("I", "Unrelated reviewer suggestion", "scope discipline", (
        _t(R + "test_scenario_i_unrelated_reviewer_suggestion_is_recorded_not_fixed", "success"),
        _t(R + "test_scenario_i_malformed_or_infrastructure_verdicts_are_left_to_the_reviewer", "fail_closed"),
    )),
    Scenario("J", "Unresolved dependency", "PR dependency handling", (
        _t(D + "test_scenario_j_red_dependency_blocks_downstream_and_resumes_when_it_is_fixed_and_landed", "success"),
        _t(D + "test_unknown_dependency_state_fails_closed", "fail_closed"),
    )),
    Scenario("K", "Known versus unknown process identity", "recovery policy", (
        _t(RC + "test_scenario_k_task_completes_a_candidate_on_the_replacement_while_the_fenced_process_keeps_writing", "success"),
        _t(RC + "test_scenario_k_coordinator_with_the_supervisor_no_longer_stalls_on_an_unknown_execution", "success"),
        _t(RC + "test_scenario_k_unknown_identity_is_never_released_and_defaults_to_fencing", "fail_closed"),
        _t(RC + "test_scenario_k_hold_policy_fails_closed_forever", "fail_closed"),
    )),
    Scenario("L", "Destructive git operation request", "recovery policy", (
        _t(RC + "test_scenario_l_force_push_of_a_candidate_branch_becomes_a_replacement_branch", "success"),
        _t(RC + "test_scenario_l_history_rewrite_of_the_integration_ref_has_no_safe_alternative_and_escalates", "fail_closed"),
    )),
)

# Readiness model. Level 0 = absent, 1 = deterministic policy with unit tests, 2 = wired into the real lifecycle and covered end to
# end locally, 3 = additionally proven on real tasks against live systems. Only streak evidence can reach 3; none exists yet.
CAPABILITY_LEVELS: dict[str, tuple[int, str]] = {
    "1 Candidate integrity (exact-SHA provenance)": (2, "coordinator guard blocks integration without evidence bound to the exact candidate"),
    "2 External workspace mutation": (2, "hooks in the executors and coordinator guard; quarantine and restore"),
    "3 Ordinary main advancement": (2, "SupervisedIntegrator refreshes the candidate and sends it back to VALIDATE"),
    "4 History rewrite detection": (2, "retargeted replacement with equivalence proof and provenance, original preserved"),
    "5 PR dependency / stacked PR handling": (1, "policy, supervisor entry points and GitHub adapter exist; no scheduler drives them yet"),
    "6 CI diagnosis (candidate vs base)": (1, "diagnosis wired into the integration guard; hosted CI is not configured in the CLI yet"),
    "7 Broken/fragile test detection": (1, "classification and remediation guard exist; no test harness emits observations yet"),
    "8 Independent review lifecycle": (2, "scope policy wraps the reviewer adapter in the CLI"),
    "9 Scope discipline": (2, "deferred-work ledger and scoped review; contract enforcement already existed"),
    "10 Merge policy and post-merge verification": (1, "policy and verification exist; PR merge flow is library-only, local ref path verifies"),
    "Isolation guard (fail closed)": (2, "enforced by the CLI whenever the supervisor is enabled"),
    "Decision trace and escalation contract": (2, "every decision is persisted; escalations are typed and validated"),
    "Unknown process identity recovery": (2, "the coordinator fences an UNKNOWN execution onto a replacement worktree instead of stalling"),
    "Destructive git operation policy": (1, "policy and preservation exist; no git wrapper routes requests through it yet"),
}
MAX_LEVEL = 3


def capability_readiness_percent() -> int:
    total = sum(level for level, _ in CAPABILITY_LEVELS.values())
    return round(100 * total / (MAX_LEVEL * len(CAPABILITY_LEVELS)))


def scenario_summary() -> dict[str, object]:
    return {
        "scenarios": {s.id: s.title for s in INCIDENT_CORPUS},
        "tests": sum(len(s.tests) for s in INCIDENT_CORPUS),
        "milestone_one": [s.id for s in INCIDENT_CORPUS if s.milestone_one],
        "capability_readiness_percent": capability_readiness_percent(),
        "required_streak": REQUIRED_STREAK,
    }
