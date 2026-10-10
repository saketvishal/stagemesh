from __future__ import annotations

import contextlib
import io
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from test_provider_pool import TASK, Rig

import stagemesh.cli as cli_module
from stagemesh.config import ConfigValidationError, load_config
from stagemesh.domain import Stage
from stagemesh.provider_pool import IMPLEMENT, REVIEW
from stagemesh.providers import RuntimeCommandAdapter, adapters_from_config


def _config(tmp_path: Path, data: dict) -> Path:
    runtime = tmp_path / ".stagemesh"
    runtime.mkdir(exist_ok=True)
    (runtime / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def _picks(rig: Rig, stage: str, count: int, implementer: str | None = None) -> list[str]:
    """Simulate `count` consecutive selections the way the executor/reviewer do (announce, take first, record use)."""
    chosen = []
    for _ in range(count):
        verdicts = rig.pool.evaluate(rig.store, stage, TASK, implementer)
        ordered = rig.pool.announce(rig.store, stage, TASK, verdicts)
        chosen.append(ordered[0].name)
        rig.pool.record_use(rig.store, stage, TASK, ordered[0].name, "SUCCEEDED")
    return chosen


# --- config ------------------------------------------------------------------------------------


def test_object_form_providers_and_policy_are_parsed(tmp_path: Path) -> None:
    project = _config(
        tmp_path,
        {
            "providers": {
                "codex": "codex exec",  # legacy string form keeps working
                "my-agent": {"command": "my-agent run", "capabilities": ["IMPLEMENT"], "priority": 5, "weight": 3},
                "reviewer-bot": {"command": "reviewer-bot review", "capabilities": ["review"]},
            },
            "routing": {
                "pools": {"IMPLEMENT": ["codex", "my-agent"], "REVIEW": ["reviewer-bot"]},
                "provider_selection_policy": "weighted",
                "provider_weights": {"codex": 2},
            },
        },
    )
    config = load_config(project)
    assert config.provider_commands == {"codex": "codex exec", "my-agent": "my-agent run", "reviewer-bot": "reviewer-bot review"}
    assert config.provider_specs["my-agent"].capabilities == frozenset({"IMPLEMENT"})
    assert (config.provider_specs["my-agent"].priority, config.provider_specs["my-agent"].weight) == (5, 3)
    assert config.provider_specs["reviewer-bot"].capabilities == frozenset({"REVIEW"})  # legacy lowercase accepted
    assert "codex" not in config.provider_specs
    assert config.provider_selection_policy == "weighted" and config.provider_weights == {"codex": 2}


def test_adapters_carry_declared_capabilities_and_builtins_stay_registered(tmp_path: Path) -> None:
    project = _config(
        tmp_path,
        {"providers": {"my-agent": {"command": "my-agent run", "capabilities": ["IMPLEMENT"]}, "reviewer-bot": {"command": "rb review", "capabilities": ["REVIEW"]}}},
    )
    adapters = {a.name: a for a in adapters_from_config(load_config(project))}
    assert {"codex", "claude", "grok"} <= set(adapters)  # built-in defaults need no config
    assert {"code", "review"} <= adapters["codex"].capabilities
    assert "code" in adapters["my-agent"].capabilities and "review" not in adapters["my-agent"].capabilities
    assert "review" in adapters["reviewer-bot"].capabilities and "code" not in adapters["reviewer-bot"].capabilities
    assert adapters["my-agent"].command == ("my-agent", "run")


@pytest.mark.parametrize(
    "data",
    [
        {"providers": {"x": {"command": "x", "capabilities": ["VALIDATE"]}}},
        {"providers": {"x": {"command": "x", "capabilities": []}}},
        {"providers": {"x": {"command": "x", "bogus": 1}}},
        {"providers": {"x": {"capabilities": ["IMPLEMENT"]}}},
        {"providers": {"x": {"command": "x", "weight": 0}}},
        {"providers": {"x": {"command": "x", "priority": -1}}},
        {"providers": {"x": {"command": "x", "weight": True}}},
        {"providers": {"has space": "x"}},
        {"routing": {"provider_selection_policy": "random"}},
        {"routing": {"provider_profile": "project-x"}},
        {"routing": {"pools": {"IMPLEMENT": ["nope"]}}},
        {"routing": {"provider_weights": {"nope": 2}}},
        {"routing": {"provider_weights": {"codex": 0}}},
    ],
)
def test_invalid_provider_and_policy_config_is_rejected(tmp_path: Path, data: dict) -> None:
    with pytest.raises(ConfigValidationError):
        load_config(_config(tmp_path, data))


def test_env_command_overrides_work_for_builtins_and_custom_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _config(tmp_path, {"providers": {"my-agent": {"command": "my-agent run"}}})
    monkeypatch.setenv("STAGEMESH_CODEX_CMD", "codex --override")
    monkeypatch.setenv("STAGEMESH_MY_AGENT_CMD", "other-agent go")
    commands = load_config(project).provider_commands
    assert commands["codex"] == "codex --override" and commands["my-agent"] == "other-agent go"


def test_default_policy_is_round_robin_and_stage_routes_still_load(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path, {"routing": {"stage_routes": {"IMPLEMENT": "codex", "REVIEW": "claude"}}}))
    assert config.provider_selection_policy == "round_robin" and config.stage_routes == {"IMPLEMENT": "codex", "REVIEW": "claude"}



def test_balanced_provider_profile_sets_generic_stage_pools(tmp_path: Path) -> None:
    config = load_config(_config(tmp_path, {"routing": {"provider_profile": "balanced"}}))
    assert config.provider_profile == "balanced"
    assert config.provider_selection_policy == "least_recently_used"
    assert config.provider_pools[IMPLEMENT] == ("codex", "grok", "claude", "agy")
    assert config.provider_pools[REVIEW] == ("claude", "grok", "agy", "codex")


def test_provider_profile_use_balanced_writes_generic_config(tmp_path: Path) -> None:
    code = cli_module.main(["--project", str(tmp_path), "provider-profile", "use", "balanced", "--json"])

    assert code == 0
    config = load_config(tmp_path)
    assert config.provider_profile == "balanced"
    assert config.provider_pools[IMPLEMENT] == ("codex", "grok", "claude", "agy")
    assert config.provider_selection_policy == "least_recently_used"


def test_capacity_reports_balanced_policy_pools_and_skip_reasons(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _config(tmp_path, {"routing": {"provider_profile": "balanced"}})

    code = cli_module.main(["--project", str(tmp_path), "capacity", "--json"])

    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["active_provider_profile"] == "balanced"
    assert data["selection_policy"] == "least_recently_used"
    assert data["stages"][IMPLEMENT]["pool"] == ["codex", "grok", "claude", "agy"]
    assert data["stages"][REVIEW]["pool"] == ["claude", "grok", "agy", "codex"]
    assert all("reason" in verdict for verdict in data["stages"][IMPLEMENT]["verdicts"])

def test_legacy_builtin_priority_config_is_normalized_to_round_robin(tmp_path: Path) -> None:
    project = _config(
        tmp_path,
        {
            "providers": {
                "codex": {"command": "codex exec", "capabilities": ["IMPLEMENT", "REVIEW"], "priority": 10},
                "claude": {"command": "claude -p", "capabilities": ["IMPLEMENT", "REVIEW"], "priority": 20},
                "grok": {"command": "grok", "capabilities": ["IMPLEMENT", "REVIEW"], "priority": 30},
            },
            "routing": {
                "provider_selection_policy": "priority",
                "pools": {
                    "IMPLEMENT": ["codex", "claude", "grok"],
                    "REVIEW": ["claude", "grok", "codex"],
                },
            },
        },
    )

    config = load_config(project)

    assert config.provider_selection_policy == "round_robin"
    assert config.provider_pools[IMPLEMENT] == ("codex", "claude", "grok")
    assert config.provider_pools[REVIEW] == ("claude", "grok", "codex")
    assert all(spec.priority is None for spec in config.provider_specs.values())

def test_config_json_shows_effective_default_providers_and_rotation_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _config(tmp_path, {})

    code = cli_module.main(["--project", str(project), "config", "--json"])

    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["routing"]["provider_selection_policy"] == "round_robin"
    assert data["routing"]["pools"][IMPLEMENT] == ["codex", "claude", "grok", "agy"]
    assert data["routing"]["pools"][REVIEW] == ["codex", "claude", "grok", "agy"]
    assert {"codex", "claude", "grok", "agy"} <= set(data["providers"])

def test_default_builtin_policy_rotates_across_all_four_providers(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": "ok", "claude": "ok", "grok": "ok", "agy": "ok"})

    assert rig.pool.policy == "round_robin"
    assert _picks(rig, IMPLEMENT, 8) == ["codex", "claude", "grok", "agy", "codex", "claude", "grok", "agy"]
    assert _picks(rig, REVIEW, 6, implementer="codex") == ["claude", "grok", "agy", "claude", "grok", "agy"]
    text = rig.text()
    assert "selection policy: round_robin" in text
    assert "skipped codex: not_independent: produced the candidate" in text

# --- custom providers, selection policies ----------------------------------------------------------


def test_custom_providers_implement_and_review_with_clear_logging(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"my-agent": "ok", "reviewer-bot": "ok"}, pools={IMPLEMENT: ("my-agent",), REVIEW: ("reviewer-bot",)})

    rig.tick(5)  # plan, implement, validate, review, integrate

    assert rig.stage == Stage.DONE
    candidate = rig.store.latest_candidate(TASK)
    assert candidate["produced_by"] == "my-agent"
    assert rig.review_payload()["review_provider"] == "reviewer-bot"
    text = rig.text()
    assert "provider registry:" in text and "my-agent (custom;" in text and "reviewer-bot (custom;" in text
    assert "selection policy: round_robin" in text
    assert "selected implementation provider my-agent (custom provider)" in text
    assert "selected review provider reviewer-bot (custom provider)" in text
    assert "final implementation provider: my-agent (custom provider)" in text


def test_builtin_default_providers_are_labelled_as_such(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"codex": "ok", "extra-agent": "ok"}, pools={IMPLEMENT: ("codex", "extra-agent"), REVIEW: ("extra-agent",)})
    rig.tick(2)
    assert "selected implementation provider codex (built-in default provider)" in rig.text()
    assert "extra-agent (custom;" in rig.text() and "codex (built-in;" in rig.text()


def test_provider_without_the_stage_capability_is_skipped(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"reviewer-bot": "ok", "my-agent": "ok"}, pools={IMPLEMENT: ("reviewer-bot", "my-agent"), REVIEW: ("reviewer-bot",)})
    bot = rig.pool.adapters["reviewer-bot"]
    rig.pool.adapters["reviewer-bot"] = RuntimeCommandAdapter(bot.name, bot.command, frozenset({"review"}))
    verdicts = {v.provider: v for v in rig.pool.evaluate(rig.store, IMPLEMENT, TASK)}
    assert not verdicts["reviewer-bot"].eligible and verdicts["reviewer-bot"].reason.startswith("missing_capability")
    assert verdicts["my-agent"].eligible
    assert rig.pool.evaluate(rig.store, REVIEW, TASK)[0].eligible


def test_explicit_priority_policy_keeps_pool_order_unless_priorities_are_declared(tmp_path: Path) -> None:
    pools = {IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)}
    rig = Rig(tmp_path / "a", {"alpha": "ok", "beta": "ok", "gamma": "ok"}, pools=pools, policy="priority")
    assert _picks(rig, IMPLEMENT, 3) == ["alpha", "alpha", "alpha"]
    ranked = Rig(tmp_path / "b", {"alpha": "ok", "beta": "ok", "gamma": "ok"}, pools=pools, policy="priority", priorities={"gamma": 1, "beta": 50})
    assert _picks(ranked, IMPLEMENT, 2) == ["gamma", "gamma"]
    assert [a.name for a in ranked.pool.order(ranked.store, IMPLEMENT, list(ranked.pool.adapters.values()))[0]] == ["gamma", "beta", "alpha"]


def test_round_robin_distributes_across_arbitrary_provider_names(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": "ok", "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="round_robin",
    )
    assert _picks(rig, IMPLEMENT, 7) == ["alpha", "beta", "gamma", "alpha", "beta", "gamma", "alpha"]
    assert "round_robin: previous IMPLEMENT provider was" in rig.text()


def test_round_robin_skips_ineligible_providers_in_the_rotation(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": None, "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="round_robin",
    )
    assert _picks(rig, IMPLEMENT, 4) == ["alpha", "gamma", "alpha", "gamma"]
    assert "skipped beta: cli_not_installed" in rig.text()


def test_least_recently_used_prefers_never_used_then_the_oldest_use(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": "ok", "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="least_recently_used",
    )
    rig.pool.record_use(rig.store, IMPLEMENT, TASK, "beta", "SUCCEEDED")
    rig.pool.record_use(rig.store, IMPLEMENT, TASK, "alpha", "SUCCEEDED")
    assert _picks(rig, IMPLEMENT, 4) == ["gamma", "beta", "alpha", "gamma"]  # gamma never used; then oldest first
    assert "least_recently_used: never used for this stage" in rig.text()
    rig.pool.record_use(rig.store, REVIEW, TASK, "gamma", "ANSWERED")  # other stages do not affect IMPLEMENT
    assert _picks(rig, IMPLEMENT, 1) == ["beta"]


def test_weighted_policy_follows_weights_deterministically(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": "ok", "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="weighted",
        weights={"alpha": 1, "beta": 2, "gamma": 1},
    )
    picks = _picks(rig, IMPLEMENT, 8)
    assert picks == ["beta", "alpha", "beta", "gamma", "beta", "alpha", "beta", "gamma"]
    assert {n: picks.count(n) for n in ("alpha", "beta", "gamma")} == {"alpha": 2, "beta": 4, "gamma": 2}
    assert "weighted: weight 2" in rig.text()


def test_review_never_selects_the_implementer_under_any_policy(tmp_path: Path) -> None:
    for policy in ("priority", "round_robin", "least_recently_used", "weighted"):
        rig = Rig(
            tmp_path / policy,
            {"alpha": "ok", "beta": "ok", "gamma": "ok", "delta": "ok"},
            pools={IMPLEMENT: ("alpha",), REVIEW: ("alpha", "beta", "gamma", "delta")},
            policy=policy,
            weights={"alpha": 9, "beta": 1, "gamma": 1, "delta": 1},
        )
        picks = _picks(rig, REVIEW, 9, implementer="alpha")
        assert "alpha" not in picks, policy
        if policy != "priority":
            assert {"beta", "gamma", "delta"} <= set(picks), policy
        assert "skipped alpha: not_independent: produced the candidate" in rig.text()


def test_review_also_skips_providers_sharing_the_implementers_command(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"alpha": "ok", "beta": "ok", "gamma": "ok"}, pools={IMPLEMENT: ("alpha",), REVIEW: ("beta", "gamma")}, policy="round_robin")
    gamma = rig.pool.adapters["gamma"]
    rig.pool.adapters["beta"] = RuntimeCommandAdapter("beta", gamma.command)  # same runtime identity as gamma
    verdicts = {v.provider: v for v in rig.pool.evaluate(rig.store, REVIEW, TASK, "gamma")}
    assert verdicts["beta"].reason == "not_independent: uses the same command as the implementer"
    assert verdicts["gamma"].reason == "not_independent: produced the candidate"


# --- fallback / cooldown / pinning / single-agent ----------------------------------------------------


def test_fallback_follows_the_policy_order_when_the_selected_provider_fails(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": "auth-fail", "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="round_robin",
    )
    rig.pool.record_use(rig.store, IMPLEMENT, TASK, "alpha", "SUCCEEDED")  # next in rotation: beta

    rig.tick(2)  # plan, implement

    assert rig.store.latest_candidate(TASK)["produced_by"] == "gamma"
    text = rig.text()
    assert "selected implementation provider beta (custom provider): round_robin" in text
    assert "fallback: beta failed (authentication_failure) -> trying gamma" in text
    assert "fallback after the preferred provider failed" in text
    assert rig.pool.uses(rig.store, IMPLEMENT)[-1][0] == "gamma"  # rotation continues from the provider that answered


def test_cooldown_removes_a_provider_from_the_rotation(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        {"alpha": "ok", "beta": "ok", "gamma": "ok"},
        pools={IMPLEMENT: ("alpha", "beta", "gamma"), REVIEW: ("alpha",)},
        policy="round_robin",
    )
    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "beta", "quota_rate_limit")
    assert _picks(rig, IMPLEMENT, 4) == ["alpha", "gamma", "alpha", "gamma"]
    assert "skipped beta: provider_cooldown: quota_rate_limit" in rig.text()


def _wired(tmp_path: Path, config: dict, **args) -> tuple[Rig, object]:
    rig = Rig(tmp_path, {"alpha": "ok"})
    script = tmp_path / "provider.py"
    commands = {name: f'"{sys.executable}" "{script}" ok {name}' for name in ("alpha", "beta", "gamma")}
    providers = {name: {"command": cmd, "capabilities": ["IMPLEMENT", "REVIEW"]} for name, cmd in commands.items()}
    (rig.project / ".stagemesh" / "config.json").write_text(json.dumps({"providers": providers, **config}), encoding="utf-8")
    loaded = load_config(rig.project)
    coord, info = cli_module._build_coordinator(
        Namespace(dry_run=False, **args), rig.project, loaded, rig.store, None, rig.log
    )
    return rig, (coord, info)


def test_explicit_provider_flag_pins_implementation_to_that_provider(tmp_path: Path) -> None:
    _, (coord, info) = _wired(
        tmp_path,
        {"routing": {"pools": {"IMPLEMENT": ["alpha", "beta"], "REVIEW": ["beta", "gamma"]}, "provider_selection_policy": "round_robin"}},
        provider="beta",
    )
    assert coord.executor.pool.pool(IMPLEMENT) == ("beta",)
    assert coord.executor.pool.pool(REVIEW) == ("beta", "gamma") and info["selection_policy"] == "round_robin"


def test_cli_wiring_reports_the_policy_selected_provider(tmp_path: Path) -> None:
    _, (coord, info) = _wired(
        tmp_path,
        {
            "routing": {
                "pools": {"IMPLEMENT": ["alpha", "beta"], "REVIEW": ["gamma", "beta"]},
                "provider_selection_policy": "weighted",
                "provider_weights": {"beta": 2},
            }
        },
        provider=None,
    )
    assert coord.executor.pool.pool(IMPLEMENT) == ("alpha", "beta")
    assert info["provider"] == "beta"
    assert info["review_provider"] == "dynamic-pool:beta,gamma"


def test_cli_review_setup_ignores_implementation_provider_backoff(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"alpha": "ok", "beta": "ok", "gamma": "ok"})
    for provider in ("alpha", "beta", "gamma"):
        rig.pool.record_failure(rig.store, IMPLEMENT, TASK, provider, "provider_timeout")
    rig.store.add_candidate(TASK, "abc123", "gamma", durable_handoff=True)
    rig.store.advance_task(TASK, Stage.REVIEW)
    commands = {
        name: f'"{sys.executable}" "{tmp_path / "provider.py"}" ok {name}'
        for name in ("alpha", "beta", "gamma")
    }
    providers = {name: {"command": cmd, "capabilities": ["IMPLEMENT", "REVIEW"]} for name, cmd in commands.items()}
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps({"providers": providers, "routing": {"pools": {"IMPLEMENT": ["alpha", "beta", "gamma"], "REVIEW": ["alpha", "beta", "gamma"]}}}),
        encoding="utf-8",
    )

    _, info = cli_module._build_coordinator(
        Namespace(dry_run=False, provider=None),
        rig.project,
        load_config(rig.project),
        rig.store,
        cli_module.TargetSelection(TASK),
        rig.log,
    )

    assert info["provider"] == "deferred"
    assert info["review_provider"].startswith("dynamic-pool:")
    assert "gamma" not in info["review_provider"]
    assert len(info["implementation_skipped"]) == 3


def test_capacity_command_reports_stage_verdicts_and_local_cooldown(tmp_path: Path) -> None:
    rig = Rig(tmp_path, {"alpha": "ok", "beta": "ok", "gamma": "ok"})
    rig.pool.record_failure(rig.store, IMPLEMENT, TASK, "beta", "quota_rate_limit")
    commands = {
        name: f'"{sys.executable}" "{tmp_path / "provider.py"}" ok {name}'
        for name in ("alpha", "beta", "gamma")
    }
    providers = {name: {"command": cmd, "capabilities": ["IMPLEMENT", "REVIEW"]} for name, cmd in commands.items()}
    (rig.project / ".stagemesh" / "config.json").write_text(
        json.dumps({"providers": providers, "routing": {"pools": {"IMPLEMENT": ["beta", "alpha"], "REVIEW": ["beta", "gamma"]}}}),
        encoding="utf-8",
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_module.main(["--project", str(rig.project), "capacity", "--task", TASK, "--json"])
    data = json.loads(out.getvalue())

    assert code == 0
    impl = {v["provider"]: v for v in data["stages"]["IMPLEMENT"]["verdicts"]}
    review = {v["provider"]: v for v in data["stages"]["REVIEW"]["verdicts"]}
    assert impl["beta"]["reason"].startswith("provider_cooldown: quota_rate_limit")
    assert review["beta"]["eligible"] is True
    assert data["providers"][0]["live_acceptance"] == "not_run"


def test_single_agent_mode_stays_single_provider_under_any_policy(tmp_path: Path) -> None:
    _, (coord, info) = _wired(
        tmp_path,
        {"routing": {"mode": "SINGLE_AGENT", "single_agent_provider": "beta", "require_independent_review": False, "provider_selection_policy": "round_robin"}},
        provider=None,
    )
    pool = coord.executor.pool
    assert pool.pool(IMPLEMENT) == ("beta",) and pool.pool(REVIEW) == ("beta",)
    assert info["review_provider"] == "single-agent-deterministic-fallback"


def test_stage_routes_are_a_preference_and_priorities_shape_the_default_pool(tmp_path: Path) -> None:
    _, (coord, _) = _wired(
        tmp_path,
        {"routing": {"stage_routes": {"IMPLEMENT": "gamma"}}},
        provider=None,
    )
    pool = coord.executor.pool
    assert pool.pool(IMPLEMENT)[0] == "gamma"  # routed provider first, everything else is fallback
    assert {"alpha", "beta"} <= set(pool.pool(IMPLEMENT))
