"""Runtime stabilization matrix: `stagemesh continue` against a fake provider CLI, in seconds, with no real provider or project.

Run it with `python scripts/stabilization_gate.py` (the default StageMesh stabilization gate) or `pytest tests/test_stabilization_matrix.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from stabilization_support import Matrix

IMPL = ("codex", "claude")  # implementation pool; review is always done by grok unless a scenario says otherwise
REVIEW = ("grok",)


@pytest.fixture(autouse=True)
def fast_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("stagemesh.providers.provider_timeout_seconds", lambda seconds=None: 1.5)


def _matrix(tmp_path: Path, tasks: list[str], plan: dict | None = None, **kwargs) -> Matrix:
    return Matrix(tmp_path, tasks, plan, implement_pool=kwargs.pop("implement_pool", IMPL), review_pool=kwargs.pop("review_pool", REVIEW), **kwargs)


def test_implementation_success(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1"])

    r = m.continue_()

    assert r.code == 0 and r.data["stop_reason"] == "DONE", r.brief
    assert m.integrated("T-1")
    assert r.calls == [("codex", "implement", "T-1", "ok"), ("grok", "review", "T-1", "pass")]
    r.assert_hands_off()


def test_no_implementation_change_falls_through_without_poisoning_the_provider(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1", "T-2"], {"codex": {"implement:T-1": "noop"}})

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1") and m.integrated("T-2"), r.brief
    assert r.implementers("T-1") == ["codex", "claude"]
    assert m.audit("provider.no_progress")[0]["provider"] == "codex"
    assert not m.audit("provider.failure")  # no-progress is not a failure cooldown
    assert r.implementers("T-2")[0] == "codex"  # codex is still first in line for the next task
    r.assert_hands_off()


def test_provider_timeout_falls_through_and_is_scoped_to_the_task(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1", "T-2"], {"codex": {"implement:T-1": "timeout"}})

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1") and m.integrated("T-2"), r.brief
    assert r.implementers("T-1") == ["codex", "claude"]
    assert [e["reason"] for e in m.audit("provider.failure")] == ["provider_timeout"]
    assert r.implementers("T-2")[0] == "codex"  # a timeout on T-1 does not cool codex for T-2
    r.assert_hands_off()


def test_quota_failure_falls_through_and_cools_only_that_stage(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1", "T-2"], {"codex": {"implement": "quota"}}, review_pool=("codex", "grok"))

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1") and m.integrated("T-2"), r.brief
    assert r.implementers("T-1") == ["codex", "claude"]
    assert r.implementers("T-2") == ["claude"]  # real capacity loss: codex is not retried for implementation
    assert [e["reason"] for e in m.audit("provider.failure")] == ["quota_rate_limit"]
    assert ("codex", "review", "T-1", "pass") in r.calls  # ...but it is still a usable reviewer
    r.assert_hands_off()


def test_workspace_mutation_is_quarantined_and_falls_through_without_cooldown(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1", "T-2"], {"codex": {"implement:T-1": "mutate"}})
    head_before = m.git.run("rev-parse", "HEAD").stdout.strip()

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1") and m.integrated("T-2"), r.brief
    assert r.implementers("T-1") == ["codex", "claude"]
    (quarantine,) = m.audit("provider.workspace_quarantined")
    assert quarantine["task_id"] == "T-1" and quarantine["provider"] == "codex"
    assert not m.audit("provider.failure")  # integrity failure is not a provider failure
    assert r.implementers("T-2")[0] == "codex"
    assert m.git.run("rev-parse", "HEAD").stdout.strip() == head_before  # the project checkout never moved
    r.assert_hands_off()


def test_validation_failure_is_remediated_automatically(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1"], {"codex": {"implement": ["bad", "ok"]}}, implement_pool=("codex",))

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1"), r.brief
    assert [mode for p, stage, _, mode in r.calls if stage == "implement"] == ["bad", "ok"]
    store = m.store()
    assert store.task_remediation_count("T-1", "VALIDATE") == 1
    store.close()
    r.assert_hands_off()


def test_review_failure_is_remediated_automatically(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1"], {"grok": {"review": ["fail", "pass"]}}, implement_pool=("codex",))

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1"), r.brief
    assert [mode for p, stage, _, mode in r.calls if stage == "review"] == ["fail", "pass"]
    assert len(r.implementers("T-1")) == 2
    r.assert_hands_off()


def test_fallback_walks_quota_then_no_progress_then_success(tmp_path: Path) -> None:
    plan = {"codex": {"implement": "quota"}, "claude": {"implement": "noop"}}
    m = _matrix(tmp_path, ["T-1"], plan, implement_pool=("codex", "claude", "grok"), review_pool=("codex",))

    r = m.drain()

    assert r.stop == "REFUSED:no_eligible_task" and m.integrated("T-1"), r.brief
    assert r.implementers("T-1") == ["codex", "claude", "grok"]
    assert ("codex", "review", "T-1", "pass") in r.calls  # an implementation quota does not stop codex from reviewing
    r.assert_hands_off()


def test_exhausted_providers_move_on_to_another_task_then_recover_it_once(tmp_path: Path) -> None:
    plan = {name: {"implement:T-1": "noop"} for name in ("codex", "claude")}
    m = _matrix(tmp_path, ["T-1", "T-2"], plan)

    first = m.continue_()

    assert first.stop == "DONE" and first.data["task_id"] == "T-2" and m.integrated("T-2"), first.brief
    assert first.implementers("T-1") == ["codex", "claude"]  # every provider tried once, then on to the next task
    assert [item["task_id"] for item in first.data["detail"]["continued_after_provider_exhaustion"]] == ["T-1"]
    first.assert_hands_off()

    second = m.continue_()  # nothing fresh is left: the exhausted task gets exactly one bounded automatic retry

    assert second.data["selection"]["mode"] == "auto_recovery" and second.data["task_id"] == "T-1", second.brief
    retried = second.implementers("T-1")[2:]  # attempts beyond the first run
    assert retried and len(retried) <= 4 and set(retried) == {"codex", "claude"}  # bounded: a pass or two over the pool, then stop
    assert second.stop != "DONE"
    assert not m.integrated("T-1") and m.task("T-1")["status"] != "DONE"
    second.assert_hands_off()


def test_all_providers_exhausted_everywhere_stops_with_a_hands_off_message(tmp_path: Path) -> None:
    plan = {name: {"implement": "quota"} for name in ("codex", "claude")}
    m = _matrix(tmp_path, ["T-1"], plan)

    r = m.continue_()

    assert r.stop != "DONE" and not m.integrated("T-1"), r.brief
    assert sorted(r.implementers("T-1")) == ["claude", "codex"]
    assert m.audit("task.capacity_failure")[0]["reason"].startswith("all_implementation_providers_failed")
    r.assert_hands_off()
    again = m.continue_()  # cooled providers are not hammered again
    assert again.implementers("T-1") == r.implementers("T-1")
    again.assert_hands_off()


def test_every_provider_mutating_one_task_blocks_it_and_moves_on_in_the_same_run(tmp_path: Path) -> None:
    plan = {name: {"implement:T-1": "mutate"} for name in ("codex", "claude")}
    m = _matrix(tmp_path, ["T-1", "T-2"], plan)

    r = m.continue_()

    assert r.stop == "DONE" and r.data["task_id"] == "T-2" and m.integrated("T-2"), r.brief
    assert not m.integrated("T-1") and m.task("T-1")["status"] == "BLOCKED"  # unsafe output is never adopted
    assert [item["task_id"] for item in r.data["detail"]["continued_after_task_block"]] == ["T-1"]
    assert {e["provider"] for e in m.audit("provider.workspace_quarantined")} == {"codex", "claude"}
    assert not m.audit("provider.failure")  # integrity failures never cool a provider
    r.assert_hands_off()


def test_blocked_task_stays_the_reported_stop_when_nothing_else_can_run(tmp_path: Path) -> None:
    plan = {name: {"implement": "mutate"} for name in ("codex", "claude")}
    m = _matrix(tmp_path, ["T-1"], plan)

    r = m.continue_()

    assert r.stop == "BLOCKED" and r.data["task_id"] == "T-1", r.brief
    assert "workspace integrity failed" in r.data["message"]
    r.assert_hands_off()


def test_stale_remediation_is_recovered_automatically_only_when_no_fresh_task_exists(tmp_path: Path) -> None:
    m = _matrix(tmp_path, ["T-1", "T-2"], {"codex": {"implement:T-1": ["bad", "ok"]}}, implement_pool=("codex",))

    interrupted = m.continue_("--task", "T-1", "--max-steps", "3")  # implement (bad candidate), validate (fails), stop

    assert interrupted.stop == "MAX_STEPS" and m.task("T-1")["stage"] == "IMPLEMENT", interrupted.brief
    store = m.store()
    assert store.task_remediation_count("T-1", "VALIDATE") == 1
    store.close()

    fresh = m.continue_()  # a fresh task wins over the stale one

    assert fresh.stop == "DONE" and fresh.data["task_id"] == "T-2", fresh.brief
    assert any("remediation pending after failed VALIDATE" in item["reason"] for item in fresh.data["selection"]["skipped"])
    assert not m.integrated("T-1")
    fresh.assert_hands_off()

    recovered = m.continue_()  # nothing fresh is left: exactly one stale task is recovered, with no operator step

    assert recovered.stop == "DONE" and recovered.data["task_id"] == "T-1", recovered.brief
    assert recovered.data["selection"]["mode"] == "auto_recovery"
    assert m.integrated("T-1") and m.integrated("T-2")
    recovered.assert_hands_off()
