"""Codex is optional unless a project explicitly configures or authorizes it."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from stagemesh import provider_auth
from stagemesh.capacity import CapacityKind
from stagemesh.cli import _real_provider_auth_blockers, main
from stagemesh.config import load_config
from stagemesh.persistence import Store
from stagemesh.provider_authorization import UNATTENDED_ENV
from stagemesh.provider_catalog import _UNSET_POOL_PRIORITY, balanced_provider_pools
from stagemesh.provider_pool import DEFAULT_PRIORITY, IMPLEMENT, ProviderPool
from stagemesh.providers import RuntimeCommandAdapter


def _project(path: Path, data: dict[str, object]) -> Path:
    runtime = path / ".stagemesh"
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return path


def _subscription(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, json.dumps({"authMethod": "claude.ai"}), "")


@dataclass(frozen=True)
class _Capacity(RuntimeCommandAdapter):
    capacity: str = CapacityKind.AVAILABLE

    def check_stage_capacity(self, capability: str) -> str:
        return self.capacity


def test_unset_balanced_priority_matches_pool_selection() -> None:
    assert _UNSET_POOL_PRIORITY == DEFAULT_PRIORITY


def test_catalog_rank_does_not_place_codex_ahead_of_a_configured_priority() -> None:
    pools = balanced_provider_pools(["codex", "claude", "agy"], {"claude": 0, "agy": 1})

    assert pools[IMPLEMENT] == ("claude", "agy", "codex")


def test_explicit_codex_priority_remains_first() -> None:
    pools = balanced_provider_pools(["codex", "claude", "grok"], {"codex": 10, "claude": 20, "grok": 30})

    assert pools[IMPLEMENT] == ("codex", "claude", "grok")


def test_missing_priorities_keep_the_catalog_order() -> None:
    pools = balanced_provider_pools(["codex", "claude", "grok", "agy"])

    assert pools[IMPLEMENT] == ("codex", "grok", "claude", "agy")
    assert pools["REVIEW"] == ("claude", "grok", "agy", "codex")


def test_balanced_profile_uses_priorities_and_explicit_pools_stay_exact(tmp_path: Path) -> None:
    ranked = _project(
        tmp_path / "ranked",
        {
            "providers": {
                "claude": {"command": "claude -p", "priority": 0},
                "agy": {"command": "agy", "priority": 1},
                "codex": {"command": "codex exec --model pinned", "priority": 50},
            },
            "routing": {"provider_profile": "balanced"},
        },
    )
    assert load_config(ranked).provider_pools[IMPLEMENT] == ("claude", "agy", "codex")

    explicit = _project(
        tmp_path / "explicit",
        {
            "providers": {
                "agy": {"command": "agy", "priority": 20},
                "claude": {"command": "claude -p", "priority": 0},
            },
            "routing": {
                "provider_profile": "balanced",
                "pools": {"IMPLEMENT": ["agy", "claude"], "REVIEW": ["claude", "agy"]},
            },
        },
    )
    config = load_config(explicit)
    assert config.provider_pools == {
        IMPLEMENT: ("agy", "claude"),
        "REVIEW": ("claude", "agy"),
    }


def test_provider_profile_command_writes_priority_order(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        {
            "providers": {
                "codex": {"command": "codex exec --model pinned", "priority": 40},
                "agy": {"command": "agy", "priority": 10},
                "claude": {"command": "claude -p", "priority": 0},
            }
        },
    )

    assert main(["--project", str(project), "provider-profile", "use", "balanced"]) == 0

    assert load_config(project).provider_pools[IMPLEMENT] == ("claude", "agy", "codex")


def test_capacity_does_not_assume_codex_or_claude(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(
        tmp_path,
        {"providers": {"claude": "claude -p", "agy": "agy"}},
    )

    assert main(["--project", str(project), "capacity", "--primary-down"]) == 2
    captured = capsys.readouterr()
    assert "capacity error: legacy fallback requires --primary and --secondary" in captured.err
    assert "chosen:" not in captured.out

    assert main(["--project", str(project), "capacity", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stages"][IMPLEMENT]["pool"] == ["claude", "agy"]
    assert "codex" not in payload["stages"][IMPLEMENT]["pool"]


def test_require_installed_checks_the_project_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_check(*args: object, **kwargs: object) -> dict[str, object]:
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {
            "providers": [],
            "api_billing_environment": [],
            "ok": True,
            "blockers": [],
            "model_calls_made": 0,
        }

    monkeypatch.setattr(provider_auth, "check", fake_check)
    project = _project(
        tmp_path,
        {
            "authorization": {"unattended_providers": ["claude", "agy"]},
            "providers": {"claude": "claude -p", "agy": "agy"},
        },
    )

    assert main(["--project", str(project), "provider-auth", "--require-installed", "--json"]) == 0
    assert seen["args"] == (("claude",),)
    assert seen["kwargs"] == {"require_installed": True}


def test_claude_and_agy_do_not_consult_codex_billing_or_installation() -> None:
    report = provider_auth.check(
        ["claude"],
        runner=_subscription,
        environ={"OPENAI_API_KEY": "sk-secret", "CODEX_API_KEY": "codex-secret"},
        require_installed=True,
        which=lambda name: "C:/bin/claude.exe" if name == "claude" else None,
    )

    assert report["ok"] is True
    assert report["api_billing_environment"] == []
    assert "codex CLI is not installed" not in report["blockers"]
    assert "sk-secret" not in json.dumps(report)

    blocked = provider_auth.check(
        ["claude"],
        runner=_subscription,
        environ={"ANTHROPIC_API_KEY": "ak-secret"},
        require_installed=True,
        which=lambda name: "C:/bin/claude.exe" if name == "claude" else None,
    )
    assert blocked["ok"] is False
    assert blocked["api_billing_environment"] == ["process:ANTHROPIC_API_KEY"]


def test_default_billing_scope_still_includes_every_historical_variable() -> None:
    assert provider_auth.billing_names_for(provider_auth.PROVIDERS) == frozenset(
        provider_auth.BILLING_ENVIRONMENT_VARIABLES
    )


def test_authorized_codex_keeps_its_pin_and_installation_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = provider_auth.check(
        ["codex"],
        runner=_subscription,
        environ={},
        require_installed=True,
        which=lambda _name: None,
    )
    assert missing["ok"] is False
    assert "codex CLI is not installed" in missing["blockers"]

    monkeypatch.setenv(UNATTENDED_ENV, "1")
    monkeypatch.setattr(provider_auth, "check", lambda *args, **kwargs: {"ok": True, "blockers": []})
    project = _project(
        tmp_path,
        {
            "authorization": {"unattended_providers": ["codex", "claude"]},
            "providers": {"codex": "codex exec", "claude": "claude -p"},
        },
    )

    assert any("model is not pinned" in blocker for blocker in _real_provider_auth_blockers(project))


def test_agy_remains_unverifiable_for_unattended_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    monkeypatch.setattr(provider_auth, "check", lambda *args, **kwargs: {"ok": True, "blockers": []})
    project = _project(
        tmp_path,
        {
            "authorization": {"unattended_providers": ["claude", "agy"]},
            "providers": {"claude": "claude -p", "agy": "agy"},
        },
    )

    blockers = _real_provider_auth_blockers(project)

    assert any("agy" in blocker and "cannot be verified" in blocker for blocker in blockers)
    assert not any("codex" in blocker for blocker in blockers)


def test_unauthorized_or_exhausted_codex_leaves_claude_eligible(tmp_path: Path) -> None:
    claude = _Capacity("claude", ("claude",), capacity=CapacityKind.AVAILABLE)
    exhausted = _Capacity("codex", ("codex",), capacity=CapacityKind.CAPACITY)
    store = Store(tmp_path / "pool.sqlite3")
    store.migrate()
    pools = {IMPLEMENT: ("codex", "claude"), "REVIEW": ("claude",)}
    pool = ProviderPool([exhausted, claude], pools, require_independent=False)
    verdicts = {item.provider: item for item in pool.evaluate(store, IMPLEMENT, "TASK")}

    assert verdicts["codex"].eligible is False
    assert verdicts["claude"].eligible is True

    ready = _Capacity("codex", ("codex",), capacity=CapacityKind.AVAILABLE)
    quota = ProviderPool([ready, claude], pools, require_independent=False)
    quota.record_failure(store, IMPLEMENT, "TASK", "codex", "quota_rate_limit")
    after_quota = {item.provider: item for item in quota.evaluate(store, IMPLEMENT, "TASK")}
    assert after_quota["codex"].eligible is False
    assert "quota_rate_limit" in after_quota["codex"].reason
    assert after_quota["claude"].eligible is True

    denied = RuntimeCommandAdapter("codex", ("codex",), denied="provider_not_authorized")
    blocked = ProviderPool([denied, claude], pools, require_independent=False)
    unauthorized = {item.provider: item for item in blocked.evaluate(store, IMPLEMENT, "TASK")}
    assert unauthorized["codex"].eligible is False
    assert unauthorized["claude"].eligible is True
