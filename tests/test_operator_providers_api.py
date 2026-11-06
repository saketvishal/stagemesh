from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi
from stagemesh.domain import ExecutionKind, ExecutionStatus
from stagemesh.operator_providers import PROVIDERS_CONTRACT, ProviderStatusProjection
from stagemesh.persistence import Store

TOKEN = "provider-status-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STAGEMESH_CODEX_CMD",
        "STAGEMESH_CLAUDE_CMD",
        "STAGEMESH_GROK_CMD",
        "STAGEMESH_AGY_CMD",
        "STAGEMESH_GITHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    db = runtime / "stagemesh.sqlite3"
    store = Store(db)
    store.migrate()
    store.close()
    return tmp_path, db


def request(
    project: Path,
    db: Path,
    target: str = "/api/v1/providers",
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    enabled: bool = True,
):
    api = ActivityApi(
        db,
        TOKEN,
        provider_status=ProviderStatusProjection(project) if enabled else None,
    )
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS)
    return status, json.loads(payload), response_headers


def test_provider_status_separates_configuration_discovery_acceptance_cooldown_and_invocation(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = project
    secret = "PRIVATE-PROVIDER-STATUS-CANARY"
    private_command = root / "private" / "provider.exe"
    (root / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "smoke": {
                        "command": f'"{private_command}" --token {secret}',
                        "capabilities": ["IMPLEMENT", "REVIEW"],
                    },
                    "secret-provider": "must-not-be-projected",
                },
                "authorization": {"unattended_providers": ["smoke"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("stagemesh.operator_providers.shutil.which", lambda _name: None)
    store = Store(db)
    task = store.upsert_task("Provider status task", source_id="provider-status-task")
    succeeded = store.start_execution(
        task_id=task, claim_id=None, kind=ExecutionKind.IMPLEMENTATION, actor="smoke"
    )
    store.finish_execution(succeeded, ExecutionStatus.SUCCEEDED, actor="smoke")
    store.start_execution(
        task_id=task, claim_id=None, kind=ExecutionKind.REVIEW, actor="smoke"
    )
    failed = store.start_execution(
        task_id=task, claim_id=None, kind=ExecutionKind.REVIEW, actor="legacy"
    )
    store.finish_execution(failed, ExecutionStatus.FAILED, actor="legacy")
    now = time.time()
    store.add_audit_event(
        "provider.live_acceptance",
        {
            "status": "PASS",
            "provider": "smoke",
            "implementation_checked": True,
            "review_checked": True,
            "implementation_ready": True,
            "review_ready": True,
            "checked_at": now,
        },
    )
    store.add_audit_event(
        "provider.failure",
        {
            "provider": "smoke",
            "stage": "REVIEW",
            "reason": "quota_rate_limit",
            "retry_at": now + 3600,
            "provider_output": secret,
        },
    )
    store.close()
    before = db.read_bytes()

    status, body, headers = request(root, db)
    encoded = json.dumps(body)

    assert status == 200 and body["contract"] == PROVIDERS_CONTRACT
    assert body["status"] == "AVAILABLE"
    assert body["provider_calls"] == body["network_calls"] == body["database_writes"] == 0
    assert body["billing_usage"] == "UNKNOWN"
    assert secret not in encoded and "provider.exe" not in encoded and str(root) not in encoded
    assert "secret-provider" not in encoded
    assert body["withheld_provider_count"] == 1
    assert body["provider_total_kind"] == "EXACT"
    smoke = next(row for row in body["providers"] if row["name"] == "smoke")
    assert smoke["configured"] is True
    assert smoke["authorized_for_unattended"] is True
    assert smoke["executable_discovery"] == "NOT_PROBED_PATH"
    assert smoke["live_acceptance"]["IMPLEMENT"]["state"] == "PASSING"
    assert smoke["live_acceptance"]["REVIEW"]["state"] == "PASSING"
    assert smoke["cooldown"]["active"] is True
    assert smoke["cooldown"]["failure_class"] == "quota_rate_limit"
    assert smoke["invocation"]["recorded"] is True
    assert smoke["invocation"]["total"] == 2
    assert smoke["invocation"]["running"] == 1
    assert smoke["invocation"]["succeeded"] == 1
    legacy = next(row for row in body["providers"] if row["name"] == "legacy")
    assert legacy["configured"] is False
    assert legacy["command_source"] == "RECORDED_ONLY"
    assert legacy["executable_discovery"] == "NOT_APPLICABLE"
    assert legacy["invocation"]["failed"] == 1
    assert headers["Cache-Control"] == "no-store"
    assert db.read_bytes() == before


def test_provider_status_never_invokes_a_provider_while_discovering_a_bare_command(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = project
    (root / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": {"smoke": "smoke-cli --version"},
                "authorization": {"unattended_providers": ["smoke"]},
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        "stagemesh.operator_providers.shutil.which",
        lambda executable: calls.append(executable) or "C:/safe/smoke-cli.exe",
    )
    monkeypatch.setattr(
        "stagemesh.providers.RuntimeCommandAdapter.check_capacity",
        lambda _self: pytest.fail("provider capacity probe was invoked"),
    )

    status, body, _ = request(root, db)

    smoke = next(row for row in body["providers"] if row["name"] == "smoke")
    assert status == 200 and smoke["executable_discovery"] == "DISCOVERED"
    assert "smoke-cli" in calls
    assert body["provider_calls"] == 0


def test_provider_status_bounds_rows_and_reports_truncation(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = project
    providers = {f"provider-{index:03d}": "provider-cli" for index in range(105)}
    (root / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": providers,
                "authorization": {"unattended_providers": ["provider-000"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("stagemesh.operator_providers.shutil.which", lambda _name: None)

    status, body, _ = request(root, db)

    assert status == 200
    assert body["provider_total"] == 109
    assert body["provider_total_kind"] == "EXACT"
    assert body["provider_limit"] == 100
    assert body["truncated"] is True
    assert len(body["providers"]) == 100


def test_truncated_execution_scan_labels_provider_total_as_lower_bound(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = project
    monkeypatch.setattr(
        "stagemesh.operator_providers._execution_rows",
        lambda _store: ({}, True, set()),
    )

    status, body, _ = request(root, db)

    assert status == 200
    assert body["provider_total_kind"] == "LOWER_BOUND"
    assert body["truncated"] is True


def test_invalid_provider_configuration_fails_closed_without_details(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    canary = "PRIVATE-INVALID-PROVIDER-CONFIG"
    (root / ".stagemesh" / "config.json").write_text("{" + canary, encoding="utf-8")

    status, body, _ = request(root, db)

    assert status == 200
    assert body["status"] == "UNAVAILABLE"
    assert body["providers"] == []
    assert body["attention"] == [
        {
            "code": "PROVIDER_CONFIGURATION_UNAVAILABLE",
            "message": "Provider configuration could not be safely loaded.",
        }
    ]
    assert canary not in json.dumps(body)


def test_truncated_acceptance_scan_never_relabels_unseen_evidence_as_missing(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db = project
    (root / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": {name: "provider-cli" for name in ("old", "new-a", "new-b")},
                "authorization": {"unattended_providers": ["old"]},
            }
        ),
        encoding="utf-8",
    )
    store = Store(db)
    for name in ("old", "new-a", "new-b"):
        store.add_audit_event(
            "provider.live_acceptance",
            {
                "status": "PASS",
                "provider": name,
                "implementation_checked": True,
                "implementation_ready": True,
                "review_checked": True,
                "review_ready": True,
                "checked_at": time.time(),
            },
        )
    store.close()
    monkeypatch.setattr("stagemesh.operator_providers.ACCEPTANCE_SCAN_LIMIT", 2)
    monkeypatch.setattr("stagemesh.operator_providers.shutil.which", lambda _name: None)

    status, body, _ = request(root, db)

    old = next(row for row in body["providers"] if row["name"] == "old")
    assert status == 200
    assert body["acceptance_scan_limit"] == 2
    assert body["acceptance_history_truncated"] is True
    assert old["live_acceptance"]["IMPLEMENT"]["state"] == "UNAVAILABLE"
    assert old["live_acceptance"]["REVIEW"]["state"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/providers?probe=true", "GET", HEADERS, 400),
        ("/api/v1/providers", "POST", HEADERS, 405),
        ("/api/v1/providers", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/providers", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_provider_status_route_fails_closed(
    project: tuple[Path, Path],
    target: str,
    method: str,
    headers: dict[str, str],
    expected: int,
) -> None:
    root, db = project
    status, body, _ = request(root, db, target, method=method, headers=headers)
    assert status == expected
    assert body["contract"] == PROVIDERS_CONTRACT
    assert set(body["error"]) == {"code", "message"}


def test_provider_status_route_is_absent_without_an_explicit_projection(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    status, body, _ = request(root, db, enabled=False)
    assert status == 404
    assert body["contract"] == PROVIDERS_CONTRACT
