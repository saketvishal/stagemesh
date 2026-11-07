from __future__ import annotations

import json
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
from stagemesh.config import load_config
from stagemesh.provider_catalog import discovered_provider_entries
from stagemesh.providers import adapters_from_config


def test_init_snapshots_discovered_providers_without_authorizing_or_invoking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    discovered = {
        "local-agent": {
            "command": "local-agent run",
            "capabilities": ["IMPLEMENT", "REVIEW"],
        }
    }
    monkeypatch.setattr(cli_module, "discovered_provider_entries", lambda: discovered)

    assert cli_module.main(["--project", str(tmp_path), "init", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["provider_registry"] == {
        "status": "initialized",
        "registered": ["local-agent"],
        "authorized": [],
        "provider_calls": 0,
    }
    data = json.loads((tmp_path / ".stagemesh/config.json").read_text(encoding="utf-8"))
    assert data["providers"] == discovered
    assert "authorization" not in data
    config = load_config(tmp_path)
    assert config.providers_explicit is True
    assert [adapter.name for adapter in adapters_from_config(config)] == ["local-agent"]


def test_init_preserves_an_existing_provider_registry_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ".stagemesh/config.json"
    path.parent.mkdir()
    original = {
        "providers": {"my-agent": {"command": "my-agent run", "capabilities": ["IMPLEMENT"]}},
        "authorization": {"unattended_providers": ["my-agent"]},
    }
    path.write_text(json.dumps(original, indent=4) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        cli_module,
        "discovered_provider_entries",
        lambda: pytest.fail("discovery must not replace an existing registry"),
    )

    assert cli_module.main(["--project", str(tmp_path), "init", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["provider_registry"]["status"] == "unchanged"
    assert report["provider_registry"]["registered"] == ["my-agent"]
    assert report["provider_registry"]["authorized"] == ["my-agent"]
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_init_records_an_explicit_empty_registry_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "discovered_provider_entries", dict)

    assert cli_module.main(["--project", str(tmp_path), "init", "--json"]) == 0

    capsys.readouterr()
    config = load_config(tmp_path)
    assert config.providers_explicit is True
    assert config.provider_commands == {}
    assert adapters_from_config(config) == []


def test_catalog_discovery_is_side_effect_free_and_only_records_available_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "STAGEMESH_CODEX_CMD",
        "STAGEMESH_CLAUDE_CMD",
        "STAGEMESH_GROK_CMD",
        "STAGEMESH_AGY_CMD",
    ):
        monkeypatch.delenv(variable, raising=False)
    looked_up: list[str] = []

    def which(executable: str) -> str | None:
        looked_up.append(executable)
        return f"/tools/{executable}" if executable in {"codex", "claude"} else None

    entries = discovered_provider_entries(which)

    assert set(entries) == {"codex", "claude"}
    assert looked_up == ["codex", "claude", "grok", "agy"]
    assert all(value["capabilities"] == ["IMPLEMENT", "REVIEW"] for value in entries.values())


def test_catalog_discovery_never_persists_command_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", "codex exec --token SECRET_VALUE")

    entries = discovered_provider_entries(lambda _executable: None)

    assert set(entries) == {"codex"}
    assert "SECRET_VALUE" not in str(entries)
    assert entries["codex"]["command"] == "codex exec --sandbox workspace-write"


def test_legacy_uninitialized_project_keeps_defaults_until_init_migrates_it(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.providers_explicit is False
    assert {"codex", "claude", "grok", "agy"} == {
        adapter.name for adapter in adapters_from_config(config)
    }


def test_balanced_profile_refuses_an_explicit_empty_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text('{"providers": {}}\n', encoding="utf-8")

    assert cli_module.main(
        ["--project", str(tmp_path), "provider-profile", "use", "balanced"]
    ) == 2

    assert "provider registry is empty" in capsys.readouterr().err
    assert json.loads(path.read_text(encoding="utf-8")) == {"providers": {}}
