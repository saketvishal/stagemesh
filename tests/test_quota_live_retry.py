from __future__ import annotations

import time
from dataclasses import replace

import pytest

from stagemesh.domain import ExecutionStatus
from stagemesh.provider_acceptance import (
    LiveProviderSmokeResult,
    live_acceptance_status,
    live_provider_readiness,
    record_live_provider_acceptance,
    refresh_expired_quota_checks,
)
from stagemesh.provider_cooldown import DEFAULT_QUOTA_COOLDOWN_SECONDS
from stagemesh.provider_pool import IMPLEMENT, REVIEW

from test_provider_pool import TASK, Rig, _live_result


def quota_failure(rig: Rig, *, age: float, reason: str = "quota_rate_limit") -> None:
    payload = LiveProviderSmokeResult(
        "FAIL", "provider-a", ExecutionStatus.FAILED, None,
        False, True, True, False, False, False, reason,
    ).to_payload()
    payload["checked_at"] = time.time() - age
    rig.store.add_audit_event("provider.live_acceptance", payload)


@pytest.mark.parametrize("stage", [IMPLEMENT, REVIEW])
def test_expired_quota_check_requires_fresh_probe_before_routing(tmp_path, monkeypatch, stage):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    quota_failure(rig, age=DEFAULT_QUOTA_COOLDOWN_SECONDS + 10)
    try:
        assert not rig.pool.evaluate(rig.store, stage, TASK)[0].eligible
        status = live_acceptance_status(rig.store, "provider-a")
        assert status["state"] == "retry_due"
        assert live_provider_readiness(rig.store, "provider-a", "code")[0] is False
        probes = []
        def smoke(adapter, **kwargs):
            probes.append(adapter.name)
            return _live_result(adapter.name, True)
        monkeypatch.setattr("stagemesh.provider_acceptance.run_live_provider_smoke", smoke)
        refresh_expired_quota_checks(rig.store, rig.project, list(rig.pool.adapters.values()))
        refresh_expired_quota_checks(rig.store, rig.project, list(rig.pool.adapters.values()))
        assert probes == ["provider-a"]
        assert rig.pool.evaluate(rig.store, stage, TASK)[0].eligible
        # A new quota refusal immediately reinstates the normal cooldown.
        rig.pool.record_failure(rig.store, stage, TASK, "provider-a", "quota_rate_limit")
        verdict = rig.pool.evaluate(rig.store, stage, TASK)[0]
        assert not verdict.eligible
        assert "cooldown" in verdict.reason
    finally:
        rig.store.close()


@pytest.mark.parametrize("reason,age", [
    ("quota_rate_limit", 60),
    ("authentication_failure", 8 * 86400),
    ("provider_timeout", 8 * 86400),
    ("invalid_review_json", 8 * 86400),
])
def test_quota_retry_does_not_bypass_fresh_or_nonquota_failures(tmp_path, reason, age):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    quota_failure(rig, age=age, reason=reason)
    try:
        assert not rig.pool.evaluate(rig.store, IMPLEMENT, TASK)[0].eligible
    finally:
        rig.store.close()


def test_strict_acceptance_does_not_treat_expired_quota_as_a_pass(tmp_path):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=True)
    quota_failure(rig, age=8 * 86400)
    try:
        verdict = rig.pool.evaluate(rig.store, IMPLEMENT, TASK)[0]
        assert not verdict.eligible
        assert verdict.reason.startswith("live_acceptance_retry_due")
        assert "provider-acceptance --live --provider provider-a" in verdict.reason
    finally:
        rig.store.close()


def test_quota_retry_preserves_independence_and_is_read_only(tmp_path):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    quota_failure(rig, age=8 * 86400)
    record_live_provider_acceptance(rig.store, _live_result("provider-a", True))
    try:
        before = rig.store.conn.total_changes
        verdict = rig.pool.evaluate(rig.store, REVIEW, TASK, "provider-a")[0]
        assert not verdict.eligible
        assert verdict.reason.startswith("not_independent")
        assert rig.store.conn.total_changes == before
    finally:
        rig.store.close()


def test_fresh_quota_recheck_failure_renews_backoff(tmp_path, monkeypatch):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    quota_failure(rig, age=8 * 86400)
    calls = []
    def smoke(adapter, **kwargs):
        calls.append(adapter.name)
        return LiveProviderSmokeResult(
            "FAIL", adapter.name, ExecutionStatus.FAILED, None,
            False, True, True, False, False, False, "quota_rate_limit",
        )
    monkeypatch.setattr("stagemesh.provider_acceptance.run_live_provider_smoke", smoke)
    try:
        for _ in range(2):
            refresh_expired_quota_checks(rig.store, rig.project, list(rig.pool.adapters.values()))
        assert calls == ["provider-a"]
        assert live_acceptance_status(rig.store, "provider-a")["state"] == "failed"
        assert not rig.pool.evaluate(rig.store, IMPLEMENT, TASK)[0].eligible
    finally:
        rig.store.close()


def test_active_cooldown_prevents_recheck(tmp_path, monkeypatch):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    quota_failure(rig, age=8 * 86400)
    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "provider-a", "quota_rate_limit")
    def no_probe(*args, **kwargs):
        raise AssertionError("an active cooldown must not contact the provider")
    monkeypatch.setattr("stagemesh.provider_acceptance.run_live_provider_smoke", no_probe)
    try:
        before = rig.store.conn.total_changes
        refresh_expired_quota_checks(rig.store, rig.project, list(rig.pool.adapters.values()))
        assert rig.store.conn.total_changes == before
    finally:
        rig.store.close()


@pytest.mark.parametrize("real_window,stale_runtime,missing_merge,expected", [
    (False, False, False, 0), (True, False, False, 1),
    (True, True, False, 0), (True, False, True, 0),
])
def test_only_admissible_real_window_preflight_refreshes(tmp_path, monkeypatch, real_window, stale_runtime, missing_merge, expected):
    import stagemesh.run_window as rw
    from test_run_window import FRESH, STALE, window

    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, [])
    calls = []
    def providers(store, config, *, strict_live):
        return rw.Check("providers", True, "fake providers")
    monkeypatch.setattr(rw, "provider_pool_check", providers)
    monkeypatch.setattr("stagemesh.provider_acceptance.refresh_expired_quota_checks", lambda *args, **kwargs: calls.append(True))
    try:
        runtime = STALE if stale_runtime else FRESH
        if missing_merge:
            options = replace(options, auto_merge_ff=True, merge_policy="stagemesh", required_checks=("backend",), check_app_id=123)
        if real_window:
            kwargs.update(runtime=runtime, provider_check=providers)
            rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)
        else:
            rw.preflight(
                store, project, config, options, base_ref="refs/heads/main",
                runtime=runtime, provider_check=providers, repo_state=hooks.repo_state,
            )
        assert len(calls) == expected
    finally:
        store.close()


@pytest.mark.parametrize("stop_kind", ["deadline", "pause"])
def test_quota_refresh_stops_before_next_provider(tmp_path, monkeypatch, stop_kind):
    rig = Rig(tmp_path, {"provider-a": "ok", "provider-b": "ok"})
    quota_failure(rig, age=8 * 86400)
    payload = _live_result("provider-b", False).to_payload()
    payload.update(failure_reason="quota_rate_limit", checked_at=time.time() - 8 * 86400)
    rig.store.add_audit_event("provider.live_acceptance", payload)
    clock = [0.0]
    stopped = [False]
    calls = []
    def smoke(adapter, *, timeout_seconds, should_stop):
        calls.append(adapter.name)
        assert timeout_seconds <= 5
        if stop_kind == "deadline":
            clock[0] = 11
        else:
            stopped[0] = True
        assert should_stop()
        return _live_result(adapter.name, True)
    monkeypatch.setattr("stagemesh.provider_acceptance.run_live_provider_smoke", smoke)
    try:
        refresh_expired_quota_checks(
            rig.store, rig.project, list(rig.pool.adapters.values()),
            deadline=10, clock=lambda: clock[0], should_stop=lambda: stopped[0],
        )
        assert calls == ["provider-a"]
        assert live_acceptance_status(rig.store, "provider-b")["state"] == "retry_due"
    finally:
        rig.store.close()


def test_quota_retry_preserves_a_passing_capability(tmp_path):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=True)
    payload = LiveProviderSmokeResult(
        "FAIL", "provider-a", ExecutionStatus.FAILED, "candidate",
        True, True, True, True, False, False, "quota_rate_limit",
    ).to_payload()
    payload["checked_at"] = time.time() - DEFAULT_QUOTA_COOLDOWN_SECONDS - 10
    rig.store.add_audit_event("provider.live_acceptance", payload)
    try:
        assert live_provider_readiness(rig.store, "provider-a", "code")[0]
        assert not live_provider_readiness(rig.store, "provider-a", "review")[0]
    finally:
        rig.store.close()


@pytest.mark.parametrize("checked_at", [None, True, -1, float("nan"), float("inf")])
def test_quota_retry_requires_a_valid_check_time(tmp_path, checked_at):
    rig = Rig(tmp_path, {"provider-a": "ok"}, require_live_acceptance=True, block_unproven=False)
    payload = LiveProviderSmokeResult(
        "FAIL", "provider-a", ExecutionStatus.FAILED, None,
        False, True, True, False, False, False, "quota_rate_limit",
    ).to_payload()
    payload["checked_at"] = checked_at
    rig.store.add_audit_event("provider.live_acceptance", payload)
    try:
        assert not rig.pool.evaluate(rig.store, IMPLEMENT, TASK)[0].eligible
    finally:
        rig.store.close()
