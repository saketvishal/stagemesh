"""CLI provider cooldown: quota/auth/rate/no-progress rest the provider (not the task); visible, clearable, expiring."""
from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path

import pytest
from test_provider_pool import TASK, Rig

import stagemesh.cli as cli_module
from stagemesh.domain import Stage, TaskStatus
from stagemesh.execution import classify_failure
from stagemesh.provider_cooldown import (
    NO_PROGRESS_THRESHOLD,
    active_cooldowns,
    clear_cooldown,
    format_cooldowns,
    no_progress_streak,
)
from stagemesh.provider_pool import IMPLEMENT, REVIEW

COOLDOWN = 900.0


def _rig(tmp_path: Path, modes: dict[str, str]) -> Rig:
    return Rig(tmp_path, modes, pools={IMPLEMENT: tuple(modes), REVIEW: tuple(modes)})


def _cli(rig: Rig, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(rig.project), *argv])
    return code, out.getvalue()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Your credit balance is too low to access the API", "quota_rate_limit"),
        ("Error: please log in to continue", "authentication_failure"),
        ("Your session has expired, please sign in again", "authentication_failure"),
        ("429 Too Many Requests: rate limit exceeded", "quota_rate_limit"),
        ("authentication_error: not logged in", "authentication_failure"),
        ("503 Service Unavailable", "transient_provider_failure"),
    ],
)
def test_provider_level_failures_are_classified_for_cooldown(message: str, expected: str) -> None:
    assert classify_failure(1, stderr=message) == (True, expected)


def test_ordinary_code_failures_are_not_provider_cooldown_material() -> None:
    assert classify_failure(1, stderr="AssertionError: expected 1 got 2") == (False, "implementation_failure")


def test_auth_failure_enters_cooldown_with_a_full_record_and_the_other_provider_carries_on(tmp_path: Path) -> None:
    rig = _rig(tmp_path, {"provider-a": "auth-fail", "provider-b": "ok"})

    before = time.time()
    rig.tick(2)  # PLAN, IMPLEMENT: provider-a is out of login, provider-b implements

    candidate = rig.store.latest_candidate(TASK)
    assert candidate is not None and candidate["produced_by"] == "provider-b"
    (entry,) = active_cooldowns(rig.store, COOLDOWN)
    assert entry["provider"] == "provider-a" and entry["failure_class"] == "authentication_failure"
    assert entry["stages"] == ["IMPLEMENT"] and entry["first_seen"] >= before - 1 and entry["last_seen"] >= entry["first_seen"]
    assert "not logged in" in entry["last_error"] and entry["seconds_remaining"] > 0
    assert "log in" in entry["action"] and entry["clear_command"] == "stagemesh cooldown clear --provider provider-a"
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN  # the task is not poisoned or blocked
    assert not any(v.eligible for v in rig.pool.evaluate(rig.store, IMPLEMENT, TASK) if v.provider == "provider-a")


def test_quota_cooldown_records_the_reset_hint_and_is_skipped_for_other_tasks(tmp_path: Path) -> None:
    rig = _rig(tmp_path, {"provider-a": "weekly-limit", "provider-b": "ok"})
    rig.tick(2)

    (entry,) = active_cooldowns(rig.store, COOLDOWN)
    assert entry["failure_class"] == "quota_rate_limit" and "resets 1am" in str(entry["reset_hint"])
    assert "wait for the reset (" in entry["action"]
    verdicts = {v.provider: v for v in rig.pool.evaluate(rig.store, IMPLEMENT, None)}  # no task id: the cooldown is provider-wide
    assert not verdicts["provider-a"].eligible and verdicts["provider-a"].reason.startswith("provider_cooldown: quota_rate_limit")
    assert verdicts["provider-b"].eligible


def test_repeated_no_progress_rests_the_provider_but_a_single_one_does_not(tmp_path: Path) -> None:
    rig = _rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    for index in range(NO_PROGRESS_THRESHOLD):
        assert active_cooldowns(rig.store, COOLDOWN) == []
        rig.pool.record_no_progress(rig.store, IMPLEMENT, TASK, "provider-a", next_provider="provider-b", sequence=["provider-a"])
        assert no_progress_streak(rig.store, "provider-a") == (index + 1 if index + 1 < NO_PROGRESS_THRESHOLD else 0)

    (entry,) = active_cooldowns(rig.store, COOLDOWN)
    assert entry["provider"] == "provider-a" and entry["failure_class"] == "repeated_no_progress"
    assert "keeps returning without changes" in entry["action"]
    verdict = next(v for v in rig.pool.evaluate(rig.store, IMPLEMENT, None) if v.provider == "provider-a")
    assert not verdict.eligible and "repeated_no_progress" in verdict.reason
    assert rig.store.get_task(TASK)["status"] == TaskStatus.OPEN  # a rested provider is not a task defect


def test_all_providers_in_cooldown_says_so_clearly_and_burns_no_attempts(tmp_path: Path) -> None:
    rig = _rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    rig.tick(1)  # PLAN
    for name in ("provider-a", "provider-b"):
        rig.pool.record_failure(rig.store, IMPLEMENT, TASK, name, "authentication_failure", provider_output="not logged in")
    executions = rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]

    ok, message, impl, _review = rig.pool.preflight(rig.store, TASK)
    rig.tick(3)

    assert not ok and "no implementation provider is available" in message
    assert message.count("provider_cooldown: authentication_failure") == 2 and "stagemesh cooldown clear" in message
    assert rig.store.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == executions  # no provider was launched
    task = rig.store.get_task(TASK)
    assert task["status"] == TaskStatus.OPEN and task["stage"] == Stage.IMPLEMENT
    assert rig.store.task_remediation_count(TASK, "IMPLEMENT") == 0  # nothing charged to the task


def test_cooldown_is_visible_in_status_doctor_cooldown_command_and_run_banner(tmp_path: Path) -> None:
    rig = _rig(tmp_path, {"provider-a": "auth-fail", "provider-b": "ok"})
    rig.tick(2)

    _, status_text = _cli(rig, "status")
    _, status_json = _cli(rig, "status", "--json")
    _, doctor_text = _cli(rig, "doctor")
    _, doctor_json = _cli(rig, "doctor", "--json")
    _, listing = _cli(rig, "cooldown")
    _, listing_json = _cli(rig, "cooldown", "list", "--json")

    for text in (status_text, doctor_text, listing):
        assert "provider cooldown provider-a: authentication_failure" in text
        assert "log in to the provider CLI" in text and "stagemesh cooldown clear --provider provider-a" in text
    for payload in (json.loads(status_json), json.loads(doctor_json), json.loads(listing_json)):
        assert payload["provider_cooldowns"][0]["provider"] == "provider-a"
    assert "provider cooldown provider-a" in "\n".join(format_cooldowns(active_cooldowns(rig.store, COOLDOWN)))


def test_cooldown_can_be_cleared_after_login_and_expires_on_its_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig(tmp_path, {"provider-a": "auth-fail", "provider-b": "ok"})
    rig.tick(2)
    assert active_cooldowns(rig.store, COOLDOWN)

    code, text = _cli(rig, "cooldown", "clear", "--provider", "provider-a")  # the operator logged in

    assert code == 0 and "cleared provider cooldown: provider-a" in text
    assert active_cooldowns(rig.store, COOLDOWN) == []
    assert next(v for v in rig.pool.evaluate(rig.store, IMPLEMENT, TASK) if v.provider == "provider-a").eligible
    assert _cli(rig, "cooldown")[1].strip() == "no provider is in cooldown"
    assert clear_cooldown(rig.store, None, COOLDOWN) == []  # nothing left to clear

    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "provider-a", "authentication_failure")  # fails again after the clear
    assert [item["provider"] for item in active_cooldowns(rig.store, COOLDOWN)] == ["provider-a"]
    assert active_cooldowns(rig.store, COOLDOWN, now=time.time() + COOLDOWN + 1) == []  # deterministic expiry
    monkeypatch.setattr("stagemesh.provider_pool.time.time", lambda: time.time_ns() / 1e9 + COOLDOWN + 1)
    assert next(v for v in rig.pool.evaluate(rig.store, IMPLEMENT, TASK) if v.provider == "provider-a").eligible
