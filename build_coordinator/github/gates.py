"""GitHub-managed human gate presentation and approval ingestion."""

from __future__ import annotations

import logging
import re
from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.github.client import GitHubClient
from build_coordinator.github.sanitizer import AUTHORIZED_OWNERS
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildObjectiveGate,
    BuildTask,
)
from build_coordinator.objectives import open_gates, resolve_gate

logger = logging.getLogger(__name__)

APPROVE_PATTERN = re.compile(r"^\s*/approve\s+([A-Za-z0-9_-]+)", re.MULTILINE | re.IGNORECASE)


def format_gate_comment(
    gate: BuildObjectiveGate,
    *,
    task: BuildTask | None = None,
    feature_sha: str | None = None,
) -> str:
    """Format a typed human gate comment adhering to Requirement 10."""
    action = gate.gate_type
    sha_line = f"- **Reviewed Feature SHA**: `{feature_sha}`\n" if feature_sha else "- **Reviewed Feature SHA**: `(none)`\n"
    return (
        f"### 🛑 STAGEMESH HUMAN GATE REQUIRED: `{gate.gate_id}`\n\n"
        f"- **Gate ID**: `{gate.gate_id}`\n"
        f"- **Objective**: `{gate.objective_id}`\n"
        f"- **Source Task**: `{gate.source_task_id or '(objective-level)'}`\n"
        f"- **Gate Type**: `{gate.gate_type}`\n"
        f"- **Requested Action**: `{action}`\n"
        f"- **Reason**: {gate.reason}\n"
        f"{sha_line}"
        f"- **Consequence of Approval**: Authorizes the build coordinator to proceed with `{gate.gate_type}` and resume execution automatically.\n\n"
        "---\n"
        f"**To approve, comment:**\n"
        f"```\n/approve {gate.gate_id}\n```\n"
        "*(Approval must be explicit and auditable. Casual comments are not interpreted as approval.)*"
    )


def publish_open_gates(
    session: Session,
    client: GitHubClient,
    repo: str,
    issue_number: int,
    objective_id: str,
) -> list[BuildObjectiveGate]:
    """Publish any unannounced open gates to GitHub."""
    gates = open_gates(session, objective_id)
    published: list[BuildObjectiveGate] = []

    for gate in gates:
        # Check if already published
        already_published = session.scalar(
            select(BuildObjectiveEvent)
            .where(BuildObjectiveEvent.objective_id == objective_id)
            .where(BuildObjectiveEvent.event_type == "github.gate_published")
            .where(BuildObjectiveEvent.actor == gate.gate_id)
        )
        if already_published is not None:
            continue

        task = session.get(BuildTask, gate.source_task_id) if gate.source_task_id else None
        comment_body = format_gate_comment(gate, task=task)
        client.add_issue_comment(repo, issue_number, comment_body)
        client.set_issue_status_label(repo, issue_number, "HUMAN_GATE")

        session.add(
            BuildObjectiveEvent(
                objective_id=objective_id,
                event_type="github.gate_published",
                actor=gate.gate_id,
                event_data={"gate_id": gate.gate_id, "gate_type": gate.gate_type},
            )
        )
        session.commit()
        published.append(gate)

    return published


def poll_and_ingest_gate_approvals(
    session: Session,
    client: GitHubClient,
    repo: str,
    issue_number: int,
    objective_id: str,
) -> list[BuildObjectiveGate]:
    """Check comments on GitHub issue for explicit `/approve <gate_id>` commands."""
    gates = {g.gate_id: g for g in open_gates(session, objective_id)}
    if not gates:
        return []

    comments = client.get_issue_comments(repo, issue_number)
    resolved: list[BuildObjectiveGate] = []

    for comment in comments:
        # Authorize commenter
        if comment.author.lower() not in AUTHORIZED_OWNERS:
            continue

        match = APPROVE_PATTERN.search(comment.body)
        if not match:
            continue

        gate_id = match.group(1).strip()
        if gate_id in gates:
            gate = gates[gate_id]
            resolved_gate = resolve_gate(
                session,
                gate_id,
                resolved_by=f"gh:{comment.author}",
                resolution_note=f"Approved via GitHub comment #{comment.id}",
            )
            session.commit()

            # Confirm on GitHub and restore active status label
            client.add_issue_comment(
                repo,
                issue_number,
                f"✅ **StageMesh Human Gate Approved**: Gate `{gate_id}` approved by @{comment.author}.\n\n"
                "Autonomous execution is resuming automatically without requiring further prompts.",
            )
            client.set_issue_status_label(repo, issue_number, "IN_PROGRESS")
            resolved.append(resolved_gate)
            del gates[gate_id]

    return resolved
