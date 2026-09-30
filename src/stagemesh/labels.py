from __future__ import annotations

import json
import subprocess
from typing import Any

from .domain import Stage

ALL_LIFECYCLE_LABELS = {
    "stagemesh:ready": {"color": "0E8A16", "description": "Ready for StageMesh claiming"},
    "stagemesh:claimed": {"color": "1D76DB", "description": "Claimed by StageMesh worker"},
    "stagemesh:running": {"color": "5319E7", "description": "Implementation in progress"},
    "stagemesh:validating": {"color": "FBCA04", "description": "Validation in progress"},
    "stagemesh:reviewing": {"color": "1D76DB", "description": "Independent review in progress"},
    "stagemesh:blocked": {"color": "D93F0B", "description": "Blocked by dependencies or failure"},
    "stagemesh:done": {"color": "0E8A16", "description": "Lifecycle complete"},
    "stagemesh:failed": {"color": "B60205", "description": "Task execution failed"},
    "stagemesh:deferred": {"color": "C5DEF5", "description": "Execution deferred"},
}

TAXONOMY_LABELS = {
    "priority:P0": {"color": "B60205", "description": "P0 Critical priority"},
    "priority:P1": {"color": "D93F0B", "description": "P1 High priority"},
    "type:bug": {"color": "D93F0B", "description": "Bug report"},
    "type:feature": {"color": "1D76DB", "description": "New feature"},
}


class LabelGateway:
    """Abstract interface for managing remote repository issue labels."""

    def list_labels(self) -> list[dict]:
        raise NotImplementedError

    def create_label(self, name: str, color: str, description: str) -> None:
        raise NotImplementedError

    def edit_label(self, name: str, color: str, description: str) -> None:
        raise NotImplementedError

    def add_issue_label(self, issue_number: int, label: str) -> None:
        raise NotImplementedError

    def remove_issue_label(self, issue_number: int, label: str) -> None:
        raise NotImplementedError


class GitHubLabelGateway(LabelGateway):
    """GitHub CLI (gh) implementation of LabelGateway."""

    def __init__(self, repo: str | None = None):
        self.repo = repo

    def _base_cmd(self) -> list[str]:
        cmd = ["gh", "label"]
        if self.repo:
            cmd.extend(["-R", self.repo])
        return cmd

    def list_labels(self) -> list[dict]:
        cmd = [*self._base_cmd(), "list", "--json", "name,color,description", "--limit", "300"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to list GitHub labels: {res.stderr}")
        return json.loads(res.stdout)

    def create_label(self, name: str, color: str, description: str) -> None:
        cmd = [*self._base_cmd(), "create", name, "--color", color, "--description", description]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create label {name}: {res.stderr}")

    def edit_label(self, name: str, color: str, description: str) -> None:
        cmd = [*self._base_cmd(), "edit", name, "--color", color, "--description", description]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to edit label {name}: {res.stderr}")

    def add_issue_label(self, issue_number: int, label: str) -> None:
        cmd = ["gh", "issue", "edit", str(issue_number), "--add-label", label]
        if self.repo:
            cmd.extend(["-R", self.repo])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to add label {label} to issue #{issue_number}: {res.stderr}")

    def remove_issue_label(self, issue_number: int, label: str) -> None:
        cmd = ["gh", "issue", "edit", str(issue_number), "--remove-label", label]
        if self.repo:
            cmd.extend(["-R", self.repo])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to remove label {label} from issue #{issue_number}: {res.stderr}")


def provision_labels(gateway: LabelGateway) -> int:
    """Provision missing lifecycle and taxonomy labels idempotently."""
    existing = {l["name"]: l for l in gateway.list_labels()}
    target_labels = {**ALL_LIFECYCLE_LABELS, **TAXONOMY_LABELS}

    created = 0
    for name, meta in target_labels.items():
        if name not in existing:
            gateway.create_label(name, meta["color"], meta["description"])
            created += 1

    return created


STAGE_TO_LABEL = {
    Stage.PLAN: "stagemesh:ready",
    Stage.IMPLEMENT: "stagemesh:running",
    Stage.VALIDATE: "stagemesh:validating",
    Stage.REVIEW: "stagemesh:reviewing",
    Stage.INTEGRATE: "stagemesh:running",
    Stage.DONE: "stagemesh:done",
}


def sync_issue_lifecycle_label(gateway: LabelGateway, issue_number: int, stage: Stage) -> None:
    """Sync StageMesh internal stage onto GitHub issue label safely."""
    target_label = STAGE_TO_LABEL.get(stage, "stagemesh:running")

    # Remove stale mutually exclusive lifecycle labels
    for label_name in ALL_LIFECYCLE_LABELS:
        if label_name != target_label:
            try:
                gateway.remove_issue_label(issue_number, label_name)
            except Exception:
                pass  # Safe ignore if issue didn't have stale label

    # Add new active lifecycle label
    gateway.add_issue_label(issue_number, target_label)
