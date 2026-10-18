from __future__ import annotations

import sys
from pathlib import Path

import stagemesh.providers as providers
from stagemesh.execution import capacity_evidence


def codex_package(tmp_path: Path, content: bytes = b"same-codex") -> tuple[Path, Path]:
    detached = tmp_path / "codex.exe"
    detached.write_bytes(b"same-codex")
    packaged = tmp_path / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(content)
    packaged.with_name("codex-code-mode-host.exe").write_bytes(b"host")
    return detached, packaged


def test_windows_codex_uses_identical_complete_package(monkeypatch, tmp_path):
    detached, packaged = codex_package(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(providers.shutil, "which", lambda executable: str(detached))
    adapter = providers.RuntimeCommandAdapter("codex", ("codex", "exec", "--sandbox", "workspace-write"))
    assert adapter.command_for("code") == (str(packaged), "exec", "--sandbox", "workspace-write")
    assert adapter.command_for("review")[0] == str(packaged)
    summary = providers.stage_command_summary(adapter)
    assert summary["IMPLEMENT"]["source"] == "default"
    assert summary["REVIEW"]["source"] == "default"


def test_windows_codex_never_changes_version(monkeypatch, tmp_path):
    detached, _ = codex_package(tmp_path, b"different-codex")
    monkeypatch.setattr(sys, "platform", "win32")
    assert providers._complete_codex_executable(str(detached)) == str(detached)


def test_stale_codex_path_fallback_uses_complete_package(monkeypatch, tmp_path):
    detached, packaged = codex_package(tmp_path)
    stale = str(tmp_path / "removed-install" / "codex.exe")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(providers.shutil, "which", lambda executable: str(detached) if executable == "codex.exe" else None)
    adapter = providers.RuntimeCommandAdapter("codex", (stale, "exec"))
    assert adapter.command_for("code") == (str(packaged), "exec")


def test_windows_codex_keeps_complete_explicit_install(monkeypatch, tmp_path):
    detached, _ = codex_package(tmp_path)
    detached.with_name("codex-code-mode-host.exe").write_bytes(b"host")
    monkeypatch.setattr(sys, "platform", "win32")
    assert providers._complete_codex_executable(str(detached)) == str(detached)


def test_windows_codex_requires_packaged_host(monkeypatch, tmp_path):
    detached, packaged = codex_package(tmp_path)
    packaged.with_name("codex-code-mode-host.exe").unlink()
    monkeypatch.setattr(sys, "platform", "win32")
    assert providers._complete_codex_executable(str(detached)) == str(detached)


def test_other_platforms_and_commands_unchanged(monkeypatch, tmp_path):
    detached, _ = codex_package(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    assert providers._complete_codex_executable(str(detached)) == str(detached)
    monkeypatch.setattr(sys, "platform", "win32")
    assert providers._complete_codex_executable(str(tmp_path / "other.exe")) == str(tmp_path / "other.exe")


def test_capacity_evidence_keeps_redacted_terminal_error():
    excerpt, retry = capacity_evidence("banner\n" + "prompt " * 300, "failed to spawn code-mode host: host executable was not found\nBearer secret-value\nretry-after: 60s")
    assert "host executable was not found" in excerpt
    assert "secret-value" not in excerpt
    assert "[redacted]" in excerpt
    assert len(excerpt) <= 500
    assert retry == "retry-after: 60s"
