"""Windows deep-worktree git failures and an informative blocked reason (both found by the live dogfood run)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_parallel import Rig

import stagemesh.git as git_module
from stagemesh.audit import record_audit
from stagemesh.git import GitWorkspace, _windows_longpaths
from stagemesh.observability import _latest_blocked_reason


def test_windows_git_commands_get_longpaths_per_command_unless_the_caller_chose() -> None:
    assert _windows_longpaths(("rebase", "abc"), windows=True) == ["-c", "core.longpaths=true"]
    assert _windows_longpaths(("rebase", "abc"), windows=False) == []
    assert _windows_longpaths(("-c", "core.longpaths=false", "rebase", "abc"), windows=True) == []  # an explicit choice wins


def test_git_workspace_run_applies_longpaths_only_on_windows_and_never_writes_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording(cmd, *args, **kwargs):
        seen.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(git_module.subprocess, "run", recording)
    GitWorkspace(repo).run("status", "--porcelain")

    assert (["-c", "core.longpaths=true"] == seen[0][1:3]) is (os.name == "nt")
    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "longpaths" not in config  # per command only; the owner's git configuration is untouched


def test_blocked_reason_names_the_stop_not_just_blocked(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ["A", "B", "C"])
    store = rig.store
    record_audit(store, "task.diagnosis_stop", {"task_id": "A", "category": "validation_gate"})
    record_audit(store, "task.remediation_exhausted", {"task_id": "B", "reason": "integration_ref_state", "stage": "INTEGRATE"})
    record_audit(store, "task.diagnosis_stop", {"task_id": "C", "category": "provider_no_progress"})
    record_audit(store, "task.blocked", {"task_id": "C", "reason": "EXTERNAL_WORKSPACE_MUTATION", "stage": "IMPLEMENT"})

    assert _latest_blocked_reason(store, "A") == "validation_gate"
    assert _latest_blocked_reason(store, "B") == "integration_ref_state"
    assert _latest_blocked_reason(store, "C") == "EXTERNAL_WORKSPACE_MUTATION"  # the newest stop wins
    assert _latest_blocked_reason(store, "unknown-task") == "blocked"
