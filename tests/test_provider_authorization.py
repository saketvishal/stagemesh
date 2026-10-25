"""Provider authorization: installing a CLI must never authorize its use, and the boundary holds under fallback and substitution.

These reproduce the 2026-10-24 incident: a fresh sandbox had no pools, so when Codex failed the built-in fallback chain dispatched
unauthorized `grok` and `agy` reviews.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stagemesh import provider_authorization as pa
from stagemesh.capacity import CapacityKind
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.persistence import Store
from stagemesh.provider_pool import IMPLEMENT, REVIEW, ProviderPool, default_pools
from stagemesh.providers import RuntimeCommandAdapter, adapters_from_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("STAGEMESH_CODEX_CMD", "STAGEMESH_CLAUDE_CMD", "STAGEMESH_GROK_CMD", "STAGEMESH_AGY_CMD", pa.UNATTENDED_ENV):
        monkeypatch.delenv(name, raising=False)


def project_with(tmp_path: Path, config: dict | None) -> Path:
    (tmp_path / ".stagemesh").mkdir()
    if config is not None:
        (tmp_path / ".stagemesh" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def pool_for(tmp_path: Path, config: dict | None, monkeypatch, available: set[str]) -> tuple[ProviderPool, Store]:
    project = project_with(tmp_path, config)
    cfg = load_config(project)
    adapters = adapters_from_config(cfg)
    monkeypatch.setattr("stagemesh.provider_pool._stage_capacity", lambda adapter, capability: CapacityKind.AVAILABLE if adapter.name in available else CapacityKind.UNKNOWN)
    names = [a.name for a in adapters]
    pools = default_pools(names, cfg.stage_routes, cfg.provider_pools)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return ProviderPool(adapters, pools, require_independent=True), store


def test_incident_default_sandbox_unattended_has_no_eligible_provider_without_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    pool, store = pool_for(tmp_path, {"routing": {"require_independent_review": True}}, monkeypatch, {"codex", "claude", "grok", "agy"})
    for stage in (IMPLEMENT, REVIEW):
        verdicts = pool.evaluate(store, stage)
        assert verdicts and not any(v.eligible for v in verdicts)
        assert all(v.reason.startswith("not_authorized") for v in verdicts)


def test_allowlist_keeps_grok_and_agy_out_even_when_codex_is_down(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    config = {"authorization": {"unattended_providers": ["codex", "claude"]}}
    pool, store = pool_for(tmp_path, config, monkeypatch, {"claude", "grok", "agy"})  # codex unavailable
    review = {v.provider: v for v in pool.evaluate(store, REVIEW, implementer="claude")}
    assert not any(v.eligible for v in review.values())  # nobody independent is left: a blocker, never a substitution
    assert review["grok"].reason.startswith("not_authorized") and review["agy"].reason.startswith("not_authorized")
    implement = {v.provider: v.eligible for v in pool.evaluate(store, IMPLEMENT)}
    assert implement["claude"] and not implement["grok"] and not implement["agy"]


def test_recovery_between_authorized_providers_keeps_review_independence(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    config = {"authorization": {"unattended_providers": ["codex", "claude"]}}
    pool, store = pool_for(tmp_path, config, monkeypatch, {"codex", "claude", "grok", "agy"})
    codex_implemented = {v.provider for v in pool.evaluate(store, REVIEW, implementer="codex") if v.eligible}
    claude_implemented = {v.provider for v in pool.evaluate(store, REVIEW, implementer="claude") if v.eligible}
    assert codex_implemented == {"claude"} and claude_implemented == {"codex"}


@pytest.mark.parametrize("method", ["execute", "review_candidate"])
def test_execution_boundary_refuses_an_unauthorized_provider_without_running_it(tmp_path, monkeypatch, method):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    project = project_with(tmp_path, {"authorization": {"unattended_providers": ["codex", "claude"]}})
    marker = tmp_path / "ran.txt"
    adapter = RuntimeCommandAdapter("grok", (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"))
    if method == "execute":
        result = adapter.execute(Store(project / ".stagemesh" / "stagemesh.sqlite3"), "T-1", None, project)
        assert "provider_not_authorized" in str(result.failure_reason)
    else:
        assert "provider_not_authorized" in adapter.review_candidate("Review candidate", project, "0" * 40)
    assert not marker.exists()


def test_unattended_execution_fails_closed_without_an_allowlist_or_with_an_unreadable_config(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    project = project_with(tmp_path, None)
    assert pa.refusal(project, "codex", ("codex", "exec"))
    (project / ".stagemesh" / "config.json").write_text("{not json", encoding="utf-8")
    assert pa.refusal(project, "codex", ("codex", "exec"))


@pytest.mark.parametrize(
    "name, argv",
    [
        ("codex", ("grok", "--permission-mode", "acceptEdits")),  # provider substitution
        ("codex", ("cmd", "/c", "agy")),  # wrapper
        ("claude", ("powershell", "-File", "claude.ps1")),
        ("claude", ("npx", "agy")),
        ("helper", ("python", "run.py", "agy")),  # custom name wrapping a real CLI
    ],
)
def test_substitutions_and_wrappers_are_rejected(name, argv):
    assert pa.denial(name, argv, allowed={"codex", "claude", "helper"}, unattended=True)


@pytest.mark.parametrize("argv", [("codex",), ("C:/Users/x/bin/codex.exe", "exec", "--model", "m"), ("codex.cmd", "exec")])
def test_the_genuine_cli_is_accepted(argv):
    assert pa.denial("codex", argv, allowed={"codex"}, unattended=True) is None


def test_env_override_pointing_at_another_cli_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", "grok --always-approve")
    cfg = load_config(project_with(tmp_path, {"authorization": {"unattended_providers": ["codex", "claude"]}}))
    adapters = {a.name: a for a in adapters_from_config(cfg)}
    assert adapters["codex"].denied and "grok" in adapters["codex"].denied


def test_optional_providers_remain_supported_with_explicit_authorization_and_interactively(tmp_path, monkeypatch):
    cfg = load_config(project_with(tmp_path, None))  # interactive, no allowlist: unchanged behaviour
    assert all(a.denied is None for a in adapters_from_config(cfg))
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    cfg = load_config(tmp_path, tmp_path / ".stagemesh" / "config.json") if False else cfg
    (tmp_path / ".stagemesh" / "config.json").write_text(json.dumps({"authorization": {"unattended_providers": ["codex", "agy"]}}), encoding="utf-8")
    denied = {a.name: a.denied for a in adapters_from_config(load_config(tmp_path))}
    assert denied["agy"] is None and denied["codex"] is None and denied["grok"] and denied["claude"]


def test_allowlist_must_name_known_providers(tmp_path):
    project = project_with(tmp_path, {"authorization": {"unattended_providers": ["codex", "typo"]}})
    with pytest.raises(ConfigValidationError):
        load_config(project)
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"authorization": {"unattended_providers": []}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_config(project)


def test_a_stale_adapter_cannot_outlive_a_changed_authorization(tmp_path, monkeypatch):
    """Retries, crash recovery and scheduler restarts re-read the project's authorization at the moment of dispatch."""
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    project = project_with(tmp_path, {"authorization": {"unattended_providers": ["codex", "claude"]}})
    marker = tmp_path / "ran.txt"
    adapter = RuntimeCommandAdapter("claude", (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"))
    assert pa.refusal(project, "claude", ("claude", "-p")) is None  # authorized as installed
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"authorization": {"unattended_providers": ["codex"]}}), encoding="utf-8")
    assert "provider_not_authorized" in adapter.review_candidate("Review candidate", project, "0" * 40)
    assert not marker.exists()


def test_driver_refuses_real_providers_without_an_allowlist_and_unverifiable_billing(tmp_path, monkeypatch):
    from stagemesh.cli import _real_provider_auth_blockers

    project = project_with(tmp_path, {"routing": {"require_independent_review": True}})
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    assert any("no explicit provider allowlist" in b for b in _real_provider_auth_blockers(project))
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"authorization": {"unattended_providers": ["codex", "grok"]}}), encoding="utf-8")
    from stagemesh import provider_auth

    monkeypatch.setattr(provider_auth, "check", lambda names=None, **kw: {"ok": True, "blockers": []})
    assert any("grok" in b and "cannot be verified" in b for b in _real_provider_auth_blockers(project))


@pytest.mark.parametrize(
    "argv, model",
    [
        (("codex", "exec", "--model", "m1"), "m1"),
        (("codex", "exec", "-m", "m2"), "m2"),
        (("codex", "exec", "--model=m3"), "m3"),
        (("codex", "exec", "-c", 'model="m4"'), "m4"),
        (("codex", "exec", "--sandbox", "workspace-write"), None),
    ],
)
def test_pinned_model_is_read_from_the_command(argv, model):
    assert pa.pinned_model(argv) == model


def test_unpinned_codex_model_blocks_unattended_startup(tmp_path, monkeypatch):
    """First live failure: no `--model`, so the CLI default (rejected for ChatGPT sign-in) applied."""
    from stagemesh import provider_auth
    from stagemesh.cli import _real_provider_auth_blockers

    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    monkeypatch.setattr(provider_auth, "check", lambda names=None, **kw: {"ok": True, "blockers": []})
    project = project_with(tmp_path, {"authorization": {"unattended_providers": ["codex", "claude"]}})
    assert any("model is not pinned" in b for b in _real_provider_auth_blockers(project))
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({
            "authorization": {"unattended_providers": ["codex", "claude"]},
            "providers": {"codex": {"command": "codex exec --model known-good --sandbox workspace-write", "capabilities": ["IMPLEMENT", "REVIEW"]}},
        }),
        encoding="utf-8",
    )
    assert _real_provider_auth_blockers(project) == []


def test_provider_authorization_report_lists_only_authorized_providers(tmp_path, capsys):
    from stagemesh.cli import main

    project = project_with(tmp_path, {
        "authorization": {"unattended_providers": ["codex", "claude"]},
        "providers": {"codex": {"command": "codex exec --model known-good", "capabilities": ["IMPLEMENT", "REVIEW"]}},
    })
    assert main(["--project", str(project), "provider-authorization"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["model_calls_made"] == 0 and report["allowlist"] == ["codex", "claude"]
    authorized = {p["provider"] for p in report["providers"] if p["authorized"]}
    assert authorized == {"codex", "claude"}
    assert all("grok" not in names and "agy" not in names for names in report["unattended_pools"].values())
    codex = next(p for p in report["providers"] if p["provider"] == "codex")
    assert codex["stages"]["code"]["model"] == "known-good"


@pytest.mark.parametrize(
    "argv",
    [
        ("./codex", "exec"),  # a script committed in the repository
        ("../x/claude", "-p"),
        ("\\\\evil\\share\\codex.exe",),
        ("C:\\evil\\codex.bat",),
        ("/tmp/x/codex.ps1",),
    ],
)
def test_a_file_that_merely_borrows_the_name_is_rejected(argv, tmp_path):
    name = "codex" if "codex" in argv[0] else "claude"
    assert pa.denial(name, argv, allowed={"codex", "claude"}, unattended=True, project=tmp_path)


def test_executables_inside_the_project_or_its_worktrees_are_rejected(tmp_path):
    inside = tmp_path / ".stagemesh" / "worktrees" / "t" / "codex"
    assert pa.denial("codex", (str(inside), "exec"), allowed={"codex"}, unattended=True, project=tmp_path)
    outside = tmp_path.parent / "elsewhere" / "codex"
    assert pa.denial("codex", (str(outside), "exec"), allowed={"codex"}, unattended=True, project=tmp_path) is None


def test_custom_provider_cannot_hide_a_real_cli_inside_a_shell_string():
    assert pa.denial("mine", ("sh", "-c", "grok -p x"), allowed={"mine"}, unattended=True)
    assert pa.denial("mine", ("python", "mock.py"), allowed={"mine"}, unattended=True) is None


def test_adapter_built_under_an_allowlist_stays_bound_when_the_config_becomes_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    project = project_with(tmp_path, {"authorization": {"unattended_providers": ["codex"]}})
    (project / ".stagemesh" / "config.json").write_text("{broken", encoding="utf-8")
    assert pa.refusal(project, "grok", ("grok",), build_time_allowed=("codex",))
    monkeypatch.delenv(pa.UNATTENDED_ENV)
    assert pa.refusal(project, "grok", ("grok",), build_time_allowed=("codex",))  # even outside unattended mode


def test_live_smoke_of_an_authorized_adapter_works_unattended_and_a_denied_one_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv(pa.UNATTENDED_ENV, "1")
    temp_project = project_with(tmp_path, None)  # a smoke project has no allowlist of its own
    with pa.pre_authorized(["codex"]):
        assert pa.refusal(temp_project, "codex", ("codex", "exec")) is None
        assert pa.refusal(temp_project, "grok", ("grok",))
    assert pa.refusal(temp_project, "codex", ("codex", "exec"))
