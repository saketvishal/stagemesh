from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap_stagemesh.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("bootstrap_stagemesh", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_dispatcher_finds_project_local_runtime_from_current_tree(tmp_path: Path) -> None:
    bootstrap = _load_bootstrap()
    dispatcher = tmp_path / "stagemesh.cmd"

    original_name = os.name
    bootstrap.os.name = "nt"
    try:
        bootstrap._write_user_dispatcher(dispatcher)
    finally:
        bootstrap.os.name = original_name

    text = dispatcher.read_text(encoding="utf-8")
    assert r"%DIR%\.stagemesh\bin\stagemesh.cmd" in text
    assert r'call "%DIR%\.stagemesh\bin\stagemesh.cmd" %*' in text
    assert "no project-local runtime found" in text


def test_user_dispatcher_prefers_writable_user_path_already_on_path(tmp_path: Path, monkeypatch) -> None:
    bootstrap = _load_bootstrap()
    local_app_data = tmp_path / "LocalAppData"
    on_path = local_app_data / "Microsoft" / "WindowsApps"
    fallback = tmp_path / "fallback"
    on_path.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("STAGEMESH_USER_BIN", str(fallback))
    bootstrap.USER_BIN = fallback

    assert bootstrap._user_owned_path_dir() == on_path.resolve()


def test_user_dispatcher_ignores_temporary_path_entries(tmp_path: Path, monkeypatch) -> None:
    bootstrap = _load_bootstrap()
    temp_path = tmp_path / ".codex" / "tmp" / "arg0"
    fallback = tmp_path / "fallback"
    temp_path.mkdir(parents=True)
    monkeypatch.setenv("PATH", str(temp_path))
    monkeypatch.setenv("STAGEMESH_USER_BIN", str(fallback))
    bootstrap.USER_BIN = fallback

    assert bootstrap._user_owned_path_dir() == fallback


def test_user_dispatcher_falls_back_to_stagemesh_user_bin(tmp_path: Path, monkeypatch) -> None:
    bootstrap = _load_bootstrap()
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("STAGEMESH_USER_BIN", str(fallback))
    bootstrap.USER_BIN = fallback

    assert bootstrap._user_owned_path_dir() == fallback


def test_agy_unattended_is_explicit_project_local_opt_in(tmp_path: Path) -> None:
    bootstrap = _load_bootstrap()
    bootstrap.ROOT = tmp_path

    bootstrap._configure_providers(["codex", "agy"], agy_unattended=True)

    data = json.loads((tmp_path / ".stagemesh" / "config.json").read_text(encoding="utf-8"))
    assert data["providers"]["agy"]["command"] == bootstrap.AGY_UNATTENDED_COMMAND
    assert data["providers"]["codex"]["command"] == bootstrap.DEFAULT_COMMANDS["codex"]
    assert data["routing"]["pools"]["IMPLEMENT"] == ["codex", "agy"]


def test_agy_unattended_requires_agy_provider(tmp_path: Path) -> None:
    bootstrap = _load_bootstrap()
    bootstrap.ROOT = tmp_path

    try:
        bootstrap._configure_providers(["codex"], agy_unattended=True)
    except SystemExit as exc:
        assert "--agy-unattended requires --providers including agy" in str(exc)
    else:
        raise AssertionError("expected --agy-unattended without agy to fail")
