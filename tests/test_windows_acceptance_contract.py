from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ACCEPTANCE = ROOT / "scripts" / "windows-acceptance"


def test_native_acceptance_prepopulates_every_required_step_as_failed() -> None:
    script = (WINDOWS_ACCEPTANCE / "run-native-acceptance.ps1").read_text(encoding="utf-8")

    assert "$requiredSummarySteps" in script
    assert "'driver_lifecycle_mock_providers'" in script
    assert "'cleanup_complete'" in script
    assert "detail = 'not completed'" in script
    assert "$requiredSummarySteps | Where-Object { -not $summary.steps[$_].ok }" in script
    assert "$summary.steps.Count -ge" not in script


def test_all_background_powershell_helpers_are_hidden() -> None:
    for name in ("run-native-acceptance.ps1", "install-handsoff.ps1", "upgrade-handsoff.ps1"):
        script = (WINDOWS_ACCEPTANCE / name).read_text(encoding="utf-8")
        assert "Start-Process" in script
        assert "-WindowStyle Hidden" in script
        assert "-NoNewWindow" not in script


def test_persistent_installer_rejects_incomplete_or_cleanup_only_passes() -> None:
    script = (WINDOWS_ACCEPTANCE / "install-handsoff.ps1").read_text(encoding="utf-8")

    assert "$s.steps.driver_lifecycle_mock_providers.ok" in script
    assert "$s.steps.cleanup_complete.ok" in script


def test_hands_off_install_and_upgrade_reject_stale_codex_executables() -> None:
    for name in ("install-handsoff.ps1", "upgrade-handsoff.ps1"):
        script = (WINDOWS_ACCEPTANCE / name).read_text(encoding="utf-8")
        assert "Configured-Executable" in script
        assert "[IO.Path]::IsPathRooted($codexExecutable)" in script
        assert "Test-Path -LiteralPath $codexExecutable -PathType Leaf" in script
        assert "Invoke-Bounded $codexExecutable @('--version') 30" in script


def test_native_acceptance_retains_driver_reports_before_disposable_cleanup() -> None:
    script = (WINDOWS_ACCEPTANCE / "run-native-acceptance.ps1").read_text(encoding="utf-8")

    assert "project\\.stagemesh\\driver\\reports" in script
    assert "Join-Path $evidence 'driver-reports'" in script
    assert "'--initial-only'" not in script


def test_native_scheduler_acceptance_uses_only_windowless_process_helpers() -> None:
    script = (WINDOWS_ACCEPTANCE / "scheduler_acceptance.py").read_text(encoding="utf-8")

    assert script.count("subprocess.run(") == 1
    assert "**popen_descendant_kwargs()" in script
    assert "pythonw.exe" in script
    assert 'for variant in ("direct", "cmd")' not in script


def test_windows_process_diagnostics_hide_every_spawned_helper() -> None:
    killtree = (WINDOWS_ACCEPTANCE / "diag-killtree.py").read_text(encoding="utf-8")
    timeout = (WINDOWS_ACCEPTANCE / "diag-timeout.py").read_text(encoding="utf-8")

    assert killtree.count("subprocess.run(") == 1
    assert killtree.count("**popen_descendant_kwargs()") >= 2
    assert "**popen_descendant_kwargs()" in timeout
