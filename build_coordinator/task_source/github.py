"""GitHub Issues adapter for StageMesh task discovery and state synchronization.

Discovers development tasks and objectives from GitHub issues and syncs
coordinator lifecycle state and review evidence back to GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

from build_coordinator.models import BuildObjective, BuildTask
from build_coordinator.objectives import create_objective
from build_coordinator.service import upsert_task
from build_coordinator.task_source.base import SyncResult, TaskSource
from build_coordinator.types import ObjectiveSpec, TaskSpec

logger = logging.getLogger(__name__)


class GitHubTaskSource(TaskSource):
    """Discovers and synchronizes tasks from GitHub issues."""

    def __init__(
        self,
        repo: str | None = None,
        *,
        labels: tuple[str, ...] = (),
        dry_run: bool = False,
        client: Any = None,
    ) -> None:
        self.repo = repo or os.getenv("BUILD_COORDINATOR_GITHUB_REPO")
        self.labels = labels
        self.dry_run = dry_run
        self._client = client  # For mocking/testing
        self._outbound_events: list[dict[str, Any]] = []

    def discover_tasks(self, session) -> list[SyncResult]:
        """Fetch open issues from the repository and ingest into the durable queue."""
        if not self.repo:
            return []
        issues = self._fetch_issues()
        results: list[SyncResult] = []
        for issue in issues:
            res = self._sync_issue(session, issue)
            if res is not None:
                results.append(res)
        return results

    def _fetch_issues(self) -> list[dict[str, Any]]:
        if self._client is not None:
            return self._client.list_issues(repo=self.repo, labels=self.labels)
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            "open",
            "--json",
            "number,title,body,labels,url,state",
            "--limit",
            "200",
        ]
        if self.labels:
            for label in self.labels:
                cmd.extend(["--label", label])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
            return json.loads(res.stdout)
        except Exception as exc:
            logger.warning("Failed to fetch GitHub issues from %s: %s", self.repo, exc)
            return []

    def _sync_issue(self, session, issue: dict[str, Any]) -> SyncResult | None:
        number = issue["number"]
        title = issue.get("title", f"Issue #{number}")
        body = issue.get("body", "")
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
        url = issue.get("url", f"https://github.com/{self.repo}/issues/{number}")

        # Check for explicit task_id in body e.g. <!-- task_id: ... -->
        task_id_match = re.search(r"<!--\s*task_id:\s*([A-Za-z0-9_-]+)\s*-->", body)
        if task_id_match:
            task_id = task_id_match.group(1)
        else:
            task_id = f"GH-{number}"

        is_objective = any("objective" in l.lower() for l in labels) or "## Objective" in body
        ac = self._parse_acceptance_criteria(body)
        deps = self._parse_dependencies(body)

        if is_objective:
            return self._sync_objective(session, task_id, title, body, ac, url)
        return self._sync_task(session, task_id, title, body, ac, deps, labels, url)

    def _sync_task(
        self,
        session,
        task_id: str,
        title: str,
        body: str,
        ac: list[str],
        deps: list[str],
        labels: list[str],
        url: str,
    ) -> SyncResult:
        existing = session.get(BuildTask, task_id)
        action = "UPDATED" if existing is not None else "CREATED"
        review_policy = "SELF" if any("review:self" in l.lower() for l in labels) else "INDEPENDENT"
        risk_level = "HIGH" if any("risk:high" in l.lower() for l in labels) else "MEDIUM"
        priority = self._parse_priority(labels, body)

        spec = TaskSpec(
            task_id=task_id,
            title=title,
            description=body[:2000],
            acceptance_criteria=ac or ["Satisfy all requirements stated in issue."],
            dependencies=deps,
            risk_level=risk_level,
            review_policy=review_policy,
        )
        task = upsert_task(session, spec)
        self._record_sync_event(session, task.task_id, priority, url, action)
        return SyncResult(
            task_id=task.task_id,
            title=task.title,
            action=action,
            source_ref=url,
            details=f"Synced from GitHub issue as {task.state} (priority: {priority})",
        )

    def _record_sync_event(
        self,
        session,
        task_id: str,
        priority: int,
        url: str,
        action: str,
    ) -> None:
        from sqlalchemy import select
        from build_coordinator.events import EventInput, record_event
        from build_coordinator.models import BuildTaskEvent
        from build_coordinator.project.backlog import SYNC_EVENT, _revision

        previous_events = session.scalars(
            select(BuildTaskEvent)
            .where(BuildTaskEvent.task_id == task_id)
            .where(BuildTaskEvent.event_type == SYNC_EVENT)
        ).all()
        last = max(previous_events, key=_revision, default=None)
        rev = (_revision(last) + 1) if last is not None else 1
        record_event(
            session,
            EventInput(
                task_id=task_id,
                event_type=SYNC_EVENT,
                actor="github-sync",
                event_data={
                    "revision": rev,
                    "priority": priority,
                    "source": url,
                    "action": action,
                },
            ),
        )

    @staticmethod
    def _parse_priority(labels: list[str], body: str) -> int:
        for label in labels:
            norm = label.strip().lower()
            if norm in ("priority:p0", "priority:0", "p0"):
                return 0
            if norm in ("priority:p1", "priority:1", "p1"):
                return 20
            if norm in ("priority:p2", "priority:2", "p2"):
                return 50
            if norm in ("priority:p3", "priority:3", "p3"):
                return 100
            m = re.match(r"^priority:\s*p?(\d+)$", norm)
            if m:
                level = int(m.group(1))
                if level == 0:
                    return 0
                elif level == 1:
                    return 20
                elif level == 2:
                    return 50
                return 100
        body_match = re.search(r"(?:<!--\s*priority:\s*(\d+)\s*-->|priority:\s*p?(\d+))", body, re.IGNORECASE)
        if body_match:
            val = body_match.group(1) or body_match.group(2)
            if val is not None:
                num = int(val)
                if num == 0:
                    return 0
                elif num == 1:
                    return 20
                elif num == 2:
                    return 50
                return num
        return 100

    def _sync_objective(
        self,
        session,
        objective_id: str,
        title: str,
        body: str,
        ac: list[str],
        url: str,
    ) -> SyncResult:
        existing = session.get(BuildObjective, objective_id)
        if existing is not None:
            return SyncResult(
                task_id=objective_id,
                title=title,
                action="SKIPPED",
                source_ref=url,
                details="Objective already exists in durable queue",
            )
        spec = ObjectiveSpec(
            objective_id=objective_id,
            goal=f"{title}: {body[:500]}",
            completion_criteria=tuple(ac) if ac else (),
        )
        obj = create_objective(session, spec)
        return SyncResult(
            task_id=obj.objective_id,
            title=title,
            action="CREATED",
            source_ref=url,
            details="Created objective from GitHub issue",
        )

    def _parse_acceptance_criteria(self, body: str) -> list[str]:
        ac: list[str] = []
        in_ac_section = False
        for line in body.splitlines():
            line_str = line.strip()
            if any(h in line_str.lower() for h in ("acceptance criteria", "required user journey", "validation")):
                in_ac_section = True
                continue
            if in_ac_section and line_str.startswith("###"):
                in_ac_section = False
                continue
            if in_ac_section:
                item_match = re.match(r"^(?:[-*]|\d+\.)\s+(.+)$", line_str)
                if item_match:
                    ac.append(item_match.group(1).strip())
        return ac

    def _parse_dependencies(self, body: str) -> list[str]:
        deps: list[str] = []
        if not body:
            return deps
        normalized = body.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
        normalized = normalized.replace("â€“", "-").replace("&ndash;", "-").replace("&mdash;", "-")

        # 1. Range patterns: "Run after issues #55-#61", "depends on issues #55-#61", "#55 to #61"
        range_patterns = [
            r"(?:run after|depends on|blocked by)\s+(?:issues?\s+)?#?(\d+)\s*(?:[\-]|\.\.|\bto\b|\bthrough\b)\s*#?(\d+)",
            r"(?:issues?\s+)#?(\d+)\s*(?:[\-]|\.\.|\bto\b|\bthrough\b)\s*#?(\d+)",
        ]
        for pattern in range_patterns:
            for m in re.finditer(pattern, normalized, re.IGNORECASE):
                start = int(m.group(1))
                end = int(m.group(2))
                if start <= end and (end - start) < 1000:
                    for num in range(start, end + 1):
                        dep_id = f"GH-{num}"
                        if dep_id not in deps:
                            deps.append(dep_id)

        # 2. Lists and single references: "depends on:", "blocked by:", "run after:"
        list_matches = re.finditer(r"(?:depends on|blocked by|run after)[:\s]+([^\r\n]+)", normalized, re.IGNORECASE)
        for dep_match in list_matches:
            raw_line = dep_match.group(1)
            # Truncate at sentence or markdown terminator so subsequent text is excluded
            clause = re.split(r"\.(?:\s|\*\*|$)", raw_line)[0]
            # Match issue numbers (#123)
            for num in re.findall(r"#(\d+)", clause):
                dep_id = f"GH-{num}"
                if dep_id not in deps:
                    deps.append(dep_id)
            # Match explicit task IDs (e.g. GH-100, SM-001, CAV-122-01)
            for tid in re.findall(r"\b([A-Za-z0-9_]+-\d+)\b", clause):
                if tid not in deps:
                    deps.append(tid)

        return deps

    def sync_outbound(
        self,
        session,
        task_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Propagate coordinator lifecycle state and evidence back to GitHub."""
        evidence = evidence or {}
        event_record = {
            "task_id": task_id,
            "state": state,
            "evidence": evidence,
        }
        self._outbound_events.append(event_record)

        if not self.repo or self.dry_run:
            return True

        num_match = re.search(r"(\d+)$", task_id)
        if not num_match:
            return False
        issue_number = num_match.group(1)

        comment_body = self._format_status_comment(task_id, state, evidence)

        if self._client is not None:
            self._client.add_comment(repo=self.repo, number=issue_number, body=comment_body)
            if state == "DONE":
                self._client.close_issue(repo=self.repo, number=issue_number)
            return True

        try:
            subprocess.run(
                ["gh", "issue", "comment", str(issue_number), "--repo", self.repo, "--body", comment_body],
                capture_output=True,
                check=False,
            )
            label = f"stagemesh:{state.lower()}"
            subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", self.repo, "--add-label", label],
                capture_output=True,
                check=False,
            )
            if state == "DONE":
                subprocess.run(
                    ["gh", "issue", "close", str(issue_number), "--repo", self.repo],
                    capture_output=True,
                    check=False,
                )
            return True
        except Exception as exc:
            logger.warning("Failed to sync outbound status to GitHub issue #%s: %s", issue_number, exc)
            return False

    def _format_status_comment(self, task_id: str, state: str, evidence: dict[str, Any]) -> str:
        lines = [
            f"### StageMesh Lifecycle Update: `{state}`",
            "",
            f"- **Task ID**: `{task_id}`",
            f"- **State**: `{state}`",
        ]
        if "worker_id" in evidence:
            lines.append(f"- **Worker**: `{evidence['worker_id']}`")
        if "claim_id" in evidence:
            lines.append(f"- **Claim**: `{evidence['claim_id']}`")
        if "feature_sha" in evidence:
            lines.append(f"- **Feature SHA**: `{evidence['feature_sha']}`")
        if "review_verdict" in evidence:
            lines.append(f"- **Review Verdict**: `{evidence['review_verdict']}`")
        if "summary" in evidence:
            lines.append(f"- **Summary**: {evidence['summary']}")
        return "\n".join(lines)
