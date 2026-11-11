"""Explicit test taxonomy and machine-enforced catalog for StageMesh regression.

Every test collected by pytest belongs to exactly one tier. Missing, duplicate,
unknown, malformed, or conflicting classifications fail closed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import PurePosixPath


class TestTier(str, Enum):
    """The five explicit tiers of the StageMesh test taxonomy."""

    __test__ = False

    DETERMINISTIC_UNIT = "deterministic_unit"
    COMPONENT_CONTRACT = "component_contract"
    INTEGRATION_PROCESS = "integration_process"
    NATIVE_PLATFORM = "native_platform"
    SERIAL_TIMING = "serial_timing"
    SOAK_FAILURE_INJECTION = "soak_failure_injection"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class IsolationContract:
    """Isolation rules and parallel safety contract for a test tier."""

    tier: TestTier
    parallel_safe: bool
    concurrency_group: str
    max_parallel_workers: int
    requires_isolated_temp: bool
    requires_isolated_git: bool
    requires_isolated_sqlite: bool
    requires_isolated_ports: bool
    requires_isolated_env: bool
    timeout_seconds: float
    rationale: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["tier"] = str(self.tier)
        return data


TIER_ISOLATION_CONTRACTS: dict[TestTier, IsolationContract] = {
    TestTier.DETERMINISTIC_UNIT: IsolationContract(
        tier=TestTier.DETERMINISTIC_UNIT,
        parallel_safe=True,
        concurrency_group="lightweight",
        max_parallel_workers=16,
        requires_isolated_temp=False,
        requires_isolated_git=False,
        requires_isolated_sqlite=False,
        requires_isolated_ports=False,
        requires_isolated_env=False,
        timeout_seconds=900.0,
        rationale=(
            "Pure in-memory unit tests, data structures, parsing, validation planning, "
            "and invariant checks without subprocesses or shared filesystem locks."
        ),
    ),
    TestTier.COMPONENT_CONTRACT: IsolationContract(
        tier=TestTier.COMPONENT_CONTRACT,
        parallel_safe=True,
        concurrency_group="lightweight",
        max_parallel_workers=16,
        requires_isolated_temp=True,
        requires_isolated_git=False,
        requires_isolated_sqlite=True,
        requires_isolated_ports=True,
        requires_isolated_env=True,
        timeout_seconds=900.0,
        rationale=(
            "Component interfaces, fake provider harnesses, domain entities, and read-model "
            "projections with isolated temporary state and loopback ports."
        ),
    ),
    TestTier.INTEGRATION_PROCESS: IsolationContract(
        tier=TestTier.INTEGRATION_PROCESS,
        parallel_safe=True,
        concurrency_group="external_process",
        max_parallel_workers=8,
        requires_isolated_temp=True,
        requires_isolated_git=True,
        requires_isolated_sqlite=True,
        requires_isolated_ports=True,
        requires_isolated_env=True,
        timeout_seconds=900.0,
        rationale=(
            "Subprocess execution, git repository operations, CLI command dispatch, "
            "and provider simulations running within hermetic worker-isolated roots."
        ),
    ),
    TestTier.NATIVE_PLATFORM: IsolationContract(
        tier=TestTier.NATIVE_PLATFORM,
        parallel_safe=False,
        concurrency_group="native_serial",
        max_parallel_workers=1,
        requires_isolated_temp=True,
        requires_isolated_git=True,
        requires_isolated_sqlite=True,
        requires_isolated_ports=True,
        requires_isolated_env=True,
        timeout_seconds=600.0,
        rationale=(
            "Host-sensitive native Windows job objects, OS process handle locks, "
            "user installer registry/shim changes, and real filesystem directory sharing."
        ),
    ),
    TestTier.SERIAL_TIMING: IsolationContract(
        tier=TestTier.SERIAL_TIMING,
        parallel_safe=False,
        concurrency_group="serial_timing",
        max_parallel_workers=1,
        requires_isolated_temp=True,
        requires_isolated_git=True,
        requires_isolated_sqlite=True,
        requires_isolated_ports=True,
        requires_isolated_env=True,
        timeout_seconds=900.0,
        rationale=(
            "Wall-clock bounded recovery, backoff, and control tests whose timing "
            "assertions require exclusive host scheduling without mocked clocks."
        ),
    ),
    TestTier.SOAK_FAILURE_INJECTION: IsolationContract(
        tier=TestTier.SOAK_FAILURE_INJECTION,
        parallel_safe=True,
        concurrency_group="external_process",
        max_parallel_workers=8,
        requires_isolated_temp=True,
        requires_isolated_git=True,
        requires_isolated_sqlite=True,
        requires_isolated_ports=True,
        requires_isolated_env=True,
        timeout_seconds=900.0,
        rationale=(
            "Long-running autonomous loops, synthetic failure matrices, crash recovery, "
            "and race-condition stress scenarios executed in dedicated projects."
        ),
    ),
}

# Explicit file classification: Every test file is mapped to exactly one tier.
# Filenames are stored with forward slashes, relative to repo root.
FILE_TIER_MAP: dict[str, TestTier] = {
    # -------------------------------------------------------------------------
    # 1. Deterministic Unit (55 files)
    # -------------------------------------------------------------------------
    "tests/test_attribution_guards.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_attribution_tool.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_check_attribution.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_ci_component_runner.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_ci_validation_plan.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_ci_workflow_policy.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_docs_links.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_git_identity_policy.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_init_profile.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_invariant_semantics.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_invariants.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_profile.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_quality_policy.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_project_smoke.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_readiness.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_runtime_info.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_stagemesh_validation_profile.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_validation_plan.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_window_journal.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_caventra_profile.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_contract_check.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_candidate_integrity.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_decisions.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_provenance.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_corpus.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_dependencies.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_base_state.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round10.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round11.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round12.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round13.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round14.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round15.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round16.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round17.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round18.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round19.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round20.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round21.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_autonomy_round24.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_security_evidence_boundaries.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_change_contracts.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_codex_optional_defaults.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_mcp_proxy.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_provider_auth.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_provider_authorization.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_provider_events.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_provider_output_security.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_provider_registry.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_spec_planning.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_spec_planning_gate.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_spec_governance_gate.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_spec_integrity_adapter.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_spec_audit.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_adverse_review_policy.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_review_evidence_report.py": TestTier.DETERMINISTIC_UNIT,
    "tests/test_adverse_review_gate.py": TestTier.DETERMINISTIC_UNIT,

    # -------------------------------------------------------------------------
    # 2. Component and Contract (45 files)
    # -------------------------------------------------------------------------
    "tests/test_activity.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_activity_cli.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_activity_evidence.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_activity_periods.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_admission_diagnosis_regression.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_agent_skills.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_api_server.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_autonomy_ci.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_codex_mcp_authorization.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_contract_feasibility.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_distinct_review_roles.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_e2e_evidence.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_efficiency.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_execution_detail_api.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_grok_provider_command.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_label_selector_admission.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_legacy_landing_history.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_objective_root_reconciliation.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_operator_api.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_operator_report.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_operator_ui_contract.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_outbound_lifecycle.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_provider_stage_commands.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_publication_merged_branch.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_queue_reconcile.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_queue_visibility.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_recovered_report.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_recovery_decision.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_recovery_ledger.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_remediation_diagnostics.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_research_context.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_research_evidence.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_research_scope_gate.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_review_disposition.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_selection_scope.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_stage_admission_fence.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_stage_dispatch_atomic.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_stage_dispatch_identity.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_step_progress.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_task_detail_api.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_task_selection.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_tool_failure_evidence.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_ui_assets.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_ui_launcher.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_validation_deadline_clock.py": TestTier.COMPONENT_CONTRACT,
    "tests/test_validation_recovery_ownership.py": TestTier.COMPONENT_CONTRACT,

    # -------------------------------------------------------------------------
    # 3. Integration and Process (86 files)
    # -------------------------------------------------------------------------
    "tests/test_auto_plan.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_delivery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_github.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_hardening.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_isolation.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_lifecycle.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_merge_policy.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_autonomy_review.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_batched_ci.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_bootstrap_stagemesh.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_builtin_execution_ownership.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_canary_regression.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_candidate_binding_hardening.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_candidate_environments.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_candidate_isolation_integration.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_capability_registry.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_codex_executable.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_completion_integrity.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_concurrent_provider_termination.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_contract_admission.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_contract_fail_closed.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_controlled_merge.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_delivery_followup.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_delivery_identity.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_diagnosis.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_e2e_loop_security.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_e2e_remediation.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_early_candidate_refresh.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_execution_timing.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_external_provider_cleanup.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_fast_forward.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_focused_validation.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_gate_executable_resolution.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_gate_execution.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_gate_temp_isolation.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_gate_timeout_diagnosis.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_git_init_encoding.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_github_task_sources.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_handoff.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_hands_off_provider_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_handsoff_sandbox.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_honest_delivery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_integration_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_landing_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_live_acceptance.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_control.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_control_api.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_control_ui.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_project_cli.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_projects.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_operator_workflow.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_parallel.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_pid_cleanup_proof.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_planning_execution.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_provider_acceptance.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_provider_authorization_e2e.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_provider_cleanup_confirmation.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_provider_cooldown.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_provider_pool.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_quality_gate.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_quota_live_retry.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_reattribution.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_research_cleanup_ownership.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_research_stage.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_research_tree_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_review_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_review_reliability.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_review_terminalization.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_review_workspace_retention.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_run_ready.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_run_window.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_run_window_progress.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_runtime_tool_evidence.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_single_task_stale_rebase.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_source_admission_publication.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_source_sync_preserves_progress.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_spec_integrity_acceptance.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_survivor_wave.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_task_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_tool_failure_recovery.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_ui_browser_behavior.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_ui_error_policy.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_validation_selection_reuse.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_workspace_ownership.py": TestTier.INTEGRATION_PROCESS,
    "tests/test_zero_config_github.py": TestTier.INTEGRATION_PROCESS,

    # -------------------------------------------------------------------------
    # 4. Serial Timing (1 file - explicit bounded serial)
    # -------------------------------------------------------------------------
    "tests/test_provider_outage_recovery.py": TestTier.SERIAL_TIMING,

    # -------------------------------------------------------------------------
    # 5. Native Platform (12 files - explicit bounded serial)
    # -------------------------------------------------------------------------
    "tests/test_active_gate_stop.py": TestTier.NATIVE_PLATFORM,
    "tests/test_bounded_execution.py": TestTier.NATIVE_PLATFORM,
    "tests/test_continuous_driver.py": TestTier.NATIVE_PLATFORM,
    "tests/test_gate_cleanup_confirmation.py": TestTier.NATIVE_PLATFORM,
    "tests/test_installed_runtime.py": TestTier.NATIVE_PLATFORM,
    "tests/test_operator_stop.py": TestTier.NATIVE_PLATFORM,
    "tests/test_regression_architecture_acceptance.py": TestTier.NATIVE_PLATFORM,
    "tests/test_research_crash_ownership.py": TestTier.NATIVE_PLATFORM,
    "tests/test_validation_crash_fence.py": TestTier.NATIVE_PLATFORM,
    "tests/test_windows_longpaths_and_block_reason.py": TestTier.NATIVE_PLATFORM,
    "tests/test_windows_user_installer.py": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_environment_repair.py": TestTier.NATIVE_PLATFORM,

    # -------------------------------------------------------------------------
    # 6. Soak and Failure Injection (13 files)
    # -------------------------------------------------------------------------
    "tests/test_stabilization_matrix.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_driver_acceptance.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_driver_cleanup_fence.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_driver_control_races.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_continuous_capacity.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_reliable_delivery_end_to_end.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_blocked_recovery.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_autonomous_recovery.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_autonomous_window.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_queue_run.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_recovery_loop_regressions.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_unknown_execution_recovery.py": TestTier.SOAK_FAILURE_INJECTION,
    "tests/test_deferred_tail_recovery.py": TestTier.SOAK_FAILURE_INJECTION,
}

# Canonical test counts per file to enable balanced deterministic bin-packing across lanes.
# Historical pre-redesign planning estimates only. Execution and acceptance never
# trust this table; canonical pytest collection is the sole current count source.
FILE_TEST_COUNTS: dict[str, int] = {
    "tests/test_active_gate_stop.py": 14,
    "tests/test_activity.py": 99,
    "tests/test_activity_cli.py": 29,
    "tests/test_activity_evidence.py": 10,
    "tests/test_activity_periods.py": 17,
    "tests/test_admission_diagnosis_regression.py": 6,
    "tests/test_adverse_review_gate.py": 15,
    "tests/test_adverse_review_policy.py": 10,
    "tests/test_agent_skills.py": 39,
    "tests/test_api_server.py": 133,
    "tests/test_attribution_guards.py": 13,
    "tests/test_attribution_tool.py": 4,
    "tests/test_auto_plan.py": 13,
    "tests/test_autonomous_recovery.py": 9,
    "tests/test_autonomous_window.py": 20,
    "tests/test_autonomy_base_state.py": 12,
    "tests/test_autonomy_ci.py": 31,
    "tests/test_autonomy_corpus.py": 10,
    "tests/test_autonomy_decisions.py": 15,
    "tests/test_autonomy_delivery.py": 18,
    "tests/test_autonomy_dependencies.py": 15,
    "tests/test_autonomy_github.py": 23,
    "tests/test_autonomy_hardening.py": 39,
    "tests/test_autonomy_isolation.py": 21,
    "tests/test_autonomy_lifecycle.py": 12,
    "tests/test_autonomy_merge_policy.py": 16,
    "tests/test_autonomy_provenance.py": 15,
    "tests/test_autonomy_recovery.py": 15,
    "tests/test_autonomy_review.py": 10,
    "tests/test_autonomy_round10.py": 9,
    "tests/test_autonomy_round11.py": 8,
    "tests/test_autonomy_round12.py": 9,
    "tests/test_autonomy_round13.py": 7,
    "tests/test_autonomy_round14.py": 5,
    "tests/test_autonomy_round15.py": 6,
    "tests/test_autonomy_round16.py": 6,
    "tests/test_autonomy_round17.py": 3,
    "tests/test_autonomy_round18.py": 3,
    "tests/test_autonomy_round19.py": 4,
    "tests/test_autonomy_round20.py": 2,
    "tests/test_autonomy_round21.py": 3,
    "tests/test_autonomy_round24.py": 3,
    "tests/test_batched_ci.py": 13,
    "tests/test_blocked_recovery.py": 15,
    "tests/test_bootstrap_stagemesh.py": 10,
    "tests/test_bounded_execution.py": 15,
    "tests/test_builtin_execution_ownership.py": 8,
    "tests/test_canary_regression.py": 48,
    "tests/test_candidate_binding_hardening.py": 17,
    "tests/test_candidate_environments.py": 20,
    "tests/test_candidate_integrity.py": 3,
    "tests/test_candidate_isolation_integration.py": 5,
    "tests/test_capability_registry.py": 97,
    "tests/test_caventra_profile.py": 46,
    "tests/test_change_contracts.py": 35,
    "tests/test_check_attribution.py": 11,
    "tests/test_ci_component_runner.py": 11,
    "tests/test_ci_validation_plan.py": 62,
    "tests/test_ci_workflow_policy.py": 6,
    "tests/test_codex_executable.py": 7,
    "tests/test_codex_mcp_authorization.py": 1,
    "tests/test_completion_integrity.py": 12,
    "tests/test_concurrent_provider_termination.py": 4,
    "tests/test_continuous_capacity.py": 16,
    "tests/test_continuous_driver.py": 57,
    "tests/test_contract_admission.py": 42,
    "tests/test_contract_check.py": 6,
    "tests/test_contract_fail_closed.py": 9,
    "tests/test_contract_feasibility.py": 17,
    "tests/test_controlled_merge.py": 31,
    "tests/test_deferred_tail_recovery.py": 13,
    "tests/test_delivery_followup.py": 40,
    "tests/test_delivery_identity.py": 12,
    "tests/test_diagnosis.py": 32,
    "tests/test_distinct_review_roles.py": 71,
    "tests/test_docs_links.py": 4,
    "tests/test_driver_acceptance.py": 4,
    "tests/test_driver_cleanup_fence.py": 57,
    "tests/test_driver_control_races.py": 5,
    "tests/test_e2e_evidence.py": 101,
    "tests/test_e2e_loop_security.py": 5,
    "tests/test_e2e_remediation.py": 32,
    "tests/test_early_candidate_refresh.py": 12,
    "tests/test_efficiency.py": 25,
    "tests/test_execution_detail_api.py": 13,
    "tests/test_execution_timing.py": 19,
    "tests/test_external_provider_cleanup.py": 12,
    "tests/test_fast_forward.py": 61,
    "tests/test_focused_validation.py": 31,
    "tests/test_gate_cleanup_confirmation.py": 24,
    "tests/test_gate_executable_resolution.py": 8,
    "tests/test_gate_execution.py": 13,
    "tests/test_gate_temp_isolation.py": 19,
    "tests/test_gate_timeout_diagnosis.py": 4,
    "tests/test_git_identity_policy.py": 41,
    "tests/test_git_init_encoding.py": 4,
    "tests/test_github_task_sources.py": 24,
    "tests/test_grok_provider_command.py": 10,
    "tests/test_handoff.py": 40,
    "tests/test_hands_off_provider_recovery.py": 2,
    "tests/test_handsoff_sandbox.py": 4,
    "tests/test_honest_delivery.py": 9,
    "tests/test_init_profile.py": 16,
    "tests/test_installed_runtime.py": 9,
    "tests/test_integration_recovery.py": 15,
    "tests/test_invariant_semantics.py": 18,
    "tests/test_invariants.py": 16,
    "tests/test_label_selector_admission.py": 2,
    "tests/test_landing_recovery.py": 32,
    "tests/test_legacy_landing_history.py": 7,
    "tests/test_live_acceptance.py": 4,
    "tests/test_mcp_proxy.py": 2,
    "tests/test_objective_root_reconciliation.py": 5,
    "tests/test_operator_api.py": 7,
    "tests/test_operator_control.py": 14,
    "tests/test_operator_control_api.py": 12,
    "tests/test_operator_control_ui.py": 1,
    "tests/test_operator_project_cli.py": 2,
    "tests/test_operator_projects.py": 6,
    "tests/test_operator_report.py": 23,
    "tests/test_operator_stop.py": 30,
    "tests/test_operator_ui_contract.py": 16,
    "tests/test_operator_workflow.py": 24,
    "tests/test_outbound_lifecycle.py": 7,
    "tests/test_parallel.py": 40,
    "tests/test_pid_cleanup_proof.py": 23,
    "tests/test_planning_execution.py": 10,
    "tests/test_profile.py": 41,
    "tests/test_quality_gate.py": 7,
    "tests/test_quality_policy.py": 11,
    "tests/test_project_smoke.py": 9,
    "tests/test_provider_acceptance.py": 22,
    "tests/test_provider_auth.py": 19,
    "tests/test_provider_authorization.py": 69,
    "tests/test_provider_authorization_e2e.py": 5,
    "tests/test_provider_cleanup_confirmation.py": 10,
    "tests/test_provider_cooldown.py": 16,
    "tests/test_provider_events.py": 28,
    "tests/test_provider_outage_recovery.py": 13,
    "tests/test_provider_output_security.py": 7,
    "tests/test_provider_pool.py": 56,
    "tests/test_provider_registry.py": 45,
    "tests/test_provider_stage_commands.py": 29,
    "tests/test_publication_merged_branch.py": 18,
    "tests/test_queue_reconcile.py": 4,
    "tests/test_queue_run.py": 55,
    "tests/test_queue_visibility.py": 15,
    "tests/test_quota_live_retry.py": 28,
    "tests/test_readiness.py": 21,
    "tests/test_reattribution.py": 3,
    "tests/test_recovered_report.py": 9,
    "tests/test_recovery_decision.py": 28,
    "tests/test_recovery_ledger.py": 4,
    "tests/test_recovery_loop_regressions.py": 11,
    "tests/test_regression_architecture_acceptance.py": 12,
    "tests/test_reliable_delivery_end_to_end.py": 1,
    "tests/test_remediation_diagnostics.py": 3,
    "tests/test_research_cleanup_ownership.py": 1,
    "tests/test_research_context.py": 1,
    "tests/test_research_crash_ownership.py": 2,
    "tests/test_research_evidence.py": 45,
    "tests/test_research_scope_gate.py": 4,
    "tests/test_research_stage.py": 22,
    "tests/test_research_tree_recovery.py": 28,
    "tests/test_review_disposition.py": 13,
    "tests/test_review_recovery.py": 5,
    "tests/test_review_reliability.py": 68,
    "tests/test_review_terminalization.py": 8,
    "tests/test_review_workspace_retention.py": 7,
    "tests/test_run_ready.py": 46,
    "tests/test_run_window.py": 93,
    "tests/test_run_window_progress.py": 5,
    "tests/test_runtime_info.py": 9,
    "tests/test_runtime_tool_evidence.py": 15,
    "tests/test_security_evidence_boundaries.py": 36,
    "tests/test_selection_scope.py": 31,
    "tests/test_single_task_stale_rebase.py": 3,
    "tests/test_source_admission_publication.py": 15,
    "tests/test_source_sync_preserves_progress.py": 4,
    "tests/test_spec_audit.py": 12,
    "tests/test_spec_governance_gate.py": 37,
    "tests/test_spec_integrity_acceptance.py": 8,
    "tests/test_spec_integrity_adapter.py": 15,
    "tests/test_spec_planning.py": 26,
    "tests/test_spec_planning_gate.py": 27,
    "tests/test_stabilization_matrix.py": 31,
    "tests/test_stage_admission_fence.py": 12,
    "tests/test_stage_dispatch_atomic.py": 21,
    "tests/test_stage_dispatch_identity.py": 3,
    "tests/test_stagemesh_validation_profile.py": 6,
    "tests/test_step_progress.py": 4,
    "tests/test_survivor_wave.py": 47,
    "tests/test_task_detail_api.py": 10,
    "tests/test_task_recovery.py": 66,
    "tests/test_task_selection.py": 32,
    "tests/test_tool_failure_evidence.py": 45,
    "tests/test_tool_failure_recovery.py": 46,
    "tests/test_ui_assets.py": 20,
    "tests/test_ui_browser_behavior.py": 14,
    "tests/test_ui_error_policy.py": 5,
    "tests/test_ui_launcher.py": 8,
    "tests/test_unknown_execution_recovery.py": 7,
    "tests/test_validation_crash_fence.py": 2,
    "tests/test_validation_deadline_clock.py": 4,
    "tests/test_validation_plan.py": 115,
    "tests/test_validation_recovery_ownership.py": 24,
    "tests/test_validation_selection_reuse.py": 8,
    "tests/test_window_journal.py": 1,
    "tests/test_windows_longpaths_and_block_reason.py": 3,
    "tests/test_windows_user_installer.py": 7,
    "tests/test_workspace_environment_repair.py": 22,
    "tests/test_workspace_ownership.py": 76,
    "tests/test_zero_config_github.py": 17,
}

# Optional explicit item-level overrides if specific tests within a file need distinct tiers.
# Historical Windows phase-duration weights used only to balance execution lanes.
# Source: full_regression_authoritative_clean3_report.json, SHA-256
# c4c197cb9f7b6f10ac076c310ea4460991499ea7b67ae9b465cf30f8d4b4227c,
# commit 23fef8b1bf6d61599680376ce4203cce808d69f9. That run failed one test,
# so these measurements are planning hints, never acceptance evidence. Missing
# or newly added files fall back to their live collected node count.
FILE_RUNTIME_SECONDS: dict[str, float] = {
'tests/test_active_gate_stop.py': 15.750,
    'tests/test_activity.py': 4.200,
    'tests/test_activity_cli.py': 1.560,
    'tests/test_activity_evidence.py': 0.310,
    'tests/test_activity_periods.py': 2.790,
    'tests/test_admission_diagnosis_regression.py': 3.270,
    'tests/test_adverse_review_gate.py': 7.500,
    'tests/test_adverse_review_policy.py': 0.001,
    'tests/test_agent_skills.py': 4.440,
    'tests/test_api_server.py': 19.830,
    'tests/test_attribution_guards.py': 179.560,
    'tests/test_attribution_tool.py': 54.490,
    'tests/test_auto_plan.py': 162.060,
    'tests/test_autonomous_recovery.py': 1053.370,
    'tests/test_autonomous_window.py': 80.890,
    'tests/test_autonomy_base_state.py': 130.760,
    'tests/test_autonomy_ci.py': 28.910,
    'tests/test_autonomy_corpus.py': 0.090,
    'tests/test_autonomy_decisions.py': 0.140,
    'tests/test_autonomy_delivery.py': 293.750,
    'tests/test_autonomy_dependencies.py': 58.500,
    'tests/test_autonomy_github.py': 50.850,
    'tests/test_autonomy_hardening.py': 172.530,
    'tests/test_autonomy_isolation.py': 86.620,
    'tests/test_autonomy_lifecycle.py': 257.230,
    'tests/test_autonomy_merge_policy.py': 49.200,
    'tests/test_autonomy_provenance.py': 161.790,
    'tests/test_autonomy_recovery.py': 69.690,
    'tests/test_autonomy_review.py': 194.240,
    'tests/test_autonomy_round10.py': 21.540,
    'tests/test_autonomy_round11.py': 103.840,
    'tests/test_autonomy_round12.py': 29.070,
    'tests/test_autonomy_round13.py': 0.001,
    'tests/test_autonomy_round14.py': 59.850,
    'tests/test_autonomy_round15.py': 81.140,
    'tests/test_autonomy_round16.py': 89.170,
    'tests/test_autonomy_round17.py': 0.001,
    'tests/test_autonomy_round18.py': 0.001,
    'tests/test_autonomy_round19.py': 0.010,
    'tests/test_autonomy_round20.py': 0.001,
    'tests/test_autonomy_round21.py': 92.200,
    'tests/test_autonomy_round24.py': 66.040,
    'tests/test_batched_ci.py': 36.980,
    'tests/test_blocked_recovery.py': 360.930,
    'tests/test_bootstrap_stagemesh.py': 0.340,
    'tests/test_bounded_execution.py': 88.820,
    'tests/test_builtin_execution_ownership.py': 57.160,
    'tests/test_canary_regression.py': 315.920,
    'tests/test_candidate_binding_hardening.py': 831.820,
    'tests/test_candidate_environments.py': 19.260,
    'tests/test_candidate_integrity.py': 91.190,
    'tests/test_candidate_isolation_integration.py': 302.460,
    'tests/test_capability_registry.py': 47.170,
    'tests/test_caventra_profile.py': 3.830,
    'tests/test_change_contracts.py': 246.800,
    'tests/test_check_attribution.py': 51.010,
    'tests/test_ci_component_runner.py': 0.110,
    'tests/test_ci_validation_plan.py': 23.170,
    'tests/test_ci_workflow_policy.py': 0.010,
    'tests/test_codex_executable.py': 0.080,
    'tests/test_codex_mcp_authorization.py': 0.070,
    'tests/test_completion_integrity.py': 367.780,
    'tests/test_concurrent_provider_termination.py': 0.800,
    'tests/test_continuous_capacity.py': 48.640,
    'tests/test_continuous_driver.py': 31.856,
    'tests/test_contract_admission.py': 52.030,
    'tests/test_contract_check.py': 1.600,
    'tests/test_contract_fail_closed.py': 69.500,
    'tests/test_contract_feasibility.py': 7.000,
    'tests/test_controlled_merge.py': 85.502,
    'tests/test_deferred_tail_recovery.py': 545.850,
    'tests/test_delivery_followup.py': 410.830,
    'tests/test_delivery_identity.py': 0.400,
    'tests/test_diagnosis.py': 1515.540,
    'tests/test_distinct_review_roles.py': 473.130,
    'tests/test_docs_links.py': 0.470,
    'tests/test_driver_acceptance.py': 0.530,
    'tests/test_driver_cleanup_fence.py': 1.120,
    'tests/test_driver_control_races.py': 0.280,
    'tests/test_e2e_evidence.py': 13.430,
    'tests/test_e2e_loop_security.py': 0.040,
    'tests/test_e2e_remediation.py': 508.260,
    'tests/test_early_candidate_refresh.py': 374.860,
    'tests/test_efficiency.py': 271.560,
    'tests/test_execution_detail_api.py': 0.650,
    'tests/test_execution_timing.py': 33.970,
    'tests/test_external_provider_cleanup.py': 11.690,
    'tests/test_fast_forward.py': 181.668,
    'tests/test_focused_validation.py': 71.300,
    'tests/test_gate_cleanup_confirmation.py': 9.400,
    'tests/test_gate_executable_resolution.py': 1.240,
    'tests/test_gate_execution.py': 5.630,
    'tests/test_gate_temp_isolation.py': 29.380,
    'tests/test_gate_timeout_diagnosis.py': 77.580,
    'tests/test_git_identity_policy.py': 73.300,
    'tests/test_git_init_encoding.py': 1.470,
    'tests/test_github_task_sources.py': 2.500,
    'tests/test_grok_provider_command.py': 0.001,
    'tests/test_handoff.py': 176.890,
    'tests/test_hands_off_provider_recovery.py': 11.170,
    'tests/test_handsoff_sandbox.py': 33.870,
    'tests/test_honest_delivery.py': 9.170,
    'tests/test_init_profile.py': 29.970,
    'tests/test_installed_runtime.py': 61.760,
    'tests/test_integration_recovery.py': 699.960,
    'tests/test_invariant_semantics.py': 102.520,
    'tests/test_invariants.py': 92.770,
    'tests/test_label_selector_admission.py': 5.960,
    'tests/test_landing_recovery.py': 1769.260,
    'tests/test_legacy_landing_history.py': 45.060,
    'tests/test_live_acceptance.py': 1.180,
    'tests/test_mcp_proxy.py': 4.520,
    'tests/test_objective_root_reconciliation.py': 5.300,
    'tests/test_operator_api.py': 0.290,
    'tests/test_operator_control.py': 0.570,
    'tests/test_operator_control_api.py': 0.970,
    'tests/test_operator_control_ui.py': 3.330,
    'tests/test_operator_project_cli.py': 0.190,
    'tests/test_operator_projects.py': 0.680,
    'tests/test_operator_report.py': 1.030,
    'tests/test_operator_stop.py': 46.190,
    'tests/test_operator_ui_contract.py': 0.001,
    'tests/test_operator_workflow.py': 52.420,
    'tests/test_outbound_lifecycle.py': 208.290,
    'tests/test_parallel.py': 1306.030,
    'tests/test_pid_cleanup_proof.py': 26.520,
    'tests/test_planning_execution.py': 198.280,
    'tests/test_profile.py': 122.770,
    'tests/test_quality_gate.py': 2.000,
    'tests/test_quality_policy.py': 0.100,
    'tests/test_project_smoke.py': 10.520,
    'tests/test_provider_acceptance.py': 125.380,
    'tests/test_codex_optional_defaults.py': 0.400,
    'tests/test_provider_auth.py': 4.250,
    'tests/test_provider_authorization.py': 13.090,
    'tests/test_provider_authorization_e2e.py': 134.770,
    'tests/test_provider_cleanup_confirmation.py': 12.090,
    'tests/test_provider_cooldown.py': 59.953,
    'tests/test_provider_events.py': 3.390,
    'tests/test_provider_outage_recovery.py': 81.850,
    'tests/test_provider_output_security.py': 0.130,
    'tests/test_provider_pool.py': 887.230,
    'tests/test_provider_registry.py': 167.190,
    'tests/test_provider_stage_commands.py': 105.950,
    'tests/test_publication_merged_branch.py': 126.230,
    'tests/test_queue_reconcile.py': 5.970,
    'tests/test_queue_run.py': 2356.900,
    'tests/test_queue_visibility.py': 56.740,
    'tests/test_quota_live_retry.py': 86.470,
    'tests/test_readiness.py': 124.970,
    'tests/test_reattribution.py': 114.620,
    'tests/test_recovered_report.py': 0.001,
    'tests/test_recovery_decision.py': 0.001,
    'tests/test_recovery_ledger.py': 0.120,
    'tests/test_recovery_loop_regressions.py': 275.270,
    'tests/test_regression_architecture_acceptance.py': 51.360,
    'tests/test_reliable_delivery_end_to_end.py': 60.300,
    'tests/test_remediation_diagnostics.py': 8.810,
    'tests/test_research_cleanup_ownership.py': 7.910,
    'tests/test_research_context.py': 0.030,
    'tests/test_research_crash_ownership.py': 4.600,
    'tests/test_research_evidence.py': 0.380,
    'tests/test_research_scope_gate.py': 9.760,
    'tests/test_research_stage.py': 155.420,
    'tests/test_research_tree_recovery.py': 9.540,
    'tests/test_review_evidence_report.py': 0.400,
    'tests/test_review_disposition.py': 395.380,
    'tests/test_review_recovery.py': 133.340,
    'tests/test_review_reliability.py': 785.160,
    'tests/test_review_terminalization.py': 27.770,
    'tests/test_review_workspace_retention.py': 52.050,
    'tests/test_run_ready.py': 690.050,
    'tests/test_run_window.py': 481.430,
    'tests/test_run_window_progress.py': 20.120,
    'tests/test_runtime_info.py': 8.630,
    'tests/test_runtime_tool_evidence.py': 171.370,
    'tests/test_security_evidence_boundaries.py': 122.550,
    'tests/test_selection_scope.py': 36.240,
    'tests/test_single_task_stale_rebase.py': 182.110,
    'tests/test_source_admission_publication.py': 1.630,
    'tests/test_source_sync_preserves_progress.py': 54.150,
    'tests/test_spec_audit.py': 26.700,
    'tests/test_spec_governance_gate.py': 106.260,
    'tests/test_spec_integrity_acceptance.py': 0.030,
    'tests/test_spec_integrity_adapter.py': 0.320,
    'tests/test_spec_planning.py': 0.001,
    'tests/test_spec_planning_gate.py': 113.370,
    'tests/test_stabilization_matrix.py': 2210.610,
    'tests/test_stage_admission_fence.py': 210.920,
    'tests/test_stage_dispatch_atomic.py': 238.490,
    'tests/test_stage_dispatch_identity.py': 18.810,
    'tests/test_stagemesh_validation_profile.py': 0.060,
    'tests/test_step_progress.py': 7.970,
    'tests/test_survivor_wave.py': 144.550,
    'tests/test_task_detail_api.py': 0.480,
    'tests/test_task_recovery.py': 947.090,
    'tests/test_task_selection.py': 475.890,
    'tests/test_tool_failure_evidence.py': 0.010,
    'tests/test_tool_failure_recovery.py': 103.940,
    'tests/test_ui_assets.py': 11.150,
    'tests/test_ui_browser_behavior.py': 50.700,
    'tests/test_ui_error_policy.py': 15.170,
    'tests/test_ui_launcher.py': 0.060,
    'tests/test_unknown_execution_recovery.py': 20.480,
    'tests/test_validation_crash_fence.py': 10.970,
    'tests/test_validation_deadline_clock.py': 0.060,
    'tests/test_validation_plan.py': 37.360,
    'tests/test_validation_recovery_ownership.py': 50.450,
    'tests/test_validation_selection_reuse.py': 75.150,
    'tests/test_window_journal.py': 0.050,
    'tests/test_windows_longpaths_and_block_reason.py': 3.600,
    'tests/test_windows_user_installer.py': 107.150,
    'tests/test_workspace_environment_repair.py': 30.110,
    'tests/test_workspace_ownership.py': 1230.320,
    'tests/test_zero_config_github.py': 46.750,
}


TEST_NODEID_OVERRIDES: dict[str, TestTier] = {
    # These contracts create, kill, and then classify a real owner process. High
    # Windows process churn can reuse the PID before identity observation, so
    # they belong with the explicit serial native process-liveness evidence.
    "tests/test_workspace_ownership.py::test_dead_owner_with_a_workspace_exactly_as_sealed_is_recovered_and_audited": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_dead_owner_then_outside_commit_is_not_recovered": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_dead_owner_then_tracked_edit_is_not_recovered": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_dead_owner_then_untracked_leftover_is_not_recovered": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_task_blocks_when_a_dead_owners_workspace_changed": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_two_simultaneous_takeovers_of_a_dead_owner_have_exactly_one_winner": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_release_permanent_sharing_contention_fails_closed_with_typed_error": TestTier.NATIVE_PLATFORM,
    "tests/test_workspace_ownership.py::test_safe_cleanup_created_owner_raises_when_unlink_permanently_fails": TestTier.NATIVE_PLATFORM,
}


def normalize_test_path(path: str) -> str:
    """Normalize test file paths to use forward slashes while preserving test symbols."""
    if "::" in path:
        file_part, symbol_part = path.split("::", 1)
        normalized_file = str(PurePosixPath(file_part.replace("\\", "/")))
        return f"{normalized_file}::{symbol_part}"
    return str(PurePosixPath(path.replace("\\", "/")))


def tier_for_test(test_id: str) -> TestTier:
    """Look up the exact tier for a test nodeid or file path.

    Fails closed with ValueError if unclassified or unknown.
    """
    clean_id = normalize_test_path(test_id)
    if clean_id in TEST_NODEID_OVERRIDES:
        return TEST_NODEID_OVERRIDES[clean_id]

    file_part = clean_id.split("::", 1)[0]
    if file_part in FILE_TIER_MAP:
        return FILE_TIER_MAP[file_part]

    raise ValueError(f"Unclassified test in taxonomy: '{test_id}'. Every test must belong to exactly one tier.")


def isolation_contract_for_tier(tier: TestTier) -> IsolationContract:
    """Return the isolation contract for the given tier."""
    if tier not in TIER_ISOLATION_CONTRACTS:
        raise ValueError(f"Unknown tier '{tier}'. Cannot resolve isolation contract.")
    return TIER_ISOLATION_CONTRACTS[tier]


def validate_catalog(
    collected_nodeids: Iterable[str],
    *,
    allow_uncollected_catalog_entries: bool = False,
) -> dict[str, TestTier]:
    """Validate that every collected test belongs to exactly one valid tier.

    Fails closed (raising ValueError) on:
    - Missing classification for any collected test.
    - Conflicting / duplicate tier mapping.
    - Unknown tier names.
    - Malformed catalog entries.
    - Orphan entries in catalog that do not match any collected test (unless allow_uncollected_catalog_entries=True).
    """
    results: dict[str, TestTier] = {}
    matched_files: set[str] = set()
    matched_overrides: set[str] = set()

    # First validate the catalog definition itself
    for path, tier in FILE_TIER_MAP.items():
        if not isinstance(path, str) or not path.startswith("tests/"):
            raise ValueError(f"Malformed catalog file path: {path}")
        if not isinstance(tier, TestTier):
            raise TypeError(f"Invalid tier {tier} for catalog path {path}")

    for nodeid, tier in TEST_NODEID_OVERRIDES.items():
        if not isinstance(nodeid, str) or "::" not in nodeid:
            raise ValueError(f"Malformed override nodeid: {nodeid}")
        if not isinstance(tier, TestTier):
            raise TypeError(f"Invalid tier {tier} for override {nodeid}")

    unknown_runtime_files = set(FILE_RUNTIME_SECONDS) - set(FILE_TIER_MAP)
    if unknown_runtime_files:
        raise ValueError(
            "Runtime planning weights contain files outside the test catalog: "
            f"{', '.join(sorted(unknown_runtime_files)[:5])}"
        )
    invalid_runtime_weights = {
        path: seconds
        for path, seconds in FILE_RUNTIME_SECONDS.items()
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0
    }
    if invalid_runtime_weights:
        raise ValueError(
            "Runtime planning weights must be positive numbers: "
            f"{sorted(invalid_runtime_weights.items())[:5]}"
        )

    # Validate each collected test
    missing: list[str] = []
    for raw_id in collected_nodeids:
        nodeid = normalize_test_path(raw_id)
        if "::" not in nodeid or not nodeid.startswith("tests/"):
            raise ValueError(f"Malformed collected test nodeid: {raw_id}")
        if nodeid in results:
            raise ValueError(f"Duplicate collected test nodeid: {nodeid}")
        file_part = nodeid.split("::", 1)[0]

        try:
            tier = tier_for_test(nodeid)
        except ValueError:
            missing.append(nodeid)
            continue

        results[nodeid] = tier
        matched_files.add(file_part)
        if nodeid in TEST_NODEID_OVERRIDES:
            matched_overrides.add(nodeid)

    if missing:
        sample = missing[:5]
        extra = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        raise ValueError(
            f"Catalog validation failed: {len(missing)} collected tests lack tier classification: "
            f"{', '.join(sample)}{extra}"
        )

    if not allow_uncollected_catalog_entries:
        orphan_files = set(FILE_TIER_MAP.keys()) - matched_files
        if orphan_files:
            raise ValueError(
                f"Catalog contains {len(orphan_files)} test files not collected by pytest: "
                f"{', '.join(sorted(orphan_files)[:5])}"
            )
        orphan_overrides = set(TEST_NODEID_OVERRIDES.keys()) - matched_overrides
        if orphan_overrides:
            raise ValueError(
                f"Catalog contains {len(orphan_overrides)} test overrides not collected: "
                f"{', '.join(sorted(orphan_overrides)[:5])}"
            )

    return results


def catalog_digest() -> str:
    """Compute a deterministic SHA-256 digest of the test catalog."""
    hasher = hashlib.sha256()
    payload = {
        "files": {k: str(v) for k, v in sorted(FILE_TIER_MAP.items())},
        "overrides": {k: str(v) for k, v in sorted(TEST_NODEID_OVERRIDES.items())},
        "runtime_seconds": dict(sorted(FILE_RUNTIME_SECONDS.items())),
    }
    hasher.update(json.dumps(payload, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()


def policy_fingerprint() -> str:
    """Compute a deterministic SHA-256 fingerprint of the isolation rules."""
    hasher = hashlib.sha256()
    contracts_payload = {
        str(tier): contract.to_dict()
        for tier, contract in sorted(TIER_ISOLATION_CONTRACTS.items(), key=lambda x: str(x[0]))
    }
    hasher.update(json.dumps(contracts_payload, sort_keys=True).encode("utf-8"))
    return hasher.hexdigest()
