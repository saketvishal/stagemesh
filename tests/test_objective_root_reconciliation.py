from __future__ import annotations

from pathlib import Path

from test_run_ready import _project

from stagemesh.config import TaskSelectionConfig
from stagemesh.domain import TaskStatus
from stagemesh.persistence import Store
from stagemesh.run_ready import run_ready
from stagemesh.scheduling import Scheduler
from stagemesh.task_selection import select_next_task
from stagemesh.task_sources import DiscoveredTask, sync_source


def _synced(project: Path) -> Store:
    store = Store(project / ".stagemesh" / "stagemesh.sqlite3")
    store.migrate()
    return store


def test_historical_github_objective_root_is_not_selected(tmp_path: Path) -> None:
    project = _project(tmp_path, ["160"], contracts=["160"], labels={"160": ["priority:p1"]})
    store = _synced(project)
    try:
        store.upsert_task("historical broad objective", source="github", source_id="71")
        store.cache_source(
            "github",
            "71",
            {"eligible": True, "state": "OPEN", "labels": ["priority:p0"], "objective": "broad architecture objective"},
            "OPEN",
        )
        store.save_objective("71", "historical broad objective", {"source": "github", "source_id": "71"})
        store.upsert_task("ordinary ready task", source="local", source_id="160")

        selection = select_next_task(store, project, TaskSelectionConfig())

        assert selection.task_id == "160"
        assert {"task_id": "71", "reason": "historical source objective root"} in selection.skipped
    finally:
        store.close()


def test_explicit_historical_objective_root_is_refused_before_auto_plan(tmp_path: Path) -> None:
    project = _project(tmp_path, ["160"], contracts=["160"])
    store = _synced(project)
    try:
        store.upsert_task("historical broad objective", source="github", source_id="71")
        store.cache_source(
            "github",
            "71",
            {"eligible": True, "state": "OPEN", "labels": ["priority:p0"], "objective": "broad architecture objective"},
            "OPEN",
        )
        store.save_objective("71", "historical broad objective", {"source": "github", "source_id": "71"})

        summary = run_ready(store, project, lambda target: (_ for _ in ()).throw(AssertionError("should not start")), task_id="71")

        assert summary.started is False
        assert summary.stop_reason == "REFUSED:objective_root_not_runnable"
        assert summary.auto_plan["occurred"] is False
        assert len(summary.steps) == 0
    finally:
        store.close()


def test_sync_retires_objective_root_without_erasing_history(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    try:
        task_id = store.upsert_task("historical broad objective", source="github", source_id="71")
        candidate_id = store.add_candidate(task_id, "a" * 40, "codex", True)

        ids = sync_source(
            store,
            [
                DiscoveredTask(
                    "github",
                    "71",
                    "broad objective",
                    labels=("stagemesh:objective", "priority:p0"),
                    body="Depends on: #75\n\n## Objective\nBroad program work",
                ),
                DiscoveredTask("github", "160", "ordinary ready task", labels=("priority:p1",)),
            ],
        )

        task = store.get_task("71")
        assert ids == ["160"]
        assert task["status"] == TaskStatus.BLOCKED
        assert Scheduler(store).decision("71").reason == "source objective root"
        assert store.latest_candidate("71")["id"] == candidate_id
        assert store.source_state("github", "71")["objective_root"] is True
        objective = store.conn.execute("SELECT payload FROM objectives WHERE id='71'").fetchone()
        assert objective is not None
    finally:
        store.close()


def test_namespaced_objective_label_is_not_selected(tmp_path: Path) -> None:
    project = _project(tmp_path, ["160"], contracts=["160"], labels={"160": ["priority:p1"]})
    store = _synced(project)
    try:
        store.upsert_task("Objective: broad program root", source="github", source_id="40")
        sync_source(
            store,
            [
                DiscoveredTask(
                    "github",
                    "40",
                    "Objective: broad program root",
                    labels=("caventra:objective", "status:QUEUED"),
                    body="## Objective\nBroad program work",
                ),
                DiscoveredTask("github", "160", "ordinary ready task", labels=("priority:p1",)),
            ],
        )

        selection = select_next_task(store, project, TaskSelectionConfig())

        assert selection.task_id == "160"
        assert store.get_task("40")["status"] == TaskStatus.BLOCKED
        assert Scheduler(store).decision("40").reason == "source objective root"
        assert store.source_state("github", "40")["objective_root"] is True
    finally:
        store.close()


def test_direct_execution_label_keeps_github_issue_runnable(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.migrate()
    try:
        ids = sync_source(
            store,
            [
                DiscoveredTask(
                    "github",
                    "72",
                    "direct objective task",
                    labels=("stagemesh:objective", "stagemesh:direct-execution"),
                )
            ],
        )

        assert ids == ["72"]
        assert store.get_task("72")["status"] == TaskStatus.OPEN
    finally:
        store.close()
