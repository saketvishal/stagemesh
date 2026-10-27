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
    assert not provider_auth.probe("claude", completed(json.dumps({"authMethod": "claude.ai"}), 1), installed).subscription
    assert not provider_auth.probe("claude", completed(json.dumps({"authMethod": "claude.ai", "loggedIn": False})), installed).subscription
    assert provider_auth.probe("claude", completed(json.dumps({"authMethod": "mystery"})), installed).auth_mode == "unparsed"
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


@pytest.mark.parametrize(
    "text",
    [
        "Logged in using OPENAI_API_KEY (not ChatGPT)",
        "Logged in using an API key - sk-***abcd",
        "Not logged in, sign in with ChatGPT",
        "Welcome to ChatGPT",
        "",
    ],
)
def test_codex_doubtful_output_never_becomes_a_subscription(text: str) -> None:
    assert not provider_auth.probe("codex", completed(text), installed).subscription


def test_no_installed_cli_blocks_and_installer_mode_requires_both() -> None:
    nothing = provider_auth.check(runner=completed(), environ={}, which=lambda _name: None)
    assert nothing["ok"] is False and "no coding CLI" in " ".join(nothing["blockers"])
    only_claude = provider_auth.check(
        runner=completed(json.dumps({"authMethod": "claude.ai"})),
        environ={},
        which=lambda name: "C:/bin/claude.exe" if name == "claude" else None,
    )
    assert only_claude["ok"] is True  # fine for a project that needs one CLI ...
    strict = provider_auth.check(
        runner=completed(json.dumps({"authMethod": "claude.ai"})),
        environ={},
        require_installed=True,
        which=lambda name: "C:/bin/claude.exe" if name == "claude" else None,
    )
    assert strict["ok"] is False and "codex CLI is not installed" in strict["blockers"]  # ... but the installer needs both


def test_tick_and_resume_refuse_when_authentication_regresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from stagemesh import continuous_driver

    monkeypatch.setattr(provider_auth, "uses_real_providers", lambda _commands: True)
    monkeypatch.setattr(provider_auth, "check", lambda *a, **k: {"ok": False, "blockers": ["API billing variables are set (user:OPENAI_API_KEY)"]})

    class Scheduler:
        name = "memory"
        handle = "StageMesh\\driver-x"

        def ensure(self, *args, **kwargs):
            raise AssertionError("must not register")

        def exists(self, _handle):
            return True

    monkeypatch.setattr(driver_scheduler, "default_driver_scheduler", lambda _project: Scheduler())
    (tmp_path / ".stagemesh").mkdir()
    continuous_driver.save_config(tmp_path, continuous_driver.DriverConfig(project=str(tmp_path), interval_seconds=60, window_seconds=60))

    assert main(["--project", str(tmp_path), "driver", "tick", "--json"]) == 1
    assert "provider authentication blocked" in capsys.readouterr().out
    assert main(["--project", str(tmp_path), "driver", "resume"]) == 2
    assert "provider authentication blocked" in capsys.readouterr().err


def test_cli_exposes_require_installed(monkeypatch, capsys):
    from stagemesh.cli import main

    seen = {}

    def fake_check(**kwargs):
        seen.update(kwargs)
        return {"providers": [], "api_billing_environment": [], "ok": True, "blockers": [], "model_calls_made": 0}

    monkeypatch.setattr(provider_auth, "check", fake_check)
    assert main(["provider-auth", "--json", "--require-installed"]) == 0
    assert seen == {"require_installed": True}


def test_wrapped_real_provider_commands_are_detected():
    assert provider_auth.uses_real_providers([["cmd", "/c", "codex", "exec"]])
    assert provider_auth.uses_real_providers([["npx", "claude", "-p"]])
    assert provider_auth.uses_real_providers([["C:\\bin\\codex.cmd"]])
    assert not provider_auth.uses_real_providers([["python", "mock_provider.py"]])


def test_authentication_checks_each_configured_stage_executable_instead_of_path():
    calls = []

    def runner(argv):
        calls.append(argv)
        mode = "api_key" if "review" in argv[0] else "claude.ai"
        return subprocess.CompletedProcess(argv, 0, json.dumps({"authMethod": mode}), "")

    report = provider_auth.check(
        ["claude"], runner=runner, environ={}, which=lambda name: name,
        executables={"claude": ["C:/coding/claude.exe", "C:/review/claude.exe"]},
    )
    assert calls == [["C:/coding/claude.exe", "auth", "status"], ["C:/review/claude.exe", "auth", "status"]]
    assert report["ok"] is False
    assert report["providers"][1]["auth_mode"] == "api_key"


def test_configured_authentication_never_falls_back_to_a_different_path_cli():
    calls = []
    report = provider_auth.check(
        ["codex"], runner=lambda argv: calls.append(argv), environ={}, require_installed=True,
        which=lambda name: "C:/other/codex.exe" if name == "codex" else None,
        executables={"codex": ["C:/missing/codex.exe"]},
    )
    assert report["ok"] is False and calls == []


def test_driver_authentication_is_bound_to_effective_implementation_and_review_commands(tmp_path, monkeypatch):
    from stagemesh.cli import _real_provider_auth_blockers

    config_dir = tmp_path / ".stagemesh"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "authorization": {"unattended_providers": ["claude"]},
        "providers": {"claude": {
            "command": "C:/coding/claude.exe -p", "review_command": "C:/review/claude.exe -p",
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("STAGEMESH_UNATTENDED", "1")
    monkeypatch.setattr("stagemesh.providers.resolve_executable", lambda *args: None)
    monkeypatch.setattr("stagemesh.providers.shutil.which", lambda name: name if ":/" in name else None)
    seen = {}

    def check(names, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "blockers": []}

    monkeypatch.setattr(provider_auth, "check", check)
    assert _real_provider_auth_blockers(tmp_path) == []
    assert seen["executables"] == {"claude": ("C:/coding/claude.exe", "C:/review/claude.exe")}
    assert seen["require_installed"] is True


def test_driver_authentication_includes_the_distinct_research_executable(tmp_path, monkeypatch):
    from stagemesh.cli import _real_provider_auth_blockers

    config_dir = tmp_path / ".stagemesh"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config = {
        "authorization": {"unattended_providers": ["claude"]},
        "providers": {"claude": "C:/coding/claude.exe -p"},
        "research": {"provider": "claude", "command": "C:/research/claude.exe -p"},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("STAGEMESH_UNATTENDED", "1")
    monkeypatch.setattr("stagemesh.providers.resolve_executable", lambda *args: None)
    monkeypatch.setattr("stagemesh.providers.shutil.which", lambda name: name if ":/" in name else None)
    seen = {}

    def check(names, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "blockers": []}

    monkeypatch.setattr(provider_auth, "check", check)
    assert _real_provider_auth_blockers(tmp_path) == []
    assert seen["executables"]["claude"] == ("C:/coding/claude.exe", "C:/research/claude.exe")
    config["research"] = {"provider": "grok", "command": "C:/research/grok.exe"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert any("research provider grok" in blocker for blocker in _real_provider_auth_blockers(tmp_path))
