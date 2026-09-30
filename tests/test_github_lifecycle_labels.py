from __future__ import annotations

from pathlib import Path
import pytest

from stagemesh.domain import Stage
from stagemesh.labels import (
    ALL_LIFECYCLE_LABELS,
    LabelGateway,
    GitHubLabelGateway,
    provision_labels,
    sync_issue_lifecycle_label,
)
from stagemesh.persistence import Store


class FakeLabelGateway(LabelGateway):
    def __init__(self, existing_labels: list[dict] | None = None):
        self.labels = {l["name"]: dict(l) for l in (existing_labels or [])}
        self.issue_labels: dict[int, set[str]] = {}
        self.should_fail = False

    def list_labels(self) -> list[dict]:
        if self.should_fail:
            raise RuntimeError("GitHub API connection error")
        return list(self.labels.values())

    def create_label(self, name: str, color: str, description: str) -> None:
        if self.should_fail:
            raise RuntimeError("GitHub API label create error")
        self.labels[name] = {"name": name, "color": color, "description": description}

    def edit_label(self, name: str, color: str, description: str) -> None:
        if self.should_fail:
            raise RuntimeError("GitHub API label edit error")
        self.labels[name] = {"name": name, "color": color, "description": description}

    def add_issue_label(self, issue_number: int, label: str) -> None:
        if self.should_fail:
            raise RuntimeError("GitHub API issue label add error")
        self.issue_labels.setdefault(issue_number, set()).add(label)

    def remove_issue_label(self, issue_number: int, label: str) -> None:
        if self.should_fail:
            raise RuntimeError("GitHub API issue label remove error")
        if issue_number in self.issue_labels:
            self.issue_labels[issue_number].discard(label)


def test_labels_setup_creates_missing_labels_idempotently():
    gateway = FakeLabelGateway()
    created_count = provision_labels(gateway)
    assert created_count > 0
    assert len(gateway.labels) >= len(ALL_LIFECYCLE_LABELS)

    for label_name in ALL_LIFECYCLE_LABELS:
        assert label_name in gateway.labels

    # Idempotent second call
    second_created = provision_labels(gateway)
    assert second_created == 0


def test_existing_labels_preserved_safely():
    existing = [{"name": "custom-label", "color": "123456", "description": "Custom"}]
    gateway = FakeLabelGateway(existing_labels=existing)
    provision_labels(gateway)
    assert "custom-label" in gateway.labels
    assert gateway.labels["custom-label"]["color"] == "123456"


def test_github_lifecycle_transitions_update_correct_issue_and_remove_stale():
    gateway = FakeLabelGateway()
    provision_labels(gateway)

    issue_number = 42

    # Transition 1: Stage.IMPLEMENT -> stagemesh:claimed / stagemesh:running
    sync_issue_lifecycle_label(gateway, issue_number, Stage.IMPLEMENT)
    assert "stagemesh:running" in gateway.issue_labels[issue_number]

    # Transition 2: Stage.VALIDATE -> stagemesh:validating (removes stagemesh:running)
    sync_issue_lifecycle_label(gateway, issue_number, Stage.VALIDATE)
    assert "stagemesh:validating" in gateway.issue_labels[issue_number]
    assert "stagemesh:running" not in gateway.issue_labels[issue_number]

    # Transition 3: Stage.DONE -> stagemesh:done (removes stagemesh:validating)
    sync_issue_lifecycle_label(gateway, issue_number, Stage.DONE)
    assert "stagemesh:done" in gateway.issue_labels[issue_number]
    assert "stagemesh:validating" not in gateway.issue_labels[issue_number]


def test_source_synchronization_failure_does_not_corrupt_internal_lifecycle(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.migrate()
    task_id = store.upsert_task("Label Failure Task", source="github", source_id="99")
    store.advance_task(task_id, Stage.VALIDATE)

    gateway = FakeLabelGateway()
    gateway.should_fail = True

    # Exception raised during remote sync projection does not crash internal store truth
    with pytest.raises(RuntimeError, match="GitHub API"):
        sync_issue_lifecycle_label(gateway, 99, Stage.VALIDATE)

    # Internal state is preserved
    task = store.get_task(task_id)
    assert task["stage"] == Stage.VALIDATE
