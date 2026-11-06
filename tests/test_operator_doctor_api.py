from __future__ import annotations

import json
from pathlib import Path

import pytest

from stagemesh.api_server import ActivityApi
from stagemesh.operator_doctor import DOCTOR_CONTRACT, ProjectDoctor
from stagemesh.persistence import SCHEMA_VERSION, Store

TOKEN = "doctor-api-token"
HEADERS = {"Host": "127.0.0.1:12345", "Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "STAGEMESH_CODEX_CMD",
        "STAGEMESH_CLAUDE_CMD",
        "STAGEMESH_GROK_CMD",
        "STAGEMESH_AGY_CMD",
        "STAGEMESH_DATABASE_URL",
        "STAGEMESH_GITHUB_OWNER",
        "STAGEMESH_GITHUB_REPO",
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
    target: str = "/api/v1/doctor",
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    enabled: bool = True,
):
    api = ActivityApi(db, TOKEN, doctor=ProjectDoctor(project) if enabled else None)
    api.bind(12345)
    status, payload, response_headers = api.handle(method, target, headers or HEADERS)
    return status, json.loads(payload), response_headers


def test_doctor_is_local_only_read_only_and_explicit_about_observation_boundaries(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    before = db.read_bytes()

    status, body, headers = request(root, db)

    assert status == 200
    assert body["contract"] == DOCTOR_CONTRACT
    assert body["verdict"] == "ATTENTION"
    assert body["database"] == {
        "backend": "SQLITE",
        "schema": "CURRENT",
        "current_version": SCHEMA_VERSION,
        "expected_version": SCHEMA_VERSION,
    }
    assert body["network_checked"] is False
    assert body["mutation_performed"] is False
    assert body["configuration"]["source"] == "DEFAULTS"
    assert {item["code"] for item in body["attention"]} == {
        "NO_UNATTENDED_PROVIDER_AUTHORIZATION"
    }
    assert headers["Cache-Control"] == "no-store"
    assert db.read_bytes() == before


def test_doctor_exposes_posture_without_config_commands_tokens_paths_or_repository_identity(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    secret = "PRIVATE-DOCTOR-CANARY"
    command_path = root / "private" / "codex.exe"
    config = {
        "github": {"owner": "private-owner", "repo": "private-repo", "token": secret},
        "providers": {"codex": f'"{command_path}" --token {secret}'},
        "authorization": {"unattended_providers": ["codex"]},
        "routing": {"require_independent_review": True},
        "planning_gate": {"require_approved_spec": False},
    }
    (root / ".stagemesh" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    status, body, _ = request(root, db)
    encoded = json.dumps(body)

    assert status == 200 and body["verdict"] == "READY"
    assert secret not in encoded
    assert "private-owner" not in encoded and "private-repo" not in encoded
    assert "codex.exe" not in encoded and str(root) not in encoded
    assert body["configuration"]["github"] == {
        "repository_configured": True,
        "credential_configured": True,
        "ready": True,
        "partially_configured": False,
    }
    codex = next(provider for provider in body["providers"] if provider["name"] == "codex")
    assert codex == {
        "name": "codex",
        "capabilities": ["IMPLEMENT", "REVIEW"],
        "command_source": "PROJECT_OR_ENV",
        "authorized_for_unattended": True,
    }
    assert "available" not in codex and "invoked" not in codex


def test_invalid_configuration_fails_closed_without_echoing_parser_details(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    canary = "PRIVATE-INVALID-CONFIG-CANARY"
    (root / ".stagemesh" / "config.json").write_text("{" + canary, encoding="utf-8")

    status, body, _ = request(root, db)

    assert status == 200
    assert body["verdict"] == "UNAVAILABLE"
    assert body["configuration"] == {"status": "UNAVAILABLE"}
    assert body["providers"] == [] and body["policies"] is None
    assert body["attention"][-1] == {
        "code": "CONFIG_UNAVAILABLE",
        "severity": "error",
        "message": "Project configuration could not be safely loaded.",
    }
    assert canary not in json.dumps(body)


def test_provider_rows_are_bounded_without_hiding_the_total(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    providers = {f"provider-{index:03d}": "provider-cli" for index in range(105)}
    config = {
        "providers": providers,
        "authorization": {"unattended_providers": ["provider-000"]},
    }
    (root / ".stagemesh" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    status, body, _ = request(root, db)

    assert status == 200
    assert body["provider_total"] == 109
    assert body["provider_limit"] == 100
    assert body["providers_truncated"] is True
    assert len(body["providers"]) == 100
    assert all(len(provider["name"]) <= 100 for provider in body["providers"])


@pytest.mark.parametrize(
    ("target", "method", "headers", "expected"),
    [
        ("/api/v1/doctor?verbose=true", "GET", HEADERS, 400),
        ("/api/v1/doctor", "POST", HEADERS, 405),
        ("/api/v1/doctor", "GET", {"Host": "127.0.0.1:12345"}, 401),
        ("/api/v1/doctor", "GET", {**HEADERS, "Host": "attacker.invalid"}, 403),
    ],
)
def test_doctor_route_fails_closed(
    project: tuple[Path, Path],
    target: str,
    method: str,
    headers: dict[str, str],
    expected: int,
) -> None:
    root, db = project
    status, body, _ = request(root, db, target, method=method, headers=headers)
    assert status == expected
    assert body["contract"] == DOCTOR_CONTRACT
    assert set(body["error"]) == {"code", "message"}


def test_doctor_route_is_absent_without_an_explicit_project_projection(
    project: tuple[Path, Path],
) -> None:
    root, db = project
    status, body, _ = request(root, db, enabled=False)
    assert status == 404
    assert body["contract"] == DOCTOR_CONTRACT
