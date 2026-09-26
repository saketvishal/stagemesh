"""Regression coverage for the #87 lifecycle-sync defect (GH-42).

StageMesh must never treat a GitHub-backed objective as fully synchronized
just because its internal state reached `COMPLETED`:

1. A completion side-effect failure (e.g. `stagemesh:done` label update
   fails) must be durable and retryable, and must not be silently reported
   as delivered.
2. An explicit human/operator gate required by the issue contract (e.g. a
   destructive historical-maintenance step) must keep the objective in
   `HUMAN_GATE`, not `COMPLETED`, until the gate is resolved.
3. Once the label/gate is resolved, reconciliation closes the source issue
   exactly once -- not duplicated across retries/restarts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from build_coordinator.db import Base, SessionLocal, engine, initialize_schema
from build_coordinator.github.sync import compute_objective_github_status
from build_coordinator.models import BuildObjective, BuildObjectiveEvent, BuildTask
from build_coordinator.objectives import (
    apply_validated_plan,
    open_gates,
    reconcile_objective,
    resolve_gate,
)
from build_coordinator.runner.models import RunnerConfig
from build_coordinator.runner.orchestrator import BuildRunner
from build_coordinator.task_source.github import GitHubTaskSource
from build_coordinator.types import ObjectivePlan, PlannedChildTask


class LabelAwareMockClient:
    """Mirrors real GitHub label-existence semantics for outbound sync."""

    def __init__(self, existing_labels: list[str] | None = None, fail_on: str | None = None):
        self.known_labels: set[str] = set(existing_labels or [])
        self.created_labels: list[str] = []
        self.comments: list[dict] = []
        self.closed: list[str] = []
        self.labels: list[dict] = []
        self.fail_on = fail_on

    def list_issues(self, repo: str, labels: tuple[str, ...]):
        return []

    def list_labels(self, repo: str):
        return sorted(self.known_labels)

    def create_label(self, repo: str, name: str):
        if self.fail_on == "create_label":
            raise RuntimeError("permission denied: cannot create labels")
        self.created_labels.append(name)
        self.known_labels.add(name)

    def add_comment(self, repo: str, number: str, body: str):
        self.comments.append({"repo": repo, "number": str(number), "body": body})

    def add_label(self, repo: str, number: str, label: str):
        if label not in self.known_labels:
            raise RuntimeError(f"label '{label}' not found in repository")
        self.labels.append({"repo": repo, "number": str(number), "label": label})

    def close_issue(self, repo: str, number: str):
        self.closed.append(str(number))


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    initialize_schema()
    yield


def _make_completed_objective(session, objective_id: str, url: str) -> None:
    """Objective with implementation/review/integration already satisfied
    (single child task DONE), reconciled up to internal COMPLETED."""
    session.add(BuildObjective(objective_id=objective_id, goal="Ship the thing", state="PLANNING"))
    session.add(
        BuildObjectiveEvent(
            objective_id=objective_id,
            event_type="objective.synced_from_source",
            actor="github-sync",
            event_data={"source": url},
        )
    )
    session.flush()
    objective = session.get(BuildObjective, objective_id)
    plan = ObjectivePlan(
        tasks=(
            PlannedChildTask(
                task_id=f"{objective_id}-A",
                title="Implement and land the change",
                description="Implementation, review, integration all succeed.",
                acceptance_criteria=("Change is implemented and integrated",),
            ),
        ),
        source="EXPLICIT_INPUT",
    )
    apply_validated_plan(session, objective, plan)
    session.commit()

    with SessionLocal() as s2:
        child = s2.get(BuildTask, f"{objective_id}-A")
        child.state = "DONE"
        s2.commit()

    with SessionLocal() as s3:
        objective = s3.get(BuildObjective, objective_id)
        summary = reconcile_objective(s3, objective)
        s3.commit()
        assert summary.completed is True
        assert s3.get(BuildObjective, objective_id).state == "COMPLETED"


def test_failed_completion_label_update_does_not_report_false_delivery():
    """Label update fails -> internal COMPLETED must not read as delivered,
    the failure must be durable/retryable, and once fixed the issue closes
    exactly once."""
    client = LabelAwareMockClient(existing_labels=[], fail_on="create_label")
    source = GitHubTaskSource(repo="example/repo", client=client)
    url = "https://github.com/example/repo/issues/87"

    with SessionLocal() as session:
        _make_completed_objective(session, "GH-87", url)

    # Internal implementation truth is COMPLETED, but the required
    # stagemesh:done label could not be provisioned -- this must not be
    # mistaken for a fully synchronized/delivered objective.
    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()

    assert "GH-87" not in cycle.outbound_synced
    assert client.closed == []
    assert client.comments == []

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "GH-87")
        assert objective.state == "COMPLETED"
        assert source.is_objective_fully_delivered(session, "GH-87") is False

        # The objective-level GitHub status surface (used by the periodic
        # controller status sync) must not report "DONE" either -- it must
        # stay in a non-terminal, retryable status until delivery succeeds.
        assert compute_objective_github_status(session, objective, "example/repo") == "REMEDIATING"

        failures = session.scalars(
            select(BuildObjectiveEvent).where(
                BuildObjectiveEvent.objective_id == "GH-87",
                BuildObjectiveEvent.event_type == "objective.outbound_sync_failed",
            )
        ).all()
        assert len(failures) == 1
        assert failures[0].event_data.get("action") == "label_provisioning"

    # Retrying without fixing anything must not fabricate success or
    # duplicate the failure record beyond what's needed to stay retryable.
    runner.run_once()
    with SessionLocal() as session:
        assert source.is_objective_fully_delivered(session, "GH-87") is False
        assert client.closed == []

    # Once the label can be provisioned, reconciliation on a later cycle
    # resolves the failure and closes the issue -- exactly once.
    client.fail_on = None
    cycle2 = runner.run_once()
    assert "GH-87" in cycle2.outbound_synced
    assert client.closed == ["87"]

    with SessionLocal() as session:
        objective = session.get(BuildObjective, "GH-87")
        assert source.is_objective_fully_delivered(session, "GH-87") is True
        assert compute_objective_github_status(session, objective, "example/repo") == "DONE"

    # Further cycles/restarts must not close or comment again.
    runner.run_once()
    runner.run_once()
    assert client.closed == ["87"]
    assert len(client.comments) == 1


def test_human_gate_step_keeps_objective_out_of_completed_until_resolved():
    """An explicit operator gate required by the issue contract (e.g. a
    destructive historical-maintenance step) must hold the objective at
    HUMAN_GATE rather than COMPLETED, and must not trigger outbound sync,
    until a human resolves it -- after which reconciliation completes and
    closes the issue exactly once."""
    client = LabelAwareMockClient(existing_labels=[])
    source = GitHubTaskSource(repo="example/repo", client=client)
    objective_id = "GH-87"
    url = "https://github.com/example/repo/issues/87"

    with SessionLocal() as session:
        session.add(BuildObjective(objective_id=objective_id, goal="Ship + rewrite history", state="PLANNING"))
        session.add(
            BuildObjectiveEvent(
                objective_id=objective_id,
                event_type="objective.synced_from_source",
                actor="github-sync",
                event_data={"source": url},
            )
        )
        session.flush()
        objective = session.get(BuildObjective, objective_id)
        plan = ObjectivePlan(
            tasks=(
                PlannedChildTask(
                    task_id=f"{objective_id}-A",
                    title="Implement the change",
                    description="Implementation, review, integration all succeed.",
                    acceptance_criteria=("Change is implemented and integrated",),
                ),
            ),
            source="EXPLICIT_INPUT",
            requested_human_gates=("DESTRUCTIVE_ACTION_APPROVAL_REQUIRED",),
        )
        apply_validated_plan(session, objective, plan)
        session.commit()

    with SessionLocal() as session:
        assert session.get(BuildObjective, objective_id).state == "HUMAN_GATE"
        gates = open_gates(session, objective_id)
        assert len(gates) == 1
        gate_id = gates[0].gate_id
        child = session.get(BuildTask, f"{objective_id}-A")
        child.state = "DONE"
        session.commit()

    # Implementation is complete, but the human gate is still open: this
    # must not read as COMPLETED, and outbound sync must not run.
    config = RunnerConfig.default()
    runner = BuildRunner(SessionLocal, config, task_source=source)
    cycle = runner.run_once()
    assert objective_id not in cycle.outbound_synced
    assert client.closed == []

    with SessionLocal() as session:
        objective = session.get(BuildObjective, objective_id)
        assert objective.state == "HUMAN_GATE"
        assert source.is_objective_fully_delivered(session, objective_id) is False

    # Human resolves the gate (e.g. approves the history rewrite/push).
    with SessionLocal() as session:
        resolve_gate(session, gate_id, resolved_by="operator", resolution_note="approved")
        objective = session.get(BuildObjective, objective_id)
        reconcile_objective(session, objective)
        session.commit()
        assert session.get(BuildObjective, objective_id).state == "COMPLETED"

    cycle2 = runner.run_once()
    assert objective_id in cycle2.outbound_synced
    assert client.closed == ["87"]

    with SessionLocal() as session:
        assert source.is_objective_fully_delivered(session, objective_id) is True

    # Reconciliation across restarts must close exactly once.
    runner.run_once()
    assert client.closed == ["87"]
