from __future__ import annotations

import json

from stagemesh.providers import (
    _command_and_input,
    _provider_permission_denial,
    _provider_text_response,
    approved_default_adapters,
)


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
    assert stdin == ""


def test_grok_json_mode_wrapper_is_normalized_to_review_json() -> None:
    response = _provider_text_response('{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"end_turn"}')

    assert response == '{"decision":"PASS"}'


def test_agy_provider_prompt_is_passed_as_print_argument() -> None:
    command, stdin = _command_and_input(("agy", "--mode", "accept-edits"), "do the task")

    assert command[:5] == ["agy", "--print", "do the task", "--mode", "accept-edits"]
    assert "--sandbox" in command and "--disable-slash-commands" in command
    assert stdin == ""


def test_agy_review_uses_json_schema_print_mode() -> None:
    command, stdin = _command_and_input(("agy", "--mode", "accept-edits"), "review it", structured_review=True)

    assert command[:3] == ["agy", "--print", "review it"]
    assert command[command.index("--output-format") + 1] == "json"
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["required"] == ["decision"]
    assert stdin == ""


def test_agy_json_mode_wrapper_is_normalized_to_review_json() -> None:
    response = _provider_text_response('{"structured_output":{"decision":"PASS"},"status":"SUCCESS"}')

    assert response == '{"decision":"PASS"}'


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
    assert {"--ignore-user-config", "--ephemeral"} <= set(codex)
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

    assert command[:4] == ["codex", "exec", "--sandbox", "workspace-write"]
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert stdin == "do the task"
