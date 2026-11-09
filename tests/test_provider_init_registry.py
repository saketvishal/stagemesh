from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import stagemesh.cli as cli_module
from stagemesh import provider_catalog
from stagemesh.config import load_config
from stagemesh.persistence import Store
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

    assert cli_module.main(["--project", str(tmp_path), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["provider_registry"] == {
        "status": "updated",
        "registered": ["local-agent"],
        "available": ["local-agent"],
        "authorized": [],
        "inactive_available": ["local-agent"],
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
    monkeypatch.setattr(cli_module, "discovered_provider_entries", dict)

    assert cli_module.main(["--project", str(tmp_path), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["provider_registry"]["status"] == "unchanged"
    assert report["provider_registry"]["registered"] == ["my-agent"]
    assert report["provider_registry"]["available"] == []
    assert report["provider_registry"]["authorized"] == ["my-agent"]
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_init_merges_a_newly_installed_provider_plugin_without_replacing_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ".stagemesh/config.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"providers": {"existing": {"command": "existing run", "capabilities": ["IMPLEMENT"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "discovered_provider_entries",
        lambda: {"plugin-agent": {"command": "plugin-agent run", "capabilities": ["IMPLEMENT", "REVIEW"]}},
    )

    assert cli_module.main(["--project", str(tmp_path), "init", "--json", "--allow-incomplete"]) == 0

    report = json.loads(capsys.readouterr().out)["provider_registry"]
    assert report["status"] == "updated"
    assert report["registered"] == ["existing", "plugin-agent"]
    assert report["available"] == ["plugin-agent"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"]["existing"]["command"] == "existing run"
    assert data["providers"]["plugin-agent"]["command"] == "plugin-agent run"


def test_init_records_an_explicit_empty_registry_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "discovered_provider_entries", dict)

    assert cli_module.main(["--project", str(tmp_path), "init", "--json", "--allow-incomplete"]) == 0

    capsys.readouterr()
    config = load_config(tmp_path)
    assert config.providers_explicit is True
    assert config.provider_commands == {}
    assert adapters_from_config(config) == []


def test_init_authorize_provider_flags_persist_detected_agents_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    discovered = {
        "agent-one": {"command": "agent-one run", "capabilities": ["IMPLEMENT", "REVIEW"]},
        "agent-two": {"command": "agent-two run", "capabilities": ["IMPLEMENT", "REVIEW"]},
    }
    monkeypatch.setattr(cli_module, "discovered_provider_entries", lambda: discovered)

    assert cli_module.main(
        [
            "--project",
            str(tmp_path),
            "init",
            "--authorize-provider",
            "agent-one",
            "--authorize-provider",
            "agent-two",
            "--json",
            "--allow-incomplete",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["provider_registry"]["authorized"] == ["agent-one", "agent-two"]
    data = json.loads((tmp_path / ".stagemesh/config.json").read_text(encoding="utf-8"))
    assert data["authorization"]["unattended_providers"] == ["agent-one", "agent-two"]
    assert report["provider_registry"]["provider_calls"] == 0
    store = Store(tmp_path / ".stagemesh/stagemesh.sqlite3")
    try:
        event = store.conn.execute(
            "SELECT payload FROM audit_events WHERE event_type='provider.authorization_changed'"
        ).fetchone()
        assert json.loads(event["payload"])["added"] == ["agent-one", "agent-two"]
    finally:
        store.close()


def test_init_authorize_provider_rejects_an_undetected_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "discovered_provider_entries", dict)

    assert cli_module.main(
        ["--project", str(tmp_path), "init", "--authorize-provider", "codex", "--allow-incomplete"]
    ) == 2

    assert "not detected" in capsys.readouterr().err


def test_interactive_init_activates_all_installed_provider_plugins_with_one_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    monkeypatch.setattr(
        cli_module,
        "discovered_provider_entries",
        lambda: {
            "claude": {"command": "claude -p", "capabilities": ["IMPLEMENT", "REVIEW"]},
            "codex": {"command": "codex exec", "capabilities": ["IMPLEMENT", "REVIEW"]},
            "grok": {"command": "grok", "capabilities": ["IMPLEMENT", "REVIEW"]},
            "agy": {"command": "agy", "capabilities": ["IMPLEMENT", "REVIEW"]},
        },
    )
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "y")

    assert cli_module.main(["--project", str(tmp_path), "init", "--allow-incomplete"]) == 0

    data = json.loads((tmp_path / ".stagemesh/config.json").read_text(encoding="utf-8"))
    assert data["authorization"]["unattended_providers"] == ["agy", "claude", "codex", "grok"]
    assert "installed adapter plugin" in capsys.readouterr().out


def test_interactive_init_reconciles_newly_installed_plugins_with_an_existing_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import builtins

    runtime = tmp_path / ".stagemesh"
    runtime.mkdir()
    (runtime / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "codex": {"command": "codex exec", "capabilities": ["IMPLEMENT", "REVIEW"]},
                    "grok": {"command": "grok", "capabilities": ["IMPLEMENT", "REVIEW"]},
                    "agy": {"command": "agy", "capabilities": ["IMPLEMENT", "REVIEW"]},
                },
                "authorization": {"unattended_providers": ["codex"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: "y")

    assert cli_module._prompt_provider_authorization(tmp_path, ("agy", "codex", "grok")) == (
        "codex", "agy", "grok"
    )

    data = json.loads((runtime / "config.json").read_text(encoding="utf-8"))
    assert data["authorization"]["unattended_providers"] == ["codex", "agy", "grok"]
    assert "Already active: codex" in capsys.readouterr().out


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


def test_catalog_discovers_an_independently_installed_provider_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    class Point:
        name = "new-agent"

        @staticmethod
        def load():
            return {
                "executable": "new-agent",
                "command": "new-agent run --safe",
                "capabilities": ["IMPLEMENT"],
            }

    class Points(list):
        def select(self, **kwargs):
            return self if kwargs == {"group": provider_catalog.PROVIDER_PLUGIN_GROUP} else []

    monkeypatch.setattr(provider_catalog.metadata, "entry_points", lambda: Points([Point()]))
    provider_catalog._provider_plugins.cache_clear()
    try:
        entries = discovered_provider_entries(
            lambda executable: f"/tools/{executable}" if executable == "new-agent" else None
        )
    finally:
        provider_catalog._provider_plugins.cache_clear()

    assert entries == {
        "new-agent": {"command": "new-agent run --safe", "capabilities": ["IMPLEMENT"]}
    }


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
