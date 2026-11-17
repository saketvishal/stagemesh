from __future__ import annotations

import contextlib
import io
import json
import shlex
from pathlib import Path

import pytest
from test_provider_pool import TASK, Rig

import stagemesh.cli as cli_module
from stagemesh.operator_providers import ProviderStatusProjection
from stagemesh.persistence import Store
from stagemesh.provider_cooldown import active_cooldowns, clear_cooldown
from stagemesh.provider_pause import is_provider_paused, operator_pause_rows
from stagemesh.provider_pool import IMPLEMENT, REVIEW


def _configure(rig: Rig) -> None:
    providers = {
        name: {
            "command": shlex.join(adapter.command),
            "capabilities": ["IMPLEMENT", "REVIEW"],
        }
        for name, adapter in rig.pool.adapters.items()
    }
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps(
            {
                "providers": providers,
                "authorization": {"unattended_providers": list(providers)},
                "routing": {
                    "pools": {
                        "IMPLEMENT": list(providers),
                        "REVIEW": list(providers),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _cli(rig: Rig, *argv: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli_module.main(["--project", str(rig.project), *argv])
    return code, stdout.getvalue(), stderr.getvalue()


def test_pause_is_durable_idempotent_and_case_insensitive(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    _configure(rig)

    assert _cli(rig, "provider", "pause", "PROVIDER-A")[0] == 0
    assert _cli(rig, "provider", "pause", "provider-a")[0] == 0
    reopened = Store(rig.store.db_path)
    try:
        assert is_provider_paused(reopened, "provider-a")
        assert operator_pause_rows(reopened, ["provider-a"])[0]["paused"] is True
    finally:
        reopened.close()
    code, output, _ = _cli(rig, "provider", "status", "--json")
    assert code == 0
    assert json.loads(output)["providers"][0]["paused"] is True
    assert _cli(rig, "provider", "resume", "provider-a")[0] == 0
    assert _cli(rig, "provider", "resume", "provider-a")[0] == 0
    assert not is_provider_paused(rig.store, "provider-a")
    code, _output, error = _cli(rig, "provider", "pause", "missing")
    assert code == 2 and error.strip() == "unknown provider: missing"


def test_paused_provider_is_skipped_for_implementation_and_review(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok", "provider-c": "ok"})
    _configure(rig)
    assert _cli(rig, "provider", "pause", "provider-a")[0] == 0

    for stage in (IMPLEMENT, REVIEW):
        verdicts = {item.provider: item for item in rig.pool.evaluate(rig.store, stage, TASK)}
        assert not verdicts["provider-a"].eligible
        assert verdicts["provider-a"].reason.startswith("paused_by_operator:")
        assert verdicts["provider-b"].eligible
        assert verdicts["provider-c"].eligible
    ok, _message, _implementation, _review = rig.pool.preflight(rig.store, TASK)
    assert ok is True


def test_pause_and_cooldown_controls_are_independent(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    _configure(rig)
    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "provider-a", "authentication_failure")
    assert _cli(rig, "provider", "pause", "provider-a")[0] == 0
    assert _cli(rig, "provider", "resume", "provider-a")[0] == 0
    assert active_cooldowns(rig.store, 900)
    assert _cli(rig, "provider", "pause", "provider-a")[0] == 0
    clear_cooldown(rig.store, "provider-a", 900)
    assert is_provider_paused(rig.store, "provider-a")


@pytest.mark.parametrize(
    "command",
    [
        ("continue", "--task", TASK, "--provider", "provider-a", "--max-steps", "1"),
        ("run-ready", "--task", TASK, "--provider", "provider-a", "--max-steps", "1"),
        ("queue-run", "--legacy-unsafe", "--concurrency", "1", "--provider", "provider-a"),
    ],
)
def test_explicit_provider_refuses_before_execution_and_reporting_is_separate(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    _configure(rig)
    assert _cli(rig, "provider", "pause", "provider-a")[0] == 0
    before = rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]

    code, output, error = _cli(rig, *command)

    assert code == 2, (output, error)
    assert "stagemesh provider resume provider-a" in error
    assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == before
    for report_command in (("status", "--json"), ("doctor", "--json", "--no-network"), ("capacity", "--json")):
        code, output, _ = _cli(rig, *report_command)
        assert code == 0
        assert "provider-a" in output and "operator_pause" in output
    projection = ProviderStatusProjection(rig.project).serialize(rig.store)
    provider = next(row for row in projection["providers"] if row["name"] == "provider-a")
    assert provider["operator_pause"] == {"paused": True}
