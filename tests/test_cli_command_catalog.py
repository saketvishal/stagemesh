from __future__ import annotations

import json
import subprocess

import stagemesh.cli as cli_module
from stagemesh.runtime_freshness import _catalog_commands


def test_default_help_is_curated_without_removing_advanced_commands() -> None:
    parser = cli_module.build_parser()
    text = parser.format_help()

    assert "COMMAND" in text
    assert "{init,doctor," not in text
    for command in ("init", "doctor", "run-window", "status", "queue", "capabilities", "config", "ui", "audit"):
        assert command in text
    for command in ("worker", "queue-run", "recover-implementation-identity", "acceptance-matrix"):
        assert command not in text

    assert parser.parse_args(["worker", "worker-1"]).command == "worker"
    assert parser.parse_args(["queue-run", "--legacy-unsafe", "--concurrency", "1"]).command == "queue-run"


def test_full_help_catalog_groups_every_compatible_command(capsys) -> None:
    parser = cli_module.build_parser()
    catalog = cli_module._command_catalog(parser)

    assert catalog["schema"] == "stagemesh.command-catalog/1"
    assert len(catalog["commands"]) == 72
    assert "uncategorized" not in catalog["groups"]
    grouped = [command for commands in catalog["groups"].values() for command in commands]
    assert len(grouped) == len(set(grouped)) == len(catalog["commands"])

    assert cli_module.main(["--help-all"]) == 0
    text = capsys.readouterr().out
    assert "Everyday project operation" in text
    assert "Task planning, inspection, repair, and recovery" in text
    assert "Automation, distributed-worker, and local service internals" in text
    for command in catalog["commands"]:
        assert command in text


def test_machine_catalog_is_json_and_independent_of_human_help(capsys) -> None:
    assert cli_module.main(["--command-catalog-json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["commands"] == sorted(payload["commands"])
    assert payload["default_help"] == list(cli_module._PRIMARY_COMMAND_HELP)
    assert "worker" in payload["commands"] and "worker" not in payload["default_help"]


def test_runtime_freshness_prefers_catalog_and_falls_back_for_older_installs() -> None:
    calls: list[list[str]] = []

    def current_runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"commands": ["status", "init"]}), "")

    assert _catalog_commands(["stagemesh"], current_runner) == ["init", "status"]
    assert calls == [["stagemesh", "--command-catalog-json"]]

    def legacy_runner(argv, **_kwargs):
        if argv[-1] == "--command-catalog-json":
            return subprocess.CompletedProcess(argv, 2, "", "unknown option")
        return subprocess.CompletedProcess(argv, 0, "usage: stagemesh COMMAND {init,status,worker}", "")

    assert _catalog_commands(["stagemesh"], legacy_runner) == ["init", "status", "worker"]
