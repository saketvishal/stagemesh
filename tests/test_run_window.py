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


def test_older_open_wave_prs_are_detected_across_runs_and_listed_in_merge_order(tmp_path: Path) -> None:
    store, project, _rig, hooks, options, config, kwargs = window(tmp_path, ["1"])
    older = [
        {"number": 301, "headRefName": "wave/task-9", "title": "older", "url": "https://example.invalid/301"},
        {"number": 288, "headRefName": "wave/task-8", "title": "oldest", "url": "https://example.invalid/288"},
    ]
    kwargs["list_prs"] = lambda project: [*older, {"number": 302, "headRefName": "wave/task-1", "title": "task 1", "url": "https://example.invalid/pr/1"}]

    report = rw.run_window(store, project, config, options, hooks, base_ref="refs/heads/main", **kwargs)

    assert [pr["number"] for pr in report.open_wave_prs] == [288, 301, 302]  # merge order: oldest first
    assert [pr["opened_this_window"] for pr in report.open_wave_prs] == [False, False, True]
    text = rw.format_window_report(report)
    assert text.index("#288") < text.index("#301") < text.index("#302") and "(this window)" in text
    assert report.next_command.startswith("review and merge the open wave PRs oldest first")
    assert any(entry for entry in report.readiness["attention"] if "open wave PR" in entry)


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


def test_the_window_has_no_merge_path(tmp_path: Path) -> None:
    import inspect

    from stagemesh import survivor_wave

    source = inspect.getsource(rw) + inspect.getsource(survivor_wave.git_gh_pr_opener)
    assert '"merge"' not in source and "pr merge" not in source and "--auto" not in source
    parser = cli_module.build_parser()
    for flag in ("--merge", "--auto-merge"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run-window", flag])


# --- CLI ---------------------------------------------------------------------------------------------------------------------------


def test_cli_defaults_to_a_two_hour_pr_mode_window() -> None:
    args = cli_module.build_parser().parse_args(["run-window"])

    assert (args.duration, args.concurrency, args.max_steps_per_task, args.integration) == ("2h", 2, 8, "pr")
    assert args.plan is False and args.no_pr is False and args.allow_stale_runtime is False and args.strict_live is False


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
