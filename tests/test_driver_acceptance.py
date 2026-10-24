from __future__ import annotations

import json
from pathlib import Path

import pytest
from driver_acceptance_support import (
    FakeWindowProcess,
    SimulatedWindowCrash,
    calls_by_task,
    load_window_state,
    seed_window_state,
    snapshot_tree,
)

from stagemesh.continuous_driver import DriverConfig, interrupted_tick, run_tick, save_config
from stagemesh.persistence import Store


def open_store(project: Path) -> Store:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return store


def invoke_tick(project: Path):
    store = open_store(project)
    try:
        return run_tick(store, project, runner=FakeWindowProcess(project))
    finally:
        store.close()


def configure(project: Path) -> None:
    save_config(
        project,
        DriverConfig(
            project=str(project),
            interval_seconds=60,
            window_seconds=1,
            concurrency=1,
            max_tasks=1,
            max_steps_per_task=4,
            max_consecutive_failures=3,
            auto_merge_ff=True,
        ),
    )


def seed_tasks(project: Path, task_ids: tuple[str, ...]) -> None:
    store = open_store(project)
    try:
        for task_id in task_ids:
            store.upsert_task(f"acceptance task {task_id}", source_id=task_id, project=str(project))
    finally:
        store.close()


def test_dependent_chain_recovers_a_crash_without_replaying_durable_work_or_touching_sentinels(
    tmp_path: Path,
) -> None:
    project = tmp_path / "primary-project"
    project.mkdir()
    root_sentinel = project / "root-sentinel.txt"
    untracked_sentinel = project / "owner-untracked.txt"
    neighbor = project / ".stagemesh" / "worktrees" / "neighbor"
    second_project = tmp_path / "second-project"
    neighbor.mkdir(parents=True)
    second_project.mkdir()
    root_sentinel.write_text("root-owner-state\n", encoding="utf-8")
    untracked_sentinel.write_text("untracked-owner-state\n", encoding="utf-8")
    (neighbor / "neighbor-sentinel.txt").write_text("neighbor-owner-state\n", encoding="utf-8")
    (second_project / "second-project-sentinel.txt").write_text("second-project-owner-state\n", encoding="utf-8")
    protected_before = {
        "root": root_sentinel.read_bytes(),
        "untracked": untracked_sentinel.read_bytes(),
        "neighbor": snapshot_tree(neighbor),
        "second": snapshot_tree(second_project),
    }

    seed_tasks(project, ("A", "B", "C"))
    store = open_store(project)
    try:
        store.add_dependency("B", "A")
        store.add_dependency("C", "B")
    finally:
        store.close()
    configure(project)
    seed_window_state(project, crash_after_merge="A")

    with pytest.raises(SimulatedWindowCrash, match="after A merge"):
        invoke_tick(project)

    store = open_store(project)
    try:
        crashed_tick = interrupted_tick(store)
        assert crashed_tick is not None
    finally:
        store.close()

    recovered = invoke_tick(project)
    b_completed = invoke_tick(project)
    c_completed = invoke_tick(project)
    drained = invoke_tick(project)

    assert recovered.recovered_interrupted_tick == crashed_tick
    assert recovered.state == "blocked" and "INTENTIONAL_FIXTURE_FAILURE" in recovered.detail
    assert b_completed.state == c_completed.state == "progress"
    assert drained.state == "idle" and "not a completed autonomy task" in drained.detail

    state = load_window_state(project)
    assert calls_by_task(state, "provider") == {"A": 1, "B": 2, "C": 1}
    assert calls_by_task(state, "candidate") == {"A": 1, "B": 2, "C": 1}
    assert calls_by_task(state, "evidence") == {"A": 3, "B": 4, "C": 3}
    assert calls_by_task(state, "pr") == {"A": 1, "B": 1, "C": 1}
    assert calls_by_task(state, "merge") == {"A": 1, "B": 1, "C": 1}
    events = state["events"]
    assert events.index("merge:A") < events.index("provider:B:1")
    assert events.index("provider:B:1") < events.index("provider:B:2") < events.index("merge:B")
    assert events.index("merge:B") < events.index("provider:C:1") < events.index("merge:C")

    store = open_store(project)
    try:
        assert {row["id"]: (row["stage"], row["status"]) for row in store.tasks()} == {
            "A": ("DONE", "DONE"),
            "B": ("DONE", "DONE"),
            "C": ("DONE", "DONE"),
        }
        candidate_counts = {
            row["task_id"]: row["count"]
            for row in store.conn.execute("SELECT task_id, COUNT(*) AS count FROM candidates GROUP BY task_id")
        }
        evidence_counts = {
            row["task_id"]: row["count"]
            for row in store.conn.execute("SELECT task_id, COUNT(*) AS count FROM evidence GROUP BY task_id")
        }
        assert candidate_counts == {"A": 1, "B": 2, "C": 1}
        assert evidence_counts == {"A": 3, "B": 4, "C": 3}
        assert interrupted_tick(store) is None
    finally:
        store.close()

    assert root_sentinel.read_bytes() == protected_before["root"]
    assert untracked_sentinel.read_bytes() == protected_before["untracked"]
    assert snapshot_tree(neighbor) == protected_before["neighbor"]
    assert snapshot_tree(second_project) == protected_before["second"]
    assert sorted(path.name for path in (project / ".stagemesh" / "worktrees").iterdir()) == [
        "neighbor",
        "task-A",
        "task-B",
        "task-C",
    ]


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("empty", "idle"), ("all-ineligible", "idle"), ("all-blocked", "blocked")],
)
def test_no_runnable_input_is_never_reported_as_task_success(
    tmp_path: Path,
    scenario: str,
    expected: str,
) -> None:
    project = tmp_path / scenario
    project.mkdir()
    configure(project)
    if scenario != "empty":
        seed_tasks(project, ("A", "B"))
    seed_window_state(project, ineligible=("A", "B") if scenario == "all-ineligible" else ())
    if scenario == "all-blocked":
        store = open_store(project)
        try:
            store.block_task("A")
            store.block_task("B")
        finally:
            store.close()

    result = invoke_tick(project)

    assert result.state == expected
    assert result.state != "progress"
    assert result.report is not None
    report = json.loads(Path(result.report).read_text(encoding="utf-8"))
    assert report["tasks"] == [] and report["merged_prs"] == []
    state = load_window_state(project)
    assert all(not calls for calls in state["calls"].values())
