from __future__ import annotations

from pathlib import Path
import re


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
    assert "New-ScheduledTaskTrigger -AtStartup" in text
    assert "New-ScheduledTaskPrincipal" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel LeastPrivilege" in text
    assert "Register-ScheduledTask" in text
    assert "-Force" in text
    assert "/SC ONLOGON" not in text
    assert "trigger = \"AtStartup\"" in text
    assert "logon_type = \"S4U\"" in text
    assert '"--once"' not in text
    assert '"--dry-run"' not in text


def test_windows_continue_startup_install_and_uninstall_are_idempotent():
    text = _script_text()
    assert "Register-ScheduledTask `" in text
    assert "-Force | Out-Null" in text
    assert "Test-TaskInstalled $EffectiveTaskName" in text
    assert "Unregister-ScheduledTask -TaskName $EffectiveTaskName -Confirm:$false" in text


def test_windows_continue_startup_task_name_is_stable_and_does_not_embed_project_details():
    text = _script_text()
    assert "SHA256" in text
    return_line = next(line for line in text.splitlines() if "StageMesh-Continue-" in line)
    assert 'return "StageMesh-Continue-$hex"' == return_line.strip()
    assert "$ProjectName" not in return_line


def test_windows_continue_startup_uses_same_effective_task_name_for_all_actions():
    text = _script_text()
    task_name_assignment = "$EffectiveTaskName = if ($TaskName) { $TaskName } else { ConvertTo-StableTaskName $TaskSeed }"
    assert text.count(task_name_assignment) == 1

    effective_name_position = text.index(task_name_assignment)
    for action in ('if ($Action -eq "status")', 'if ($Action -eq "uninstall")', "Register-ScheduledTask `"):
        assert effective_name_position < text.index(action)

    assert "Get-ScheduledTask -TaskName $Name" in text
    assert "Unregister-ScheduledTask -TaskName $EffectiveTaskName" in text
    assert "-TaskName $EffectiveTaskName `" in text


def test_windows_continue_startup_task_seed_distinguishes_variants():
    text = _script_text()
    task_seed_match = re.search(r"\$TaskSeed = if \(\$All\).*?else \{ \$ResolvedProjectDir \}", text)
    assert task_seed_match is not None
    task_seed = task_seed_match.group(0)

    assert '"all|$ResolvedProjectDir"' in task_seed
    assert '"$ResolvedProjectDir|$ProjectName"' in task_seed
    assert "else { $ResolvedProjectDir }" in task_seed


def test_windows_startup_docs_show_matching_status_and_uninstall_variants():
    docs = (REPO_ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")

    assert "AtStartup" in docs
    assert "without an interactive logon" in docs
    assert "Use the same task-name-affecting flags for `status` and `uninstall`" in docs

    status_project_name = re.search(r"stagemesh-windows-startup\.ps1 status `\n\s+-ProjectDir .*? `\n\s+-ProjectName my-product", docs)
    uninstall_project_name = re.search(r"stagemesh-windows-startup\.ps1 uninstall `\n\s+-ProjectDir .*? `\n\s+-ProjectName my-product", docs)
    status_all = re.search(r"stagemesh-windows-startup\.ps1 status `\n\s+-ProjectDir .*? `\n\s+-All", docs)
    uninstall_all = re.search(r"stagemesh-windows-startup\.ps1 uninstall `\n\s+-ProjectDir .*? `\n\s+-All", docs)

    assert status_project_name is not None
    assert uninstall_project_name is not None
    assert status_all is not None
    assert uninstall_all is not None
