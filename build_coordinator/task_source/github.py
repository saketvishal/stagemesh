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
    TASK_STATES,
)
from build_coordinator.objectives import _ensure_planner_task, create_objective, get_planner_task
from build_coordinator.service import upsert_task, utcnow
from build_coordinator.task_source.base import (
    SOURCE_DEFERRED,
    SOURCE_ELIGIBLE,
    SyncResult,
    TaskSource,
    source_identity_metadata,
)
from build_coordinator.types import EventInput, OBJECTIVE_ROOT_COMPAT_REASON, ObjectiveSpec, TaskSpec

logger = logging.getLogger(__name__)


def _normalize_label(label: Any) -> str:
    return str(label or "").strip().lower()


def _resolve_issue_number_static(
    session,
    entity_id: str,
    is_objective: bool = False,
    repo: str | None = None,
) -> int | None:
    """Instance-independent counterpart of `GitHubTaskSource._resolve_issue_number`."""
    if not is_objective:
        task = session.get(BuildTask, entity_id)
        metadata = dict(task.definition_metadata or {}) if task is not None else {}
        has_source_identity = metadata.get("source_type") is not None or metadata.get("source_owner") is not None
        if has_source_identity:
            if metadata.get("source_type") != "github":
                return None
            if repo is not None and metadata.get("source_owner") != repo:
                return None
            source_ref = metadata.get("source_ref")
            if source_ref is not None and re.fullmatch(r"\d+", str(source_ref)):
                return int(source_ref)
            issue_number = metadata.get("source_issue_number")
            if issue_number is not None and re.fullmatch(r"\d+", str(issue_number)):
                return int(issue_number)

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
            pattern = (
                rf"^https://github\.com/{re.escape(repo)}/issues/(\d+)$"
                if repo is not None
                else r"/issues/(\d+)$"
            )
            sm = re.search(pattern, src)
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
            pattern = (
                rf"^https://github\.com/{re.escape(repo)}/issues/(\d+)$"
                if repo is not None
                else r"/issues/(\d+)$"
            )
            sm = re.search(pattern, src)
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
            pattern = (
                rf"^https://github\.com/{re.escape(repo)}/issues/(\d+)$"
                if repo is not None
                else r"/issues/(\d+)$"
            )
            sm = re.search(pattern, src)
            if sm:
                return int(sm.group(1))

    return None


def _is_outbound_synced_static(session, entity_id: str, state: str, is_objective: bool = False) -> bool:
    """Instance-independent counterpart of `GitHubTaskSource._is_outbound_synced`."""
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


def check_objective_fully_delivered(
    session, objective_id: str, *, repo: str | None = None, dry_run: bool = False
) -> bool:
    """Whether a GitHub-backed objective's internal COMPLETED state has actually
    been mirrored onto the source issue (label + close).

    Standalone counterpart of `GitHubTaskSource.is_objective_fully_delivered`,
    usable by callers (e.g. `github/sync.py`) that don't hold a `GitHubTaskSource`
    instance. #87: internal `COMPLETED` reflects implementation truth only -- it
    must never be read as "fully synchronized/delivered" on its own.
    """
    obj = session.get(BuildObjective, objective_id)
    if obj is None or obj.state != "COMPLETED":
        return False
    if not repo or dry_run:
        return True
    issue_number = _resolve_issue_number_static(session, objective_id, is_objective=True, repo=repo)
    if issue_number is None:
        return True
    return _is_outbound_synced_static(session, objective_id, "COMPLETED", is_objective=True)


def check_task_fully_delivered(
    session, task_id: str, *, repo: str | None = None, dry_run: bool = False
) -> bool:
    """Standalone counterpart of `GitHubTaskSource.is_task_fully_delivered`. See #87."""
    task = session.get(BuildTask, task_id)
    if task is None or task.state != "DONE":
        return False
    if not repo or dry_run:
        return True
    issue_number = _resolve_issue_number_static(session, task_id, is_objective=False, repo=repo)
    if issue_number is None:
        return True
    return _is_outbound_synced_static(session, task_id, "DONE", is_objective=False)


class GitHubTaskSource(TaskSource):
    """Discovers and synchronizes tasks from GitHub issues."""

    # Every stagemesh:* task lifecycle label this adapter may apply to a GitHub issue.
    LIFECYCLE_LABELS: tuple[str, ...] = tuple(f"stagemesh:{state.lower()}" for state in TASK_STATES)

    def __init__(
        self,
        repo: str | None = None,
        *,
        labels: tuple[str, ...] = (),
        dry_run: bool = False,
        client: Any = None,
        eligibility_include_labels: tuple[str, ...] = (),
        eligibility_exclude_labels: tuple[str, ...] = (),
    ) -> None:
        self.repo = repo or os.getenv("BUILD_COORDINATOR_GITHUB_REPO")
        self.labels = labels
        self.dry_run = dry_run
        self._client = client  # For mocking/testing
        self.eligibility_include_labels = tuple(_normalize_label(l) for l in eligibility_include_labels if str(l).strip())
        configured_excludes = tuple(_normalize_label(l) for l in eligibility_exclude_labels if str(l).strip())
        self.eligibility_exclude_labels = configured_excludes or ("stagemesh:deferred",)
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

    def _remove_stale_lifecycle_labels(self, issue_number: int, current_label: str) -> bool:
        stale_labels = tuple(label for label in self.LIFECYCLE_LABELS if label != current_label)
        if not stale_labels:
            return True

        if self._client is not None:
            if not hasattr(self._client, "remove_label"):
                return True
            present_labels = self._client_issue_label_names(issue_number)
            for label in stale_labels:
                if present_labels is not None and label not in present_labels:
                    continue
                try:
                    self._client.remove_label(repo=self.repo, number=str(issue_number), label=label)
                except Exception as exc:
                    if self._is_absent_label_error(exc):
                        continue
                    raise
            return True

        present_labels = self._issue_label_names(issue_number)
        for label in stale_labels:
            if label not in present_labels:
                continue
            proc_remove = subprocess.run(
                ["gh", "issue", "edit", str(issue_number), "--repo", self.repo, "--remove-label", label],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc_remove.returncode != 0:
                err = (proc_remove.stderr or proc_remove.stdout or "gh issue edit --remove-label failed").strip()
                raise RuntimeError(err)
        return True

    def _issue_label_names(self, issue_number: int) -> set[str]:
        proc = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", self.repo, "--json", "labels"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "gh issue view failed").strip()
            raise RuntimeError(err)
        data = json.loads(proc.stdout or "{}")
        return {label.get("name", "") for label in data.get("labels", []) if isinstance(label, dict)}

    def _client_issue_label_names(self, issue_number: int) -> set[str] | None:
        if self._client is None:
            return None
        if hasattr(self._client, "get_issue"):
            issue = self._client.get_issue(repo=self.repo, issue_number=issue_number)
            labels = getattr(issue, "labels", None)
            if labels is not None:
                return {label.get("name", "") if isinstance(label, dict) else str(label) for label in labels}
        if hasattr(self._client, "issues"):
            for issue in getattr(self._client, "issues"):
                if str(issue.get("number")) == str(issue_number):
                    return {
                        label.get("name", "") if isinstance(label, dict) else str(label)
                        for label in issue.get("labels", [])
                    }
            return None
        return None

    @staticmethod
    def _is_absent_label_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "not found" in message
            or "does not exist" in message
            or "missing" in message
            or "not applied" in message
        )

    def discover_tasks(self, session) -> list[SyncResult]:
        """Fetch open issues from the repository and ingest into the durable queue."""
        if not self.repo:
            return []
        self.ensure_labels(session)
        issues = self._fetch_issues()
        results: list[SyncResult] = []
        open_issue_numbers: set[int] = set()
        for issue in issues:
            try:
                open_issue_numbers.add(int(issue["number"]))
            except (KeyError, TypeError, ValueError):
                pass
            res = self._sync_issue(session, issue)
            if res is not None:
                results.append(res)
        results.extend(self._reconcile_source_states(session, open_issue_numbers))
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

    def _fetch_issue_by_number(self, issue_number: int) -> dict[str, Any] | None:
        if self._client is not None:
            if not hasattr(self._client, "get_issue"):
                return None
            issue = self._client.get_issue(repo=self.repo, issue_number=issue_number)
            if isinstance(issue, dict):
                return issue
            return {
                "number": getattr(issue, "number", issue_number),
                "state": getattr(issue, "state", ""),
                "url": getattr(issue, "html_url", ""),
            }
        cmd = [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            self.repo,
            "--json",
            "number,state,url",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            data = json.loads(res.stdout)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _reconcile_source_states(
        self,
        session,
        open_issue_numbers: set[int],
    ) -> list[SyncResult]:
        task_ids = session.scalars(
            select(BuildTaskEvent.task_id)
            .where(BuildTaskEvent.actor == "github-sync")
            .where(BuildTaskEvent.task_id.isnot(None))
            .distinct()
        ).all()
        results: list[SyncResult] = []
        for task_id in task_ids:
            if not task_id:
                continue
            task = session.get(BuildTask, task_id)
            if task is None:
                continue
            issue_number = self._resolve_issue_number(session, task_id, is_objective=False)
            if issue_number is None:
                continue
            metadata = dict(task.definition_metadata or {})
            previous_state = str(metadata.get("source_state") or "").upper()

            if issue_number in open_issue_numbers:
                source_state = "OPEN"
                source_url = metadata.get("source_url") or f"https://github.com/{self.repo}/issues/{issue_number}"
            else:
                source = self._fetch_issue_by_number(issue_number)
                if source is None:
                    continue
                source_state = str(source.get("state") or "").upper()
                source_url = source.get("url") or metadata.get("source_url") or f"https://github.com/{self.repo}/issues/{issue_number}"

            if source_state not in {"OPEN", "CLOSED"}:
                continue

            metadata.update(
                source_identity_metadata(
                    source_type="github",
                    source_owner=self.repo,
                    source_ref=str(issue_number),
                    source_url=source_url,
                    source_state=source_state,
                    legacy={"source_issue_number": issue_number},
                )
            )
            task.definition_metadata = metadata

            if source_state != previous_state:
                record_event(
                    session,
                    EventInput(
                        task_id=task.task_id,
                        event_type="task_source.source_state_changed",
                        actor="github-sync",
                        event_data={
                            "source": source_url,
                            "issue_number": issue_number,
                            "from_state": previous_state or None,
                            "to_state": source_state,
                        },
                    ),
                )
                results.append(
                    SyncResult(
                        task_id=task.task_id,
                        title=task.title,
                        action=f"SOURCE_{source_state}",
                        source_ref=str(source_url),
                        details=(
                            "GitHub source issue closed; local task preserved but suppressed"
                            if source_state == "CLOSED"
                            else "GitHub source issue reopened; source suppression cleared"
                        ),
                    )
                )
        objective_ids = session.scalars(
            select(BuildObjectiveEvent.objective_id)
            .where(BuildObjectiveEvent.actor == "github-sync")
            .where(BuildObjectiveEvent.objective_id.isnot(None))
            .distinct()
        ).all()
        for objective_id in objective_ids:
            if not objective_id:
                continue
            objective = session.get(BuildObjective, objective_id)
            if objective is None:
                continue
            issue_number = self._resolve_issue_number(session, objective_id, is_objective=True)
            if issue_number is None:
                continue
            if issue_number in open_issue_numbers:
                source_state = "OPEN"
                source_url = f"https://github.com/{self.repo}/issues/{issue_number}"
            else:
                source = self._fetch_issue_by_number(issue_number)
                if source is None:
                    continue
                source_state = str(source.get("state") or "").upper()
                source_url = source.get("url") or f"https://github.com/{self.repo}/issues/{issue_number}"
            if source_state not in {"OPEN", "CLOSED"}:
                continue
            previous_state = self._latest_objective_source_state(session, objective.objective_id)
            self._apply_objective_source_state(
                session,
                objective,
                issue_number=issue_number,
                source_url=source_url,
                source_state=source_state,
                previous_state=previous_state,
            )
            if source_state != previous_state:
                results.append(
                    SyncResult(
                        task_id=objective.objective_id,
                        title=objective.goal[:240],
                        action=f"SOURCE_{source_state}",
                        source_ref=str(source_url),
                        details=(
                            "GitHub objective issue closed; planner and objective-generated work suppressed"
                            if source_state == "CLOSED"
                            else "GitHub objective issue reopened; objective source suppression cleared"
                        ),
                    )
                )
        return results

    def _latest_objective_source_state(self, session, objective_id: str) -> str:
        planner = get_planner_task(session, objective_id)
        if planner is not None:
            metadata = planner.definition_metadata or {}
            if metadata.get("source_type") == "github" and metadata.get("source_state") is not None:
                return str(metadata.get("source_state") or "").upper()
        event = session.scalar(
            select(BuildObjectiveEvent)
            .where(BuildObjectiveEvent.objective_id == objective_id)
            .where(BuildObjectiveEvent.event_type == "objective.source_state_changed")
            .order_by(BuildObjectiveEvent.created_at.desc())
        )
        return str(((event.event_data if event is not None else {}) or {}).get("to_state") or "").upper()

    def _apply_objective_source_state(
        self,
        session,
        objective: BuildObjective,
        *,
        issue_number: int,
        source_url: str,
        source_state: str,
        previous_state: str,
        eligibility: str = SOURCE_ELIGIBLE,
        eligibility_reason: str | None = None,
    ) -> None:
        planner = get_planner_task(session, objective.objective_id)
        if planner is not None:
            self._apply_planner_source_metadata(
                planner,
                issue_number=issue_number,
                source_url=source_url,
                source_state=source_state,
                eligibility=eligibility,
                eligibility_reason=eligibility_reason,
            )
        if source_state == previous_state:
            return
        session.add(
            BuildObjectiveEvent(
                objective_id=objective.objective_id,
                event_type="objective.source_state_changed",
                actor="github-sync",
                event_data={
                    "source": source_url,
                    "issue_number": issue_number,
                    "from_state": previous_state or None,
                    "to_state": source_state,
                },
            )
        )

    def _sync_issue(self, session, issue: dict[str, Any]) -> SyncResult | None:
        number = issue["number"]
        title = issue.get("title", f"Issue #{number}")
        body = issue.get("body", "")
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
        url = issue.get("url", f"https://github.com/{self.repo}/issues/{number}")
        eligibility, eligibility_reason = self._source_eligibility_from_labels(labels)

        # Check for explicit task_id in body e.g. <!-- task_id: ... -->
        task_id_match = re.search(r"<!--\s*task_id:\s*([A-Za-z0-9_-]+)\s*-->", body)
        if task_id_match:
            task_id = task_id_match.group(1)
        else:
            task_id = f"GH-{number}"

        is_objective = self._is_objective_issue(labels, body)
        ac = self._parse_acceptance_criteria(body)
        deps = self._parse_dependencies(body)

        if is_objective:
            direct_execution = self._objective_direct_execution_enabled(labels, body)
            was_current = self._objective_sync_is_current(session, task_id, title, body, ac, deps)
            existed = session.get(BuildObjective, task_id) is not None
            previous_eligibility = self._objective_source_eligibility(session, task_id)
            objective = self._sync_objective(
                session,
                task_id,
                title,
                body,
                ac,
                deps,
                url,
                eligibility=eligibility,
                eligibility_reason=eligibility_reason,
                reconcile_historical_root=not direct_execution,
            )
            if not direct_execution:
                current_eligibility = self._objective_source_eligibility(session, task_id)
                if existed and previous_eligibility != current_eligibility:
                    action = "SOURCE_ELIGIBILITY_CHANGED"
                else:
                    action = "SKIPPED" if was_current else ("UPDATED" if existed else "CREATED")
                return SyncResult(
                    task_id=task_id,
                    title=title,
                    action=action,
                    source_ref=url,
                    details=(
                        "Synced from GitHub issue as authoritative objective; "
                        f"eligibility: {current_eligibility or eligibility}; no root implementation task created"
                    ),
                )
            deps = list(objective.dependencies)

        return self._sync_task(
            session,
            task_id,
            title,
            body,
            ac,
            deps,
            labels,
            url,
            eligibility=eligibility,
            eligibility_reason=eligibility_reason,
            objective_id=task_id if is_objective else None,
        )

    def _source_eligibility_from_labels(self, labels: list[str]) -> tuple[str, str | None]:
        normalized = {_normalize_label(label) for label in labels}
        excludes = set(self.eligibility_exclude_labels)
        matched_excludes = sorted(normalized & excludes)
        if matched_excludes:
            return SOURCE_DEFERRED, f"matched exclude label(s): {', '.join(matched_excludes)}"
        includes = set(self.eligibility_include_labels)
        if includes and not (normalized & includes):
            return SOURCE_DEFERRED, f"missing include label(s): {', '.join(sorted(includes))}"
        return SOURCE_ELIGIBLE, "eligible by source label policy"

    @staticmethod
    def _objective_direct_execution_enabled(labels: list[str], body: str) -> bool:
        explicit_labels = {"stagemesh:direct-execution", "objective:direct-execution"}
        if any(label.strip().lower() in explicit_labels for label in labels):
            return True
        return re.search(r"<!--\s*objective_direct_execution:\s*true\s*-->", body, re.IGNORECASE) is not None

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
        eligibility: str = SOURCE_ELIGIBLE,
        eligibility_reason: str | None = None,
        objective_id: str | None = None,
    ) -> SyncResult:
        existing = session.get(BuildTask, task_id)
        source_was_closed = bool(
            existing is not None
            and str((existing.definition_metadata or {}).get("source_state") or "").upper() == "CLOSED"
        )
        review_policy = self._review_policy_from_labels(labels)
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
        previous_eligibility = str(
            ((existing.definition_metadata or {}) if existing is not None else {}).get("source_eligibility")
            or "ELIGIBLE"
        ).upper()

        issue_match = re.search(r"/issues/(\d+)$", url)
        issue_number = int(issue_match.group(1)) if issue_match else None
        spec = TaskSpec(
            task_id=task_id,
            title=title,
            description=body[:2000],
            acceptance_criteria=criteria,
            dependencies=deps,
            risk_level=risk_level,
            review_policy=review_policy,
            definition_metadata={
                **(existing.definition_metadata if existing is not None else {}),
                **source_identity_metadata(
                    source_type="github",
                    source_owner=self.repo,
                    source_ref=str(issue_number if issue_number is not None else task_id),
                    source_url=url,
                    source_state="OPEN",
                    source_eligibility=eligibility,
                    source_eligibility_reason=eligibility_reason,
                    legacy={"source_issue_number": issue_number},
                ),
            },
        )
        task = upsert_task(session, spec)
        current_eligibility = str((task.definition_metadata or {}).get("source_eligibility") or "ELIGIBLE").upper()
        if existing is not None and action == "SKIPPED" and previous_eligibility != current_eligibility:
            action = "SOURCE_ELIGIBILITY_CHANGED"
        if source_was_closed:
            action = "SOURCE_OPEN"
            record_event(
                session,
                EventInput(
                    task_id=task.task_id,
                    event_type="task_source.source_state_changed",
                    actor="github-sync",
                    event_data={
                        "source": url,
                        "issue_number": (task.definition_metadata or {}).get("source_issue_number"),
                        "from_state": "CLOSED",
                        "to_state": "OPEN",
                    },
                ),
            )
        self._reconcile_reopened_task_from_open_issue(session, task, labels, url)
        if objective_id:
            task.objective_id = objective_id
            if task.reason_created == OBJECTIVE_ROOT_COMPAT_REASON:
                task.reason_created = "GITHUB_SOURCE"
                if task.state == "STALE":
                    task.state = "READY"
        session.flush()
        if action != "SKIPPED":
            self._record_sync_event(session, task.task_id, priority, url, action)
        details = (
            f"in sync ({task.state})"
            if action == "SKIPPED"
            else (
                "GitHub source issue reopened; source suppression cleared"
                if action == "SOURCE_OPEN"
                else f"Synced from GitHub issue as {task.state} (priority: {priority}; eligibility: {eligibility})"
            )
        )
        return SyncResult(
            task_id=task.task_id,
            title=task.title,
            action=action,
            source_ref=url,
            details=details,
        )

    def _reconcile_reopened_task_from_open_issue(
        self,
        session,
        task: BuildTask,
        labels: list[str],
        url: str,
    ) -> None:
        if task.state != "DONE":
            return
        from_state = task.state
        task.state = "READY"
        task.current_claim_id = None
        task.lease_expires_at = None
        task.last_heartbeat_at = None
        task.updated_at = utcnow()
        record_event(
            session,
            EventInput(
                task_id=task.task_id,
                event_type="task.reopened_from_source",
                actor="github-sync",
                from_state=from_state,
                to_state="READY",
                event_data={
                    "source": url,
                    "reason": "GitHub issue is open again; reopening StageMesh task for dispatch",
                    "had_stale_done_label": "stagemesh:done" in {label.strip().lower() for label in labels},
                },
            ),
        )

    @staticmethod
    def _review_policy_from_labels(labels: list[str]) -> str:
        normalized = {label.strip().lower() for label in labels}
        mapping = (
            ("review:none", "NONE"),
            ("review:self", "SELF"),
            ("review:independent-worker", "INDEPENDENT_WORKER"),
            ("review:independent_provider", "INDEPENDENT_PROVIDER"),
            ("review:independent-provider", "INDEPENDENT_PROVIDER"),
            ("review:two-reviewers", "TWO_REVIEWERS"),
            ("review:two-providers", "TWO_PROVIDERS"),
        )
        for label, policy in mapping:
            if label in normalized:
                return policy
        return "INDEPENDENT_WORKER"

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
        if (
            any("status:queued" in l.lower() for l in labels)
            or GitHubTaskSource._is_objective_issue(labels, body)
        ):
            if re.search(r"\b(?:primary|alpha|v1 internal alpha)\b", body, re.IGNORECASE):
                return 10
            return 20
        return 100

    @staticmethod
    def _is_objective_issue(labels: list[str], body: str) -> bool:
        normalized = {label.strip().lower() for label in labels}
        return bool(
            {"objective", "stagemesh:objective"} & normalized
            or re.search(r"^\s*#{1,6}\s*objective\b", body or "", re.IGNORECASE | re.MULTILINE)
        )

    def _sync_objective(
        self,
        session,
        objective_id: str,
        title: str,
        body: str,
        ac: list[str],
        deps: list[str],
        url: str,
        *,
        eligibility: str = SOURCE_ELIGIBLE,
        eligibility_reason: str | None = None,
        reconcile_historical_root: bool = True,
    ) -> BuildObjective:
        existing = session.get(BuildObjective, objective_id)
        if existing is not None:
            historical_task = session.get(BuildTask, objective_id)
            authoritative_deps = list(deps)
            if not authoritative_deps and historical_task is not None and historical_task.dependencies:
                authoritative_deps = list(historical_task.dependencies)
            existing.goal = f"{title}: {body[:500]}"
            # completion_criteria is authoritative child-task-id state owned by
            # the objective lifecycle (see _reassess_completion); GitHub's
            # parsed acceptance-criteria prose must never overwrite it here,
            # or a synced objective could never reach COMPLETED.
            existing.dependencies = authoritative_deps
            if reconcile_historical_root:
                self._reconcile_historical_objective_task(session, existing, authoritative_deps, url)
            planner = get_planner_task(session, objective_id)
            if planner is None and existing.state == "PLANNING":
                planner = _ensure_planner_task(session, existing)
            if planner is not None:
                planner.dependencies = authoritative_deps
                self._apply_planner_source_metadata(
                    planner,
                    issue_number=self._issue_number_from_url(url),
                    source_url=url,
                    source_state="OPEN",
                    eligibility=eligibility,
                    eligibility_reason=eligibility_reason,
                )
            return existing
        obj = create_objective(
            session,
            ObjectiveSpec(
                objective_id=objective_id,
                goal=f"{title}: {body[:500]}",
                # completion_criteria is authoritative child-task-id state;
                # it is populated by planning, not by GitHub AC prose.
                completion_criteria=(),
                dependencies=tuple(deps),
            ),
        )
        session.add(
            BuildObjectiveEvent(
                objective_id=objective_id,
                event_type="objective.synced_from_source",
                actor="github-sync",
                event_data={
                    "source": url,
                    "title": title,
                    "dependencies": list(deps),
                    "acceptance_criteria": list(ac),
                },
            )
        )
        issue_match = re.search(r"/issues/(\d+)$", url)
        issue_number = int(issue_match.group(1)) if issue_match else None
        if issue_number is not None:
            self._apply_objective_source_state(
                session,
                obj,
                issue_number=issue_number,
                source_url=url,
                source_state="OPEN",
                previous_state="",
                eligibility=eligibility,
                eligibility_reason=eligibility_reason,
            )
        planner = get_planner_task(session, objective_id)
        if planner is not None:
            planner.dependencies = list(deps)
        session.flush()
        return obj

    @staticmethod
    def _issue_number_from_url(url: str) -> int | None:
        issue_match = re.search(r"/issues/(\d+)$", url)
        return int(issue_match.group(1)) if issue_match else None

    def _apply_planner_source_metadata(
        self,
        planner: BuildTask,
        *,
        issue_number: int | None,
        source_url: str,
        source_state: str,
        eligibility: str = SOURCE_ELIGIBLE,
        eligibility_reason: str | None = None,
    ) -> None:
        metadata = dict(planner.definition_metadata or {})
        metadata.update(
            source_identity_metadata(
                source_type="github",
                source_owner=self.repo,
                source_ref=str(issue_number if issue_number is not None else planner.task_id),
                source_url=source_url,
                source_state=source_state,
                source_eligibility=eligibility,
                source_eligibility_reason=eligibility_reason,
                legacy={"source_issue_number": issue_number},
            )
        )
        planner.definition_metadata = metadata

    def _objective_source_eligibility(self, session, objective_id: str) -> str | None:
        planner = get_planner_task(session, objective_id)
        if planner is None:
            return None
        return str((planner.definition_metadata or {}).get("source_eligibility") or "ELIGIBLE").upper()

    def _objective_sync_is_current(
        self,
        session,
        objective_id: str,
        title: str,
        body: str,
        ac: list[str],
        deps: list[str],
    ) -> bool:
        obj = session.get(BuildObjective, objective_id)
        if obj is None:
            return False
        return obj.goal == f"{title}: {body[:500]}" and obj.dependencies == list(deps)

    def _reconcile_historical_objective_task(
        self,
        session,
        objective: BuildObjective,
        deps: list[str],
        url: str,
    ) -> None:
        task = session.get(BuildTask, objective.objective_id)
        if task is None:
            return
        if not objective.dependencies and task.dependencies:
            objective.dependencies = list(task.dependencies)
        if task.reason_created == OBJECTIVE_ROOT_COMPAT_REASON:
            task.dependencies = list(deps)
            return
        task.reason_created = OBJECTIVE_ROOT_COMPAT_REASON
        task.objective_id = objective.objective_id
        task.dependencies = list(deps)
        if task.state in {"READY", "RESUMABLE", "STALE"}:
            task.state = "STALE"
        session.add(
            BuildObjectiveEvent(
                objective_id=objective.objective_id,
                event_type="objective.historical_root_task_reconciled",
                actor="github-sync",
                event_data={
                    "task_id": task.task_id,
                    "task_state": task.state,
                    "dependencies": list(task.dependencies),
                    "source": url,
                },
            )
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
            # Match explicit task IDs (e.g. GH-100, SM-001, TASK-122-01)
            for tid in re.findall(r"\b([A-Za-z0-9_]+-\d+)\b", clause):
                if tid not in deps:
                    deps.append(tid)

        return deps

    def _resolve_issue_number(self, session, entity_id: str, is_objective: bool = False) -> int | None:
        if not is_objective:
            task = session.get(BuildTask, entity_id)
            metadata = dict(task.definition_metadata or {}) if task is not None else {}
            has_source_identity = metadata.get("source_type") is not None or metadata.get("source_owner") is not None
            if has_source_identity:
                if (
                    metadata.get("source_type") != "github"
                    or metadata.get("source_owner") != self.repo
                ):
                    return None
                source_ref = metadata.get("source_ref")
                if source_ref is not None and re.fullmatch(r"\d+", str(source_ref)):
                    return int(source_ref)
                issue_number = metadata.get("source_issue_number")
                if issue_number is not None and re.fullmatch(r"\d+", str(issue_number)):
                    return int(issue_number)

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
                sm = re.search(rf"^https://github\.com/{re.escape(str(self.repo or ''))}/issues/(\d+)$", src)
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
                sm = re.search(rf"^https://github\.com/{re.escape(str(self.repo or ''))}/issues/(\d+)$", src)
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
                sm = re.search(rf"^https://github\.com/{re.escape(str(self.repo or ''))}/issues/(\d+)$", src)
                if sm:
                    return int(sm.group(1))

        return None

    def is_objective_fully_delivered(self, session, objective_id: str) -> bool:
        """Whether a GitHub-backed objective's internal COMPLETED state has
        actually been mirrored onto the source issue (label + close).

        #87: internal `COMPLETED` reflects implementation truth only -- it
        must never be read as "fully synchronized/delivered" on its own.
        Callers that need to know whether the objective is *durably* done
        from GitHub's perspective (not just locally) must consult this
        instead of `BuildObjective.state`.
        """
        return check_objective_fully_delivered(session, objective_id, repo=self.repo, dry_run=self.dry_run)

    def is_task_fully_delivered(self, session, task_id: str) -> bool:
        """Task-level counterpart of `is_objective_fully_delivered`. See #87."""
        return check_task_fully_delivered(session, task_id, repo=self.repo, dry_run=self.dry_run)

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
        synced_state: str,
        is_objective: bool = False,
    ) -> bool:
        # #87: the recorded `synced_state` must match the value idempotency
        # checks look up (the caller's lifecycle `state`, e.g. "COMPLETED"
        # for an objective) -- not a value derived from `should_close`/
        # `label` here. A mismatch would make `_is_outbound_synced` never
        # recognize a prior success, causing side effects (comment/close) to
        # be re-attempted forever instead of becoming a stable no-op.
        if not self.repo or self.dry_run:
            self._record_outbound_synced(
                session,
                entity_id,
                issue_number,
                state=synced_state,
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

                self._remove_stale_lifecycle_labels(issue_number, label)
                if hasattr(self._client, "add_label"):
                    self._client.add_label(repo=self.repo, number=str(issue_number), label=label)

                if should_close and not is_closed:
                    self._client.close_issue(repo=self.repo, number=str(issue_number))

                self._record_outbound_synced(
                    session,
                    entity_id,
                    issue_number,
                    state=synced_state,
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
            try:
                self._remove_stale_lifecycle_labels(issue_number, label)
            except Exception as exc:
                err = str(exc)
                logger.warning("Failed to remove stale lifecycle labels from GitHub issue #%s: %s", issue_number, err)
                self._record_outbound_failed(
                    session,
                    entity_id,
                    issue_number,
                    error=err,
                    action="gh_remove_stale_labels",
                    is_objective=is_objective,
                )
                return False

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
                state=synced_state,
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
            synced_state=state,
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
            synced_state=state,
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
