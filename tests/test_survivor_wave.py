"""Survivor wave orchestration. No provider, git remote or gh is used: the task runner, baseline advancer and PR opener are fakes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from test_run_ready import _project

import stagemesh.cli as cli_module
from stagemesh.audit import record_audit
from stagemesh.domain import EvidenceKind, EvidenceStatus, ExecutionKind, Stage
from stagemesh.persistence import Store
from stagemesh.survivor_wave import (
    ORPHAN_EVENT,
    RunResult,
    WaveHooks,
    format_report,
    infrastructure_signature,
    parse_task_ids,
    run_wave,
    select_eligible,
    should_advance_baseline,
)

OLD, NEW = "a" * 40, "b" * 40


def make_store(tmp_path: Path, ids: list[str]) -> tuple[Store, Path]:
    project = _project(tmp_path, ids)
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    cli_module._sync_all_sources(store, project, cli_module.load_config(project), None)
    for task_id in ids:
        store.set_task_baseline(task_id, OLD)
    return store, project


class Rig:
    """Fake hooks. `behaviour[task]` is called in the worker thread with its own store connection, like a real child process would."""

    def __init__(self, project: Path, behaviour: dict[str, object] | None = None, *, ancestors: bool = True, target: str | None = NEW):
        self.project = project
        self.behaviour = behaviour or {}
        self.advanced: list[str] = []
        self.runs: list[str] = []
        self.prs: list[str] = []
        self.lock = threading.Lock()
        self.ancestors = ancestors
        self.target = target

    def worker_store(self) -> Store:
        store = Store(self.project / ".stagemesh" / "stagemesh.sqlite3")
        store.migrate()
        return store

    def run_task(self, task_id: str, max_steps: int) -> RunResult:
        with self.lock:
            self.runs.append(task_id)
        action = self.behaviour.get(task_id)
        if action is None:
            return self.finish_done(task_id)
        return action(self, task_id) if callable(action) else action

    def finish_done(self, task_id: str, *, review_failed_first: bool = False) -> RunResult:
        store = self.worker_store()
        try:
            sha = (task_id.replace("-", "") + "1" * 40)[:40]
            store.add_candidate(task_id, sha, "codex", True)
            store.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
            if review_failed_first:  # a product finding: the normal loop remediates it inside the same run
                store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
            store.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.PASSED)
            store.add_evidence(task_id, sha, EvidenceKind.INTEGRATION, EvidenceStatus.PASSED)
            store.advance_task(task_id, Stage.DONE)
        finally:
            store.close()
        return RunResult("DONE")

    def advance(self, task_id: str) -> dict:
        self.advanced.append(task_id)
        return {"new_baseline": NEW}

    def open_pr(self, record) -> str:
        self.prs.append(record.task_id)
        return f"https://example.invalid/pr/{record.task_id}"

    def hooks(self) -> WaveHooks:
        return WaveHooks(
            run_task=self.run_task,
            advance_baseline=self.advance,
            open_pr=self.open_pr,
            target_sha=lambda: self.target,
            is_ancestor=lambda older, newer: self.ancestors,
        )


def test_the_wave_selects_only_eligible_tasks_and_says_why_it_skips_the_rest(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])
    store.advance_task("T-2", Stage.DONE)
    store.start_execution(task_id="T-3", claim_id=None, kind=ExecutionKind.VALIDATION)

    eligible, skipped = select_eligible(store, ["T-1", "T-2", "T-3", "T-9"])

    assert eligible == ["T-1"]
    assert skipped == {"T-2": "already done", "T-3": "has an active claim or running execution", "T-9": "no such task"}
    rig = Rig(project)
    report = run_wave(store, ["T-1", "T-2", "T-3", "T-9"], rig.hooks(), concurrency=2)
    assert rig.runs == ["T-1"]
    assert {t.task_id: t.outcome for t in report.tasks} == {"T-1": "done", "T-2": "skipped", "T-3": "skipped", "T-9": "skipped"}


def test_stale_baseline_is_advanced_only_when_that_cannot_discard_progress(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3", "T-4"])
    store.rebaseline_task("T-3", NEW)  # already current
    store.add_candidate("T-2", "c" * 40, "codex", True)
    store.add_evidence("T-2", "c" * 40, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)  # validated work worth keeping
    rig = Rig(project)
    hooks = rig.hooks()

    assert should_advance_baseline(store, "T-1", hooks) is True
    assert should_advance_baseline(store, "T-2", hooks) is False  # integration refreshes a validated candidate by itself
    assert should_advance_baseline(store, "T-3", hooks) is False
    rig.ancestors = False
    assert should_advance_baseline(store, "T-4", hooks) is False  # not an ancestor of the target: never moved sideways
    rig.ancestors, rig.target = True, None
    assert should_advance_baseline(store, "T-4", hooks) is False

    rig.target = NEW
    report = run_wave(store, ["T-1", "T-2", "T-3"], hooks, concurrency=1, open_prs=False)
    assert rig.advanced == ["T-1"]
    assert {t.task_id: t.baseline_advanced for t in report.tasks} == {"T-1": True, "T-2": False, "T-3": False}

    store2, project2 = make_store(tmp_path / "off", ["T-1"])
    rig2 = Rig(project2)
    off = run_wave(store2, ["T-1"], rig2.hooks(), advance_baselines=False, open_prs=False)
    assert rig2.advanced == [] and off.tasks[0].baseline_advanced is False


def test_a_completed_task_gets_a_pr_request_but_nothing_is_merged(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2"])
    rig = Rig(project, {"T-2": RunResult("MAX_STEPS", "stopped after 3 steps")})

    report = run_wave(store, ["T-1", "T-2"], rig.hooks(), concurrency=2)

    by_id = {t.task_id: t for t in report.tasks}
    assert by_id["T-1"].outcome == "done" and by_id["T-1"].pr_url == "https://example.invalid/pr/T-1"
    assert by_id["T-2"].outcome == "open" and by_id["T-2"].pr_url is None  # unfinished: no PR
    assert rig.prs == ["T-1"]
    assert not any("merge" in name for name in WaveHooks.__dataclass_fields__)  # the wave has no way to merge

    no_pr = run_wave(store, ["T-2"], rig.hooks(), open_prs=False)
    assert rig.prs == ["T-1"] and no_pr.tasks[0].pr_url is None


def test_a_pr_failure_is_reported_and_does_not_hide_that_the_task_is_done(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.open_pr = lambda record: (_ for _ in ()).throw(RuntimeError("gh is not installed"))

    report = run_wave(store, ["T-1"], hooks)

    assert report.tasks[0].outcome == "done" and report.tasks[0].pr_url is None
    assert "gh is not installed" in report.tasks[0].pr_error and report.ok


def test_the_same_infrastructure_failure_on_two_tasks_stops_the_wave(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3", "T-4"])
    fail = RunResult("CURRENT_PROBLEM", "health has current problems: stale_running_executions")
    rig = Rig(project, {"T-1": fail, "T-2": fail})

    report = run_wave(store, ["T-1", "T-2", "T-3", "T-4"], rig.hooks(), concurrency=1)

    assert report.stopped_early and "shared infrastructure failure on tasks T-1, T-2" in report.stop_reason
    assert rig.runs == ["T-1", "T-2"]  # nothing else was started
    assert {t.task_id: t.outcome for t in report.tasks} == {
        "T-1": "infrastructure_failure",
        "T-2": "infrastructure_failure",
        "T-3": "not_started",
        "T-4": "not_started",
    }
    assert report.ok is False


def test_different_infrastructure_failures_do_not_stop_the_wave(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])
    rig = Rig(project, {"T-1": RunResult("CURRENT_PROBLEM", "blocked_tasks"), "T-2": RunResult("TARGET_ERROR", "no such target")})

    report = run_wave(store, ["T-1", "T-2", "T-3"], rig.hooks(), concurrency=1)

    assert report.stopped_early is False and rig.runs == ["T-1", "T-2", "T-3"]
    assert infrastructure_signature(RunResult("DONE")) is None and infrastructure_signature(RunResult("BLOCKED", "review failed")) is None


def test_a_product_review_failure_is_remediated_inside_the_run_and_is_not_a_wave_failure(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2"])
    rig = Rig(project, {"T-1": lambda rig, task: rig.finish_done(task, review_failed_first=True)})

    report = run_wave(store, ["T-1", "T-2"], rig.hooks(), concurrency=2)

    assert report.ok and not report.stopped_early
    assert {t.task_id: t.outcome for t in report.tasks} == {"T-1": "done", "T-2": "done"}
    assert report.tasks[0].review == "PASSED"  # the failed review is history; the final one passed


def test_a_design_blocker_marks_only_that_task_and_the_wave_carries_on(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])

    def design_block(rig: Rig, task_id: str) -> RunResult:
        worker = rig.worker_store()
        try:
            sha = "d" * 40
            worker.add_candidate(task_id, sha, "codex", True)
            worker.add_evidence(task_id, sha, EvidenceKind.VALIDATION, EvidenceStatus.PASSED)
            worker.add_evidence(task_id, sha, EvidenceKind.REVIEW, EvidenceStatus.FAILED)
            worker.upsert_finding("f1", task_id, sha, "error", "unproven providers are eligible by default")
            worker.block_task(task_id)
        finally:
            worker.close()
        return RunResult("BLOCKED", "task exhausted its remediation budget")

    rig = Rig(project, {"T-2": design_block})

    report = run_wave(store, ["T-1", "T-2", "T-3"], rig.hooks(), concurrency=1)

    by_id = {t.task_id: t for t in report.tasks}
    assert by_id["T-2"].outcome == "blocked_needs_decision" and by_id["T-2"].final_status == "BLOCKED"
    assert "needs a decision" in by_id["T-2"].reason and "1 open" in by_id["T-2"].reason
    assert by_id["T-1"].outcome == by_id["T-3"].outcome == "done"  # others are unaffected
    assert report.ok and rig.runs == ["T-1", "T-2", "T-3"]

    store2, project2 = make_store(tmp_path / "again", ["T-1", "T-2", "T-3"])
    rig2 = Rig(project2, {"T-1": design_block})
    stopped = run_wave(store2, ["T-1", "T-2", "T-3"], rig2.hooks(), concurrency=1, stop_on_blocked=True)
    assert stopped.stopped_early and rig2.runs == ["T-1"]  # policy: stop on the first blocked task


def test_an_orphan_event_increase_stops_the_wave_and_fails_it(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])

    def leak_an_orphan(rig: Rig, task_id: str) -> RunResult:
        worker = rig.worker_store()
        try:
            record_audit(worker, ORPHAN_EVENT, {"task_id": "T-2", "reason": "ORPHANED_BUILTIN_STAGE_EXECUTION"})
        finally:
            worker.close()
        return rig.finish_done(task_id)

    rig = Rig(project, {"T-1": leak_an_orphan})

    report = run_wave(store, ["T-1", "T-2", "T-3"], rig.hooks(), concurrency=1)

    assert report.stopped_early and "orphan" in report.stop_reason
    assert (report.orphan_events_before, report.orphan_events_after) == (0, 1)
    assert report.ok is False and rig.runs == ["T-1"]
    assert report.tasks[1].outcome == "not_started" and report.tasks[2].outcome == "not_started"


def test_the_concurrency_limit_is_respected(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3", "T-4"])
    state = {"now": 0, "peak": 0}

    def slow(rig: Rig, task_id: str) -> RunResult:
        with rig.lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.15)
        with rig.lock:
            state["now"] -= 1
        return rig.finish_done(task_id)

    rig = Rig(project, {t: slow for t in ("T-1", "T-2", "T-3", "T-4")})

    report = run_wave(store, ["T-1", "T-2", "T-3", "T-4"], rig.hooks(), concurrency=2, open_prs=False)

    assert state["peak"] == 2 and all(t.outcome == "done" for t in report.tasks)


def test_the_report_has_per_task_status_baselines_candidates_and_pr_urls(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1", "T-2"])
    rig = Rig(project, {"T-2": RunResult("MAX_STEPS", "stopped after 2 steps")})

    report = run_wave(store, ["T-1", "T-2"], rig.hooks(), concurrency=2)

    text = format_report(report)
    assert "survivor wave: OK" in text and "orphan-recovery events: 0 -> 0" in text
    assert "T-1: done (DONE/DONE)" in text and "T-2: open" in text
    assert "PR: https://example.invalid/pr/T-1" in text
    assert text.count("baseline aaaaaaa") == 2  # before; the fake advancer does not rewrite the ledger
    data = json.loads(json.dumps(report.to_dict()))
    first = next(t for t in data["tasks"] if t["task_id"] == "T-1")
    assert first["pr_url"] and first["validation"] == "PASSED" and first["review"] == "PASSED" and first["integration"] == "PASSED"
    assert first["baseline_before"] == OLD and first["candidates"] and first["final_status"] == "DONE"
    assert data["ok"] is True and data["orphan_events_before"] == data["orphan_events_after"] == 0


def test_cli_surface_has_the_wave_flags_and_no_merge_option() -> None:
    parser = cli_module.build_parser()

    args = parser.parse_args(["survivor-wave", "--tasks", "155,156,159", "--concurrency", "2", "--max-steps-per-task", "9"])

    assert (args.tasks, args.concurrency, args.max_steps_per_task) == ("155,156,159", 2, 9)
    assert args.no_pr is False and args.no_advance_baselines is False and args.stop_on_blocked is False
    assert parse_task_ids("155, 156,155 159") == ["155", "156", "159"]
    with pytest.raises(SystemExit):
        parser.parse_args(["survivor-wave", "--tasks", "1", "--merge"])  # the wave never merges


def test_subprocess_runner_maps_continue_output_to_a_run_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from stagemesh import survivor_wave

    outputs = iter(
        [
            sp.CompletedProcess([], 0, json.dumps({"stop_reason": "DONE", "message": ""}), ""),
            sp.CompletedProcess([], 2, json.dumps({"refused": True, "code": "current_problems", "message": "health has current problems"}), ""),
            sp.CompletedProcess([], 1, "Traceback (most recent call last): boom", ""),
        ]
    )
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return next(outputs)

    monkeypatch.setattr(survivor_wave.subprocess, "run", fake_run)
    run = survivor_wave.subprocess_task_runner(tmp_path)

    done, refused, crashed = run("155", 6), run("156", 6), run("159", 6)

    assert (done.stop_reason, done.exit_code) == ("DONE", 0)
    assert refused.stop_reason == "CURRENT_PROBLEMS" and "current problems" in refused.message
    assert crashed.stop_reason == "RUNNER_CRASH" and "boom" in crashed.message
    assert seen[0][-6:] == ["continue", "--task", "155", "--max-steps", "6", "--json"]
    assert "--dry-run" not in seen[0] and seen[1][-4] == "156" and seen[2][-4] == "159"
    assert infrastructure_signature(refused) == "message:health has current problems"
    assert infrastructure_signature(crashed).startswith("RUNNER_CRASH:")


def test_survivor_wave_command_reports_skips_without_running_anything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    store.advance_task("T-1", Stage.DONE)
    store.close()

    code = cli_module.main(["--project", str(project), "survivor-wave", "--tasks", "T-1,T-9", "--no-pr", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["ok"] is True
    assert {t["task_id"]: (t["outcome"], t["reason"]) for t in data["tasks"]} == {
        "T-1": ("skipped", "already done"),
        "T-9": ("skipped", "no such task"),
    }
    assert cli_module.main(["--project", str(project), "survivor-wave", "--tasks", " ,", "--no-pr"]) == 2
