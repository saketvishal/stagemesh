"""Tests for brand-neutral build coordinator infrastructure and backward compatibility."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from build_coordinator.config import get_settings
from build_coordinator.coordinator_config import (
    CoordinatorConfig,
    load_coordinator_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "build_coordinator" / "bin"


def test_build_coordinator_config_env_var_takes_precedence(monkeypatch, tmp_path):
    primary_config = tmp_path / "primary.json"
    primary_config.write_text(json.dumps({"max_active_builders": 4}), encoding="utf-8")
    monkeypatch.setenv("BUILD_COORDINATOR_CONFIG", str(primary_config))

    config = load_coordinator_config()
    assert config.max_active_builders == 4


def test_repo_root_env_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("BUILD_COORDINATOR_CONFIG", raising=False)

    primary_root = tmp_path / "primary_root"
    primary_root.mkdir()
    legacy_root = tmp_path / "legacy_root"
    legacy_root.mkdir()

    monkeypatch.setenv("BUILD_COORDINATOR_REPO_ROOT", str(primary_root))
    monkeypatch.setenv("REPO_ROOT", str(legacy_root))

    settings = get_settings()
    assert settings.repo_root == primary_root.resolve()


def test_legacy_repo_root_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("BUILD_COORDINATOR_CONFIG", raising=False)
    monkeypatch.delenv("BUILD_COORDINATOR_REPO_ROOT", raising=False)
    monkeypatch.delenv("REPO_ROOT", raising=False)

    legacy_root = tmp_path / "legacy_root"
    legacy_root.mkdir()
    monkeypatch.setenv("REPO_ROOT", str(legacy_root))

    settings = get_settings()
    assert settings.repo_root == legacy_root.resolve()


def test_neutral_launchers_invoke_safe_path_flag():
    posix = (BIN_DIR / "build-coordinator").read_text(encoding="utf-8")
    ps1 = (BIN_DIR / "build-coordinator.ps1").read_text(encoding="utf-8")
    assert "python -P -m build_coordinator.cli" in posix
    assert "python -P -m build_coordinator.cli" in ps1


@pytest.mark.skipif(os.name != "nt", reason="PowerShell launcher is Windows-specific")
def test_powershell_neutral_launcher_runs_from_unrelated_directory(tmp_path):
    control_repo_root = tmp_path / "control-repo"
    control_repo_root.mkdir()
    db_path = tmp_path / "coordinator-data" / "coordinator.sqlite3"
    db_path.parent.mkdir(parents=True)
    config_path = tmp_path / "build-coordinator.json"
    config_path.write_text(
        json.dumps(
            {
                "control_repo_root": str(control_repo_root),
                "database_url": f"sqlite:///{db_path.as_posix()}",
                "data_dir": str(db_path.parent),
            }
        ),
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()

    env = dict(os.environ)
    env["BUILD_COORDINATOR_CONFIG"] = str(config_path)
    interpreter_dir = str(Path(sys.executable).parent)
    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([interpreter_dir, current])

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BIN_DIR / "build-coordinator.ps1"),
            "objective",
            "status",
        ],
        cwd=str(unrelated),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["workspace"]["control_repo_root"] == str(control_repo_root.resolve())
