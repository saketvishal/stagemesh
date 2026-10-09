from __future__ import annotations

import json

from stagemesh.providers import (
    _command_and_input,
    _provider_text_response,
    approved_default_adapters,
)


def test_grok_provider_prompt_is_passed_as_single_turn_argument() -> None:
    command, stdin = _command_and_input(("grok", "--permission-mode", "acceptEdits"), "do the task")

    assert command == ["grok", "-p", "do the task", "--permission-mode", "acceptEdits"]
    assert stdin == ""


def test_grok_provider_existing_single_flag_gets_prompt_before_options() -> None:
    command, stdin = _command_and_input(("grok", "-p", "--permission-mode", "acceptEdits"), "do the task")

    assert command == ["grok", "-p", "do the task", "--permission-mode", "acceptEdits"]
    assert stdin == ""


def test_grok_review_uses_json_schema_single_turn_mode() -> None:
    command, stdin = _command_and_input(("grok", "--permission-mode", "acceptEdits"), "review it", structured_review=True)

    assert command[:3] == ["grok", "-p", "review it"]
    schema = json.loads(command[command.index("--json-schema") + 1])
    assert schema["properties"]["decision"]["enum"] == ["PASS", "FAIL"]
    assert stdin == ""


def test_grok_json_mode_wrapper_is_normalized_to_review_json() -> None:
    response = _provider_text_response('{"text":"{\\"decision\\":\\"PASS\\"}","stopReason":"end_turn"}')

    assert response == '{"decision":"PASS"}'


def test_agy_provider_prompt_is_passed_as_print_argument() -> None:
    command, stdin = _command_and_input(("agy", "--mode", "accept-edits"), "do the task")

    assert command == ["agy", "--print", "do the task", "--mode", "accept-edits"]
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


def test_grok_default_command_auto_approves_edits() -> None:
    adapters = {adapter.name: adapter.command for adapter in approved_default_adapters()}

    assert "--always-approve" in adapters["grok"]


def test_agy_default_command_does_not_bypass_permissions() -> None:
    adapters = {adapter.name: adapter.command for adapter in approved_default_adapters()}

    assert adapters["agy"] == ("agy", "--mode", "accept-edits")


def test_non_grok_provider_keeps_stdin_prompt() -> None:
    command, stdin = _command_and_input(("codex", "exec", "--sandbox", "workspace-write"), "do the task")

    assert command == ["codex", "exec", "--sandbox", "workspace-write"]
    assert stdin == "do the task"
