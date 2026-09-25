"""Idempotent GitHub label provisioning for the watcher (SDD-001 section 4.5).

A minimal GitHub adapter boundary used only by label provisioning: get
repository labels, create a label, update a label. The adapter is a small
`Protocol` so tests can supply a fake and never touch the network, and the
default implementation shells out to the authenticated `gh` CLI, matching
the zero-secret pattern already used by
`tooling.build_coordinator.github.client.GitHubClient`.

Labels are deterministic, documented constants, namespaced `coordinator:*`
to avoid colliding with the existing `caventra:objective` / `status:*`
labels already provisioned by `GitHubClient.ensure_orchestration_labels`
(see `tooling/build_coordinator/github/client.py`) -- this is new,
watcher-lifecycle-facing labeling, not a replacement for the existing
issue-status labels.
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
    LabelSpec("coordinator:objective", "5319e7", "Caventra build-coordinator objective"),
    LabelSpec("coordinator:task", "1d76db", "Caventra build-coordinator task"),
    LabelSpec("coordinator:human-gate", "b60205", "Blocked on a required human approval gate"),
    LabelSpec("coordinator:blocked", "d93f0b", "Blocked pending remediation or an external dependency"),
    LabelSpec("coordinator:review-ready", "0e8a16", "Ready for independent review"),
    LabelSpec("coordinator:integration-ready", "0e8a16", "Reviewed and ready for integration"),
    LabelSpec("coordinator:done", "cfd3d7", "Completed"),
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
