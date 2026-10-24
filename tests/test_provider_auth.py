from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stagemesh import driver_scheduler, provider_auth
from stagemesh.cli import main


def completed(stdout: str = "", code: int = 0, stderr: str = ""):
    return lambda argv: subprocess.CompletedProcess(argv, code, stdout, stderr)


def installed(name: str) -> str | None:
    return f"C:/bin/{name}.exe"


def test_claude_subscription_and_api_modes_are_classified_without_exposing_output() -> None:
    sub = provider_auth.probe("claude", completed(json.dumps({"authMethod": "claude.ai", "email": "x@y.z"})), installed)
    key = provider_auth.probe("claude", completed(json.dumps({"authMethod": "api_key"})), installed)
    out = provider_auth.probe("claude", completed("not json", 1), installed)

    assert sub.subscription and sub.auth_mode == "claude.ai"
    assert not key.subscription and key.auth_mode == "api_key"
    assert out.auth_mode == "not_logged_in" and not out.subscription
    assert "x@y.z" not in json.dumps(sub.to_dict())


def test_codex_api_key_login_is_never_mistaken_for_a_subscription() -> None:
    chatgpt = provider_auth.probe("codex", completed("Logged in using ChatGPT"), installed)
    metered = provider_auth.probe("codex", completed("Logged in using an API key - sk-***abcd"), installed)
    both = provider_auth.probe("codex", completed("ChatGPT workspace, API key present"), installed)

    assert chatgpt.subscription
    assert metered.auth_mode == "api_key" and not metered.subscription
    assert both.auth_mode == "api_key"
    assert "sk-" not in json.dumps(metered.to_dict())


def test_missing_cli_is_reported_not_installed() -> None:
    state = provider_auth.probe("codex", completed(), lambda _name: None)
    assert (state.installed, state.auth_mode, state.subscription) == (False, "not_installed", False)


def test_billing_environment_blocks_and_reports_names_only() -> None:
    report = provider_auth.check(
        runner=completed(json.dumps({"authMethod": "claude.ai"})),
        environ={"OPENAI_API_KEY": "sk-secret-value", "PATH": "x"},
    )

    assert report["ok"] is False and report["model_calls_made"] == 0
    assert report["api_billing_environment"] == ["process:OPENAI_API_KEY"]
    assert "sk-secret-value" not in json.dumps(report)


def test_real_provider_detection_ignores_mock_commands() -> None:
    assert provider_auth.uses_real_providers([("C:/x/codex.cmd", "exec")])
    assert provider_auth.uses_real_providers([("claude", "-p")])
    assert not provider_auth.uses_real_providers([("python.exe", "mock_provider.py", "--calls", "x")])


def test_driver_install_refuses_real_providers_under_api_billing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class Scheduler:
        name = "memory"
        handle = "StageMesh\\driver-x"

        def ensure(self, *args, **kwargs):
            raise AssertionError("must not register anything")

        def exists(self, _handle):
            return False

    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda _project: Scheduler())
    monkeypatch.setattr(provider_auth, "check", lambda *a, **k: {"ok": False, "blockers": ["API billing variables are set (process:OPENAI_API_KEY)"]})
    monkeypatch.setattr(provider_auth, "uses_real_providers", lambda _commands: True)
    (tmp_path / ".stagemesh").mkdir()

    code = main(["--project", str(tmp_path), "driver", "install", "--acknowledge-provider-use"])

    assert code == 2
    assert "refusing persistent real-provider startup" in capsys.readouterr().err
