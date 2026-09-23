from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.models import (
    BuildCoordinatorState,
    BuildRunnerExecution,
    BuildTask,
    BuildTaskCheckpoint,
    BuildTaskClaim,
    BuildTaskEvent,
)
from build_coordinator.service import (
    ClaimRequest,
    claim_task,
    list_available_tasks,
    provide_task_input,
    recover_expired,
    request_task_input,
    transition_task,
    upsert_task,
)
from build_coordinator.types import TaskSpec


@pytest.fixture(autouse=True)
def clean_build_coordinator():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    with SessionLocal() as session:
        for model in (
            BuildRunnerExecution,
            BuildTaskEvent,
            BuildTaskCheckpoint,
            BuildTaskClaim,
            BuildTask,
            BuildCoordinatorState,
        ):
            session.execute(delete(model))
        session.commit()
    yield


def _task(task_id: str) -> TaskSpec:
    return TaskSpec(task_id, task_id, "d", ["ok"])


def test_waiting_for_input_lifecycle_and_restart(clean_build_coordinator):
    with SessionLocal() as session:
        upsert_task(session, _task("WAIT-1"))
        claim_task(session, ClaimRequest("WAIT-1", worker_id="builder-1"))
        transition_task(session, "WAIT-1", "IN_PROGRESS", actor="builder-1")
        request_task_input(session, "WAIT-1", "What is the target SHA?", actor="builder-1")
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "WAIT-1")
        assert task.state == "WAITING_FOR_INPUT"
        assert task.waiting_input["question"] == "What is the target SHA?"
        assert task.current_claim_id is None
        assert "WAIT-1" not in {item.task_id for item in list_available_tasks(session)}
        recovered = recover_expired(session)
        assert session.get(BuildTask, "WAIT-1").state == "WAITING_FOR_INPUT"
        assert recovered == [] or all(item.task_id != "WAIT-1" or item.state == "WAITING_FOR_INPUT" for item in recovered)

    with SessionLocal() as session:
        provide_task_input(session, "WAIT-1", "abc123", actor="operator")
        session.commit()

    with SessionLocal() as session:
        task = session.get(BuildTask, "WAIT-1")
        assert task.state == "RESUMABLE"
        assert task.waiting_input["response"] == "abc123"
        assert "WAIT-1" in {item.task_id for item in list_available_tasks(session)}
        events = list(session.scalars(select(BuildTaskEvent).where(BuildTaskEvent.task_id == "WAIT-1")))
        types = {event.event_type for event in events}
        assert "task.waiting_for_input" in types
        assert "task.input_provided" in types
        claim_task(session, ClaimRequest("WAIT-1", worker_id="builder-2"))
        transition_task(session, "WAIT-1", "IN_PROGRESS", actor="builder-2")
        assert session.get(BuildTask, "WAIT-1").state == "IN_PROGRESS"
