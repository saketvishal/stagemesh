"""Safe, opt-in GitHub issue classification performed by ``stagemesh init``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .audit import record_audit
from .auto_plan import plannable
from .config import StageMeshConfig
from .contract_check import check_contract
from .contracts import task_contract_path
from .github import GitHubClient, GitHubResult, UrlLibGitHubTransport
from .persistence import Store

READY_LABEL = "stagemesh:ready"
TRIAGE_LABEL = "stagemesh:needs-triage"
LABEL_METADATA = {
    READY_LABEL: ("2DA44E", "StageMesh can plan and validate this issue"),
    TRIAGE_LABEL: ("D4C5F9", "Needs scope or validation details before StageMesh can run it"),
}


@dataclass(frozen=True)
class GitHubBootstrapResult:
    status: str
    scanned: int = 0
    ready_labeled: int = 0
    triage_labeled: int = 0
    failed: int = 0
    reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.ready_labeled + self.triage_labeled > 0

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "scanned": self.scanned,
            "ready_labeled": self.ready_labeled,
            "triage_labeled": self.triage_labeled,
            "failed": self.failed,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def classify_unlabeled_github_issues(
    store: Store,
    project: Path,
    config: StageMeshConfig,
) -> GitHubBootstrapResult:
    """Classify open issues with no StageMesh admission label; preserve every existing team label."""
    github = config.github
    if not github.auto_label_unlabeled:
        return GitHubBootstrapResult("disabled", reason="not enabled by project configuration")
    if not github.owner or not github.repo:
        return GitHubBootstrapResult("unavailable", reason="GitHub repository is not configured")

    candidates = _unclassified_open_tasks(store)
    if not candidates:
        return GitHubBootstrapResult("no_unclassified_issues")
    if not github.token:
        result = GitHubBootstrapResult(
            "write_authorization_required",
            scanned=len(candidates),
            reason="use an interactive terminal with an authenticated GitHub CLI, or set STAGEMESH_GITHUB_TOKEN with Issues write permission",
        )
        record_audit(
            store,
            "github.init_classification_deferred",
            {"repository": f"{github.owner}/{github.repo}", "unclassified_issues": len(candidates)},
        )
        return result

    client = GitHubClient(github.owner, github.repo, UrlLibGitHubTransport(github.token))
    ensured: dict[str, GitHubResult] = {}
    ready = 0
    triage = 0
    failed = 0
    for task_id in candidates:
        label, basis = _classification(store, project, task_id)
        if label not in ensured:
            color, description = LABEL_METADATA[label]
            try:
                ensured[label] = client.ensure_label(label, color, description)
            except OSError:
                ensured[label] = GitHubResult("UNKNOWN", {})
        if ensured[label].status != "OK":
            failed += 1
            _record_result(store, task_id, label, basis, "FAILED")
            continue
        try:
            labeled = client.add_issue_labels(task_id, (label,))
        except OSError:
            labeled = GitHubResult("UNKNOWN", {})
        if labeled.status != "OK":
            failed += 1
            _record_result(store, task_id, label, basis, "FAILED")
            continue
        if label == READY_LABEL:
            ready += 1
        else:
            triage += 1
        _record_result(store, task_id, label, basis, "LABELED")

    changed = ready + triage
    status = "labeled" if failed == 0 else ("partial" if changed else "failed")
    return GitHubBootstrapResult(status, len(candidates), ready, triage, failed)


def _unclassified_open_tasks(store: Store) -> tuple[str, ...]:
    """Open issues StageMesh can safely classify without replacing an existing workflow decision."""
    lifecycle_labels = {
        READY_LABEL,
        TRIAGE_LABEL,
        "stagemesh:blocked",
        "stagemesh:deferred",
        "status:blocked",
        "status:remediating",
    }
    rows = store.conn.execute(
        "SELECT t.id, sc.state FROM tasks t "
        "JOIN source_cache sc ON sc.source=t.source AND sc.source_id=t.source_id "
        "WHERE t.source='github' ORDER BY t.id"
    ).fetchall()
    found: list[str] = []
    for row in rows:
        try:
            state = json.loads(row["state"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or str(state.get("state", "")).upper() != "OPEN":
            continue
        if state.get("objective_root") is True:
            continue
        labels = state.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            continue
        if lifecycle_labels.isdisjoint(label.casefold() for label in labels):
            found.append(str(row["id"]))
    return tuple(found)


def _classification(store: Store, project: Path, task_id: str) -> tuple[str, str]:
    task = store.get_task(task_id)
    if task is not None:
        state = store.source_state(str(task["source"]), str(task["source_id"]))
        if state.get("validation_errors"):
            return TRIAGE_LABEL, "source_validation_failed"
    contract_path = task_contract_path(project, task_id)
    if contract_path is not None:
        report = check_contract(project, task_id, contract_path)
        return (READY_LABEL, "explicit_contract") if report.get("ok") is True else (TRIAGE_LABEL, "invalid_contract")
    reason = plannable(store, project, task_id)
    return (READY_LABEL, "deterministic_auto_plan") if reason is None else (TRIAGE_LABEL, "planning_information_missing")


def _record_result(store: Store, task_id: str, label: str, basis: str, outcome: str) -> None:
    record_audit(
        store,
        "github.init_issue_classified",
        {"source": "github", "source_id": task_id, "label": label, "basis": basis, "outcome": outcome},
    )
