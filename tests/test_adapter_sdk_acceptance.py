from __future__ import annotations

from build_coordinator.agents.profiles import PROFILES
from build_coordinator.execution.results import RESULT_STATUS_VALUES, result_file_contract_for_role
from build_coordinator.execution.sdk import (
    ACCEPTANCE_MATRIX,
    ADAPTER_ERROR_TAXONOMY,
    ADAPTER_RESULT_SEMANTICS,
    AUTH_ADAPTER_ERRORS,
    EXHAUSTION_ADAPTER_ERRORS,
    RETRYABLE_ADAPTER_ERRORS,
    RUNTIME_ACCEPTANCE_MATRIX,
    SUPPORTED_ADAPTER_PROTOCOL_VERSION,
    acceptance_matrix_by_stage,
    runtime_matrix_by_runtime,
)


def test_adapter_sdk_defines_required_stage_acceptance_matrix():
    matrix = acceptance_matrix_by_stage()

    assert set(matrix) == {"planning", "coding", "review"}
    assert matrix["planning"].role == "PLANNER"
    assert matrix["coding"].role == "BUILDER"
    assert matrix["review"].role == "REVIEWER"

    for entry in ACCEPTANCE_MATRIX:
        assert entry.structured_result
        assert "result" in entry.structured_result.lower() or "envelope" in entry.structured_result.lower()
        assert entry.cancellation_resume
        assert "auth" in entry.auth_exhaustion.lower()
        assert "exhaust" in entry.auth_exhaustion.lower() or "quota" in entry.auth_exhaustion.lower()
        assert "headless" in entry.headless_requirement.lower()


def test_adapter_sdk_result_and_error_semantics_match_runner_contracts():
    assert SUPPORTED_ADAPTER_PROTOCOL_VERSION == 1
    assert ADAPTER_RESULT_SEMANTICS["result_channel"] == "BUILD_COORDINATOR_RESULT_PATH"
    assert ADAPTER_RESULT_SEMANTICS["status_values"] == RESULT_STATUS_VALUES
    assert ADAPTER_RESULT_SEMANTICS["stdout_semantics"] == "diagnostic_only"
    assert "schema_version" in ADAPTER_RESULT_SEMANTICS["identity_fields"]

    assert RETRYABLE_ADAPTER_ERRORS == {"RATE_LIMITED", "UNAVAILABLE", "NETWORK_FAILURE"}
    assert AUTH_ADAPTER_ERRORS == {"AUTH_FAILURE"}
    assert {"QUOTA_EXHAUSTED", "RATE_LIMITED"} <= EXHAUSTION_ADAPTER_ERRORS
    assert {"AUTH_FAILURE", "QUOTA_EXHAUSTED", "RATE_LIMITED", "EXECUTION_FAILURE"} <= set(ADAPTER_ERROR_TAXONOMY)


def test_role_result_contracts_cover_planning_coding_and_review():
    planner = result_file_contract_for_role("PLANNER")
    builder = result_file_contract_for_role("BUILDER")
    reviewer = result_file_contract_for_role("REVIEWER")

    assert planner["schema_version"] == 1
    assert "plan" in planner["required_top_level_fields"]
    assert builder["write_json_to_env"] == "BUILD_COORDINATOR_RESULT_PATH"
    assert reviewer["verdict"]["ready_for_integration"] == "boolean"
    assert "GREEN" in reviewer["verdict"]["consistency"]


def test_runtime_acceptance_matrix_keeps_gui_only_runtime_unsupported():
    matrix = runtime_matrix_by_runtime()

    assert set(matrix) == set(PROFILES)
    for runtime, entry in matrix.items():
        assert entry.provider == PROFILES[runtime].provider
        if PROFILES[runtime].headless:
            assert entry.support == "SUPPORTED_WHEN_READY"
            assert "headless probe" in entry.headless_gate
        else:
            assert entry.support == "UNSUPPORTED_GUI_ONLY"
            assert "until a reliable headless automation path is proven" in entry.headless_gate

    assert matrix["antigravity"].support == "UNSUPPORTED_GUI_ONLY"
