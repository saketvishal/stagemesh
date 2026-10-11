"""Operational readiness page (`stagemesh doctor`). No provider, no network and no real `gh`: the PR lister and runtime check are fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_survivor_wave import _commit_on, _main_ref, _with_origin, make_store

import stagemesh.cli as cli_module
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


def test_tasks_blocked_on_unresolved_review_findings_are_listed_as_needing_a_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])
    store, project = make_store(tmp_path, ["T-1", "T-2"])
    sha = "d" * 40
    store.add_candidate("T-1", sha, "codex", True)
    store.add_evidence("T-1", sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
    store.upsert_finding("f1", "T-1", sha, "error", "design question")
    store.block_task("T-1")
    store.block_task("T-2")  # blocked, but not on review findings: not a design decision
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    assert [item["task_id"] for item in data["needs_decision"]] == ["T-1"]
    assert "1 task(s) need a design decision" in data["attention"]
    assert "T-1 task T-1: review findings on ddddddd" in "\n".join(readiness.format_readiness(data))


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
    assert by_name["codex"]["live_acceptance"].startswith("live_acceptance_unproven")  # never checked live: said so, not "ok"
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
