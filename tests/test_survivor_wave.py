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
    assert "T-1: done -> PR opened, ready for merge (OPEN) (DONE/DONE)" in text and "T-2: open" in text
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


# --- integration modes, repo state and runtime freshness ----------------------------------------------------------------------------


def _git(project: Path, *argv: str) -> str:
    import subprocess as sp

    proc = sp.run(["git", *argv], cwd=project, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _commit(project: Path, name: str) -> str:
    (project / name).write_text(name, encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", name)
    return _git(project, "rev-parse", "HEAD")


def _with_origin(tmp_path: Path, project: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "-u", "origin", "HEAD")
    return origin


def _main_ref(project: Path) -> str:
    return _git(project, "symbolic-ref", "HEAD")


def _commit_on(project: Path, ref: str, name: str) -> str:
    """Add an empty-tree-changing commit on top of `ref` without touching the checkout."""
    tip = _git(project, "rev-parse", ref)
    tree = _git(project, "rev-parse", f"{tip}^{{tree}}")
    sha = _git(project, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit-tree", "-p", tip, "-m", name, tree)
    _git(project, "update-ref", ref, sha, tip)
    return sha


class GitRig(Rig):
    """A Rig whose `run_task` lands the finished task on `land_ref` the way a child `continue --integration-ref` would."""

    def __init__(self, project: Path, land_ref: str, **kwargs):
        super().__init__(project, **kwargs)
        self.land_ref = land_ref
        self.commits: dict[str, str] = {}

    def run_task(self, task_id: str, max_steps: int) -> RunResult:
        result = super().run_task(task_id, max_steps)
        if result.stop_reason == "DONE" and task_id not in self.commits:
            with self.lock:
                self.commits[task_id] = _commit_on(self.project, self.land_ref, f"task {task_id}")
        return result


def test_pr_mode_lands_on_the_staging_ref_and_never_advances_local_main(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import STAGING_REF, git_repo_state, prepare_staging_ref

    store, project = make_store(tmp_path, ["T-1", "T-2"])
    _with_origin(tmp_path, project)
    main_ref = _main_ref(project)
    main_before = _git(project, "rev-parse", main_ref)
    assert prepare_staging_ref(project, main_ref) == main_before and _git(project, "rev-parse", STAGING_REF) == main_before

    rig = GitRig(project, STAGING_REF)
    hooks = rig.hooks()
    hooks.repo_state = git_repo_state(store, project, main_ref)
    report = run_wave(store, ["T-1", "T-2"], hooks, concurrency=1, integration_mode="pr", staging_ref=STAGING_REF)

    assert _git(project, "rev-parse", main_ref) == main_before  # local main did not move
    assert _git(project, "rev-parse", STAGING_REF) == rig.commits["T-2"]  # the work is on the staging ref, stacked
    assert report.main_moved is False and report.ok and report.repo_after.relation == "in_sync" and report.repo_after.ahead == 0
    assert report.wave_state == "needs_pr_merges"
    for task in report.tasks:
        assert (task.landing, task.ready_for_merge, task.pr_state) == ("pr_opened", True, "OPEN")  # opened, not "landed"


def test_pr_mode_flags_a_wave_that_moved_local_main_anyway(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import git_repo_state

    store, project = make_store(tmp_path, ["T-1"])
    _with_origin(tmp_path, project)
    main_ref = _main_ref(project)
    rig = GitRig(project, main_ref)  # a child that ignores the staging ref and lands on main
    hooks = rig.hooks()
    hooks.repo_state = git_repo_state(store, project, main_ref)

    report = run_wave(store, ["T-1"], hooks, integration_mode="pr")

    assert report.main_moved and report.integration_violation and report.ok is False
    assert report.wave_state == "main_moved_unexpectedly" and report.repo_after.relation == "ahead"
    text = format_report(report)
    assert "LOCAL MAIN MOVED IN PR MODE" in text and "local main MOVED" in text


def test_local_mode_is_explicit_moves_main_reports_it_and_opens_no_pr(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import git_repo_state

    store, project = make_store(tmp_path, ["T-1"])
    _with_origin(tmp_path, project)
    main_ref = _main_ref(project)
    main_before = _git(project, "rev-parse", main_ref)
    rig = GitRig(project, main_ref)
    hooks = rig.hooks()
    hooks.repo_state = git_repo_state(store, project, main_ref)

    report = run_wave(store, ["T-1"], hooks, integration_mode="local")

    task = report.tasks[0]
    assert rig.prs == [] and task.pr_url is None and task.ready_for_merge is False  # the PR is not a second integration path
    assert task.landing == "locally_integrated" and report.main_moved and report.ok
    assert report.wave_state == "local_main_advanced" and _git(project, "rev-parse", main_ref) != main_before
    text = format_report(report)
    assert "integration mode: local" in text and "local main MOVED" in text and "integrated into the local integration branch" in text
    with pytest.raises(ValueError):
        run_wave(store, ["T-1"], hooks, integration_mode="sideways")


def test_cli_defaults_to_pr_mode_and_passes_the_staging_ref_to_the_child_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from stagemesh import survivor_wave

    parser = cli_module.build_parser()
    assert parser.parse_args(["survivor-wave", "--tasks", "1"]).integration == "pr"
    assert parser.parse_args(["survivor-wave", "--tasks", "1", "--integration", "local"]).integration == "local"
    with pytest.raises(SystemExit):
        parser.parse_args(["survivor-wave", "--tasks", "1", "--integration", "auto-merge"])

    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return sp.CompletedProcess([], 0, json.dumps({"stop_reason": "DONE"}), "")

    monkeypatch.setattr(survivor_wave.subprocess, "run", fake_run)
    survivor_wave.subprocess_task_runner(tmp_path, integration_ref=survivor_wave.STAGING_REF)("7", 3)
    survivor_wave.subprocess_task_runner(tmp_path)("7", 3)

    assert seen[0][-2:] == ["--integration-ref", "refs/heads/wave/staging"] and "--integration-ref" not in seen[1]
    assert parser.parse_args(["continue", "--task", "7", "--integration-ref", "refs/heads/x"]).integration_ref == "refs/heads/x"


def test_the_pr_branch_contains_the_candidate_commit_and_nothing_is_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from stagemesh import survivor_wave
    from stagemesh.survivor_wave import (
        STAGING_REF,
        TaskRecord,
        git_gh_pr_opener,
        prepare_staging_ref,
    )

    _store, project = make_store(tmp_path, ["T-1"])
    origin = _with_origin(tmp_path, project)
    main_ref = _main_ref(project)
    main_before = _git(project, "rev-parse", main_ref)
    prepare_staging_ref(project, main_ref)
    candidate = _commit_on(project, STAGING_REF, "feature")
    gh_calls: list[list[str]] = []
    real_run = sp.run

    def fake_run(argv, **kwargs):
        if argv[0] == "gh":
            gh_calls.append(list(argv))
            return sp.CompletedProcess(argv, 0, "https://example.invalid/pr/1\n", "")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(survivor_wave.subprocess, "run", fake_run)
    record = TaskRecord("T-1", title="the task", integrated_candidate=candidate)

    url = git_gh_pr_opener(project, base=main_ref.removeprefix("refs/heads/"))(record)

    assert url == "https://example.invalid/pr/1"
    assert _git(project, "rev-parse", "refs/heads/wave/task-T-1") == candidate  # the PR branch is exactly the candidate
    assert _git(origin, "rev-parse", "refs/heads/wave/task-T-1") == candidate  # and it was pushed
    assert _git(project, "rev-parse", main_ref) == main_before and _git(origin, "rev-parse", main_ref) == main_before
    assert [call[:3] for call in gh_calls] == [["gh", "pr", "create"]]  # create only: no merge call exists
    assert not any(call[1:3] == ["pr", "merge"] or "--auto" in call for call in gh_calls)


def test_staging_ref_is_created_kept_when_stacked_moved_when_merged_and_refused_when_diverged(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import STAGING_REF, prepare_staging_ref

    _store, project = make_store(tmp_path, ["T-1"])
    main_ref = _main_ref(project)
    base = _git(project, "rev-parse", main_ref)

    assert prepare_staging_ref(project, main_ref) == base  # absent: created at the base
    stacked = _commit_on(project, STAGING_REF, "a")
    assert prepare_staging_ref(project, main_ref) == stacked and _git(project, "rev-parse", STAGING_REF) == stacked  # earlier wave work kept
    _git(project, "update-ref", main_ref, stacked, base)  # its PR got merged into main
    _git(project, "update-ref", STAGING_REF, base)  # staging is now behind main
    assert prepare_staging_ref(project, main_ref) == stacked  # a stale ref moves up; nothing is lost
    _commit_on(project, STAGING_REF, "b")
    _commit_on(project, main_ref, "c")  # main moves on its own: staging and main now diverge
    staged = _git(project, "rev-parse", STAGING_REF)
    with pytest.raises(RuntimeError, match="diverged"):
        prepare_staging_ref(project, main_ref)
    assert _git(project, "rev-parse", STAGING_REF) == staged  # refused, and not moved


def test_stale_installed_runtime_is_detected_and_reported_not_silently_used(tmp_path: Path) -> None:
    import subprocess as sp

    from stagemesh.runtime_freshness import check_runtime

    project = tmp_path / "proj"
    (project / "src" / "stagemesh").mkdir(parents=True)
    (project / "src" / "stagemesh" / "cli.py").write_text("", encoding="utf-8")
    scripts = project / ".stagemesh" / "tooling" / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    exe = scripts / "stagemesh.exe"
    exe.write_text("", encoding="utf-8")
    outputs = {"installed": "usage: stagemesh [-h] {init,status,worktrees}", "source": "usage: stagemesh [-h] {init,status,worktrees,survivor-wave}"}

    def fake_run(argv, **kwargs):
        return sp.CompletedProcess(argv, 0, outputs["installed" if str(exe) == argv[0] else "source"], "")

    from_source = str(project / "src" / "stagemesh" / "__init__.py")
    stale = check_runtime(project, runner=fake_run, running_file=from_source)

    assert stale.stale and stale.missing_from_installed == ["survivor-wave"] and stale.running_from_source
    assert "stale" in stale.message and "lacks survivor-wave" in stale.message and "running from source" in stale.message
    assert "PYTHONPATH=src" in stale.message and "pip install --upgrade" in stale.message  # says what to do; changes nothing itself
    installed_copy = check_runtime(project, runner=fake_run, running_file=str(scripts / "site-packages" / "stagemesh" / "__init__.py"))
    assert installed_copy.stale and installed_copy.running_from_source is False and "not from source" in installed_copy.message
    outputs["installed"] = outputs["source"]
    fresh = check_runtime(project, runner=fake_run, running_file=from_source)
    assert fresh.stale is False and fresh.missing_from_installed == []


def test_report_separates_orphan_events_cleanable_resumable_and_done_unmerged_worktrees(tmp_path: Path) -> None:
    from stagemesh.survivor_wave import git_repo_state
    from stagemesh.workspaces import _task_key, worktree_root

    store, project = make_store(tmp_path, ["T-1", "T-2", "T-3"])
    _with_origin(tmp_path, project)
    main_ref = _main_ref(project)
    root = worktree_root(project)
    root.mkdir(parents=True, exist_ok=True)
    store.advance_task("T-1", Stage.DONE)  # done and already in main -> cleanable
    store.advance_task("T-2", Stage.DONE)  # done but only on a side commit -> waiting on a PR
    for task_id in ("T-1", "T-2", "T-3"):  # T-3 stays OPEN -> resumable
        _git(project, "worktree", "add", "-q", "--detach", str(root / _task_key(task_id)), "HEAD")
    _git(root / _task_key("T-2"), "-c", "user.email=a@b.c", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "unmerged")
    record_audit(store, ORPHAN_EVENT, {"task_id": "T-3"})

    state = git_repo_state(store, project, main_ref)()

    assert (state.cleanable_worktrees, state.done_unmerged_worktrees, state.resumable_worktrees) == (1, 1, 1)
    assert state.running_executions == 0 and state.active_claims == 0
    assert state.relation == "in_sync" and state.main_sha == state.origin_sha
    assert sorted(path.name for path in root.iterdir()) == sorted(_task_key(t) for t in ("T-1", "T-2", "T-3"))  # read-only: nothing cleaned

    rig = Rig(project, {"T-3": RunResult("MAX_STEPS", "stopped")})
    hooks = rig.hooks()
    hooks.repo_state = git_repo_state(store, project, main_ref)
    report = run_wave(store, ["T-3"], hooks, open_prs=False)
    data = report.to_dict()
    assert data["orphan_events_before"] == 1 and data["repo_before"]["orphan_events"] == 1  # event count and worktree counts are separate
    for key in ("active_claims", "running_executions", "cleanable_worktrees", "resumable_worktrees", "done_unmerged_worktrees", "ahead", "behind", "relation"):
        assert key in data["repo_after"]
    text = format_report(report)
    assert "worktrees: cleanable 1 -> 1" in text and "none were cleaned" in text and "active claims: 0; running executions: 0" in text


def test_hitting_the_step_cap_leaves_a_resumable_task_and_is_not_a_failure(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    rig = Rig(project, {"T-1": RunResult("MAX_STEPS", "stopped after 4 steps")})

    report = run_wave(store, ["T-1"], rig.hooks())

    task = report.tasks[0]
    assert (task.outcome, task.resumable, task.stop_reason, task.final_status) == ("open", True, "MAX_STEPS", "OPEN")
    assert report.ok and not report.stopped_early and rig.prs == [] and task.landing is None
    assert not store.has_active_claim("T-1") and list(store.running_executions()) == []
    assert "resumable: not a failure" in format_report(report)


def test_the_wave_has_no_merge_path_in_code_or_cli() -> None:
    import inspect

    from stagemesh import survivor_wave

    assert not any("merge" in name for name in survivor_wave.WaveHooks.__dataclass_fields__)
    source = inspect.getsource(survivor_wave.git_gh_pr_opener)
    assert '"merge"' not in source and "pr merge" not in source and "--auto" not in source
    assert "push" in source and "--force" not in source
    parser = cli_module.build_parser()
    for flag in ("--merge", "--auto-merge", "--force"):
        with pytest.raises(SystemExit):
            parser.parse_args(["survivor-wave", "--tasks", "1", flag])


def test_a_failing_pr_state_lookup_does_not_turn_an_opened_pr_into_a_failure(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.pr_state = lambda url: (_ for _ in ()).throw(OSError("gh timed out"))

    report = run_wave(store, ["T-1"], hooks)

    task = report.tasks[0]
    assert (task.landing, task.ready_for_merge, task.pr_state, task.pr_error) == ("pr_opened", True, "OPEN", None)
    assert report.wave_state == "needs_pr_merges"


def test_a_broken_repo_census_does_not_sink_the_wave_or_lose_the_report(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    rig = Rig(project)
    hooks = rig.hooks()
    hooks.repo_state = lambda: (_ for _ in ()).throw(RuntimeError("git exploded"))

    report = run_wave(store, ["T-1"], hooks)

    assert report.tasks[0].outcome == "done" and report.repo_before is None and report.repo_after is None and report.ok
    assert "repo state:" in format_report(report) and json.dumps(report.to_dict())


def test_done_work_with_no_pr_is_not_reported_as_ready(tmp_path: Path) -> None:
    store, project = make_store(tmp_path, ["T-1"])
    rig = Rig(project)

    report = run_wave(store, ["T-1"], rig.hooks(), open_prs=False)

    assert report.tasks[0].landing == "staged_not_published" and report.wave_state == "unpublished_work"
    assert rig.prs == []


def test_the_installed_commands_are_read_from_the_subcommand_list_not_the_first_braces(tmp_path: Path) -> None:
    import subprocess as sp

    from stagemesh.runtime_freshness import _help_commands

    text = "usage: stagemesh [-h] [--mode {a,b}] {init,status,survivor-wave} ..."

    assert _help_commands(["x"], lambda argv, **kw: sp.CompletedProcess(argv, 0, text, "")) == ["init", "status", "survivor-wave"]
