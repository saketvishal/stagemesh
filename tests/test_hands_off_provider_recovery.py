from __future__ import annotations

from stagemesh.coordinator import Coordinator, TargetSelection
from stagemesh.diagnosis import PROVIDER_NO_PROGRESS, diagnose
from stagemesh.domain import TaskStatus
from stagemesh.recovery import task_doctor

from test_bounded_execution import TASK
from test_diagnosis import DOCS_CONTRACT, Attempts, _setup, drive, events


def test_provider_no_progress_recommends_continue_not_operator_provider_choice(tmp_path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    coordinator = Coordinator(store, project, executor=Attempts(nothing=True), target=TargetSelection(TASK))
    for _ in range(2):
        coordinator.tick()

    diagnosis = diagnose(store, TASK, project)

    assert diagnosis.category == PROVIDER_NO_PROGRESS
    assert "stagemesh continue" in diagnosis.recommendation
    assert "--provider" not in diagnosis.recommendation
    assert "task-doctor" not in diagnosis.recommendation
    assert "retry-task" not in diagnosis.recommendation


def test_task_doctor_keeps_provider_no_progress_on_continue_path(tmp_path) -> None:
    project, store = _setup(tmp_path, DOCS_CONTRACT)
    drive(project, store, Attempts(nothing=True), ticks=10)

    assert store.get_task(TASK)["status"] == TaskStatus.BLOCKED
    report = task_doctor(store, project, TASK)

    assert report["diagnosis"]["category"] == PROVIDER_NO_PROGRESS
    assert report["recommended_command"] == "stagemesh continue"
    assert "--provider" not in report["recommendation_reason"]
    assert "task-doctor" not in report["recommendation_reason"]
    assert "retry-task" not in report["recommendation_reason"]
    (stop,) = events(store, "task.diagnosis_stop")
    assert "stagemesh continue" in stop["recommendation"]
