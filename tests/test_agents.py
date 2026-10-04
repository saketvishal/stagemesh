"""Agent plugins, operator agent configuration, routing behavior and observability. Fake providers only: no real Codex/Claude/Grok."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

import stagemesh.cli as cli_module
import test_provider_pool as tpp
from stagemesh.agent_config import AgentSettings, AgentState, apply_agent_state, load_state, parse_state, state_path
from stagemesh.agents import AgentPlugin, AgentPluginError, AgentRegistry, default_registry
from stagemesh.concurrency import ProviderLimiter
from stagemesh.config import BUILTIN_PROVIDERS, SELECTION_POLICIES, ConfigValidationError, load_base_config, load_config
from stagemesh.persistence import Store
from stagemesh.provider_pool import IMPLEMENT, REVIEW, ProviderLog, ProviderPool
from stagemesh.providers import RuntimeCommandAdapter, adapters_from_config, approved_default_adapters

from test_run_ready import _project

PY = sys.executable


def run(project: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_module.main(["--project", str(project), *argv])
    return code, out.getvalue(), err.getvalue()


def plain_project(tmp_path: Path, config: dict | None = None) -> Path:
    project = _project(tmp_path, ["T-1"], config=config)
    return project


# --- the plugin registry --------------------------------------------------------------------------------------------------------------


def test_builtin_agents_are_loaded_through_the_plugin_registry() -> None:
    registry = default_registry()
    assert registry.builtin_ids() == BUILTIN_PROVIDERS == ("codex", "claude", "grok")
    for plugin in registry.plugins():
        assert isinstance(plugin, AgentPlugin) and registry.is_builtin(plugin.id)
        d = plugin.describe()
        assert d["stages"] == ["IMPLEMENT", "REVIEW"] and d["structured_review"] and d["supports_implementation"] and d["supports_readonly_review"]
        assert plugin.attribution["label"] and plugin.env_command_var == f"STAGEMESH_{plugin.id.upper()}_CMD"
    commands = {a.name: a.command for a in approved_default_adapters()}  # the adapters come from the plugins, not a hard-coded table
    assert commands["codex"] == ("codex", "exec") and commands["claude"] == ("claude", "-p") and commands["grok"] == ("grok",)


def test_a_new_plugin_uses_the_same_interface_and_is_validated(tmp_path: Path) -> None:
    registry = AgentRegistry()
    plugin = AgentPlugin("local-bot", "Local Bot", "local-bot run", stages=frozenset({"IMPLEMENT"}), structured_review=False, default_max_concurrency=3)
    registry.register(plugin)
    with pytest.raises(AgentPluginError, match="already registered"):
        registry.register(plugin)
    for bad in (
        lambda: AgentPlugin("Bad Id", "x", "x"),
        lambda: AgentPlugin("ok", "x", "x", stages=frozenset({"DEPLOY"})),
        lambda: AgentPlugin("ok", "x", "x", stages=frozenset({"REVIEW"}), supports_readonly_review=False),
        lambda: AgentPlugin("ok", "x", "x", default_max_concurrency=0),
    ):
        with pytest.raises(AgentPluginError):
            bad()
    config = apply_agent_state(plain_project(tmp_path), load_base_config(plain_project(tmp_path / "again")), registry)
    assert "local-bot" in config.agent_report and config.agent_report["local-bot"]["max_concurrency"] == 3
    assert config.agent_report["local-bot"]["sources"]["max_concurrency"] == "built-in default"  # the plugin's own default


# --- non-interactive configuration ----------------------------------------------------------------------------------------------------


def configure(project: Path, *argv: str) -> dict:
    code, out, err = run(project, "agents", "configure", "--json", *argv)
    assert code == 0, err
    return json.loads(out)


def test_configure_updates_enabled_roles_policy_weights_concurrency_and_expiry(tmp_path: Path) -> None:
    project = plain_project(tmp_path)
    saved = configure(
        project,
        "--enable", "codex,grok,claude", "--disable", "codex",
        "--implementation", "claude,grok", "--review", "grok,claude",
        "--policy", "weighted", "--max-concurrency", "claude=2,grok=1", "--weight", "claude=3,grok=2", "--priority", "claude=10",
        "--expires-at", "claude=2030-01-01T00:00:00Z,grok=1893456000",
    )
    assert saved["saved"].endswith("agents.json") and saved["state"]["policy"] == "weighted"
    config = load_config(project)
    assert config.disabled_agents == {"codex"} and config.provider_selection_policy == "weighted"
    assert config.provider_pools == {"IMPLEMENT": ("claude", "grok"), "REVIEW": ("grok", "claude")}
    assert (config.provider_specs["claude"].max_concurrency, config.provider_specs["grok"].max_concurrency) == (2, 1)
    assert config.provider_weights == {"claude": 3, "grok": 2} and config.provider_specs["claude"].priority == 10
    assert config.agent_expiry["claude"] == 1893456000.0 == config.agent_expiry["grok"]
    assert not {a.name for a in adapters_from_config(config)} & {"codex"}  # a disabled agent is not even a candidate
    assert not (project / ".stagemesh" / "agents.json").read_text(encoding="utf-8").count("token")


def test_configure_show_and_status_report_values_with_their_sources(tmp_path: Path) -> None:
    project = plain_project(tmp_path, {"providers": {"grok": {"command": f"{PY} -c pass", "max_concurrency": 4, "priority": 7}}, "routing": {"provider_selection_policy": "round_robin"}})
    configure(project, "--max-concurrency", "claude=2", "--disable", "codex", "--policy", "expires_soon")
    code, out, _ = run(project, "agents", "status", "--json")
    report = json.loads(out)
    agents = {a["id"]: a for a in report["agents"]}
    assert report["policy"] == "expires_soon" and report["policy_source"] == "runtime config"
    assert agents["claude"]["max_concurrency"] == 2 and agents["claude"]["sources"]["max_concurrency"] == "runtime config"
    assert agents["grok"]["max_concurrency"] == 4 and agents["grok"]["sources"]["max_concurrency"] == "project config"  # legacy still honoured
    assert agents["grok"]["priority"] == 7 and agents["grok"]["sources"]["priority"] == "project config"
    assert agents["codex"]["enabled"] is False and agents["codex"]["sources"]["enabled"] == "runtime config"
    assert agents["claude"]["sources"]["priority"] == "built-in default"
    for key in ("stages", "enabled", "weight", "expires_at", "healthy", "health", "cooldown", "recent_use", "skipped"):
        assert key in agents["claude"]
    code, text, _ = run(project, "agents", "configure", "--show")
    assert "selection policy: expires_soon (source: runtime config)" in text and "DISABLED" in text and "[runtime config]" in text
    code, listing, _ = run(project, "agents", "list")
    assert code == 0 and "codex:" in listing and "claude:" in listing and "grok:" in listing
    assert run(project, "configure", "agents", "--show")[0] == 0  # the `configure agents` spelling is the same command


def test_environment_overrides_the_runtime_policy_and_provider_command_overrides_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = plain_project(tmp_path)
    configure(project, "--policy", "weighted")
    monkeypatch.setenv("STAGEMESH_PROVIDER_SELECTION_POLICY", "priority")
    monkeypatch.setenv("STAGEMESH_CLAUDE_CMD", "my-claude --flag")
    config = load_config(project)
    assert config.provider_selection_policy == "priority" and config.agent_policy_source == "environment"
    assert config.provider_commands["claude"] == "my-claude --flag"
    assert {a.name: a.command for a in adapters_from_config(config)}["claude"] == ("my-claude", "--flag")


def test_runtime_config_beats_project_config_for_pools_and_weights(tmp_path: Path) -> None:
    project = plain_project(
        tmp_path,
        {"routing": {"pools": {"IMPLEMENT": ["codex", "claude"], "REVIEW": ["claude"]}, "provider_weights": {"claude": 5}, "provider_selection_policy": "weighted"}},
    )
    before = load_config(project)
    assert before.provider_pools["IMPLEMENT"] == ("codex", "claude") and before.provider_weights == {"claude": 5}
    configure(project, "--implementation", "grok", "--weight", "claude=9")
    after = load_config(project)
    assert after.provider_pools["IMPLEMENT"] == ("grok",)  # runtime beats routing.pools for the stage it names
    assert after.provider_pools["REVIEW"] == ("claude",)  # ...and project config still decides the stage it does not
    assert after.provider_weights["claude"] == 9 and after.provider_selection_policy == "weighted"
    assert after.agent_report["claude"]["sources"]["weight"] == "runtime config"


def test_existing_configs_without_agent_state_behave_exactly_as_before(tmp_path: Path) -> None:
    project = plain_project(tmp_path, {"providers": {"my-agent": {"command": "my-agent run", "capabilities": ["IMPLEMENT"], "priority": 5}}, "routing": {"stage_routes": {"IMPLEMENT": "claude"}}})
    config = load_config(project)
    assert config.agent_state_file is None and config.disabled_agents == frozenset()
    assert set(config.provider_specs) == {"my-agent"}  # untouched built-ins gain no spec
    assert config.stage_routes == {"IMPLEMENT": "claude"} and config.provider_specs["my-agent"].capabilities == frozenset({"IMPLEMENT"})
    assert config.provider_selection_policy == "priority" and config.agent_policy_source == "built-in default"


def test_configuration_is_validated_and_refused_without_saving(tmp_path: Path) -> None:
    project = plain_project(tmp_path)
    for argv, needle in (
        (("--enable", "nope"), "unknown agent"),
        (("--max-concurrency", "claude=0"), "integer >= 1"),
        (("--max-concurrency", "claude"), "name=value"),
        (("--expires-at", "claude=tomorrow"), "ISO-8601"),
        (("--disable", "codex", "--implementation", "codex,claude"), "disabled and cannot be assigned"),
        (("--implementation", "claude,claude"), "distinct"),
    ):
        code, _, err = run(project, "agents", "configure", *argv)
        assert code == 2 and needle in err, (argv, err)
    assert not state_path(project).exists()
    with pytest.raises(SystemExit) as exit_info, contextlib.redirect_stderr(io.StringIO()):
        run(project, "agents", "configure", "--policy", "bogus")  # argparse rejects an unknown policy
    assert exit_info.value.code == 2


def test_the_state_file_cannot_hold_secrets_and_never_touches_contracts(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="unsupported keys"):
        parse_state({"schema_version": 1, "agents": {"claude": {"api_token": "sk-secret"}}})
    with pytest.raises(ConfigValidationError, match="unsupported keys"):
        parse_state({"schema_version": 1, "token": "x"})
    project = plain_project(tmp_path)
    contract = project / ".stagemesh" / "contracts" / "T-1.json"
    digest = hashlib.sha256(contract.read_bytes()).hexdigest()
    configure(project, "--disable", "codex", "--policy", "least_recently_used", "--max-concurrency", "claude=2")
    assert hashlib.sha256(contract.read_bytes()).hexdigest() == digest
    state_text = state_path(project).read_text(encoding="utf-8")
    assert "command" not in state_text and "secret" not in state_text.lower()


# --- routing: pools, skips, policies --------------------------------------------------------------------------------------------------


def fake_pool(tmp_path: Path, modes: dict[str, str | None], config_state: AgentState | None = None, **kwargs):
    """A real Rig (fake provider scripts) whose pool is built from an effective config."""
    project = plain_project(tmp_path / "cfg")
    if config_state is not None:
        state_path(project).write_text(json.dumps(config_state.to_json()), encoding="utf-8")
    config = load_config(project)
    rig = tpp.Rig(tmp_path / "rig", modes, pools={IMPLEMENT: tuple(n for n in modes if n not in config.disabled_agents), REVIEW: tuple(n for n in modes if n not in config.disabled_agents)}, **kwargs)
    return config, rig


def test_disabled_codex_is_not_selected_and_the_log_says_why(tmp_path: Path) -> None:
    from stagemesh.agent_config import pool_kwargs

    config, rig = fake_pool(tmp_path, {"codex": "ok", "claude": "ok", "grok": "ok"}, AgentState(agents={"codex": AgentSettings(enabled=False)}))
    rig.pool.skips = pool_kwargs(config)["skips"]
    verdicts = rig.pool.evaluate(rig.store, IMPLEMENT, tpp.TASK)
    assert {v.provider: v.eligible for v in verdicts} == {"codex": False, "claude": True, "grok": True}
    eligible = rig.pool.announce(rig.store, IMPLEMENT, tpp.TASK, verdicts)
    assert "codex" not in [a.name for a in eligible]
    assert "skipped codex: disabled: turned off in runtime config" in "\n".join(rig.log.lines)
    rig.tick(2)
    assert rig.store.latest_candidate(tpp.TASK)["produced_by"] != "codex"


def test_review_independence_still_avoids_the_candidate_producer(tmp_path: Path) -> None:
    config, rig = fake_pool(tmp_path, {"claude": "ok", "grok": "ok"})
    verdicts = rig.pool.evaluate(rig.store, REVIEW, tpp.TASK, implementer="claude")
    assert {v.provider: (v.eligible, v.reason) for v in verdicts}["claude"] == (False, "not_independent: produced the candidate")
    assert {v.provider: v.eligible for v in verdicts}["grok"] is True
    rig.tick(5)
    payload = rig.review_payload()
    assert payload["implementer_provider"] == "claude" and payload["review_provider"] == "grok" and payload["independent_reviewer"] is True


def test_expires_soon_prefers_the_agent_whose_window_ends_first(tmp_path: Path) -> None:
    now = time.time()
    config, rig = fake_pool(
        tmp_path,
        {"codex": "ok", "claude": "ok", "grok": "ok"},
        policy="expires_soon",
        expires_at={"claude": now + 3 * 3600, "grok": now + 1800, "codex": now - 60},  # codex's window already ended
    )
    eligible = [rig.pool.adapters[n] for n in ("codex", "claude", "grok")]
    ordered, reasons = rig.pool.order(rig.store, IMPLEMENT, eligible)
    assert [a.name for a in ordered] == ["grok", "claude", "codex"]
    assert reasons["grok"].startswith("expires_soon: capacity window ends in 29m") and "soonest" in reasons["grok"]
    assert reasons["codex"].startswith("expires_soon: no pending capacity window")
    verdicts = rig.pool.evaluate(rig.store, IMPLEMENT, tpp.TASK)
    rig.pool.announce(rig.store, IMPLEMENT, tpp.TASK, verdicts)
    text = "\n".join(rig.log.lines)
    assert "preferred provider: grok" in text and "because expires_soon: capacity window ends in" in text
    rig.tick(2)
    assert rig.store.latest_candidate(tpp.TASK)["produced_by"] == "grok"


def test_expires_soon_policy_is_configurable_and_validated(tmp_path: Path) -> None:
    assert "expires_soon" in SELECTION_POLICIES
    project = plain_project(tmp_path)
    configure(project, "--policy", "expires_soon", "--expires-at", "claude=2031-01-01T00:00:00Z,grok=2030-01-01T00:00:00Z")
    config = load_config(project)
    assert config.provider_selection_policy == "expires_soon" and config.agent_expiry["grok"] < config.agent_expiry["claude"]
    assert configure(project, "--expires-at", "claude=none")["state"]["agents"].get("claude") is None  # 'none' clears a window


def test_unhealthy_cooling_and_maxed_out_agents_are_skipped_and_logged(tmp_path: Path) -> None:
    limiter = ProviderLimiter(None, {"claude": 1})
    config, rig = fake_pool(tmp_path, {"codex": None, "claude": "ok", "grok": "ok"}, limiter=limiter)
    rig.pool.record_failure(rig.store, IMPLEMENT, tpp.TASK, "grok", "quota_rate_limit")
    verdicts = {v.provider: v.reason for v in rig.pool.evaluate(rig.store, IMPLEMENT, tpp.TASK)}
    assert verdicts["codex"].startswith("cli_not_installed")  # unhealthy
    assert verdicts["grok"].startswith("recent_failure: quota_rate_limit")  # cooling
    assert verdicts["claude"] == "eligible"
    held = limiter.acquire(["claude"])  # claude is now at its max concurrency of 1
    assert held == "claude"
    rig.tick(1)  # plan
    claude_only = rig.pool.pools[IMPLEMENT]
    assert claude_only == ("codex", "claude", "grok")
    limiter.release("claude")
    # at capacity: with a free alternative the saturated agent hands the task over, and says so
    rig2_config, rig2 = fake_pool(tmp_path / "second", {"claude": "ok", "grok": "ok"}, limiter=ProviderLimiter(None, {"claude": 1}))
    assert rig2.pool.limiter.acquire(["claude"]) == "claude"
    rig2.tick(2)
    text = rig2.text()
    assert "provider claude is at capacity -> using grok" in text and rig2.store.latest_candidate(tpp.TASK)["produced_by"] == "grok"


_PASS_LINE = "    print('{\"decision\":\"PASS\"}'); sys.exit(0)" + chr(10)
MALFORMED_SCRIPT = tpp.SCRIPT.replace(
    _PASS_LINE,
    "    if mode == 'review-malformed':" + chr(10) + "        print('I think this looks fine!'); sys.exit(0)" + chr(10) + _PASS_LINE,
)
assert MALFORMED_SCRIPT != tpp.SCRIPT


def test_malformed_review_json_cools_down_the_reviewer_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tpp, "SCRIPT", MALFORMED_SCRIPT)
    rig = tpp.Rig(tmp_path, {"codex": "ok", "claude": "review-malformed", "grok": "ok"}, pools={IMPLEMENT: ("codex",), REVIEW: ("claude", "grok")})
    rig.tick(4)  # plan, implement, validate, review
    payload = rig.review_payload()
    assert payload["review_provider"] == "grok" and payload["independent_reviewer"] is True
    assert "fallback: claude failed (malformed_review_output" in rig.text() and "final review provider: grok" in rig.text()
    failures = [json.loads(r["payload"]) for r in rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.failure'")]
    assert [(f["provider"], f["stage"]) for f in failures] == [("claude", "REVIEW")] and "malformed_review_output" in failures[0]["reason"]
    assert not rig.store.open_findings_for_candidate(tpp.TASK, rig.store.latest_candidate(tpp.TASK)["sha"])  # never a product finding
    # the reviewer is now cooling for this task and stage
    assert next(v for v in rig.pool.evaluate(rig.store, REVIEW, tpp.TASK, "codex") if v.provider == "claude").reason.startswith("recent_failure")


def test_malformed_review_with_no_other_reviewer_is_infrastructure_not_a_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tpp, "SCRIPT", MALFORMED_SCRIPT)
    rig = tpp.Rig(tmp_path, {"codex": "ok", "claude": "review-malformed"}, pools={IMPLEMENT: ("codex",), REVIEW: ("claude",)})
    rig.tick(4)
    sha = rig.store.latest_candidate(tpp.TASK)["sha"]
    assert not rig.store.open_findings_for_candidate(tpp.TASK, sha)
    row = rig.store.conn.execute("SELECT status FROM evidence WHERE kind='REVIEW'").fetchone()
    assert row["status"] == "CAPACITY" and rig.stage == "REVIEW"  # stays in review; no remediation is spent on the code
    event = rig.store.conn.execute("SELECT payload FROM audit_events WHERE event_type='review.infrastructure_failure'").fetchone()
    assert "malformed_review_output" in json.loads(event["payload"])["reason"]


def test_agent_without_reliable_review_json_is_not_an_implicit_reviewer_and_is_never_preferred(tmp_path: Path) -> None:
    registry = AgentRegistry()
    for pid in ("alpha", "beta"):
        registry.register(AgentPlugin(pid, pid.title(), f"{pid} run", structured_review=pid != "beta"))
    project = plain_project(tmp_path, {"providers": {"alpha": "alpha run", "beta": "beta run"}})
    base = load_base_config(project)
    implicit = apply_agent_state(project, base, registry)
    assert implicit.agent_report["beta"]["stages"] == ["IMPLEMENT"] and "beta" in implicit.agent_unstructured
    assert implicit.agent_skips["REVIEW"]["beta"].startswith("not_structured_review")
    state = AgentState(pools={"IMPLEMENT": ("alpha", "beta"), "REVIEW": ("beta", "alpha")})  # explicitly allowed, listed first
    explicit = apply_agent_state(project, base, registry, state=state)
    assert explicit.provider_pools["REVIEW"] == ("beta", "alpha") and "beta" in explicit.agent_unstructured
    adapters = [RuntimeCommandAdapter("alpha", (PY, "-c", "pass")), RuntimeCommandAdapter("beta", (PY, "-c", "pass"))]
    pool = ProviderPool(adapters, explicit.provider_pools, unstructured=explicit.agent_unstructured, log=ProviderLog(echo=False))
    store = Store(project / ".stagemesh" / "s.sqlite3")
    store.migrate()
    ordered, reasons = pool.order(store, REVIEW, adapters)
    assert [a.name for a in ordered] == ["alpha", "beta"] and reasons["beta"].startswith("last resort")
    store.close()


def test_plugin_response_parser_normalises_a_review_answer_before_it_is_judged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = tpp.SCRIPT.replace("print('{\"decision\":\"PASS\"}'); sys.exit(0)", "print('```json\\n{\"decision\":\"PASS\"}\\n```'); sys.exit(0)")
    monkeypatch.setattr(tpp, "SCRIPT", fenced)
    strip = lambda text: text.replace("```json", "").replace("```", "").strip()  # noqa: E731
    rig = tpp.Rig(tmp_path, {"codex": "ok", "claude": "ok"}, pools={IMPLEMENT: ("codex",), REVIEW: ("claude",)}, response_parsers={"claude": strip})
    rig.tick(5)
    assert rig.review_payload()["review_provider"] == "claude" and rig.stage == "DONE"


# --- queue-run end to end with fake providers -----------------------------------------------------------------------------------------

END_TO_END = (
    "import os, pathlib, sys, time\n"
    "prompt = sys.stdin.read()\n"
    "if 'Review candidate' in prompt:\n"
    "    print('{\"decision\":\"PASS\"}'); sys.exit(0)\n"
    "letter = prompt.split('StageMesh task: task ', 1)[1].split()[0]\n"
    "barrier = pathlib.Path(os.environ['AGENT_BARRIER']); barrier.mkdir(exist_ok=True)\n"
    "(barrier / letter).write_text('x')\n"
    "deadline = time.time() + float(os.environ.get('AGENT_BARRIER_SECONDS', '30'))\n"
    "while len(list(barrier.iterdir())) < 2:\n"
    "    if time.time() > deadline: sys.stderr.write('barrier timeout'); sys.exit(1)\n"
    "    time.sleep(0.05)\n"
    "pathlib.Path('out').mkdir(exist_ok=True)\n"
    "pathlib.Path('out', letter + '.txt').write_text(letter + chr(10))\n"
)


def queue_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: str = "30") -> Path:
    project = _project(tmp_path, ["A", "B"], contracts=[])
    for letter in "AB":
        (project / ".stagemesh" / "contracts" / f"{letter}.json").write_text(
            json.dumps({"objective": f"write out/{letter}.txt", "allowed_files": [f"out/{letter}.txt"], "required_tests": [{"name": "smoke", "command": [PY, "-c", "pass"]}]}),
            encoding="utf-8",
        )
    script = tmp_path / "agent.py"
    script.write_text(END_TO_END, encoding="utf-8")
    (project / ".stagemesh" / "config.json").write_text(
        json.dumps({"providers": {"claude": f'"{PY}" "{script}"'}, "routing": {"require_independent_review": False}}), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_BARRIER", str(tmp_path / "barrier"))
    monkeypatch.setenv("AGENT_BARRIER_SECONDS", seconds)
    GitHead = __import__("stagemesh.git", fromlist=["GitWorkspace"]).GitWorkspace
    git = GitHead(project)
    git.run("add", "-A", "--", ".", ":!.stagemesh")
    git.run("commit", "-q", "--allow-empty", "-m", "prep")
    return project


def test_only_claude_with_max_concurrency_two_runs_two_non_overlapping_tasks_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = queue_project(tmp_path, monkeypatch)
    configure(project, "--enable", "claude", "--disable", "codex,grok", "--max-concurrency", "claude=2")
    code, out, err = run(project, "queue-run", "--concurrency", "2", "--json")  # both providers' barrier only opens if they overlap
    data = json.loads(out)
    assert code == 0 and data["task_outcomes"] == {"A": "DONE", "B": "DONE"}, (out, err)
    assert data["provider_config"]["implementation_pool"] == ["claude"] and data["provider_config"]["review_pool"] == ["claude"]
    producers = {r["task_id"]: r["produced_by"] for r in Store(project / ".stagemesh" / "stagemesh.sqlite3").conn.execute("SELECT task_id, produced_by FROM candidates")}
    assert producers == {"A": "claude", "B": "claude"}


def test_max_concurrency_one_keeps_the_same_agent_from_running_two_tasks_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = queue_project(tmp_path, monkeypatch, seconds="4")
    configure(project, "--enable", "claude", "--disable", "codex,grok", "--max-concurrency", "claude=1")
    code, out, _ = run(project, "queue-run", "--concurrency", "2", "--json")
    outcomes = json.loads(out)["task_outcomes"]
    assert code != 0 and "DONE" not in set(outcomes.values()) or set(outcomes.values()) != {"DONE"}  # they could not overlap, so the barrier timed out


def test_queue_run_logs_the_selected_agent_and_why_others_were_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = queue_project(tmp_path, monkeypatch)
    configure(project, "--enable", "claude", "--disable", "codex,grok", "--max-concurrency", "claude=2", "--policy", "priority")
    code, out, err = run(project, "queue-run", "--concurrency", "2")
    assert code == 0, (out, err)
    assert "selected implementation provider claude" in out and "skipped codex: disabled: turned off in runtime config" in out
    events = [json.loads(r["payload"]) for r in Store(project / ".stagemesh" / "stagemesh.sqlite3").conn.execute("SELECT payload FROM audit_events WHERE event_type='provider.selection'")]
    skipped = {v["provider"]: v["reason"] for e in events if e["stage"] == "IMPLEMENT" for v in e["verdicts"] if not v["eligible"]}
    assert skipped["codex"].startswith("disabled:") and skipped["grok"].startswith("disabled:")


# --- runtime safety and observability -------------------------------------------------------------------------------------------------


def test_a_run_keeps_the_configuration_it_started_with_and_new_runs_see_the_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = queue_project(tmp_path, monkeypatch)
    configure(project, "--enable", "claude", "--disable", "codex,grok")
    started_with = load_config(project)  # what a run loads once, at start
    configure(project, "--enable", "codex,grok", "--implementation", "grok,codex")  # an operator edit while that run is in progress
    assert started_with.disabled_agents == {"codex", "grok"} and "IMPLEMENT" not in started_with.provider_pools
    later = load_config(project)  # the next run
    assert later.disabled_agents == frozenset() and later.provider_pools["IMPLEMENT"] == ("grok", "codex")


def test_capacity_shows_agents_roles_policy_limits_expiry_and_health(tmp_path: Path) -> None:
    project = plain_project(tmp_path)
    configure(project, "--policy", "expires_soon", "--max-concurrency", "claude=2", "--weight", "claude=3", "--expires-at", "claude=2030-01-01T00:00:00Z", "--disable", "grok")
    code, out, _ = run(project, "capacity", "--json")
    data = json.loads(out)
    agents = {a["id"]: a for a in data["agents"]["agents"]}
    assert data["agents"]["policy"] == "expires_soon" and agents["claude"]["max_concurrency"] == 2 and agents["claude"]["weight"] == 3
    assert agents["claude"]["window_active"] and agents["claude"]["expires_at_text"].startswith("2030-01-01T00:00:00Z")
    assert agents["grok"]["enabled"] is False and agents["codex"]["stages"] == ["IMPLEMENT", "REVIEW"]
    assert {"healthy", "health", "cooldown", "recent_use"} <= set(agents["claude"])
    code, text, _ = run(project, "capacity")
    assert "agent claude (Anthropic Claude; built-in): enabled" in text and "capacity window: 2030-01-01T00:00:00Z" in text
    assert "max concurrency: 2 [runtime config]" in text and "DISABLED" in text


def test_load_state_round_trips_and_rejects_unknown_agents(tmp_path: Path) -> None:
    project = plain_project(tmp_path)
    configure(project, "--max-concurrency", "claude=2", "--expires-at", "claude=2030-01-01T00:00:00Z")
    state = load_state(project)
    assert state.agents["claude"].max_concurrency == 2 and state.agents["claude"].expires_at == 1893456000.0
    state_path(project).write_text(json.dumps({"schema_version": 1, "agents": {"ghost": {"enabled": False}}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="unknown agent: ghost"):
        load_config(project)
