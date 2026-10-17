"""Unattended run window. No provider, no network, no real `gh`: hooks, PR lister, runtime check and provider check are fakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_survivor_wave import Rig, make_store

import stagemesh.cli as cli_module
import stagemesh.run_window as rw
from stagemesh import readiness
from stagemesh.domain import ExecutionKind, Stage
from stagemesh.runtime_freshness import RuntimeFreshness
from stagemesh.survivor_wave import ORPHAN_EVENT, RepoState, RunResult
from stagemesh.window_journal import last_window, record_started

FRESH = RuntimeFreshness(running_from="src", running_from_source=True, installed_cli="x", source_cli="y")
STALE = RuntimeFreshness(installed_cli="C:/x/stagemesh.exe", missing_from_installed=["run-window"], stale=True, refresh_command="python bootstrap")
OK_PROVIDERS = lambda store, config, strict_live: rw.Check("providers", True, "fake providers")
IN_SYNC = RepoState(main_ref="main", main_sha="a" * 40, origin_sha="a" * 40, relation="in_sync")


@pytest.fixture(autouse=True)
def _no_real_runtime_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "check_runtime", lambda project: FRESH)  # build_readiness would otherwise run two CLI subprocesses
    monkeypatch.setattr(readiness, "active_cooldowns", lambda store, seconds: [])


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def window(tmp_path: Path, ids: list[str], behaviour: dict | None = None, **opts):
    store, project = make_store(tmp_path, ids)
    rig = Rig(project, behaviour)
    hooks = rig.hooks()
    hooks.repo_state = lambda: IN_SYNC
    options = rw.WindowOptions(report_path=tmp_path / "out" / "window.json", **opts)
    run_kwargs = {
        "runtime": opts.pop("_runtime", FRESH),
        "list_prs": lambda project: [],
        "provider_check": OK_PROVIDERS,
        "clock": Clock(),
    }
    config = cli_module.load_config(project)
    return store, project, rig, hooks, options, config, run_kwargs


def go(tmp_path: Path, ids: list[str], behaviour: dict | None = None, hooks_edit=None, **opts):
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ids, behaviour, **opts)
    if hooks_edit:
        hooks_edit(hooks, rig, store)
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    return report, store, rig, options


# --- parsing and selection -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "seconds"), [("2h", 7200), ("90m", 5400), ("45s", 45), ("1.5h", 5400), ("30", 30), (" 2H ", 7200)])
def test_durations_parse(text: str, seconds: float) -> None:
    assert rw.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "0h", "-5m", "2 hours"])
def test_bad_durations_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="duration must look like"):
        rw.parse_duration(text)


def test_selection_picks_only_runnable_tasks_and_never_blocked_parked_or_claimed_ones(tmp_path: Path) -> None:
    store, _project = make_store(tmp_path, ["10", "2", "T-blocked", "T-parked", "T-claimed", "T-done"])
    store.advance_task("T-done", Stage.DONE)
    store.block_task("T-blocked")
    with store.conn:  # fixture tasks are local; make this one GitHub-sourced so the upstream-eligibility rule applies to it
        store.conn.execute("UPDATE tasks SET source='github', source_id='T-parked' WHERE id='T-parked'")
    store.cache_source("github", "T-parked", {"state": "OPEN", "eligible": False}, "OPEN")  # open upstream, not runnable
    store.start_execution(task_id="T-claimed", claim_id=None, kind=ExecutionKind.VALIDATION)

    picked, skipped = rw.select_tasks(store, attempted=set(), limit=10)

    assert picked == ["2", "10"]  # numeric order: oldest first
    assert "T-blocked" not in skipped and "T-done" not in skipped  # blocked work is never news in automatic mode
    assert skipped["T-parked"] == "source no longer eligible" or "eligible" in skipped["T-parked"]
    assert "T-claimed" in skipped
    assert rw.select_tasks(store, attempted={"2"}, limit=1)[0] == ["10"]  # a task attempted this window is not retried
    _, named = rw.select_tasks(store, attempted=set(), limit=10, only=["T-blocked", "T-nope"])
    assert "not picked automatically" in named["T-blocked"] and named["T-nope"] == "no such task"


# --- the happy path --------------------------------------------------------------------------------------------------------------


def test_a_window_runs_eligible_tasks_opens_prs_and_reports_everything(tmp_path: Path) -> None:
    report, store, rig, options = go(tmp_path, ["1", "2"], concurrency=2)

    assert report.ok and report.exit_code == 0 and not report.refused and not report.stopped_early
    assert sorted(rig.runs) == ["1", "2"] and report.counts() == {"done": 2}
    assert [t.landing for t in report.tasks] == ["pr_opened", "pr_opened"] and rig.prs == ["1", "2"]
    assert report.options.integration_mode == "pr" and report.next_command.startswith("review and merge the open wave PRs")
    assert not store.has_active_claim("1") and list(store.running_executions()) == []
    data = json.loads(options.report_path.read_text(encoding="utf-8"))
    assert data["window_id"] == report.window_id and data["counts"] == {"done": 2} and data["readiness"]["ledger"]["done"] == 2
    window_entry = last_window(store)
    assert window_entry["state"] == "finished" and window_entry["ok"] is True and len(window_entry["prs"]) == 2
    text = rw.format_window_report(report)
    for needle in ("run window", "preflight:", "what ran: 2 task(s)", "provider health:", "ledger:", "left behind: 0 running", "next:"):
        assert needle in text


def test_an_empty_backlog_is_a_clean_window_with_nothing_to_do(tmp_path: Path) -> None:
    report, _store, rig, _ = go(tmp_path, [])

    assert report.ok and rig.runs == [] and report.selected == []
    assert report.next_command == "nothing to do: no eligible work left"
    assert "ended: no eligible work left" in rw.format_window_report(report)


def test_the_task_limit_bounds_a_window(tmp_path: Path) -> None:
    report, _store, rig, _ = go(tmp_path, ["1", "2", "3", "4"], max_tasks=2, concurrency=1)

    assert len(rig.runs) == 2 and report.task_limit_reached and report.ok


def test_the_time_budget_stops_cleanly_and_leaves_the_rest_for_the_next_window(tmp_path: Path) -> None:
    clock = Clock()

    def slow(rig: Rig, task_id: str) -> RunResult:
        clock.now += 3600  # each task "takes" an hour
        return rig.finish_done(task_id)

    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1", "2", "3", "4"], {t: slow for t in "1234"}, duration_seconds=5400, concurrency=1)
    kwargs["clock"] = clock

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)

    assert report.time_limit_reached and report.ok and not report.stopped_early
    assert rig.runs == ["1", "2"]  # the second started inside the budget; the next batch was never picked
    assert store.get_task("3")["status"] == "OPEN" and store.get_task("4")["status"] == "OPEN"  # untouched, ready for the next window
    assert not store.has_active_claim("1") and list(store.running_executions()) == []
    assert "ended: time budget reached (clean stop)" in rw.format_window_report(report)


def test_a_task_waiting_inside_the_batch_when_the_budget_ends_is_not_started(tmp_path: Path) -> None:
    clock = Clock()

    def slow(rig: Rig, task_id: str) -> RunResult:
        clock.now += 3600
        return rig.finish_done(task_id)

    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1", "2"], {t: slow for t in "12"}, duration_seconds=1800, concurrency=1)
    kwargs["clock"] = clock

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)

    assert rig.runs == ["1"] and report.time_limit_reached and report.ok
    waiting = next(t for t in report.tasks if t.task_id == "2")
    assert waiting.outcome == "not_started" and "time budget reached" in waiting.reason


# --- stop rules ------------------------------------------------------------------------------------------------------------------


def test_a_new_orphan_event_stops_the_window(tmp_path: Path) -> None:
    from stagemesh.audit import record_audit

    def orphan(rig: Rig, task_id: str) -> RunResult:
        worker = rig.worker_store()
        try:
            record_audit(worker, ORPHAN_EVENT, {"task_id": task_id})
        finally:
            worker.close()
        return rig.finish_done(task_id)

    report, _s, rig, _ = go(tmp_path, ["1", "2", "3"], {"1": orphan}, concurrency=1)

    assert not report.ok and report.exit_code == 1 and "orphan" in report.stop_reason and rig.runs == ["1"]
    assert "doctor" in report.next_command


def test_the_same_infrastructure_failure_on_two_tasks_stops_the_window(tmp_path: Path) -> None:
    fail = RunResult("CURRENT_PROBLEM", "health has current problems: stale_running_executions")

    report, _s, rig, _ = go(tmp_path, ["1", "2", "3", "4"], {"1": fail, "2": fail}, concurrency=1)

    assert not report.ok and "shared infrastructure failure on tasks 1, 2" in report.stop_reason and rig.runs == ["1", "2"]


def test_local_main_moving_in_pr_mode_stops_the_window(tmp_path: Path) -> None:
    tips = iter(["a" * 40, "b" * 40, "b" * 40, "b" * 40])  # before the window, then after task 1

    def edit(hooks, rig, store) -> None:
        hooks.main_sha = lambda: next(tips)

    report, _s, rig, _ = go(tmp_path, ["1", "2", "3"], hooks_edit=edit, concurrency=1)

    assert not report.ok and "local main moved in PR mode" in report.stop_reason and rig.runs == ["1"]


def test_an_execution_left_running_after_its_runner_exits_stops_the_window(tmp_path: Path) -> None:
    def leak(rig: Rig, task_id: str) -> RunResult:
        worker = rig.worker_store()
        try:
            worker.start_execution(task_id=task_id, claim_id=None, kind=ExecutionKind.VALIDATION)  # a provider process that never finished
        finally:
            worker.close()
        return rig.finish_done(task_id)

    def edit(hooks, rig, store) -> None:
        from stagemesh.survivor_wave import store_leftover

        hooks.leftover = store_leftover(store)

    report, _store, rig, _ = go(tmp_path, ["1", "2"], {"1": leak}, hooks_edit=edit, concurrency=1)

    assert not report.ok and "provider process cleanup failure: task 1 still has 1 RUNNING execution" in report.stop_reason
    assert rig.runs == ["1"] and report.tasks[0].outcome == "infrastructure_failure"


def test_a_stale_installed_runtime_refuses_to_start_unless_explicitly_allowed(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    kwargs["runtime"] = STALE

    refused = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    assert refused.refused and refused.exit_code == 2 and rig.runs == []
    assert "installed CLI is stale: it lacks run-window" in refused.stop_reason and refused.next_command == "python bootstrap"
    assert last_window(store)["state"] == "refused"

    options.allow_stale_runtime = True
    allowed = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)
    assert not allowed.refused and rig.runs == ["1"]
    assert any("allowed by --allow-stale-runtime" in c.detail for c in allowed.preflight)


def test_a_damaged_ledger_refuses_to_start(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])

    class Conn:
        def __init__(self, real) -> None:
            self.real = real

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA quick_check"):
                return self.real.execute("SELECT 'database disk image is malformed'")
            return self.real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.real, name)

    store.conn = Conn(store.conn)

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    assert report.refused and "ledger integrity check failed: database disk image is malformed" in report.stop_reason and rig.runs == []
    assert "do not edit it by hand" in report.preflight[1].fix


def test_an_unwritable_report_path_refuses_to_start_and_a_report_lost_mid_run_is_a_distinct_exit(tmp_path: Path) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    options.report_path = blocker / "sub" / "window.json"  # a "directory" that is a file

    refused = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)
    assert refused.refused and "cannot write the report" in refused.stop_reason and rig.runs == []

    target = tmp_path / "rep" / "window.json"

    def vanish(rig: Rig, task_id: str) -> RunResult:
        target.unlink()
        target.mkdir()  # the report path becomes a directory while the window runs
        return rig.finish_done(task_id)

    report, _s, _rig, _ = go(tmp_path / "second", ["1"], {"1": vanish}, hooks_edit=None)
    del report  # the helper uses its own report path; exercise the mid-run loss directly below

    store2, project2, _rig2, hooks2, options2, config2, kwargs2 = window(tmp_path / "third", ["1"], {"1": vanish})
    options2.report_path = target
    lost = rw.run_window(store2, project2, config2, options2, hooks2, base_ref="refs/heads/main", **kwargs2)
    assert lost.exit_code == 3 and "cannot write the report" in lost.report_error and not lost.ok
    assert "REPORT ERROR" in rw.format_window_report(lost)


def test_a_provider_setup_that_cannot_review_independently_refuses_to_start(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    seen: dict = {}

    def no_review(store, config, strict_live):
        seen["strict_live"] = strict_live
        return rw.Check("providers", False, "independent review is required but cannot be satisfied", fix="stagemesh capacity")

    kwargs["provider_check"] = no_review
    options.strict_live = True

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    assert report.refused and rig.runs == [] and seen["strict_live"] is True and report.next_command == "stagemesh capacity"


def test_a_local_main_that_is_ahead_or_diverged_refuses_pr_mode(tmp_path: Path) -> None:
    for relation in ("ahead", "diverged"):
        store, project, rig, hooks, options, config, kwargs = window(tmp_path / relation, ["1"])
        hooks.repo_state = lambda relation=relation: RepoState(main_ref="main", main_sha="a" * 40, origin_sha="b" * 40, ahead=2, behind=0, relation=relation)

        report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

        assert report.refused and f"local main is {relation} origin" in report.stop_reason and rig.runs == []


# --- recovery and resume ---------------------------------------------------------------------------------------------------------


def _fake_control(monkeypatch: pytest.MonkeyPatch, states: list[str]):
    rows = [{"id": f"e{i}", "task_id": "1", "process_state": s, "kind": "IMPLEMENTATION", "pid": 1} for i, s in enumerate(states)]
    calls: list[str] = []
    state = {"rows": rows}

    def control(store):
        return {"state": "running", "active_executions": list(state["rows"])}

    class Action:
        execution_id, action = "e0", "RELEASED"

        def to_dict(self):
            return {"execution_id": "e0", "kind": "IMPLEMENTATION", "pid": 1, "process_state": "DEAD", "action": "RELEASED"}

    def recover(store, task_id):
        calls.append(task_id)
        state["rows"] = [r for r in state["rows"] if r["process_state"] != "DEAD"]
        return [Action()]

    monkeypatch.setattr(rw, "queue_control_report", control)
    monkeypatch.setattr(rw, "recover_stale", recover)
    return calls


def test_dead_executions_are_recovered_automatically_before_the_window_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_control(monkeypatch, ["DEAD"])

    report, _s, rig, _ = go(tmp_path, ["1"])

    assert calls == ["1"] and report.ok and rig.runs == ["1"]
    assert report.recovered[0]["action"] == "RELEASED"
    assert "recovered dead executions: e0 (task 1: RELEASED)" in rw.format_window_report(report)


@pytest.mark.parametrize("state", ["LIVE", "UNKNOWN"])
def test_live_or_unknown_executions_fail_closed_with_the_operator_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    calls = _fake_control(monkeypatch, [state])

    report, _s, rig, _ = go(tmp_path, ["1"])

    assert calls == [] and report.refused and rig.runs == []
    if state == "UNKNOWN":
        assert "recover-stale --task 1 --release-unknown --execution e0 --reason" in report.stop_reason
    else:
        assert "is LIVE" in report.stop_reason


def test_a_paused_queue_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rw, "queue_control_report", lambda store: {"state": "paused", "active_executions": []})

    report, _s, _rig, _ = go(tmp_path, ["1"])

    assert report.refused and "queue admission is paused" in report.stop_reason and report.next_command == "stagemesh queue-control resume"


def test_doctor_shows_an_interrupted_window_and_what_to_do(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["1"])
    record_started(store, "abc12345", {"mode": "pr", "duration_seconds": 7200})
    config = cli_module.load_config(project)

    data = readiness.build_readiness(store, project, config, list_prs=None, runtime=FRESH)

    assert data["window"]["state"] == "interrupted" and data["verdict"] == "attention"
    assert any("abc12345 was interrupted" in item for item in data["attention"])
    text = "\n".join(readiness.format_readiness(data))
    assert "last run-window abc12345: INTERRUPTED" in text and "recovers dead executions" in text


def test_an_interrupt_during_the_window_still_writes_the_finished_journal_entry(tmp_path: Path) -> None:
    def interrupt(rig: Rig, task_id: str) -> RunResult:
        raise KeyboardInterrupt

    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, ["1"], {"1": interrupt}, concurrency=1)

    with pytest.raises(KeyboardInterrupt):
        rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    entry = last_window(store)
    assert entry["state"] == "finished" and entry["ok"] is False and "interrupted: KeyboardInterrupt" in entry["stop_reason"]


# --- PR-mode safety and the report -----------------------------------------------------------------------------------------------


def test_older_open_wave_prs_are_detected_across_runs_and_listed_in_merge_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    older = [
        {"number": 301, "headRefName": "wave/task-9", "title": "older", "url": "https://example.invalid/301"},
        {"number": 288, "headRefName": "wave/task-8", "title": "oldest", "url": "https://example.invalid/288"},
    ]
    kwargs["list_prs"] = lambda project: [*older, {"number": 302, "headRefName": "wave/task-1", "title": "task 1", "url": "https://example.invalid/pr/1"}]
    monkeypatch.setattr(readiness, "merge_order", lambda project, prs: sorted(prs, key=lambda pr: pr["number"]))  # no git here: number order

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    assert [pr["number"] for pr in report.open_wave_prs] == [288, 301, 302]
    assert [pr["opened_this_window"] for pr in report.open_wave_prs] == [False, False, True]
    text = rw.format_window_report(report)
    assert text.index("1. #288") < text.index("2. #301") < text.index("3. #302") and "(this window)" in text
    assert report.next_command.startswith("review and merge the open wave PRs oldest first")
    assert any(entry for entry in report.readiness["attention"] if "open wave PR" in entry)


def test_wave_prs_are_ordered_by_their_stack_not_by_pr_number(tmp_path: Path) -> None:
    """Branches are stacked in integration order; PR numbers are assigned later, in task order, so they can disagree."""
    from test_survivor_wave import _commit_on, _git, _with_origin

    _store, project = make_store(tmp_path, ["1"])
    _with_origin(tmp_path, project)
    main_ref = _git(project, "symbolic-ref", "HEAD")
    base = _git(project, "rev-parse", main_ref)
    _git(project, "branch", "staging", base)
    first = _commit_on(project, "refs/heads/staging", "w5")  # integrated first
    second = _commit_on(project, "refs/heads/staging", "w4")
    third = _commit_on(project, "refs/heads/staging", "w6")
    for name, sha in (("wave/task-w5", first), ("wave/task-w4", second), ("wave/task-w6", third)):
        _git(project, "push", "-q", "origin", f"{sha}:refs/heads/{name}")
    _git(project, "fetch", "-q", "origin")
    prs = [  # opened in task order, so PR numbers disagree with the stack
        {"number": 297, "headRefName": "wave/task-w4"},
        {"number": 298, "headRefName": "wave/task-w5"},
        {"number": 299, "headRefName": "wave/task-w6"},
    ]

    ordered = readiness.merge_order(project, prs, base=f"origin/{main_ref.removeprefix('refs/heads/')}")

    assert [pr["number"] for pr in ordered] == [298, 297, 299]


def test_blocked_tasks_are_reported_with_the_reason_and_the_window_carries_on(tmp_path: Path) -> None:
    def blocked(rig: Rig, task_id: str) -> RunResult:
        worker = rig.worker_store()
        try:
            worker.block_task(task_id)
        finally:
            worker.close()
        return RunResult("BLOCKED", "task exhausted its remediation budget")

    report, _s, _rig, _ = go(tmp_path, ["1", "2"], {"1": blocked}, concurrency=1)

    outcomes = {t.task_id: t.outcome for t in report.tasks}
    assert outcomes == {"1": "blocked", "2": "done"} and report.ok
    assert "why: task exhausted its remediation budget" in rw.format_window_report(report)


def test_a_window_where_every_task_made_no_progress_is_incomplete_but_runs_all_eligible_work(tmp_path: Path) -> None:
    report, _s, _rig, _ = go(tmp_path, ["1", "2"], {"1": RunResult("NO_PROGRESS", "no change"), "2": RunResult("NO_PROGRESS", "no change")}, concurrency=1)

    assert {t.task_id: t.outcome for t in report.tasks} == {"1": "open", "2": "open"}
    assert not report.ok and report.exit_code == rw.EXIT_STOPPED
    assert "no task delivered" in (report.stop_reason or "") and "1, 2" in report.stop_reason
    text = rw.format_window_report(report)
    assert "STOPPED" in text and "OK (" not in text
    assert json.loads(report.options.report_path.read_text(encoding="utf-8"))["ok"] is False
    assert report.next_command.startswith("stagemesh doctor")


def test_no_progress_beside_a_delivered_task_or_no_work_stays_ok(tmp_path: Path) -> None:
    report, *_ = go(tmp_path, ["1", "2"], {"1": RunResult("NO_PROGRESS", "no change")}, concurrency=1)
    assert report.ok and report.stop_reason is None and {t.outcome for t in report.tasks} == {"open", "done"}

    empty, *_ = go(tmp_path / "empty", [])
    assert empty.ok and empty.stop_reason is None


def test_step_cap_tasks_are_resumable_and_the_report_says_how_to_resume(tmp_path: Path) -> None:
    report, _s, _rig, _ = go(tmp_path, ["1"], {"1": RunResult("MAX_STEPS", "stopped after 4 steps")})

    assert report.ok and report.tasks[0].resumable and report.next_command.startswith("stagemesh run-window")


def test_local_mode_does_not_prepare_staging_or_open_prs(tmp_path: Path) -> None:
    prepared: list[int] = []
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"], integration_mode="local")

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", prepare_staging=lambda: prepared.append(1), **kwargs)

    assert prepared == []  # local mode has no staging branch; its ahead/diverged repo check is skipped too
    assert rig.prs == [] and report.tasks[0].landing == "locally_integrated"


def test_a_failing_staging_preparation_refuses_to_start(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])

    def diverged() -> None:
        raise RuntimeError("refs/heads/wave/staging has diverged from refs/heads/main")

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", prepare_staging=diverged, **kwargs)

    assert report.refused and "has diverged" in report.stop_reason and rig.runs == []


def test_window_never_merges_without_explicit_opt_in(tmp_path: Path) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    def forbidden_merge(record, deadline):
        pytest.fail("default PR window invoked a merge")
    report = rw.run_window(store, project, config, options, hooks,
                           base_ref="refs/heads/main", merge_pr=forbidden_merge, **kwargs)
    assert report.ok and report.merged_prs == []
    assert report.tasks[0].pr_state != "MERGED"
    assert not cli_module.build_parser().parse_args(["run-window"]).auto_merge_ff
    for flag in ("--merge", "--auto-merge"):
        with pytest.raises(SystemExit):
            cli_module.build_parser().parse_args(["run-window", flag])


# --- CLI ---------------------------------------------------------------------------------------------------------------------------


def test_cli_defaults_to_a_safe_45_minute_pr_mode_window() -> None:
    args = cli_module.build_parser().parse_args(["run-window"])

    assert (args.duration, args.concurrency, args.max_steps_per_task, args.integration) == ("45m", None, 8, "pr")
    assert args.plan is False and args.no_pr is False and args.allow_stale_runtime is False and args.strict_live is False
    assert args.report is None and args.tasks is None


def test_cli_concurrency_overrides_only_concurrency() -> None:
    args = cli_module.build_parser().parse_args(["run-window", "--concurrency", "3"])

    assert (args.duration, args.concurrency, args.max_steps_per_task, args.integration) == ("45m", 3, 8, "pr")
    assert args.no_pr is False and args.strict_live is False and args.report is None


def test_cli_help_shows_the_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(["run-window", "--help"])
    out = " ".join(capsys.readouterr().out.split())

    assert "default 45m" in out and "default 2)" in out and "default 8)" in out
    assert "pr (default" in out and ".stagemesh/reports/run-window/<timestamp>.json" in out


def test_cli_default_report_path_is_auto_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = make_store(tmp_path, ["1"])
    store.close()
    monkeypatch.setattr(rw, "check_runtime", lambda project: FRESH)

    cli_module.main(["--project", str(project), "run-window", "--plan", "--json"])

    # --plan is read-only, but its preflight proves the default report location is creatable
    reports = list((project / ".stagemesh" / "reports" / "run-window").glob("*.json"))
    assert len(reports) == 1


@pytest.mark.parametrize("strict", [False, True])
def test_window_passes_provider_policy_to_child_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    store, project = make_store(tmp_path, ["generic-task"])
    store.close()
    seen = {}
    actual_hooks = cli_module._wave_hooks

    def hooks(*args, **kwargs):
        seen.update(kwargs)
        return actual_hooks(*args, **kwargs)

    monkeypatch.setattr(cli_module, "_wave_hooks", hooks)
    monkeypatch.setattr(rw, "check_runtime", lambda project: FRESH)
    argv = ["--project", str(project), "run-window", "--plan", "--json"]
    if strict:
        argv.append("--strict-live")
    assert cli_module.main(argv) in (0, 2)
    assert seen["live_acceptance"] == ("required" if strict else "skip_failed")


def test_cli_plan_is_read_only_and_shows_the_selection(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    store, project = make_store(tmp_path, ["1", "2"])
    store.close()
    monkeypatch.setattr(rw, "check_runtime", lambda project: FRESH)

    code = cli_module.main(["--project", str(project), "run-window", "--plan", "--json", "--report", str(tmp_path / "plan.json")])

    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"preflight", "would_run", "left_alone", "ok"}
    assert data["would_run"] == ["1", "2"]
    assert code in (0, 2)  # 2 when this machine has no usable provider setup; the selection is shown either way
    again = Store_open(project)
    assert last_window(again) is None  # planning started nothing and journaled nothing
    again.close()


def Store_open(project: Path):
    from stagemesh.persistence import Store

    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return store


def test_cli_rejects_a_bad_duration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store, project = make_store(tmp_path, ["1"])
    store.close()

    assert cli_module.main(["--project", str(project), "run-window", "--duration", "soon"]) == 2
    assert "duration must look like" in capsys.readouterr().err


def test_pr_mode_refuses_to_start_unless_the_checkout_is_on_the_integration_branch(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"])

    wrong = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", checkout_ref=lambda: "refs/heads/feature", **kwargs)

    assert wrong.refused and rig.runs == []
    assert "project checkout is on refs/heads/feature, not refs/heads/main" in wrong.stop_reason and "unmerged commits" in wrong.stop_reason
    assert wrong.next_command == "git switch main"
    detached = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", checkout_ref=lambda: None, **kwargs)
    assert detached.refused and "a detached HEAD" in detached.stop_reason

    right = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", checkout_ref=lambda: "refs/heads/main", **kwargs)
    assert not right.refused and rig.runs == ["1"]


def test_local_mode_does_not_need_the_checkout_on_the_integration_branch(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"], integration_mode="local")

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", checkout_ref=lambda: "refs/heads/feature", **kwargs)

    assert not report.refused and rig.runs == ["1"]


def test_survivor_wave_pr_mode_also_refuses_from_the_wrong_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store, project = make_store(tmp_path, ["1"])
    store.close()
    (project / ".stagemesh" / "config.json").write_text(json.dumps({"integration_ref": "other"}), encoding="utf-8")

    code = cli_module.main(["--project", str(project), "survivor-wave", "--tasks", "1", "--no-pr"])

    err = capsys.readouterr().err
    assert code == 2 and "project checkout is on" in err and "not refs/heads/other" in err and "git switch other" in err


def test_ff_window_lands_each_task_before_dispatching_the_next(tmp_path: Path) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, required_checks=("web",), concurrency=1)
    store.add_dependency("2", "1")
    merged = []
    original = hooks.run_task
    def run(task, steps):
        if task == "2":
            assert merged == ["1"], "dependent task started before remote landing"
        return original(task, steps)
    hooks.run_task = run
    def land(record, deadline):
        merged.append(record.task_id)
        return {"merged": True, "pr": len(merged), "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks,
                           base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and merged == ["1", "2"]
    assert all(t.pr_state == "MERGED" and not t.ready_for_merge for t in report.tasks)
    assert len(report.to_dict()["merged_prs"]) == 2


def test_ff_window_stops_before_dependents_when_merge_is_refused(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, required_checks=("web",), concurrency=1)
    report = rw.run_window(store, project, config, options, hooks,
        base_ref="refs/heads/main", merge_pr=lambda record, deadline: {"merged": False, "reason": "CI failed"}, **kwargs)
    assert not report.ok and report.exit_code == 1 and rig.runs == ["1"]
    assert "CI failed" in report.stop_reason and report.tasks[0].pr_state != "MERGED"


@pytest.mark.parametrize("overrides", [{"concurrency": 0}, {"required_checks": ()}, {"open_prs": False}, {"integration_mode": "local"}])
def test_ff_window_refuses_incompatible_options(tmp_path: Path, overrides) -> None:
    opts = {"auto_merge_ff": True, "concurrency": 1, "required_checks": ("web",)}
    opts.update(overrides)
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["1"], **opts)
    report = rw.run_window(store, project, config, options, hooks,
        base_ref="refs/heads/main", merge_pr=lambda r,d: {"merged": True}, **kwargs)
    assert report.refused and rig.runs == []


def test_ff_window_refuses_prior_unmerged_prs(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    kwargs["list_prs"] = lambda project: [{"number": 3}]
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda r,d: {"merged": True}, **kwargs)
    assert report.refused and rig.runs == [] and "prior_prs" in report.stop_reason


@pytest.mark.parametrize("workers", [2, 5])
def test_parallel_ff_finishes_workers_then_lands_in_ancestry_not_task_order(tmp_path: Path, workers: int) -> None:
    import threading

    ids = [str(number) for number in range(1, workers + 1)]
    barrier = threading.Barrier(workers)
    finished = []
    def work(rig, task_id):
        barrier.wait(timeout=10)
        result = rig.finish_done(task_id)
        with rig.lock:
            finished.append(task_id)
        return result
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ids, {task_id: work for task_id in ids},
        auto_merge_ff=True, required_checks=("web",), concurrency=workers)
    # Integration order is the reverse of task/PR listing order.
    default_ancestry = hooks.is_ancestor
    hooks.is_ancestor = lambda older, newer: (
        int(older[0]) > int(newer[0])
        if older[0].isdigit() and newer[0].isdigit()
        else default_ancestry(older, newer)
    )
    landed = []
    def land(record, deadline):
        assert sorted(finished) == ids
        assert not list(store.running_executions())
        landed.append(record.task_id)
        return {"merged": True, "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and sorted(rig.runs) == ids
    assert landed == list(reversed(ids))
    assert all(record.landing == "fast_forward_merged" for record in report.tasks)


def test_parallel_ff_preserves_all_prs_when_first_landing_fails(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.is_ancestor = lambda older, newer: older.startswith("1") and newer.startswith("2")
    attempted = []
    def land(record, deadline):
        attempted.append(record.task_id)
        return {"merged": False, "reason": "hosted checks failed"}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert not report.ok and attempted == ["1"] and sorted(rig.runs) == ["1", "2"]
    assert all(record.pr_url and record.ready_for_merge for record in report.tasks)
    assert not report.merged_prs


def test_parallel_ff_refuses_unrelated_candidates_before_landing_any(tmp_path: Path) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.is_ancestor = lambda older, newer: False
    attempted = []
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: attempted.append(record.task_id), **kwargs)
    assert not report.ok and not attempted and "ancestry chain" in report.stop_reason
    assert not any(record.pr_url for record in report.tasks)
    assert all(record.resumable for record in report.tasks)


def test_parallel_ff_resumes_entire_owned_stack_before_new_work(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2", "3"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.run_task("1", 8)
    hooks.run_task("2", 8)
    rig.runs.clear()
    hooks.is_ancestor = lambda older, newer: older.startswith("1") and newer.startswith("2")
    kwargs["list_prs"] = lambda project: [
        {"headRefName": "wave/task-2", "url": "https://github.com/x/y/pull/2"},
        {"headRefName": "wave/task-1", "url": "https://github.com/x/y/pull/1"},
    ]
    landed = []
    original = hooks.run_task
    def work(task_id, steps):
        assert landed == ["1", "2"]
        return original(task_id, steps)
    hooks.run_task = work
    def land(record, deadline):
        landed.append(record.task_id)
        return {"merged": True, "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and landed == ["1", "2", "3"] and rig.runs == ["3"]
    assert all(record["resumed"] for record in report.merged_prs[:2])


def test_parallel_ff_refuses_pending_stack_with_unowned_member(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.run_task("1", 8)
    rig.runs.clear()
    kwargs["list_prs"] = lambda project: [
        {"headRefName": "wave/task-1", "url": "https://github.com/x/y/pull/1"},
        {"headRefName": "wave/task-foreign", "url": "https://github.com/x/y/pull/2"},
    ]
    landed = []
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: landed.append(record.task_id), **kwargs)
    assert report.refused and not rig.runs and not landed


def test_parallel_ff_pending_stack_obeys_task_budget(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2", "3"], auto_merge_ff=True, concurrency=3,
        required_checks=("web",), max_tasks=1)
    for task_id in ("1", "2", "3"):
        hooks.run_task(task_id, 8)
    rig.runs.clear()
    hooks.is_ancestor = lambda older, newer: int(older[0]) < int(newer[0])
    kwargs["list_prs"] = lambda project: [
        {"headRefName": f"wave/task-{task_id}", "url": f"https://github.com/x/y/pull/{task_id}"}
        for task_id in ("3", "1", "2")
    ]
    landed = []
    def land(record, deadline):
        landed.append(record.task_id)
        return {"merged": True, "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and report.task_limit_reached and landed == ["1"] and not rig.runs


def test_parallel_ff_pause_between_landings_retains_remaining_prs(tmp_path: Path) -> None:
    from stagemesh.parallel import request_queue_control

    store, project, _rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.is_ancestor = lambda older, newer: older.startswith("1") and newer.startswith("2")
    landed = []
    def land(record, deadline):
        landed.append(record.task_id)
        request_queue_control(store, "paused", reason="pause between stack members")
        return {"merged": True, "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert not report.ok and landed == ["1"] and report.stop_reason == "queue admission paused"
    remaining = next(record for record in report.tasks if record.task_id == "2")
    assert remaining.pr_url and remaining.ready_for_merge


def test_parallel_ff_restart_refuses_descendant_with_deferred_ancestor(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.run_task("1", 8)
    hooks.run_task("2", 8)
    store.advance_task("1", Stage.INTEGRATE)  # ancestor publication was interrupted
    rig.runs.clear()
    hooks.main_sha = lambda: "a" * 40
    hooks.is_ancestor = lambda older, newer: older == newer or (older.startswith("1") and newer.startswith("2"))
    kwargs["list_prs"] = lambda project: [
        {"headRefName": "wave/task-2", "url": "https://github.com/x/y/pull/2"},
    ]
    landed = []
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: landed.append(record.task_id), **kwargs)
    assert report.refused and not landed and not rig.runs
    assert "unlanded task 1" in report.stop_reason


def test_parallel_ff_publication_failure_never_publishes_descendants(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    hooks.is_ancestor = lambda older, newer: older.startswith("2") and newer.startswith("1")
    published, landed = [], []
    def publish(record):
        published.append(record.task_id)
        raise OSError("ancestor publication unavailable")
    hooks.open_pr = publish
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: landed.append(record.task_id), **kwargs)
    assert not report.ok and published == ["2"] and not landed
    assert all(record.resumable and not record.pr_url for record in report.tasks)


def test_parallel_ff_validates_entire_pending_set_before_first_merge(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=2, required_checks=("web",))
    for task_id in ("1", "2"):
        hooks.run_task(task_id, 8)
    rig.runs.clear()
    hooks.is_ancestor = lambda older, newer: older.startswith("1") and newer.startswith("2")
    kwargs["list_prs"] = lambda project: [
        {"headRefName": f"wave/task-{task_id}", "url": f"https://github.com/x/y/pull/{task_id}"}
        for task_id in ("1", "2")
    ]
    validated, landed = [], []
    def land(record, deadline):
        landed.append(record.task_id)
        return {"merged": True}
    def validate(records, deadline):
        validated.append([record.task_id for record in records])
        return "later PR head changed"
    land.validate_records = validate
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.refused and validated == [["1", "2"]] and not landed and not rig.runs


@pytest.mark.parametrize("republished", [False, True])
def test_ff_window_resumes_one_owned_evidenced_pr_before_new_work(tmp_path: Path, republished) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    hooks.run_task("1", 8)
    assert store.get_task("1")["stage"] == "DONE"
    head_ref = "wave/task-1" + ("-" + store.latest_candidate("1")["sha"][:7] if republished else "")
    kwargs["list_prs"] = lambda project: [{"number": 7, "headRefName": head_ref, "url": "https://github.com/x/y/pull/7"}]
    landed = []
    def land(record, deadline):
        landed.append(record.task_id)
        return {"merged": True, "pr": len(landed), "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and landed == ["1", "2"]
    assert report.merged_prs[0]["resumed"] is True
    assert rig.runs.count("1") == 1


def test_ff_window_honors_pause_after_landing_before_next_dispatch(tmp_path: Path) -> None:
    from stagemesh.parallel import request_queue_control
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    def land(record, deadline):
        request_queue_control(store, "paused", reason="pause after first landing")
        return {"merged": True, "pr": 1, "candidate_sha": record.integrated_candidate}
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert not report.ok and rig.runs == ["1"] and len(report.merged_prs) == 1
    assert report.stop_reason == "queue admission paused"


def test_ff_lookup_failure_is_a_reported_preflight_refusal(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    kwargs["list_prs"] = lambda project: (_ for _ in ()).throw(OSError("GitHub unavailable"))
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: {"merged": True}, **kwargs)
    assert report.refused and rig.runs == [] and "GitHub unavailable" in report.stop_reason
    assert options.report_path.exists()


def test_ff_partial_landing_is_not_reported_as_a_completed_merge(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main",
        merge_pr=lambda record, deadline: {"merged": False, "remote_landed": True, "local_synced": False,
            "pr": 7, "reason": "local sync blocked"}, **kwargs)
    assert not report.ok and rig.runs == ["1"]
    assert report.merged_prs == [] and report.landing_results[0]["remote_landed"] is True
    assert report.tasks[0].landing == "remote_landed_sync_blocked"
    assert "local sync blocked" in rw.format_window_report(report)


def test_ff_recovers_journaled_landing_missing_from_open_pr_listing(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import TaskRecord
    store, project, _rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], auto_merge_ff=True, concurrency=1, required_checks=("web",))
    hooks.run_task("1", 8)
    sha = store.latest_candidate("1")["sha"]
    recovered = TaskRecord("1", outcome="done", ready_for_merge=True,
        pr_url="https://github.com/x/y/pull/7", integrated_candidate=sha)
    calls = []
    def land(record, deadline):
        calls.append(record.task_id)
        return {"merged": True, "pr": 7, "candidate_sha": record.integrated_candidate}
    land.pending_records = lambda: [recovered]
    kwargs["list_prs"] = lambda project: []
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", merge_pr=land, **kwargs)
    assert report.ok and calls == ["1", "2"] and report.merged_prs[0]["resumed"]



def test_unpublished_work_resumes_after_main_guard_without_reimplementation(tmp_path: Path) -> None:
    store, project, rig, hooks, options, config, kwargs = window(tmp_path, ["241"], max_tasks=1, concurrency=1)
    tip = {"sha": "a" * 40}
    hooks.main_sha = lambda: tip["sha"]
    hooks.repo_state = lambda: RepoState(main_ref="main", main_sha=tip["sha"], origin_sha=tip["sha"], relation="in_sync")

    def finish_while_main_advances(rig, task_id):
        result = rig.finish_done(task_id)
        tip["sha"] = "b" * 40
        return result

    rig.behaviour["241"] = finish_while_main_advances
    first = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert not first.ok and first.to_dict()["main_moved"]
    assert "local main moved in PR mode" in first.stop_reason
    assert rig.prs == []
    task = store.get_task("241")
    assert (task["stage"], task["status"]) == ("INTEGRATE", "OPEN")
    assert first.tasks[0].resumable and first.tasks[0].landing == "staged_not_published"
    candidates = [tuple(row) for row in store.conn.execute("SELECT * FROM candidates WHERE task_id='241'")]
    evidence = [tuple(row) for row in store.conn.execute("SELECT * FROM evidence WHERE task_id='241'")]
    advances_before = list(rig.advanced)

    def finish_integration_only(rig, task_id):
        worker = rig.worker_store()
        try:
            assert worker.get_task(task_id)["stage"] == "INTEGRATE"
            worker.advance_task(task_id, Stage.DONE)
        finally:
            worker.close()
        return RunResult("DONE")

    rig.behaviour["241"] = finish_integration_only
    second = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert second.ok and second.tasks[0].landing == "pr_opened"
    assert rig.prs == ["241"] and rig.advanced == advances_before
    assert candidates == [tuple(row) for row in store.conn.execute("SELECT * FROM candidates WHERE task_id='241'")]
    assert evidence == [tuple(row) for row in store.conn.execute("SELECT * FROM evidence WHERE task_id='241'")]
    third = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert third.ok and third.selected == [] and rig.prs == ["241"]


def test_failed_pr_publication_preserves_a_resumable_integration_stage(tmp_path: Path) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, ["241"], max_tasks=1, concurrency=1)
    hooks.open_pr = lambda record: (_ for _ in ()).throw(RuntimeError("publication unavailable"))
    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert not report.ok
    assert report.tasks[0].landing == "pr_failed" and report.tasks[0].resumable
    assert report.tasks[0].integrated_candidate == store.latest_candidate("241")["sha"]
    assert (store.get_task("241")["stage"], store.get_task("241")["status"]) == ("INTEGRATE", "OPEN")
    assert report.tasks[0].validation == report.tasks[0].review == report.tasks[0].integration == "PASSED"
    assert not store.has_active_claim("241") and not list(store.running_executions())
