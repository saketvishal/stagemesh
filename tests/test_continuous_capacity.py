"""Work-conserving coding capacity, with bounded independent verification."""

from __future__ import annotations

import threading

import pytest
from stabilization_support import _brief_text
from test_run_window import window
from test_survivor_wave import Rig, make_store

from stagemesh.domain import Stage
from stagemesh.run_window import run_window
from stagemesh.survivor_wave import RunResult, run_wave


def test_verification_releases_coding_capacity_before_the_slow_builder_finishes(tmp_path):
    store, project = make_store(tmp_path, ["1", "2", "3", "4"])
    for task_id in ("1", "2", "3", "4"):
        store.advance_task(task_id, Stage.IMPLEMENT)
    first_builders = threading.Barrier(2)
    third_builder = threading.Event()
    validation_started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = {"coding": 0, "verification": 0}
    peaks = dict(active)
    budgets = []

    def step(task_id, max_steps):
        worker = rig.worker_store()
        phase = "coding" if worker.get_task(task_id)["stage"] == Stage.IMPLEMENT else "verification"
        with lock:
            budgets.append(max_steps)
            active[phase] += 1
            peaks[phase] = max(peaks[phase], active[phase])
        try:
            if phase == "coding":
                if task_id in {"1", "2"}:
                    first_builders.wait(5)
                if task_id == "2":
                    assert release.wait(5), "coding slot was not refilled"
                if task_id == "3":
                    third_builder.set()
                    assert validation_started.wait(5), "validation did not overlap coding"
                    release.set()
                worker.advance_task(task_id, Stage.VALIDATE)
                return RunResult("MAX_STEPS")
            if task_id == "1":
                validation_started.set()
                assert third_builder.wait(5)
                assert release.wait(5)
            return rig.finish_done(task_id)
        finally:
            with lock:
                active[phase] -= 1
            worker.close()

    rig = Rig(project)
    hooks = rig.hooks()
    hooks.run_task = step
    report = run_wave(store, ["1", "2", "3", "4"], hooks, concurrency=2,
                      verification_concurrency=1, max_steps=4, project=project)
    assert report.ok and all(t.outcome == "done" for t in report.tasks), {
        "task_count": len(report.tasks),
        "task_outcomes": [(t.task_id, t.outcome, t.stop_reason, _brief_text(t.reason)) for t in report.tasks[:10]],
    }
    assert peaks == {"coding": 2, "verification": 1}
    assert set(budgets) == {1}
    assert report.capacity["peak_coding"] == 2


def test_window_admits_newly_ready_dependency_without_waiting_for_the_wave(tmp_path):
    started = threading.Event()

    def slow(rig, task_id):
        assert started.wait(5), "newly ready work waited for unrelated slow work"
        return rig.finish_done(task_id)

    def downstream(rig, task_id):
        started.set()
        return rig.finish_done(task_id)

    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2", "3"], {"1": slow, "3": downstream}, concurrency=2)
    for task_id in ("1", "2", "3"):
        store.advance_task(task_id, Stage.IMPLEMENT)
    store.add_dependency("3", "2")
    report = run_window(store, project, config, options, hooks,
                        base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert report.ok and set(rig.runs) == {"1", "2", "3"}
    assert all(t.outcome == "done" for t in report.tasks)
    import json

    contexts = [json.loads(row["payload"]) for row in store.conn.execute(
        "SELECT payload FROM audit_events WHERE event_type='window.task_context'",
    )]
    assert any(context["task_id"] == "3" and context["integration_ref"] == "refs/heads/wave/staging" for context in contexts)


def test_continuous_frontier_rechecks_provider_budget_before_starting_another_task(tmp_path):
    from dataclasses import replace

    from stagemesh.efficiency import record_provider_call

    def spend(rig, task_id):
        worker = rig.worker_store()
        try:
            record_provider_call(worker, task_id=task_id, purpose="implementation", provider="mock",
                                 outcome="succeeded", duration_seconds=1.0)
        finally:
            worker.close()
        return rig.finish_done(task_id)

    store, project, rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2", "3"], {"1": spend, "2": spend, "3": spend}, concurrency=1)
    config = replace(config, budgets=replace(config.budgets, max_provider_calls_per_window=1))
    report = run_window(store, project, config, options, hooks,
                        base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert "provider-call budget reached" in report.stop_reason
    assert rig.runs == ["1"]
    assert store.get_task("2")["stage"] == Stage.IMPLEMENT


def test_background_source_refresh_preserves_dependency_before_dispatch(tmp_path):
    release = threading.Event()

    def prerequisite(rig, task_id):
        assert release.wait(5)
        return rig.finish_done(task_id)

    def dependent(rig, task_id):
        worker = rig.worker_store()
        try:
            assert worker.get_task("1")["stage"] == Stage.DONE
        finally:
            worker.close()
        return rig.finish_done(task_id)

    store, project, _rig, hooks, options, config, kwargs = window(
        tmp_path, ["1", "2"], {"1": prerequisite, "2": dependent}, concurrency=2)
    store.add_dependency("2", "1")  # the backlog fixture does not declare this registered prerequisite
    states = []

    def capacity(state):
        states.append(state)
        release.set()

    hooks.on_capacity = capacity
    report = run_window(store, project, config, options, hooks,
                        base_ref="refs/heads/main", staging_ref="refs/heads/wave/staging", **kwargs)
    assert report.ok and all(task.outcome == "done" for task in report.tasks)
    assert store.conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 1
    assert states[0]["coding"] == 1


def test_stage_yield_keeps_conflicting_task_scope_reserved(tmp_path):
    import json

    store, project = make_store(tmp_path, ["1", "2"])
    for task_id in ("1", "2"):
        store.advance_task(task_id, Stage.IMPLEMENT)
        path = project / ".stagemesh/contracts" / f"{task_id}.json"
        contract = json.loads(path.read_text())
        contract["allowed_files"] = ["docs/shared.md"]
        contract["exclusive_resources"] = ["shared-test-state"]
        path.write_text(json.dumps(contract))
    order = []
    rig = Rig(project)

    def step(task_id, max_steps):
        worker = rig.worker_store()
        stage = worker.get_task(task_id)["stage"]
        order.append((task_id, stage))
        try:
            if stage == Stage.IMPLEMENT:
                worker.advance_task(task_id, Stage.VALIDATE)
                return RunResult("MAX_STEPS")
            return rig.finish_done(task_id)
        finally:
            worker.close()

    hooks = rig.hooks()
    hooks.run_task = step
    report = run_wave(store, ["1", "2"], hooks, concurrency=2, verification_concurrency=1,
                      max_steps=4, project=project)
    assert report.ok
    assert order == [("1", Stage.IMPLEMENT), ("1", Stage.VALIDATE),
                     ("2", Stage.IMPLEMENT), ("2", Stage.VALIDATE)]


def test_five_real_coding_processes_overlap_in_separate_task_directories(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    task_ids = [str(n) for n in range(1, 6)]
    store, project = make_store(tmp_path, task_ids)
    for task_id in task_ids:
        store.advance_task(task_id, Stage.IMPLEMENT)
    markers = project / "overlap-proof"
    markers.mkdir()
    source = str(Path(__file__).resolve().parents[1] / "src")
    child = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from stagemesh.persistence import Store
from stagemesh.domain import Stage, EvidenceKind, EvidenceStatus
project, task_id = Path(sys.argv[2]), sys.argv[3]
store = Store(project / ".stagemesh/stagemesh.sqlite3")
stage = store.get_task(task_id)["stage"]
if stage == Stage.IMPLEMENT:
    directory = project / ("worker-" + task_id)
    directory.mkdir()
    (directory / "change.txt").write_text(task_id)
    (project / "overlap-proof" / (task_id + ".started")).write_text(str(os.getpid()))
    deadline = time.monotonic() + 10
    while len(list((project / "overlap-proof").glob("*.started"))) < 5:
        if time.monotonic() >= deadline:
            raise RuntimeError("coding processes did not overlap")
        time.sleep(0.01)
    store.advance_task(task_id, Stage.VALIDATE)
    reason = "MAX_STEPS"
else:
    sha = task_id * 40
    store.add_candidate(task_id, sha, "synthetic-process", True)
    for kind in (EvidenceKind.VALIDATION, EvidenceKind.REVIEW, EvidenceKind.INTEGRATION):
        store.add_evidence(task_id, sha, kind, EvidenceStatus.PASSED)
    store.advance_task(task_id, Stage.DONE)
    reason = "DONE"
store.close()
print(json.dumps({"stop_reason": reason}))
'''
    rig = Rig(project)
    hooks = rig.hooks()

    def run_child(task_id, max_steps):
        assert max_steps == 1
        result = subprocess.run([sys.executable, "-c", child, source, str(project), task_id],
                                capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == 0, result.stderr
        return RunResult(json.loads(result.stdout)["stop_reason"])

    hooks.run_task = run_child
    report = run_wave(store, task_ids, hooks, concurrency=5, verification_concurrency=2,
                      max_steps=4, project=project)
    assert report.ok and all(t.outcome == "done" for t in report.tasks)
    assert len({p.read_text() for p in markers.glob("*.started")}) == 5
    assert report.capacity["peak_coding"] == 5
    assert report.capacity["peak_verification"] <= 2
    for task_id in task_ids:
        assert (project / f"worker-{task_id}/change.txt").read_text() == task_id


def test_stage_dispatches_share_the_original_bounded_task_budget(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    store.advance_task("1", Stage.IMPLEMENT)
    rig = Rig(project)
    calls = []

    def step(task_id, max_steps):
        calls.append(max_steps)
        worker = rig.worker_store()
        try:
            old = worker.get_task(task_id)["stage"]
            worker.advance_task(task_id, Stage.VALIDATE if old == Stage.IMPLEMENT else Stage.IMPLEMENT)
            return RunResult("MAX_STEPS")
        finally:
            worker.close()

    hooks = rig.hooks()
    hooks.run_task = step
    report = run_wave(store, ["1"], hooks, concurrency=1, verification_concurrency=1,
                      max_steps=3, project=project)
    assert calls == [1, 1, 1]
    assert report.tasks[0].outcome == "open" and report.tasks[0].resumable
    assert report.tasks[0].stop_reason == "MAX_STEPS"


def test_deadline_after_coding_preserves_candidate_stage_and_starts_no_verification(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    store.advance_task("1", Stage.IMPLEMENT)
    now = [0.0]
    rig = Rig(project)
    calls = []

    def step(task_id, max_steps):
        calls.append(task_id)
        worker = rig.worker_store()
        try:
            worker.add_candidate(task_id, "c" * 40, "synthetic", True)
            worker.advance_task(task_id, Stage.VALIDATE)
            now[0] = 11.0
            return RunResult("MAX_STEPS")
        finally:
            worker.close()

    hooks = rig.hooks()
    hooks.run_task = step
    report = run_wave(store, ["1"], hooks, concurrency=1, verification_concurrency=1,
                      deadline=10.0, clock=lambda: now[0], project=project)
    assert calls == ["1"] and report.time_limit_reached
    assert report.tasks[0].outcome == "open" and report.tasks[0].resumable
    assert store.latest_candidate("1")["sha"] == "c" * 40
    assert store.get_task("1")["stage"] == Stage.VALIDATE


def test_verification_capacity_must_be_positive(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    rig = Rig(project)
    with pytest.raises(ValueError, match="verification_concurrency"):
        run_wave(store, ["1"], rig.hooks(), verification_concurrency=0)
    assert not rig.runs


def test_baseline_preparation_cannot_turn_verification_into_an_extra_coder(tmp_path):
    store, project = make_store(tmp_path, ["1", "2", "3"])
    for task_id in ("1", "2"):
        store.advance_task(task_id, Stage.IMPLEMENT)
    store.advance_task("3", Stage.VALIDATE)
    release = threading.Event()
    rig = Rig(project)
    hooks = rig.hooks()
    first = []

    def prepare(task_id):
        store.advance_task(task_id, Stage.IMPLEMENT)
        return {}

    def step(task_id, max_steps):
        assert release.wait(5)
        return rig.finish_done(task_id)

    def capacity(state):
        if not first:
            first.append(dict(state))
        release.set()

    hooks.advance_baseline, hooks.run_task, hooks.on_capacity = prepare, step, capacity
    report = run_wave(store, ["1", "2", "3"], hooks, concurrency=2, verification_concurrency=1,
                      project=project)
    assert report.ok and all(t.outcome == "done" for t in report.tasks)
    assert first[0]["coding"] == 2
    assert report.capacity["peak_coding"] == 2


def test_baseline_preparation_rechecks_deadline_before_starting_a_child(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    rig = Rig(project)
    hooks = rig.hooks()
    now = [0.0]

    def prepare(task_id):
        now[0] = 11.0
        return {}

    hooks.advance_baseline = prepare
    report = run_wave(store, ["1"], hooks, concurrency=1, verification_concurrency=1,
                      deadline=10.0, clock=lambda: now[0], project=project)
    assert report.time_limit_reached and rig.runs == []
    assert report.tasks[0].outcome == "not_started"


def test_baseline_preparation_rechecks_provider_budget_before_starting_a_child(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    rig = Rig(project)
    hooks = rig.hooks()
    exhausted = False

    def prepare(task_id):
        nonlocal exhausted
        exhausted = True  # Another worker can consume the final call during preparation.
        return {}

    hooks.advance_baseline = prepare
    report = run_wave(
        store, ["1"], hooks, concurrency=1, verification_concurrency=1, project=project,
        admission_problem=lambda: "provider budget exhausted during preparation" if exhausted else None,
    )
    assert rig.runs == []
    assert report.stop_reason == "provider budget exhausted during preparation"
    assert report.tasks[0].outcome == "not_started"


def test_same_stage_recovery_keeps_running_within_the_task_budget(tmp_path):
    store, project = make_store(tmp_path, ["1"])
    store.advance_task("1", Stage.IMPLEMENT)
    rig = Rig(project)
    calls = []

    def step(task_id, max_steps):
        calls.append(max_steps)
        # Provider fallback can progress without changing the persisted stage.
        return RunResult("MAX_STEPS")

    hooks = rig.hooks()
    hooks.run_task = step
    report = run_wave(store, ["1"], hooks, concurrency=1, verification_concurrency=1,
                      max_steps=3, project=project)
    assert calls == [1, 1, 1]
    assert report.tasks[0].outcome == "open" and report.tasks[0].resumable
    assert report.tasks[0].stop_reason == "MAX_STEPS"


def test_cli_has_separate_coding_and_verification_capacity():
    from stagemesh.cli import build_parser

    default = build_parser().parse_args(["run-window"])
    assert default.verification_concurrency == 2
    args = build_parser().parse_args(["run-window", "--concurrency", "5",
                                      "--verification-concurrency", "3"])
    assert args.concurrency == 5 and args.verification_concurrency == 3
