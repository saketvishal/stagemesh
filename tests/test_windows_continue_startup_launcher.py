from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "build_coordinator" / "bin" / "stagemesh-windows-startup.ps1"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_windows_continue_startup_script_is_installed_with_launchers():
    assert SCRIPT.exists()
    text = _script_text()
    assert "stagemesh.ps1" in text
    assert '"continue"' in text
    assert '"--project-dir"' in text


def test_windows_continue_startup_uses_unattended_logon_task():
    text = _script_text()
    assert "/SC ONLOGON" in text
    assert "/RL LIMITED" in text
    assert '"--once"' not in text
    assert '"--dry-run"' not in text


def test_windows_continue_startup_install_and_uninstall_are_idempotent():
    text = _script_text()
    assert "/Create /F" in text
    assert "Test-TaskInstalled $EffectiveTaskName" in text
    assert "/Delete /TN $EffectiveTaskName /F" in text


def test_windows_continue_startup_task_name_is_stable_and_does_not_embed_project_details():
    text = _script_text()
    assert "SHA256" in text
    return_line = next(line for line in text.splitlines() if "StageMesh-Continue-" in line)
    assert 'return "StageMesh-Continue-$hex"' == return_line.strip()
    assert "$ProjectName" not in return_line
