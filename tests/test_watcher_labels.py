"""Label provisioning idempotency/drift tests (SDD-001 section 9.5)."""

from __future__ import annotations

from pathlib import Path

from build_coordinator.watcher.authorization import AuthorizedRepository
from build_coordinator.watcher.labels import MANAGED_LABELS, LabelSpec, provision_labels


class FakeLabelGateway:
    def __init__(self, initial: dict[str, LabelSpec] | None = None):
        self.labels = dict(initial or {})
        self.created: list[str] = []
        self.updated: list[str] = []

    def get_labels(self, repo: str) -> dict[str, LabelSpec]:
        return dict(self.labels)

    def create_label(self, repo: str, spec: LabelSpec) -> None:
        self.labels[spec.name] = spec
        self.created.append(spec.name)

    def update_label(self, repo: str, spec: LabelSpec) -> None:
        self.labels[spec.name] = spec
        self.updated.append(spec.name)


def _repo(**overrides) -> AuthorizedRepository:
    defaults = dict(
        slug="saketvishal/caventra-orchestrator",
        control_repo_root=Path("C:/caventra-orchestrator"),
        labels=True,
    )
    defaults.update(overrides)
    return AuthorizedRepository(**defaults)


def test_provision_creates_missing_labels():
    gateway = FakeLabelGateway()
    result = provision_labels(_repo(), gateway=gateway)
    assert set(result.created) == {spec.name for spec in MANAGED_LABELS}
    assert result.updated == ()
    assert len(gateway.labels) == len(MANAGED_LABELS)


def test_provision_leaves_already_correct_labels_unchanged():
    gateway = FakeLabelGateway(initial={spec.name: spec for spec in MANAGED_LABELS})
    result = provision_labels(_repo(), gateway=gateway)
    assert result.created == ()
    assert result.updated == ()
    assert set(result.unchanged) == {spec.name for spec in MANAGED_LABELS}
    assert gateway.created == []
    assert gateway.updated == []


def test_provision_updates_drifted_labels():
    first = MANAGED_LABELS[0]
    drifted = LabelSpec(first.name, "ffffff", "a stale description")
    gateway = FakeLabelGateway(initial={drifted.name: drifted})
    result = provision_labels(_repo(), gateway=gateway)
    assert first.name in result.updated
    assert gateway.labels[first.name] == first


def test_provision_is_idempotent_across_repeated_calls():
    gateway = FakeLabelGateway()
    provision_labels(_repo(), gateway=gateway)
    second = provision_labels(_repo(), gateway=gateway)
    assert second.created == ()
    assert set(second.unchanged) == {spec.name for spec in MANAGED_LABELS}


def test_provision_skips_when_labels_disabled():
    gateway = FakeLabelGateway()
    result = provision_labels(_repo(labels=False), gateway=gateway)
    assert result.created == () and result.updated == () and result.unchanged == ()
    assert gateway.labels == {}


def test_provision_dry_run_does_not_mutate():
    gateway = FakeLabelGateway()
    result = provision_labels(_repo(), gateway=gateway, dry_run=True)
    assert set(result.created) == {spec.name for spec in MANAGED_LABELS}
    assert gateway.labels == {}
    assert gateway.created == []
