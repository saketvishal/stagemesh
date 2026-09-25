"""Autonomous GitHub-driven engineering controller.

Coordinates:
1. Issue Ingestion: authorized GitHub issues -> coordinator objectives
2. Gate Ingestion: GitHub `/approve <gate_id>` comments -> gate resolution
3. Execution: runner cycles (planner, builder, reviewer, integration)
4. Git/PR Automation: pushing branches and creating PRs on GitHub
5. Gate Publishing: posting typed human gates to GitHub
6. State Sync: synchronizing meaningful state without heartbeat noise
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from build_coordinator.github.client import GitHubClient, GitHubIssue
from build_coordinator.github.gates import (
    poll_and_ingest_gate_approvals,
    publish_open_gates,
)
from build_coordinator.github.ingestion import ingest_github_issue
from build_coordinator.github.sync import sync_objective_status_to_github
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildRunnerExecution,
    BuildTask,
)
from build_coordinator.github.sanitizer import full_repo_slug
from build_coordinator.objectives import (
    list_objectives,
    objective_tasks,
    open_gates,
    resolve_task_repo,
)
from build_coordinator.runner import BuildRunner
from build_coordinator.runner.models import RunnerConfig

logger = logging.getLogger(__name__)


@dataclass
class GitHubControllerCycleResult:
    issues_ingested: list[str] = field(default_factory=list)
    gates_approved: list[str] = field(default_factory=list)
    gates_published: list[str] = field(default_factory=list)
    prs_created: list[str] = field(default_factory=list)
    statuses_synced: dict[str, str] = field(default_factory=dict)
    runner_result: Any = None


class GitHubAutonomousController:
    """End-to-end controller connecting GitHub issues to the autonomous BuildRunner."""

    def __init__(
        self,
        session_factory,
        runner_config: RunnerConfig | None = None,
        *,
        github_client: GitHubClient | None = None,
        default_repo: str = "saketvishal/Caventra",
        issue_label: str = "caventra:objective",
        executors: dict | None = None,
        git=None,
    ) -> None:
        self._session_factory = session_factory
        self._runner_config = runner_config or RunnerConfig.default()
        self._github = github_client or GitHubClient()
        self._default_repo = default_repo
        self._issue_label = issue_label
        self._runner = BuildRunner(
            self._session_factory,
            self._runner_config,
            executors=executors,
            git=git,
        )

    def run_once(self) -> GitHubControllerCycleResult:
        result = GitHubControllerCycleResult()

        with self._session_factory() as session:
            try:
                self._github.ensure_orchestration_labels(self._default_repo)
            except Exception as exc:
                logger.warning("failed during github label bootstrap: %s", exc)

            # 1. Ingest authorized GitHub issues
            try:
                open_issues = self._github.get_authorized_issues(
                    self._default_repo, label=self._issue_label, state="open"
                )
                for issue in open_issues:
                    obj, is_new = ingest_github_issue(session, self._github, self._default_repo, issue)
                    if is_new:
                        result.issues_ingested.append(obj.objective_id)
            except Exception as exc:
                logger.warning("failed during github issue ingestion: %s", exc)

            # 2. Check and ingest human gate approvals on GitHub
            objectives = list_objectives(session)
            for obj in objectives:
                issue_number = self._extract_issue_number(obj.objective_id)
                if issue_number is not None:
                    resolved = poll_and_ingest_gate_approvals(
                        session, self._github, self._default_repo, issue_number, obj.objective_id
                    )
                    for g in resolved:
                        result.gates_approved.append(g.gate_id)

        # 3. Run BuildRunner cycle (planner, builder, reviewer, integration)
        runner_cycle = self._runner.run_once()
        result.runner_result = runner_cycle

        with self._session_factory() as session:
            # 4. Automate branch push and PR creation for completed builder tasks
            self._ensure_prs_and_pushes(session, result)

            # 5. Publish any newly opened human gates to GitHub
            objectives = list_objectives(session)
            for obj in objectives:
                issue_number = self._extract_issue_number(obj.objective_id)
                if issue_number is not None:
                    published = publish_open_gates(
                        session, self._github, self._default_repo, issue_number, obj.objective_id
                    )
                    for g in published:
                        result.gates_published.append(g.gate_id)

            # 6. Synchronize objective states to GitHub
            for obj in objectives:
                issue_number = self._extract_issue_number(obj.objective_id)
                if issue_number is not None:
                    new_status = sync_objective_status_to_github(
                        session, self._github, self._default_repo, issue_number, obj
                    )
                    if new_status:
                        result.statuses_synced[obj.objective_id] = new_status

        return result

    def run_forever(self, *, interval_seconds: float = 5.0) -> None:
        """Continuously loop through the autonomous lifecycle."""
        while True:
            self.run_once()
            time.sleep(interval_seconds)

    def _extract_issue_number(self, objective_id: str) -> int | None:
        """Parse issue number from objective ID (e.g. GH-caventra-42 -> 42)."""
        match = re.search(r"GH-[A-Za-z0-9_-]+-(\d+)$", objective_id)
        if match:
            return int(match.group(1))
        return None

    def _ensure_prs_and_pushes(
        self, session: Session, result: GitHubControllerCycleResult
    ) -> None:
        """Push completed builder branches and create PRs on GitHub."""
        # Find tasks in REVIEW_READY, REVIEWING, or VALIDATING with an active branch
        tasks = session.scalars(
            select(BuildTask).where(
                BuildTask.state.in_(("VALIDATING", "REVIEW_READY", "REVIEWING", "DONE"))
            )
        ).all()

        for task in tasks:
            if not task.branch_name or not task.objective_id:
                continue

            issue_number = self._extract_issue_number(task.objective_id)
            if issue_number is None:
                continue

            # Check if PR already created
            existing_event = session.scalar(
                select(BuildObjectiveEvent)
                .where(BuildObjectiveEvent.objective_id == task.objective_id)
                .where(BuildObjectiveEvent.event_type == "github.pr_created")
                .where(BuildObjectiveEvent.actor == task.task_id)
            )
            if existing_event is not None:
                continue

            # Find execution with worktree
            execution = session.scalar(
                select(BuildRunnerExecution)
                .where(BuildRunnerExecution.task_id == task.task_id)
                .where(BuildRunnerExecution.role == "BUILDER")
                .where(BuildRunnerExecution.status == "SUCCEEDED")
                .order_by(BuildRunnerExecution.completed_at.desc())
            )
            if execution is None or not execution.worktree_path:
                continue

            worktree = Path(execution.worktree_path)
            branch = task.branch_name

            # Resolve which repository this task actually targets -- never
            # assume it's the controller's default_repo. A push or PR must
            # never land on a repo other than the one the objective/task
            # resolves to; if it can't be resolved, fail closed by skipping
            # rather than guessing.
            task_repo = resolve_task_repo(session, task)
            if task_repo is None:
                logger.warning(
                    "skipping push/PR for %s: could not resolve its target repository",
                    task.task_id,
                )
                continue
            target_repo = full_repo_slug(task_repo)

            # Push branch to remote -- push_branch itself refuses to push if
            # this worktree's remote doesn't actually match target_repo.
            try:
                self._github.push_branch(
                    worktree,
                    branch,
                    remote=self._runner_config.remote_name,
                    expected_repo=target_repo,
                )
            except Exception as exc:
                logger.warning("failed to push branch %s: %s", branch, exc)
                continue

            # Create Pull Request
            title = f"feat: {task.title}"
            body = (
                f"Automated PR created by Caventra Build Coordinator for Task `{task.task_id}`.\n\n"
                f"- **Objective**: `{task.objective_id}`\n"
                f"- **Resolves Issue**: #{issue_number}\n"
                f"- **Worktree**: `{execution.worktree_path}`\n\n"
                "Reviewer role will independently review this change before integration."
            )
            try:
                pr = self._github.create_pull_request(
                    target_repo,
                    head_branch=branch,
                    base_branch=self._runner_config.main_ref,
                    title=title,
                    body=body,
                )
                pr_url = pr.get("url") or pr.get("html_url", "")
                result.prs_created.append(f"{task.task_id}:{pr_url}")

                session.add(
                    BuildObjectiveEvent(
                        objective_id=task.objective_id,
                        event_type="github.pr_created",
                        actor=task.task_id,
                        event_data={"task_id": task.task_id, "branch": branch, "pr_url": pr_url},
                    )
                )
                session.commit()

                self._github.add_issue_comment(
                    target_repo,
                    issue_number,
                    f"🔀 **Pull Request Opened**: [{branch}]({pr_url}) for Task `{task.task_id}`.\n\n"
                    "Automated independent review is being dispatched.",
                )
            except Exception as exc:
                logger.warning("failed to create PR for %s: %s", task.task_id, exc)
