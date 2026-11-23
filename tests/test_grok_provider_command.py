from __future__ import annotations

import json
from pathlib import Path

import pytest

import stagemesh.providers as providers_module
from stagemesh.providers import (
    ProviderValidationError,
    _agy_command_with_builtin_skill_access,
    _command_and_input,
    _launch_provider_process,
    _provider_permission_denial,
    _provider_text_response,
    _remove_temporary_provider_settings,
    _review_provider_response,
    approved_default_adapters,
)
from stagemesh.review import VERDICT_REVIEW_INCOMPLETE, classify_review_response


def test_grok_provider_prompt_is_passed_as_single_turn_argument() -> None:
    command, stdin = _command_and_input(("grok", "--permission-mode", "acceptEdits"), "do the task")

    assert command[:5] == ["grok", "-p", "do the task", "--permission-mode", "dontAsk"]
    assert stdin == ""
    assert "--always-approve" not in command
    assert command[command.index("--tools") + 1] == "Read,Grep,Edit,Bash"
    assert "--disable-web-search" in command and "--no-subagents" in command


def test_grok_provider_existing_single_flag_gets_prompt_before_options() -> None:
    command, stdin = _command_and_input(("grok", "-p", "--permission-mode", "acceptEdits"), "do the task")

    assert command[:5] == ["grok", "-p", "do the task", "--permission-mode", "dontAsk"]
    assert stdin == ""


def test_grok_review_uses_json_schema_single_turn_mode() -> None:
    command, stdin = _command_and_input(("grok", "--permission-mode", "acceptEdits"), "review it", structured_review=True)

    assert command[:3] == ["grok", "-p", "review it"]
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["properties"]["decision"]["enum"] == ["PASS", "FAIL_WITH_FINDINGS", "REVIEW_INCOMPLETE"]
    assert command[command.index("--tools") + 1] == "Read,Grep"
    assert "--allow" not in command
    assert stdin == ""


def test_grok_large_review_prompt_uses_a_prompt_file_instead_of_the_windows_command_line(tmp_path: Path) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    command, stdin = _command_and_input(
        ("grok", "-p", "old prompt", "--permission-mode", "acceptEdits"),
        "x" * 40_000,
        structured_review=True,
        prompt_file=prompt_file,
    )

    assert "-p" not in command and "--single" not in command
    assert command[-2:] == ["--prompt-file", str(prompt_file)]
    assert "x" * 100 not in " ".join(command)
    assert stdin == ""


def test_grok_json_mode_wrapper_is_normalized_to_review_json() -> None:
    response = _provider_text_response('{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"end_turn"}')

    assert response == '{"decision":"PASS"}'


def test_cancelled_grok_review_envelope_is_not_a_verdict() -> None:
    response = _review_provider_response('{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"cancelled"}')

    parsed = json.loads(response)
    assert parsed["decision"] == "INFRASTRUCTURE_FAILURE"
    assert parsed["decision"] != "PASS"
    assert parsed["reason"] == "review_provider_cancelled"
    verdict = classify_review_response(response)
    assert verdict.kind != "PASS"
    assert verdict.kind == VERDICT_REVIEW_INCOMPLETE
    assert verdict.provider_failure is True
    assert verdict.reason == "review_provider_cancelled"


def test_completed_grok_review_envelope_still_unwraps_its_verdict() -> None:
    response = _review_provider_response('{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"end_turn"}')

    assert response == '{"decision":"PASS"}'
    assert classify_review_response(response).kind == "PASS"


@pytest.mark.parametrize("stop_reason", ["max_turn_requests", "max_tokens", "refusal", "cancelled"])
def test_unfinished_grok_review_stop_cannot_pass(stop_reason: str) -> None:
    response = _review_provider_response(
        '{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"' + stop_reason + '"}'
    )

    parsed = json.loads(response)
    assert parsed["decision"] == "INFRASTRUCTURE_FAILURE"
    assert parsed["decision"] != "PASS"
    verdict = classify_review_response(response)
    assert verdict.kind != "PASS"
    assert verdict.kind == VERDICT_REVIEW_INCOMPLETE
    assert verdict.provider_failure is True
    if stop_reason == "cancelled":
        assert verdict.reason == "review_provider_cancelled"
    else:
        assert verdict.reason == f"review_provider_stopped:{stop_reason}"


def _grok_tool_values(command: list[str]) -> list[str]:
    return [command[index + 1] for index, token in enumerate(command[:-1]) if token == "--tools"]


def test_restricted_grok_read_only_tools_are_not_widened() -> None:
    command, stdin = _command_and_input(
        ("grok", "--permission-mode", "dontAsk", "--tools", "read_file,grep,list_dir"),
        "review the packet",
        structured_review=True,
    )

    assert stdin == ""
    assert _grok_tool_values(command) == ["read_file,grep,list_dir"]
    assert "--json-schema" in command


def test_equals_form_read_only_grok_tools_are_not_replaced_with_edit_or_bash() -> None:
    command, _stdin = _command_and_input(
        ("grok", "--permission-mode", "dontAsk", "--tools=read_file,grep,list_dir"),
        "review the packet",
        structured_review=True,
    )

    assert not any(token.startswith("--tools=") for token in command)
    assert _grok_tool_values(command) == ["read_file,grep,list_dir"]


def test_equals_form_unsafe_grok_tools_still_use_the_unattended_default() -> None:
    command, _stdin = _command_and_input(
        ("grok", "--tools=read_file,grep,list_dir,run_terminal_cmd"),
        "work",
    )

    assert not any(token.startswith("--tools=") for token in command)
    assert _grok_tool_values(command) == ["Read,Grep,Edit,Bash"]


def test_allow_rules_do_not_widen_a_read_only_grok_tool_list() -> None:
    command, _stdin = _command_and_input(
        (
            "grok",
            "--tools",
            "read_file,grep,list_dir",
            "--allow",
            "Edit",
            "--allow",
            "Bash(git status*)",
            "--allowedTools",
            "Write",
        ),
        "review the packet",
        structured_review=True,
    )

    assert _grok_tool_values(command) == ["read_file,grep,list_dir"]
    assert command.count("--tools") == 1


def test_unsafe_grok_tool_selection_stays_on_the_unattended_default() -> None:
    command, _ = _command_and_input(
        ("grok", "--tools", "read_file,grep,list_dir,run_terminal_cmd"),
        "work",
    )

    assert command[command.index("--tools") + 1] == "Read,Grep,Edit,Bash"


def test_agy_provider_prompt_is_passed_as_stream_json() -> None:
    command, stdin = _command_and_input(("agy", "--mode", "accept-edits"), "do the task")

    assert command[:3] == ["agy", "--mode", "accept-edits"]
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--sandbox" in command and "--disable-slash-commands" in command
    assert json.loads(stdin) == {"event": "user", "message": {"content": "do the task"}}


def test_agy_review_uses_json_schema_stream_mode() -> None:
    command, stdin = _command_and_input(("agy", "--mode", "accept-edits"), "review it", structured_review=True)

    assert command[:3] == ["agy", "--mode", "accept-edits"]
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--output-format") + 1] == "stream-json"
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["required"] == ["decision"]
    assert json.loads(stdin) == {"event": "user", "message": {"content": "review it"}}


@pytest.mark.parametrize("equals_form", [False, True])
def test_agy_gets_ephemeral_read_only_access_to_bundled_skills(
    tmp_path: Path, equals_form: bool
) -> None:
    from stagemesh.agent_skills import SKILL_ROOT

    settings = tmp_path / "agy-settings.json"
    original = {
        "toolPermission": "proceed-in-sandbox",
        "permissions": {
            "allow": ["read_file(.)"],
            "deny": ["read_url(*)"],
            "ask": [],
        },
    }
    settings.write_text(json.dumps(original), encoding="utf-8")
    option = f"--settings={settings}" if equals_form else "--settings"
    command = ["agy", option]
    if not equals_form:
        command.append(str(settings))

    prepared, temporary = _agy_command_with_builtin_skill_access(command, tmp_path)

    assert temporary is not None and temporary.is_file()
    selected = next(
        part.split("=", 1)[1] if part.startswith("--settings=") else prepared[index + 1]
        for index, part in enumerate(prepared)
        if part == "--settings" or part.startswith("--settings=")
    )
    assert Path(selected) == temporary
    derived = json.loads(temporary.read_text(encoding="utf-8"))
    skill_root = SKILL_ROOT.resolve().as_posix()
    assert f"read_file({skill_root})" in derived["permissions"]["allow"]
    assert f"write_file({skill_root})" in derived["permissions"]["deny"]
    skill_files = sorted(
        path.resolve().as_posix()
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert skill_files
    assert all(
        f"read_file({skill_file})" in derived["permissions"]["allow"]
        for skill_file in skill_files
    )
    assert all(
        f"write_file({skill_file})" in derived["permissions"]["deny"]
        for skill_file in skill_files
    )
    assert derived["permissions"]["allow"][0] == "read_file(.)"
    assert "read_file(*)" not in derived["permissions"]["allow"]
    assert json.loads(settings.read_text(encoding="utf-8")) == original

    _remove_temporary_provider_settings(temporary)
    assert not temporary.exists()


def test_non_agy_command_does_not_create_ephemeral_settings(tmp_path: Path) -> None:
    command = ["codex", "exec", "--settings", "unused.json"]

    prepared, temporary = _agy_command_with_builtin_skill_access(command, tmp_path)

    assert prepared == command
    assert temporary is None


def test_agy_inline_settings_receive_the_same_bounded_skill_access(tmp_path: Path) -> None:
    from stagemesh.agent_skills import SKILL_ROOT

    inline = json.dumps(
        {"toolPermission": "proceed-in-sandbox", "permissions": {"allow": [], "deny": [], "ask": []}}
    )

    prepared, temporary = _agy_command_with_builtin_skill_access(
        ["agy", f"--settings={inline}"], tmp_path
    )

    assert temporary is not None
    derived = json.loads(temporary.read_text(encoding="utf-8"))
    skill_root = SKILL_ROOT.resolve().as_posix()
    assert f"read_file({skill_root})" in derived["permissions"]["allow"]
    assert any(
        rule.startswith(f"read_file({skill_root}/")
        for rule in derived["permissions"]["allow"]
    )
    assert prepared[1] == f"--settings={temporary}"
    _remove_temporary_provider_settings(temporary)


def test_agy_skill_access_is_exact_deterministic_and_does_not_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stagemesh import providers

    skill_root = tmp_path / "skills"
    nested = skill_root / "nested"
    nested.mkdir(parents=True)
    first = skill_root / "a.md"
    second = nested / "z.md"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    monkeypatch.setattr(providers, "SKILL_ROOT", skill_root)
    inline = json.dumps(
        {"toolPermission": "proceed-in-sandbox", "permissions": {"allow": [], "deny": [], "ask": []}}
    )

    _prepared, temporary = _agy_command_with_builtin_skill_access(
        ["agy", f"--settings={inline}"], tmp_path
    )

    assert temporary is not None
    derived = json.loads(temporary.read_text(encoding="utf-8"))
    root = skill_root.resolve().as_posix()
    exact_reads = [f"read_file({first.resolve().as_posix()})", f"read_file({second.resolve().as_posix()})"]
    exact_writes = [
        f"write_file({first.resolve().as_posix()})",
        f"write_file({second.resolve().as_posix()})",
    ]
    assert derived["permissions"]["allow"] == [f"read_file({root})", *exact_reads]
    assert derived["permissions"]["deny"] == [f"write_file({root})", *exact_writes]
    assert "read_file(*)" not in derived["permissions"]["allow"]
    _remove_temporary_provider_settings(temporary)


@pytest.mark.parametrize(
    "command",
    [
        ["agy"],
        ["agy", "--settings", "missing.json"],
        ["agy", "--settings", "[]"],
        ["agy", "--settings", '{"permissions":{"allow":"all"}}'],
        ["agy", "--settings", '{"permissions":{"deny":[1]}}'],
        ["agy", "--settings", '{"permissions":{"ask":"all"}}'],
    ],
)
def test_agy_settings_fail_closed_before_provider_launch(
    tmp_path: Path, command: list[str]
) -> None:
    with pytest.raises(ProviderValidationError):
        _agy_command_with_builtin_skill_access(command, tmp_path)


@pytest.mark.parametrize("executable", ["agy.cmd", "agy.bat"])
def test_windows_agy_wrappers_receive_prompt_and_skill_access(
    tmp_path: Path, executable: str
) -> None:
    settings = tmp_path / "agy-settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": [], "deny": [], "ask": []}}),
        encoding="utf-8",
    )

    command, provider_input = _command_and_input(
        (executable, "-p", "--settings", str(settings)), "work"
    )
    prepared, temporary = _agy_command_with_builtin_skill_access(command, tmp_path)

    assert "--print" not in command and "work" not in command
    assert "--settings" in command and str(settings) in command
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert json.loads(provider_input) == {
        "event": "user",
        "message": {"content": "work"},
    }
    assert prepared[0] == executable
    assert temporary is not None and temporary.is_file()
    _remove_temporary_provider_settings(temporary)


def test_interrupted_agy_process_creation_removes_ephemeral_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "agy-settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": [], "deny": [], "ask": []}}),
        encoding="utf-8",
    )
    observed: list[Path] = []

    def interrupt(command: list[str], **_kwargs: object) -> None:
        selected = Path(command[command.index("--settings") + 1])
        assert selected.is_file()
        observed.append(selected)
        raise KeyboardInterrupt("synthetic provider-start interruption")

    monkeypatch.setattr(providers_module.subprocess, "Popen", interrupt)

    with pytest.raises(KeyboardInterrupt):
        _launch_provider_process(["agy", "--settings", str(settings)], tmp_path)

    assert observed and not observed[0].exists()


def test_agy_json_mode_wrapper_is_normalized_to_review_json() -> None:
    response = _provider_text_response('{"structured_output":{"decision":"PASS"},"status":"SUCCESS"}')

    assert response == '{"decision":"PASS"}'


def test_agy_unsuccessful_json_mode_cannot_recover_stale_review_output() -> None:
    response = _provider_text_response(
        '{"structured_output":{"decision":"PASS"},"status":"ERROR"}'
    )

    assert response == ""


def test_structured_denied_actions_are_a_provider_permission_failure() -> None:
    output = '{"status":"SUCCESS","response":"","denied_actions":[{"action":"read_file"}]}'

    assert _provider_permission_denial(output, "") == "provider_permission_denied"


def test_grok_default_command_uses_headless_permissions_and_valid_tool_names() -> None:
    adapters = {adapter.name: adapter.command for adapter in approved_default_adapters()}

    assert "--always-approve" not in adapters["grok"]
    assert adapters["grok"][adapters["grok"].index("--permission-mode") + 1] == "dontAsk"
    assert adapters["grok"][adapters["grok"].index("--tools") + 1] == "Read,Grep,Edit,Bash"


def test_saved_provider_commands_are_hardened_at_invocation_time() -> None:
    grok, _ = _command_and_input(
        ("grok", "--always-approve", "--tools", "default", "--permission-mode", "bypassPermissions"),
        "work",
    )
    claude, _ = _command_and_input(("claude", "-p", "--dangerously-skip-permissions"), "work")
    codex, _ = _command_and_input(("codex", "exec", "--dangerously-bypass-approvals-and-sandbox"), "work")
    agy, _ = _command_and_input(("agy", "--dangerously-skip-permissions"), "work")

    assert "--always-approve" not in grok and "bypassPermissions" not in grok
    assert grok[grok.index("--permission-mode") + 1] == "dontAsk"
    assert grok[grok.index("--tools") + 1] == "Read,Grep,Edit,Bash"
    assert "--dangerously-skip-permissions" not in claude
    assert {"--safe-mode", "--no-chrome", "--strict-mcp-config"} <= set(claude)
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex
    assert {"--approve-for-me", "--ignore-user-config", "--ephemeral"} <= set(codex)
    assert "--dangerously-skip-permissions" not in agy
    assert {"--sandbox", "--disable-slash-commands"} <= set(agy)


def test_explicit_claude_capability_configuration_is_preserved_without_safe_mode() -> None:
    command, _ = _command_and_input(
        ("claude", "-p", "--safe-mode", "--mcp-config", '{"mcpServers":{"project":{}}}'),
        "review",
    )

    assert "--safe-mode" not in command
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{"project":{}}}'
    assert "--strict-mcp-config" in command


def test_agy_default_command_does_not_bypass_permissions() -> None:
    adapters = {adapter.name: adapter.command for adapter in approved_default_adapters()}

    assert adapters["agy"] == (
        "agy", "--mode", "accept-edits", "--sandbox", "--disable-slash-commands",
    )


def test_non_grok_provider_keeps_stdin_prompt() -> None:
    command, stdin = _command_and_input(("codex", "exec", "--sandbox", "workspace-write"), "do the task")

    assert command[:2] == ["codex", "exec"]
    assert "--sandbox" not in command and "workspace-write" not in command
    assert {"--approve-for-me", "--ignore-user-config", "--ephemeral"} <= set(command)
    assert stdin == "do the task"


@pytest.mark.parametrize(
    "legacy",
    [
        ("codex", "exec", "--sandbox", "workspace-write"),
        ("codex", "exec", "--sandbox=workspace-write"),
        ("codex", "exec", "-s", "workspace-write"),
        ("codex", "exec", "-s=workspace-write"),
    ],
)
def test_codex_legacy_sandbox_is_migrated_to_bounded_unattended_approval(
    legacy: tuple[str, ...],
) -> None:
    command, _ = _command_and_input(legacy, "review the candidate")

    assert "--approve-for-me" in command
    assert not any(
        token in {"--sandbox", "-s", "workspace-write"}
        or token.startswith(("--sandbox=", "-s="))
        for token in command
    )
