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

from sqlalchemy import select

from build_coordinator.events import record_event
from build_coordinator.models import (
    BuildObjective,
    BuildObjectiveEvent,
    BuildTask,
    BuildTaskEvent,
)
from build_coordinator.objectives import create_objective
from build_coordinator.service import upsert_task
from build_coordinator.task_source.base import SyncResult, TaskSource
from build_coordinator.types import EventInput, ObjectiveSpec, TaskSpec

logger = logging.getLogger(__name__)


class GitHubTaskSource(TaskSource):
    """Discovers and synchronizes tasks from GitHub issues."""

    # Every stagemesh:* label this adapter may apply to a GitHub issue.
    LIFECYCLE_LABELS: tuple[str, ...] = ("stagemesh:done",)

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
        self._labels_ensured = False

    def ensure_labels(self, session) -> bool:
        """Ensure every stagemesh:* lifecycle label exists in the repository.

        Idempotent and safe to call every cycle: once labels are confirmed
        present in this process, subsequent calls are no-ops. Failures (auth,
        network, permissions) are recorded as durable outbound-sync evidence
        and never raised, so missing labels can never block local task
        execution -- provisioning is simply retried on the next sync.
        """
        if not self.repo or self._labels_ensured:
            return True
        if self.dry_run:
            self._labels_ensured = True
            return True
        try:
            existing = self._list_labels()
            for label in self.LIFECYCLE_LABELS:
                if label not in existing:
                    self._create_label(label)
            self._labels_ensured = True
            return True
        except Exception as exc:
            err = str(exc)
            logger.warning("Failed to provision GitHub lifecycle labels for %s: %s", self.repo, err)
            record_event(
                session,
                EventInput(
                    task_id=None,
                    event_type="task_source.label_provisioning_failed",
                    actor="github-sync",
                    event_data={"error": err, "repo": self.repo},
                ),
            )
            session.flush()
            return False

    def _list_labels(self) -> set[str]:
        if self._client is not None:
            if hasattr(self._client, "list_labels"):
                return set(self._client.list_labels(repo=self.repo))
            return set()
        cmd = ["gh", "label", "list", "--repo", self.repo, "--json", "name", "--limit", "200"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return {item["name"] for item in json.loads(res.stdout)}

    def _create_label(self, label: str) -> None:
        if self._client is not None:
            if hasattr(self._client, "create_label"):
                self._client.create_label(repo=self.repo, name=label)
            return
        subprocess.run(
            ["gh", "label", "create", label, "--repo", self.repo, "--color", "6f42c1", "--force"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    def discover_tasks(self, session) -> list[SyncResult]:
        """Fetch open issues from the repository and ingest into the durable queue."""
        if not self.repo:
            return []
        self.ensure_labels(session)
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
        except FileNotFoundError:
            raise RuntimeError("GitHub CLI ('gh') is not installed or not found on PATH")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            if "not logged in" in err.lower() or "authentication" in err.lower() or "auth login" in err.lower():
                raise RuntimeError(f"authentication unavailable for GitHub repository {self.repo}: {err}")
            raise RuntimeError(f"failed to fetch GitHub issues from {self.repo}: {err}")
        except Exception as exc:
            raise RuntimeError(f"failed to fetch GitHub issues from {self.repo}: {exc}")

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
            self._sync_objective(session, task_id, title, body, ac, url)

        return self._sync_task(
            session,
            task_id,
            title,
            body,
            ac,
            deps,
            labels,
            url,
            objective_id=task_id if is_objective else None,
        )

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
        objective_id: str | None = None,
    ) -> SyncResult:
        existing = session.get(BuildTask, task_id)
        review_policy = "SELF" if any("review:self" in l.lower() for l in labels) else "INDEPENDENT"
        risk_level = "HIGH" if any("risk:high" in l.lower() for l in labels) else "MEDIUM"
        priority = self._parse_priority(labels, body)
        criteria = ac or ["Satisfy all requirements stated in issue."]

        if existing is not None:
            unchanged = (
                existing.title == title
                and existing.description == body[:2000]
                and existing.dependencies == deps
                and existing.acceptance_criteria == criteria
                and existing.review_policy == review_policy
                and existing.risk_level == risk_level
            )
            action = "SKIPPED" if unchanged else "UPDATED"
        else:
            action = "CREATED"

        spec = TaskSpec(
            task_id=task_id,
            title=title,
            description=body[:2000],
            acceptance_criteria=criteria,
            dependencies=deps,
            risk_level=risk_level,
            review_policy=review_policy,
        )
        task = upsert_task(session, spec)
        if objective_id:
            task.objective_id = objective_id
        session.flush()
        if action != "SKIPPED":
            self._record_sync_event(session, task.task_id, priority, url, action)
        details = (
            f"in sync ({task.state})"
            if action == "SKIPPED"
            else f"Synced from GitHub issue as {task.state} (priority: {priority})"
        )
        return SyncResult(
            task_id=task.task_id,
            title=task.title,
            action=action,
            source_ref=url,
            details=details,
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
        if any("status:queued" in l.lower() for l in labels) or any("objective" in l.lower() for l in labels) or "## Objective" in body:
            if re.search(r"\b(?:primary|alpha|v1 internal alpha)\b", body, re.IGNORECASE):
                return 10
            return 20
        return 100

    def _sync_objective(
        self,
        session,
        objective_id: str,
        title: str,
        body: str,
        ac: list[str],
        url: str,
    ) -> BuildObjective:
        existing = session.get(BuildObjective, objective_id)
        if existing is not None:
            return existing
        obj = BuildObjective(
            objective_id=objective_id,
            goal=f"{title}: {body[:500]}",
            completion_criteria=list(ac) if ac else [],
            state="PLANNING",
        )
        session.add(obj)
        session.add(
            BuildObjectiveEvent(
                objective_id=objective_id,
                event_type="objective.synced_from_source",
                actor="github-sync",
                event_data={"source": url, "title": title},
            )
        )
        session.flush()
        return obj

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

    def _resolve_issue_number(self, session, entity_id: str, is_objective: bool = False) -> int | None:
        # 1. Exact GH-<digits> pattern
        m = re.match(r"^GH-(\d+)$", entity_id)
        if m:
            return int(m.group(1))

        # 2. Check sync events in database
        if not is_objective:
            events = session.scalars(
                select(BuildTaskEvent)
                .where(
                    BuildTaskEvent.task_id == entity_id,
                    BuildTaskEvent.actor == "github-sync",
                )
                .order_by(BuildTaskEvent.created_at.asc())
            ).all()
            for ev in events:
                src = (ev.event_data or {}).get("source", "")
                sm = re.search(r"/issues/(\d+)$", src)
                if sm:
                    return int(sm.group(1))
        else:
            obj_events = session.scalars(
                select(BuildObjectiveEvent)
                .where(
                    BuildObjectiveEvent.objective_id == entity_id,
                    BuildObjectiveEvent.actor == "github-sync",
                )
                .order_by(BuildObjectiveEvent.created_at.asc())
            ).all()
            for ev in obj_events:
                src = (ev.event_data or {}).get("source", "")
                sm = re.search(r"/issues/(\d+)$", src)
                if sm:
                    return int(sm.group(1))
            events = session.scalars(
                select(BuildTaskEvent)
                .where(
                    BuildTaskEvent.task_id == entity_id,
                    BuildTaskEvent.actor == "github-sync",
                )
                .order_by(BuildTaskEvent.created_at.asc())
            ).all()
            for ev in events:
                src = (ev.event_data or {}).get("source", "")
                sm = re.search(r"/issues/(\d+)$", src)
                if sm:
                    return int(sm.group(1))

        return None

    def _is_outbound_synced(self, session, entity_id: str, state: str, is_objective: bool = False) -> bool:
        if is_objective:
            events = session.scalars(
                select(BuildObjectiveEvent).where(
                    BuildObjectiveEvent.objective_id == entity_id,
                    BuildObjectiveEvent.event_type == "objective.outbound_synced",
                )
            ).all()
            return any((e.event_data or {}).get("state") == state for e in events)
        else:
            events = session.scalars(
                select(BuildTaskEvent).where(
                    BuildTaskEvent.task_id == entity_id,
                    BuildTaskEvent.event_type == "task.outbound_synced",
                )
            ).all()
            return any((e.event_data or {}).get("state") == state for e in events)

    def _record_outbound_synced(
        self,
        session,
        entity_id: str,
        issue_number: int,
        state: str,
        is_objective: bool = False,
    ) -> None:
        if is_objective:
            session.add(
                BuildObjectiveEvent(
                    objective_id=entity_id,
                    event_type="objective.outbound_synced",
                    actor="github-sync",
                    event_data={"state": state, "issue_number": issue_number},
                )
            )
        else:
            record_event(
                session,
                EventInput(
                    task_id=entity_id,
                    event_type="task.outbound_synced",
                    actor="github-sync",
                    event_data={"state": state, "issue_number": issue_number},
                ),
            )
        session.flush()

    def _record_outbound_failed(
        self,
        session,
        entity_id: str,
        issue_number: int | None,
        error: str,
        action: str,
        is_objective: bool = False,
    ) -> None:
        if is_objective:
            session.add(
                BuildObjectiveEvent(
                    objective_id=entity_id,
                    event_type="objective.outbound_sync_failed",
                    actor="github-sync",
                    event_data={"error": error, "issue_number": issue_number, "action": action},
                )
            )
        else:
            record_event(
                session,
                EventInput(
                    task_id=entity_id,
                    event_type="task.outbound_sync_failed",
                    actor="github-sync",
                    event_data={"error": error, "issue_number": issue_number, "action": action},
                ),
            )
        session.flush()

    def _execute_outbound(
        self,
        session,
        entity_id: str,
        issue_number: int,
        comment_body: str,
        label: str,
        should_close: bool,
        is_objective: bool = False,
    ) -> bool:
        if not self.repo or self.dry_run:
            self._record_outbound_synced(
                session,
                entity_id,
                issue_number,
                state="DONE" if should_close else label,
                is_objective=is_objective,
            )
            return True

        if self._client is not None:
            try:
                is_closed = hasattr(self._client, "closed") and str(issue_number) in [
                    str(c) for c in self._client.closed
                ]
                already_commented = False
                if hasattr(self._client, "comments"):
                    for c in self._client.comments:
                        if str(c.get("number")) == str(issue_number) and (
                            "Lifecycle Update" in c.get("body", "")
                            or "Objective Completed" in c.get("body", "")
                        ):
                            already_commented = True
                            break
                if not already_commented:
                    self._client.add_comment(repo=self.repo, number=str(issue_number), body=comment_body)

                if hasattr(self._client, "add_label"):
                    self._client.add_label(repo=self.repo, number=str(issue_number), label=label)

                if should_close and not is_closed:
                    self._client.close_issue(repo=self.repo, number=str(issue_number))

                self._record_outbound_synced(
                    session,
                    entity_id,
                    issue_number,
                    state="DONE" if should_close else label,
                    is_objective=is_objective,
                )
                return True
            except Exception as exc:
                err = str(exc)
                logger.warning("Failed to sync outbound to GitHub via client for #%s: %s", issue_number, err)
                self._record_outbound_failed(
                    session,
                    entity_id,
                    issue_number,
                    error=err,
                    action="client_sync",
                    is_objective=is_objective,
                )
                return False

        try:
            # 1. gh issue comment
            proc_comment = subprocess.run(
                ["gh", "issue", "comment", str(issue_number), "--repo", self.repo, "--body", comment_body],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc_comment.returncode != 0:
                err = (proc_comment.stderr or proc_comment.stdout or "gh issue comment failed").strip()
                logger.warning("Failed to comment on GitHub issue #%s: %s", issue_number, err)
                self._record_outbound_failed(
                    session,
                    entity_id,
                    issue_number,
                    error=err,
                    action="gh_comment",
                    is_objective=is_objective,
                )
                return False

            # 2. gh issue edit --add-label
            proc_label = subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", self.repo, "--add-label", label],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc_label.returncode != 0:
                err = (proc_label.stderr or proc_label.stdout or "gh issue edit --add-label failed").strip()
                logger.warning("Failed to add label to GitHub issue #%s: %s", issue_number, err)
                self._record_outbound_failed(
                    session,
                    entity_id,
                    issue_number,
                    error=err,
                    action="gh_label",
                    is_objective=is_objective,
                )
                return False

            # 3. gh issue close
            if should_close:
                proc_close = subprocess.run(
                    ["gh", "issue", "close", str(issue_number), "--repo", self.repo],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if proc_close.returncode != 0:
                    err = (proc_close.stderr or proc_close.stdout or "gh issue close failed").strip()
                    logger.warning("Failed to close GitHub issue #%s: %s", issue_number, err)
                    self._record_outbound_failed(
                        session,
                        entity_id,
                        issue_number,
                        error=err,
                        action="gh_close",
                        is_objective=is_objective,
                    )
                    return False

            self._record_outbound_synced(
                session,
                entity_id,
                issue_number,
                state="DONE" if should_close else label,
                is_objective=is_objective,
            )
            return True
        except Exception as exc:
            err = str(exc)
            logger.warning("Subprocess exception syncing outbound to GitHub issue #%s: %s", issue_number, err)
            self._record_outbound_failed(
                session,
                entity_id,
                issue_number,
                error=err,
                action="subprocess_exception",
                is_objective=is_objective,
            )
            return False

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

        # Objective Safety Distinction:
        # If task_id corresponds to a BuildObjective, do NOT close or label stagemesh:done
        # at the task level! The objective issue is only closed when BuildObjective reaches COMPLETED.
        if session.get(BuildObjective, task_id) is not None:
            return True

        # Check source identity: Only GitHub-originating tasks can update GitHub!
        issue_number = self._resolve_issue_number(session, task_id, is_objective=False)
        if issue_number is None:
            return True

        # Check idempotency: already synced for this state?
        if self._is_outbound_synced(session, task_id, state, is_objective=False):
            return True

        comment_body = self._format_status_comment(task_id, state, evidence)
        label = f"stagemesh:{state.lower()}"
        should_close = (state == "DONE")

        if not self.ensure_labels(session):
            self._record_outbound_failed(
                session,
                task_id,
                issue_number,
                error=f"GitHub lifecycle label provisioning failed for {self.repo}",
                action="label_provisioning",
                is_objective=False,
            )
            return False

        return self._execute_outbound(
            session,
            task_id,
            issue_number,
            comment_body=comment_body,
            label=label,
            should_close=should_close,
            is_objective=False,
        )

    def sync_objective_outbound(
        self,
        session,
        objective_id: str,
        state: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Propagate objective completion state and evidence back to GitHub."""
        evidence = evidence or {}
        event_record = {
            "objective_id": objective_id,
            "state": state,
            "evidence": evidence,
        }
        self._outbound_events.append(event_record)

        # Only sync when objective is actually COMPLETED
        obj = session.get(BuildObjective, objective_id)
        if obj is None or obj.state != "COMPLETED":
            return True

        # Resolve issue number
        issue_number = self._resolve_issue_number(session, objective_id, is_objective=True)
        if issue_number is None:
            return True

        # Idempotency check
        if self._is_outbound_synced(session, objective_id, state, is_objective=True):
            return True

        comment_body = self._format_objective_status_comment(objective_id, state, evidence)
        label = "stagemesh:done"
        should_close = True

        if not self.ensure_labels(session):
            self._record_outbound_failed(
                session,
                objective_id,
                issue_number,
                error=f"GitHub lifecycle label provisioning failed for {self.repo}",
                action="label_provisioning",
                is_objective=True,
            )
            return False

        return self._execute_outbound(
            session,
            objective_id,
            issue_number,
            comment_body=comment_body,
            label=label,
            should_close=should_close,
            is_objective=True,
        )

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

    def _format_objective_status_comment(self, objective_id: str, state: str, evidence: dict[str, Any]) -> str:
        lines = [
            f"### StageMesh Objective Completed: `{state}`",
            "",
            f"- **Objective ID**: `{objective_id}`",
            f"- **State**: `{state}`",
        ]
        if "goal" in evidence:
            lines.append(f"- **Goal**: {evidence['goal']}")
        if "completion_criteria" in evidence and evidence["completion_criteria"]:
            lines.append("- **Satisfied Criteria**:")
            for item in evidence["completion_criteria"]:
                lines.append(f"  - [x] {item}")
        return "\n".join(lines)
