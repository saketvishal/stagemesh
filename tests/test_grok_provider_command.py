from __future__ import annotations

from stagemesh.providers import _command_and_input


def test_grok_provider_prompt_is_passed_as_single_turn_argument() -> None:
    command, stdin = _command_and_input(("grok", "--permission-mode", "acceptEdits"), "do the task")

    assert command == ["grok", "-p", "do the task", "--permission-mode", "acceptEdits"]
    assert stdin == ""


def test_grok_provider_existing_single_flag_gets_prompt_before_options() -> None:
    command, stdin = _command_and_input(("grok", "-p", "--permission-mode", "acceptEdits"), "do the task")

    assert command == ["grok", "-p", "do the task", "--permission-mode", "acceptEdits"]
    assert stdin == ""


def test_non_grok_provider_keeps_stdin_prompt() -> None:
    command, stdin = _command_and_input(("codex", "exec", "--sandbox", "workspace-write"), "do the task")

    assert command == ["codex", "exec", "--sandbox", "workspace-write"]
    assert stdin == "do the task"
