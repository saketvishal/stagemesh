"""Operational readiness page (`stagemesh doctor`). No provider, no network and no real `gh`: the PR lister and runtime check are fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_survivor_wave import _commit_on, _main_ref, _with_origin, make_store

import stagemesh.cli as cli_module
from stagemesh.audit import record_audit
from stagemesh.block_kinds import BlockKind, record_block
from stagemesh import readiness
from stagemesh.domain import EvidenceKind, EvidenceStatus
from stagemesh.runtime_freshness import RuntimeFreshness

FRESH = RuntimeFreshness(running_from="src", running_from_source=True, installed_cli="C:/x/stagemesh.exe", source_cli="C:/x/src/stagemesh/cli.py")
STALE = RuntimeFreshness(
    running_from="C:/x/src/stagemesh",
    running_from_source=True,
    installed_cli="C:/x/.stagemesh/tooling/venv/Scripts/stagemesh.exe",
    source_cli="C:/x/src/stagemesh/cli.py",
    missing_from_installed=["survivor-wave", "worktrees"],
    stale=True,
    refresh_command='python "C:/x/scripts/bootstrap_stagemesh.py"',
)


def build(tmp_path: Path, *, prs=lambda project: [], runtime=FRESH, version_warning=None, ids=("T-1",)):
    store, project = make_store(tmp_path, list(ids))
    _with_origin(tmp_path, project)
    config = cli_module.load_config(project)
    return store, project, readiness.build_readiness(store, project, config, version_warning=version_warning, list_prs=prs, runtime=runtime)


def test_a_clean_checkout_is_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    _store, _project, data = build(tmp_path)

    assert data["verdict"] == "ready" and data["attention"] == []
    assert data["main"]["relation"] == "in_sync" and data["activity"] == {"running_executions": 0, "active_claims": 0}
    assert data["wave_prs"] == {"available": True, "prs": []} and data["needs_decision"] == []
    text = "\n".join(readiness.format_readiness(data))
    assert "operational readiness: READY" in text and "open wave PRs: none" in text and "tasks needing a design decision: none" in text


def test_the_stale_runtime_warning_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    _store, _project, data = build(tmp_path, runtime=STALE)

    text = "\n".join(readiness.format_readiness(data))
    assert data["verdict"] == "attention" and "installed runtime is stale" in data["attention"]
    assert "installed command: C:/x/.stagemesh/tooling/venv/Scripts/stagemesh.exe" in text  # exact installed path
    assert "source command:    C:/x/src/stagemesh/cli.py" in text  # exact source path
    assert "missing from installed: survivor-wave, worktrees" in text
    assert 'PYTHONPATH=src python -c "from stagemesh.cli import main; main()" <command>' in text  # safe: run from source now
    assert 'reinstall (not done automatically): python "C:/x/scripts/bootstrap_stagemesh.py"' in text  # the sanctioned refresh


def test_a_version_only_stale_warning_still_shows_how_to_reinstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    _store, project, data = build(tmp_path, version_warning="project-local StageMesh runtime is stale: installed 0.1, repository source 0.2")

    text = "\n".join(readiness.format_readiness(data))
    assert data["runtime"]["stale"] and "installed 0.1, repository source 0.2" in text
    assert str(project / "scripts" / "bootstrap_stagemesh.py") in text


def test_open_wave_prs_are_listed_and_an_unreachable_github_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    prs = [
        {"number": 289, "headRefName": "wave/task-240", "title": "b", "url": "https://example.invalid/289"},
        {"number": 288, "headRefName": "wave/task-248", "title": "a", "url": "https://example.invalid/288"},
    ]
    _s, _p, data = build(tmp_path, prs=lambda project: prs)
    text = "\n".join(readiness.format_readiness(data))
    assert "2 open wave PR(s) waiting for merge" in data["attention"]
    assert text.index("#288") < text.index("#289")  # oldest first: wave PRs are stacked

    def down(project):
        raise RuntimeError("gh: not logged in")

    _s, _p, offline = build(tmp_path / "off", prs=down)
    assert offline["wave_prs"]["available"] is False and "not logged in" in offline["wave_prs"]["reason"]
    assert offline["verdict"] == "ready"  # an unanswered question is not a finding
    _s, _p, skipped = build(tmp_path / "skip", prs=None)
    assert skipped["wave_prs"]["reason"].startswith("skipped")


def test_local_main_ahead_and_inflight_work_are_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh.domain import ExecutionKind

    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["T-1"])
    _with_origin(tmp_path, project)
    _commit_on(project, _main_ref(project), "local-only")
    store.start_execution(task_id="T-1", claim_id=None, kind=ExecutionKind.VALIDATION)
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    assert data["main"]["relation"] == "ahead" and data["main"]["ahead"] == 1
    assert data["activity"]["running_executions"] == 1
    assert any("is ahead origin" in item for item in data["attention"]) and any("in flight" in item for item in data["attention"])
    assert "ahead 1, behind 0" in "\n".join(readiness.format_readiness(data))


def test_only_genuine_human_decision_blocks_are_listed_as_needing_a_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])
    sha = "d" * 40
    store.add_candidate("T-1", sha, "codex", True)
    store.add_evidence("T-1", sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    store.upsert_finding("f1", "T-1", sha, "error", "design question")
    record_block(store, "T-1", BlockKind.HUMAN_DECISION, "review finding states a decision is needed: which retention period applies?")
    # a spent remediation budget is not a question for a person, with or without failed review evidence behind it
    store.add_candidate("T-2", "e" * 40, "codex", True)
    store.add_evidence("T-2", "e" * 40, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    store.block_task("T-2")
    record_audit(store, "task.remediation_exhausted", {"task_id": "T-2", "candidate_sha": "e" * 40, "stage": "REVIEW"})
    store.block_task("T-3")  # blocked with no recorded cause: not a design decision either
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    assert [item["task_id"] for item in data["needs_decision"]] == ["T-1"]
    assert "1 task(s) need a design decision" in data["attention"]
    assert "which retention period applies?" in "\n".join(readiness.format_readiness(data))


def test_provider_cooldowns_and_live_check_state_are_shown_per_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        readiness,
        "active_cooldowns",
        lambda store, seconds: [{"provider": "grok", "failure_class": "quota_rate_limit", "seconds_remaining": 600, "clear_command": "stagemesh cooldown clear grok"}],
    )
    _s, _p, data = build(tmp_path)

    by_name = {item["provider"]: item for item in data["providers"]}
    assert {"codex", "claude", "grok", "agy"} <= set(by_name)
    assert by_name["grok"]["cooldown"]["failure_class"] == "quota_rate_limit" and by_name["codex"]["cooldown"] is None
    assert by_name["codex"]["live_acceptance"]["state"] == "missing"  # never checked live: said so, not "ok"
    assert by_name["codex"]["command_available"] in (True, False)
    text = "\n".join(readiness.format_readiness(data))
    assert "provider grok: COOLING DOWN (quota_rate_limit, 600s left; clear: stagemesh cooldown clear grok)" in text
    assert "provider grok is cooling down (quota_rate_limit)" in data["attention"]


def test_the_worktree_census_counts_a_plain_directory_as_other_not_as_a_dirty_worktree(tmp_path: Path) -> None:
    from stagemesh.domain import Stage
    from stagemesh.survivor_wave import git_repo_state
    from stagemesh.workspaces import _task_key, worktree_root

    store, project = make_store(tmp_path, ["T-1"])
    store.advance_task("T-1", Stage.DONE)
    root = worktree_root(project)
    (root / _task_key("T-1")).mkdir(parents=True)  # a leftover directory with no .git: git inside it would resolve to the parent repo
    (root / _task_key("T-1") / ".tmp-install").mkdir()

    state = git_repo_state(store, project, _main_ref(project))()

    assert (state.other_worktrees, state.cleanable_worktrees, state.done_unmerged_worktrees) == (1, 0, 0)


def test_doctor_prints_and_exports_the_readiness_section_without_touching_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    store.close()
    monkeypatch.setattr(readiness, "check_runtime", lambda project: STALE)
    monkeypatch.setattr(readiness, "gh_open_wave_prs", lambda project: pytest.fail("--no-network must not call gh"))

    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0
    text = capsys.readouterr().out
    assert "operational readiness: ATTENTION" in text and "missing from installed: survivor-wave, worktrees" in text
    assert "open wave PRs: not checked" in text and "local main:" in text and "worktrees: cleanable" in text

    assert cli_module.main(["--project", str(project), "doctor", "--no-network", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["readiness"]["runtime"]["stale"] is True and data["readiness"]["wave_prs"]["available"] is False
    assert set(data["readiness"]) >= {"runtime", "wave_prs", "main", "activity", "worktrees", "needs_decision", "providers", "verdict"}


# --- live acceptance readiness -------------------------------------------------------------------------------------------------------


def _record_live(store, provider: str, *, passed: bool, age_seconds: float = 0.0) -> None:
    import time

    from stagemesh.domain import ExecutionStatus
    from stagemesh.provider_acceptance import LiveProviderSmokeResult

    payload = LiveProviderSmokeResult(
        "PASS" if passed else "FAIL",
        provider,
        ExecutionStatus.SUCCEEDED if passed else ExecutionStatus.FAILED,
        "candidate-sha",
        True,  # file_ok
        True,  # implementation_checked
        True,  # review_checked
        passed,  # implementation_ready
        passed,  # review_ready
        passed,  # review_json_ok
        None if passed else "provider_timeout",
    ).to_payload()
    payload["checked_at"] = time.time() - age_seconds
    store.add_audit_event("provider.live_acceptance", payload)


def test_live_acceptance_is_classified_missing_failed_stale_or_passing(tmp_path: Path) -> None:
    from stagemesh.provider_acceptance import (
        DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS,
        live_acceptance_status,
    )

    store, _project = make_store(tmp_path, ["T-1"])
    _record_live(store, "ok", passed=True)
    _record_live(store, "broken", passed=False)
    _record_live(store, "old", passed=True, age_seconds=DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS + 60)

    states = {name: live_acceptance_status(store, name) for name in ("ok", "broken", "old", "never")}

    assert {name: item["state"] for name, item in states.items()} == {"ok": "passing", "broken": "failed", "old": "stale", "never": "missing"}
    assert states["old"]["age_seconds"] > DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS and states["ok"]["ttl_seconds"] == DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS
    assert "provider_timeout" in states["broken"]["reason"] and "--live --provider old" in states["old"]["reason"]


def test_the_live_acceptance_freshness_window_is_configurable(tmp_path: Path) -> None:
    from stagemesh.config import (
        DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS,
        ConfigValidationError,
        load_config,
    )
    from stagemesh.provider_acceptance import live_acceptance_status

    store, project = make_store(tmp_path, ["T-1"])
    _record_live(store, "p", passed=True, age_seconds=2 * 3600)  # two hours old

    assert load_config(project).live_acceptance_ttl_seconds == DEFAULT_LIVE_ACCEPTANCE_TTL_SECONDS == 7 * 86400
    assert live_acceptance_status(store, "p")["state"] == "passing"
    assert live_acceptance_status(store, "p", ttl_seconds=3600)["state"] == "stale"  # a one-hour window makes it stale

    config_path = project / ".stagemesh" / "config.json"
    config_path.write_text(json.dumps({"routing": {"live_acceptance_ttl_seconds": 3600}}), encoding="utf-8")
    assert load_config(project).live_acceptance_ttl_seconds == 3600.0
    config_path.write_text(json.dumps({"routing": {"live_acceptance_ttl_seconds": 0}}), encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="live_acceptance_ttl_seconds"):
        load_config(project)


def test_missing_live_checks_are_informational_by_default_and_fail_closed_in_strict_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["T-1"])
    _with_origin(tmp_path, project)
    config = cli_module.load_config(project)

    relaxed = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)
    strict = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH, strict_live=True)

    assert relaxed["verdict"] == "ready" and not any("live acceptance" in item for item in relaxed["attention"])  # informational
    assert strict["verdict"] == "attention" and any("strict mode requires" in item for item in strict["attention"])
    relaxed_text = "\n".join(readiness.format_readiness(relaxed))
    strict_text = "\n".join(readiness.format_readiness(strict))
    assert "informational" in relaxed_text and "REQUIRED (strict)" in strict_text

    for name in (item["provider"] for item in strict["providers"]):
        _record_live(store, name, passed=True)
    passing = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH, strict_live=True)
    assert passing["verdict"] == "ready" and all(item["live_acceptance"]["state"] == "passing" for item in passing["providers"])


def test_doctor_strict_live_exits_nonzero_but_the_default_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    store.close()
    monkeypatch.setattr(readiness, "check_runtime", lambda project: FRESH)

    assert cli_module.main(["--project", str(project), "doctor", "--no-network"]) == 0
    assert cli_module.main(["--project", str(project), "doctor", "--no-network", "--strict-live"]) == 1


def test_a_cooling_provider_does_not_block_the_others_and_capacity_shows_live_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    for name in ("codex", "claude", "agy"):
        _record_live(store, name, passed=True)
    store.close()
    monkeypatch.setattr(
        cli_module,
        "active_cooldowns",
        lambda store, seconds: [{"provider": "grok", "failure_class": "quota_rate_limit", "seconds_remaining": 600, "clear_command": "x", "stages": ["IMPLEMENT"], "action": "wait"}],
    )

    assert cli_module.main(["--project", str(project), "capacity", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    rows = {row["provider"]: row for row in data["providers"]}
    assert rows["codex"]["live_status"]["state"] == "passing" and rows["grok"]["live_status"]["state"] == "missing"
    assert rows["codex"]["command_available"] in (True, False) and "live_readiness" in rows["codex"]
    assert [item["provider"] for item in data["provider_cooldowns"]] == ["grok"]  # only grok is cooling; the others are untouched


def test_the_ledger_separates_retired_and_parked_tasks_from_real_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from stagemesh.domain import Stage

    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3", "T-4"])
    store.advance_task("T-1", Stage.DONE)

    def retire(task_id: str, reason: str) -> None:
        task = store.get_task(task_id)
        store.cache_source(task["source"], task["source_id"], {"retirement_reason": reason}, "CLOSED" if reason == "source closed" else "OPEN")
        store.block_task(task_id)

    retire("T-2", "source closed")
    retire("T-3", "source no longer eligible")
    store.block_task("T-4")  # a genuinely blocked task: this is the real work
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    ledger = data["ledger"]
    assert (ledger["done"], ledger["retired_closed"], ledger["retired_ineligible"], ledger["blocked"], ledger["open"]) == (1, 1, 1, 1, 0)
    assert [t["task_id"] for t in ledger["live_tasks"]] == ["T-4"]  # neither the closed nor the parked task is listed as work
    text = "\n".join(readiness.format_readiness(data))
    assert "1 retired (source closed); 1 parked" in text and "1 blocked" in text and "T-4" in text


def _ledger_with_retired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sources: dict[str, str]):
    from stagemesh.domain import TaskStatus  # noqa: F401

    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["R-github", "R-local", "T-blocked", "T-design"])
    for task_id, source in sources.items():
        store.conn.execute("UPDATE tasks SET source=?, source_id=? WHERE id=?", (source, task_id, task_id))
    store.conn.commit()
    return store, project


def test_audited_retirement_of_github_and_local_backlog_tasks_is_not_real_blocked_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = _ledger_with_retired(tmp_path, monkeypatch, {"R-github": "github", "R-local": "local-backlog"})
    assert store.retire_source_task("github", "R-github", "already satisfied on main") == "R-github"
    assert store.retire_source_task("local-backlog", "R-local", "duplicate local proof task") == "R-local"
    store.block_task("T-blocked")  # genuinely blocked, actionable
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    ledger = data["ledger"]
    assert ledger["retired_operator"] == 2
    assert [t["task_id"] for t in ledger["live_tasks"]] == ["T-design", "T-blocked"] or {t["task_id"] for t in ledger["live_tasks"]} == {"T-blocked", "T-design"}
    assert "R-github" not in {t["task_id"] for t in ledger["live_tasks"]} and "R-local" not in {t["task_id"] for t in ledger["live_tasks"]}
    assert "2 retired (audited, not work)" in "\n".join(readiness.format_readiness(data))


def test_a_restored_audited_retirement_counts_as_work_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = _ledger_with_retired(tmp_path, monkeypatch, {"R-github": "github"})
    store.retire_source_task("github", "R-github", "already satisfied")
    store.restore_source_task("github", "R-github", "already satisfied")
    config = cli_module.load_config(project)

    ledger = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)["ledger"]

    assert ledger["retired_operator"] == 0 and "R-github" in {t["task_id"] for t in ledger["live_tasks"]}


def test_design_decision_tasks_are_still_reported_separately_from_retired_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = _ledger_with_retired(tmp_path, monkeypatch, {"R-local": "local-backlog"})
    store.retire_source_task("local-backlog", "R-local", "duplicate local proof task")
    monkeypatch.setattr(readiness, "blocked_needs_decision", lambda s, task_id: "needs a human design decision" if task_id == "T-design" else None)
    store.block_task("T-design")
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    assert [d["task_id"] for d in data["needs_decision"]] == ["T-design"]
    assert "R-local" not in {t["task_id"] for t in data["ledger"]["live_tasks"]}


def _block_on_provider_failure(store, task_id: str, provider: str, remaining: float, reason: str = "quota_rate_limit") -> None:
    import time

    store.add_audit_event("provider.failure", {"task_id": task_id, "stage": "IMPLEMENT", "provider": provider, "reason": reason, "retry_at": time.time() + remaining})
    store.add_audit_event("task.diagnosis_stop", {"task_id": task_id, "category": "provider_no_progress"})
    store.block_task(task_id)


def _cooldown_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining: float):
    store, project = make_store(tmp_path, ["T-cool", "T-real"])
    _block_on_provider_failure(store, "T-cool", "grok", remaining)
    store.block_task("T-real")  # blocked for no provider reason: actionable
    config = cli_module.load_config(project)
    return readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)


def test_a_task_blocked_on_a_long_provider_cooldown_is_parked_not_real_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _cooldown_ledger(tmp_path, monkeypatch, remaining=3 * 3600)

    ledger = data["ledger"]
    assert ledger["parked_cooldown"] == 1 and ledger["blocked"] == 1
    assert [t["task_id"] for t in ledger["live_tasks"]] == ["T-real"]
    waiting = ledger["parked_cooldown_tasks"][0]["waiting_on"][0]
    assert waiting["provider"] == "grok" and 0 < waiting["seconds_remaining"] <= 3 * 3600
    text = "\n".join(readiness.format_readiness(data))
    assert "1 parked: provider cooldown" in text and "parked: provider cooldown - T-cool" in text and "grok 2h5" in text


def test_a_short_provider_cooldown_stays_real_work_and_becomes_actionable_when_it_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    short = _cooldown_ledger(tmp_path, monkeypatch, remaining=300)["ledger"]
    assert short["parked_cooldown"] == 0 and {t["task_id"] for t in short["live_tasks"]} == {"T-cool", "T-real"}

    expired = _cooldown_ledger(tmp_path / "expired", monkeypatch, remaining=-10)["ledger"]
    assert expired["parked_cooldown"] == 0 and "T-cool" in {t["task_id"] for t in expired["live_tasks"]}
