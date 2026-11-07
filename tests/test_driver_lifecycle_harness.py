from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from stagemesh.execution import popen_session_kwargs
from stagemesh.persistence import Store

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows-acceptance" / "driver_lifecycle.py"
MOCK = ROOT / "scripts" / "windows-acceptance" / "mock_provider.py"
spec = importlib.util.spec_from_file_location("driver_lifecycle", SCRIPT)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)


def test_mock_provider_commands_have_distinct_review_identities(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".stagemesh").mkdir(parents=True)

    lifecycle.write_providers(project, tmp_path / "calls.jsonl")

    providers = json.loads((project / ".stagemesh" / "config.json").read_text(encoding="utf-8"))[
        "providers"
    ]
    commands = {name: value["command"] for name, value in providers.items()}
    assert len(set(commands.values())) == len(commands)
    assert '--identity "mock-review-a" --role review' in commands["mock-review-a"]
    assert '--identity "mock-review-b" --role review' in commands["mock-review-b"]


def test_mock_provider_records_the_explicit_identity_and_role(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"

    result = subprocess.run(
        [
            sys.executable,
            str(MOCK),
            "--calls",
            str(calls),
            "--identity",
            "mock-review-b",
            "--role",
            "review",
        ],
        input='{"decision":"PASS"}',
        text=True,
        capture_output=True,
        check=False,
        **popen_session_kwargs(),
    )

    record = json.loads(calls.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "PASS"
    assert record["identity"] == "mock-review-b" and record["stage"] == "review"


def test_terminal_block_detection_preserves_task_state_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    store.upsert_task("one", source_id="ONE", project=str(project))
    store.upsert_task("two", source_id="TWO", project=str(project))
    store.block_task("ONE")
    store.close()

    assert lifecycle.all_remaining_tasks_blocked(project) is None

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.block_task("TWO")
    store.close()
    snapshot = lifecycle.all_remaining_tasks_blocked(project)

    assert snapshot is not None
    assert {task["status"] for task in snapshot["tasks"]} == {"BLOCKED"}


def test_initial_only_cannot_weaken_scheduler_acceptance(monkeypatch) -> None:
    monkeypatch.setenv("STAGEMESH_ACCEPT_DRIVER", "1")

    assert lifecycle.run(False, 1, initial_only=True) == 2
