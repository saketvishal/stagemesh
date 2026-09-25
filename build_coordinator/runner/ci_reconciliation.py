"""Work-conserving reconciliation for tasks awaiting external CI (#65).

A task that finishes integration and is pushed for a project with
`execution.external_ci.enabled: true` moves to `AWAITING_EXTERNAL_CI`
instead of `DONE`. That state is not in `CLAIMABLE_STATES`
(`build_coordinator.policy`), so it holds zero builder/reviewer/integration
capacity while pending -- other independent READY work keeps being
scheduled normally. This module is what periodically checks whether the
exact pushed SHA's external CI has resolved, and reconciles the task
accordingly.

See docs/design/CI_WORK_CONSERVING_SCHEDULING.md for the full design.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.events import record_event
from build_coordinator.models import BuildRunnerExecution, BuildTask
from build_coordinator.service import transition_task
from build_coordinator.types import EventInput

CIStatus = str  # "PENDING" | "SUCCESS" | "FAILURE" | "UNREACHABLE"


@dataclass(frozen=True)
class CIObservation:
    status: CIStatus
    sha: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


class CIClient(Protocol):
    """Injection point for tests; mirrors GitHubTaskSource's `client=` pattern."""

    def check_runs_for_sha(self, *, repo: str, sha: str) -> list[dict[str, Any]]: ...


class GhCIClient:
    """Real `gh` CLI backed CIClient. Queries by commit SHA (not by PR number),
    since StageMesh's push may land before any PR is opened for the branch."""

    def check_runs_for_sha(self, *, repo: str, sha: str) -> list[dict[str, Any]]:
        cmd = [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/check-runs",
            "--jq",
            ".check_runs",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        except FileNotFoundError:
            raise RuntimeError("GitHub CLI ('gh') is not installed or not found on PATH")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"failed to fetch check-runs for {repo}@{sha}: {err}")
        try:
            return json.loads(res.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"malformed check-runs response for {repo}@{sha}: {exc}")


def _summarize(sha: str, checks: list[dict[str, Any]]) -> CIObservation:
    if not checks:
        return CIObservation(status="PENDING", sha=sha, checks=[], detail="no check runs reported yet")
    statuses = {c.get("status") for c in checks}
    conclusions = {c.get("conclusion") for c in checks if c.get("status") == "completed"}
    if statuses - {"completed"}:
        return CIObservation(status="PENDING", sha=sha, checks=checks, detail="one or more checks still running")
    if conclusions <= {"success", "neutral", "skipped"}:
        return CIObservation(status="SUCCESS", sha=sha, checks=checks, detail="all required checks passed")
    return CIObservation(
        status="FAILURE",
        sha=sha,
        checks=checks,
        detail=f"checks concluded: {sorted(c for c in conclusions if c)}",
    )


def poll_external_ci(*, repo: str, sha: str, client: CIClient | None = None) -> CIObservation:
    client = client or GhCIClient()
    try:
        checks = client.check_runs_for_sha(repo=repo, sha=sha)
    except RuntimeError as exc:
        return CIObservation(status="UNREACHABLE", sha=sha, detail=str(exc))
    return _summarize(sha, checks)


def _last_integration_sha(session: Session, task_id: str) -> str | None:
    rows = session.scalars(
        select(BuildRunnerExecution)
        .where(BuildRunnerExecution.task_id == task_id)
        .where(BuildRunnerExecution.role == "INTEGRATION")
        .order_by(BuildRunnerExecution.launched_at.desc())
    ).all()
    for execution in rows:
        sha = (execution.result_data or {}).get("merge_commit_sha")
        if sha:
            return sha
    return None


def reconcile_awaiting_ci(
    session: Session,
    *,
    repo: str,
    client: CIClient | None = None,
    max_consecutive_errors: int = 5,
    actor: str = "runner",
) -> list[dict[str, Any]]:
    """Reconcile every task in AWAITING_EXTERNAL_CI against its recorded SHA.

    Returns one outcome dict per task examined. Never blocks: a PENDING or
    UNREACHABLE observation leaves the task exactly where it is so the
    caller's dispatch loop can move on to other READY work in the same
    cycle -- this function does not retry/poll internally.
    """
    tasks = session.scalars(select(BuildTask).where(BuildTask.state == "AWAITING_EXTERNAL_CI")).all()
    outcomes: list[dict[str, Any]] = []
    for task in tasks:
        sha = _last_integration_sha(session, task.task_id)
        if not sha:
            outcomes.append(
                {"task_id": task.task_id, "status": "UNREACHABLE", "detail": "no recorded integration SHA"}
            )
            continue
        observation = poll_external_ci(repo=repo, sha=sha, client=client)
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="runner.external_ci_observed",
                actor=actor,
                event_data={
                    "status": observation.status,
                    "sha": observation.sha,
                    "detail": observation.detail,
                    "checks": observation.checks,
                },
            ),
        )
        if observation.status == "SUCCESS":
            transition_task(session, task.task_id, "DONE", actor=actor, reason="external CI passed")
        elif observation.status == "FAILURE":
            transition_task(session, task.task_id, "REWORK_REQUIRED", actor=actor, reason="external CI failed")
        elif observation.status == "UNREACHABLE":
            error_count = int((task.waiting_input or {}).get("external_ci_error_count", 0)) + 1
            task.waiting_input = {**(task.waiting_input or {}), "external_ci_error_count": error_count}
            if error_count >= max_consecutive_errors:
                transition_task(
                    session,
                    task.task_id,
                    "BLOCKED",
                    actor=actor,
                    reason=f"external CI unreachable after {error_count} attempts: {observation.detail}",
                )
        # PENDING: leave the task in AWAITING_EXTERNAL_CI, no transition.
        outcomes.append({"task_id": task.task_id, "status": observation.status, "detail": observation.detail})
    return outcomes
