"""Guards that docs/operator-ui-contract.md stays in step with the activity projection it specifies."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stagemesh import activity
from stagemesh.activity import ActivityEntry, ActivityQuery, ActivityQueryError, validate_query
from stagemesh.cli import build_parser, main
from stagemesh.domain import Stage
from stagemesh.remote_hosting import RemoteHostingPlanError, validate_remote_hosting_plan

DOC = (Path(__file__).resolve().parents[1] / "docs" / "operator-ui-contract.md").read_text(encoding="utf-8")


def _remote_plan() -> dict[str, object]:
    return {
        "schema": "stagemesh.remote-hosting-plan/1",
        "deployment": {
            "target": "single_tenant_reverse_proxy",
            "tls_owner": "reverse_proxy",
            "public_origin": "https://stagemesh.example.test",
        },
        "identity": {
            "strategy": "oidc_authorization_code_pkce",
            "issuer": "https://identity.example.test",
            "client_id": "stagemesh-operator",
            "session_ttl_seconds": 3600,
            "browser_storage": "http_only_secure_same_site_cookie",
        },
        "authorization": {
            "default_role": "read_only",
            "read_permission": "project.read",
            "control_permission": "project.control",
            "project_scope_required": True,
        },
        "secrets": {"store": "operator-selected-secret-store", "rotation_days": 30},
        "audit": {
            "record_reads": True,
            "record_controls": True,
            "record_failures": True,
            "retention_days": 90,
        },
        "limits": {
            "requests_per_minute": 120,
            "max_payload_bytes": 1_000_000,
            "log_body_policy": "never",
        },
        "recovery": {
            "backup_target": "operator-selected-encrypted-backup",
            "restore_test_days": 30,
            "rollback_strategy": "versioned_atomic_rollback",
        },
    }


def test_remote_hosting_plan_is_stable_and_contains_no_secret_material() -> None:
    first = validate_remote_hosting_plan(_remote_plan())
    second = validate_remote_hosting_plan(json.loads(json.dumps(_remote_plan())))
    assert first.digest == second.digest
    assert first.to_dict()["status"] == "PLAN_VALIDATED"


def test_remote_hosting_plan_fails_closed_on_unsafe_or_secret_bearing_input() -> None:
    unsafe = _remote_plan()
    assert isinstance(unsafe["deployment"], dict)
    unsafe["deployment"]["public_origin"] = "http://public.example.test"
    with pytest.raises(RemoteHostingPlanError, match="HTTPS"):
        validate_remote_hosting_plan(unsafe)

    secret = _remote_plan()
    assert isinstance(secret["identity"], dict)
    secret["identity"]["client_secret"] = "must-not-be-here"
    with pytest.raises(RemoteHostingPlanError, match="never contain secrets"):
        validate_remote_hosting_plan(secret)


def test_remote_plan_command_does_not_add_a_remote_bind_option() -> None:
    parser = build_parser()
    serve = parser.parse_args(["serve", "--port", "0"])
    assert not hasattr(serve, "host")
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--host", "0.0.0.0"])


def test_remote_plan_cli_validates_without_starting_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = tmp_path / "remote-plan.json"
    plan_path.write_text(json.dumps(_remote_plan()), encoding="utf-8")
    assert main(["remote-hosting-plan", "--file", str(plan_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PLAN_VALIDATED"
    assert payload["listener_changed"] is False
    assert payload["remote_server_started"] is False


def test_documents_every_projection_field_and_bound():
    for field in dataclasses.fields(ActivityEntry):
        assert f"`{field.name}`" in DOC, field.name
    for field in dataclasses.fields(ActivityQuery):
        assert f"`{field.name}`" in DOC, field.name
    for page_field in ("total", "limit", "offset", "next_offset", "truncated"):
        assert f'"{page_field}"' in DOC
    assert f"1 to {activity.MAX_LIMIT}" in DOC
    assert f"default {activity.DEFAULT_LIMIT}" in DOC
    assert f"0 to {activity.MAX_SCAN - 1}" in DOC
    assert str(activity.MAX_SCAN) in DOC


def test_documents_the_complete_outcome_allowlist_and_stages():
    for code in activity._SAFE_RESULTS:
        assert f"`{code}`" in DOC, code
    assert "`Stage` value" in DOC
    for stage in Stage:
        assert f"`{stage.value}`" in DOC, stage.value
    for kind in ("task", "execution", "audit"):
        assert f"`{kind}`" in DOC


def test_read_only_and_fail_closed_posture_is_stated():
    for phrase in ("`GET` only", "fail closed", "loopback", "Bearer", "DNS", "`invalid_query`", "`store_unavailable`"):
        assert phrase in DOC, phrase
    assert "## Architecture decisions" in DOC
    assert "Remote hosting: OPEN" in DOC.replace("**", "")
    for stale in ("Design only", "Nothing here is implemented", "Decisions needed before"):
        assert stale not in DOC, stale
    assert "`GET /api/v1/health`" in DOC
    assert "## Known gaps against #134" in DOC


def test_documents_bounded_unknown_execution_release_control() -> None:
    for phrase in (
        "`release_unknown_execution`",
        "exact task id",
        "exact execution id",
        "classified `UNKNOWN`",
        "never kills a process",
        "cross-task target is refused",
        "PID, executable identity",
    ):
        assert phrase in DOC, phrase


@pytest.mark.parametrize(
    "query",
    [
        ActivityQuery(state="pending"),
        ActivityQuery(stage="NOPE"),
        ActivityQuery(task_id=""),
        ActivityQuery(provider="x" * 201),
        ActivityQuery(since=datetime(2026, 1, 1)),  # noqa: DTZ001 - naive on purpose
        ActivityQuery(since=datetime(2026, 1, 2, tzinfo=UTC), until=datetime(2026, 1, 1, tzinfo=UTC)),
        ActivityQuery(limit=0),
        ActivityQuery(limit=activity.MAX_LIMIT + 1),
        ActivityQuery(offset=-1),
        ActivityQuery(offset=activity.MAX_SCAN),
    ],
)
def test_documented_invalid_queries_fail_closed(query):
    with pytest.raises(ActivityQueryError):
        validate_query(query)


def test_documented_valid_boundaries_pass():
    validate_query(ActivityQuery(limit=activity.MAX_LIMIT, offset=activity.MAX_SCAN - 1))
    validate_query(ActivityQuery(state="open", stage="IMPLEMENT", since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_documents_strict_timestamp_and_query_decoding_rules():
    for phrase in ("RFC 3339", "20260101T000000Z", "%2B", "%FF", "U+FFFD", "caf%C3%A9", "100%25", "never echo values"):
        assert phrase in DOC, phrase


def test_documents_bounded_objective_projection_and_privacy_boundary():
    for phrase in (
        "`GET /api/v1/objectives`", "`stagemesh.objectives/1`", "`UNPLANNED`",
        "`PARTIAL`", "`IN_PROGRESS`", "`BLOCKED`", "`COMPLETE`", "`UNAVAILABLE`",
        "raw objective payload", "source issue body", "100 newest objective records",
        "No objective mutation",
    ):
        assert phrase in DOC, phrase


def test_documents_local_only_doctor_and_provider_observation_boundaries():
    for phrase in (
        "`GET /api/v1/doctor`", "`stagemesh.project-doctor/1`", "`CONFIG_UNAVAILABLE`",
        "never migrates the store", "invokes a provider", "contacts GitHub",
        "configuration and authorization do not claim command availability",
        "config or workspace paths", "`network_checked=false`", "`mutation_performed=false`",
        "100 provider rows", "`provider_total`", "`providers_truncated`",
    ):
        assert phrase in DOC, phrase


def test_documents_bounded_provider_status_and_evidence_distinctions():
    for phrase in (
        "`GET /api/v1/providers`", "`stagemesh.provider-status/1`",
        "`provider_total`", "`RECORDED_ONLY`", "`configured` means",
        "`authorized_for_unattended` means", "`executable_discovery=DISCOVERED`",
        "durable prior evidence", "`invocation.recorded`",
        "`provider_calls=0`", "Billing usage remains `UNKNOWN`",
        "`NOT_PROBED_PATH`", "`PROVIDER_CONFIGURATION_UNAVAILABLE`",
        "never starts a provider", "`acceptance_scan_limit`",
        "`acceptance_history_truncated`", "never silently relabelled `MISSING`",
        "`withheld_provider_count`", "secret-shaped",
    ):
        assert phrase in DOC, phrase


def test_documents_bounded_human_action_queue_and_non_inference_boundary():
    for phrase in (
        "`GET /api/v1/attention`", "`stagemesh.attention-queue/1`",
        "`BLOCKED_HUMAN_DECISION`", "`approval_coverage=UNKNOWN`",
        "`queue_total_kind=LOWER_BOUND`", "at most 500", "at most 100 rows",
        "Recoverable operational blocks", "missing records never establish permission",
        "explicitly enables", "never requests notification permission on load",
        "generic count", "deduplicates subsequent rows", "in memory",
    ):
        assert phrase in DOC, phrase


def test_documents_cross_platform_desktop_palette_and_launcher_privacy():
    for phrase in (
        "Windows, macOS and Linux",
        "without a shell",
        "never an executable or filesystem path",
        "command palette",
        "does not evaluate text",
        "Browser-reserved shortcuts are not required",
    ):
        assert phrase in DOC, phrase


def test_documents_safe_bounded_execution_diagnostics_and_log_boundary():
    for phrase in (
        "`GET /api/v1/diagnostics`", "`stagemesh.execution-diagnostics/1`",
        "newest 100 durable execution rows", "500-row `scan_limit`",
        "`UNAVAILABLE_WITHOUT_CANDIDATE`", "`raw_logs=WITHHELD`",
        "`raw_result_projected=false`", "This is deliberately not a log endpoint",
        "polls only while visible", "PID, executable, process",
    ):
        assert phrase in DOC, phrase


def test_ci_runs_the_activity_contract_and_api_modules_on_both_platforms():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    windows, _, linux = workflow.partition("\n  linux:")
    for module in (
        "test_operator_ui_contract.py", "test_operator_api.py", "test_task_detail_api.py",
        "test_execution_detail_api.py", "test_operator_doctor_api.py",
        "test_operator_providers_api.py",
        "test_operator_attention_api.py",
        "test_operator_diagnostics_api.py",
        "test_activity.py", "test_api_server.py", "test_ui_launcher.py",
        "test_ui_assets.py", "test_ui_browser_behavior.py",
    ):
        assert module in windows, f"windows job omits {module}"
        assert module in linux, f"linux job omits {module}"
