"""Idempotent GitHub label provisioning for the watcher.

A minimal GitHub adapter boundary used only by label provisioning: get
repository labels, create a label, update a label. The adapter is a small
`Protocol` so tests can supply a fake and never touch the network, and the
default implementation shells out to the authenticated `gh` CLI.

Labels are deterministic, documented constants for public repository
triage. They intentionally avoid StageMesh runtime state: task lifecycle
mirroring remains the job of the GitHub task-source adapter's `stagemesh:*`
labels, not this repository taxonomy.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol

from build_coordinator.watcher.authorization import AuthorizedRepository


class LabelProvisionError(RuntimeError):
    """Raised when label provisioning cannot complete."""


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


MANAGED_LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("priority:P0", "b60205", "Critical stabilization or release-blocking work"),
    LabelSpec("priority:P1", "d93f0b", "Important work planned for the active stabilization window"),
    LabelSpec("priority:P2", "fbca04", "Useful work that can wait behind active stabilization"),
    LabelSpec("type:bug", "d73a4a", "Incorrect behavior or regression"),
    LabelSpec("type:docs", "0075ca", "Documentation-only change"),
    LabelSpec("type:maintenance", "5319e7", "Repository, tooling, dependency, or housekeeping work"),
    LabelSpec("type:feature", "1d76db", "New or expanded user-visible capability"),
    LabelSpec("lifecycle:needs-triage", "cfd3d7", "Needs owner review before it is treated as active backlog"),
    LabelSpec("lifecycle:superseded", "ededed", "Replaced by a canonical issue or pull request"),
    LabelSpec("lifecycle:validated", "0e8a16", "Integrated or otherwise validated with durable evidence"),
    LabelSpec("roadmap:future-work", "bfdadc", "Accepted direction, deferred beyond the current roadmap cut"),
    LabelSpec("good first issue", "7057ff", "Small, well-scoped task suitable for a first contribution"),
    LabelSpec("help wanted", "008672", "External contributor help is welcome"),
)


class LabelGateway(Protocol):
    def get_labels(self, repo: str) -> dict[str, LabelSpec]: ...

    def create_label(self, repo: str, spec: LabelSpec) -> None: ...

    def update_label(self, repo: str, spec: LabelSpec) -> None: ...


class GhCliLabelGateway:
    """Default label gateway backed by the authenticated `gh` CLI."""

    def __init__(self, *, gh_path: str = "gh") -> None:
        self._gh_path = gh_path

    def _run(self, args: list[str]) -> str:
        try:
            completed = subprocess.run(
                [self._gh_path, *args],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise LabelProvisionError(f"failed to run gh command {args}: {exc}") from exc
        if completed.returncode != 0:
            err = completed.stderr.strip() or completed.stdout.strip()
            raise LabelProvisionError(f"gh {' '.join(args)} failed (code {completed.returncode}): {err}")
        return completed.stdout

    def get_labels(self, repo: str) -> dict[str, LabelSpec]:
        stdout = self._run(
            ["label", "list", "--repo", repo, "--json", "name,color,description", "--limit", "200"]
        )
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LabelProvisionError(f"invalid json from gh label list: {exc}") from exc
        return {
            str(item["name"]): LabelSpec(
                name=str(item["name"]),
                color=str(item.get("color", "")).lower(),
                description=str(item.get("description") or ""),
            )
            for item in raw
        }

    def create_label(self, repo: str, spec: LabelSpec) -> None:
        self._run(
            [
                "label",
                "create",
                spec.name,
                "--repo",
                repo,
                "--color",
                spec.color,
                "--description",
                spec.description,
            ]
        )

    def update_label(self, repo: str, spec: LabelSpec) -> None:
        self._run(
            [
                "label",
                "edit",
                spec.name,
                "--repo",
                repo,
                "--color",
                spec.color,
                "--description",
                spec.description,
            ]
        )


@dataclass(frozen=True)
class ProvisionResult:
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()


def provision_labels(
    repo: AuthorizedRepository,
    *,
    gateway: LabelGateway | None = None,
    dry_run: bool = False,
) -> ProvisionResult:
    """Idempotently converge `repo`'s managed labels: create missing ones,
    update drifted color/description, leave already-correct labels
    untouched. Fails closed before any mutation if `repo.labels` is False
    or the repository has not been validated as authorized -- callers must
    pass an `AuthorizedRepository` obtained from
    `watcher.authorization.authorize`, never a raw slug."""
    if not repo.labels:
        return ProvisionResult()
    gateway = gateway or GhCliLabelGateway()
    existing = gateway.get_labels(repo.slug)

    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for spec in MANAGED_LABELS:
        current = existing.get(spec.name)
        if current is None:
            created.append(spec.name)
            if not dry_run:
                gateway.create_label(repo.slug, spec)
            continue
        if current.color != spec.color.lower() or current.description != spec.description:
            updated.append(spec.name)
            if not dry_run:
                gateway.update_label(repo.slug, spec)
            continue
        unchanged.append(spec.name)
    return ProvisionResult(created=tuple(created), updated=tuple(updated), unchanged=tuple(unchanged))
