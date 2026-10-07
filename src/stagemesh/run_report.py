"""Structured report of the most recent StageMesh run (`stagemesh report latest`).

Read-only: everything here is derived from the store and git; nothing is written except the optional output file.
A "run" is a task's most recent activity. Test counts are validation-gate counts (the gate commands StageMesh ran against the
candidate); raw test-runner output is not persisted, so per-test counts are not available.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contracts import changed_files
from .domain import EvidenceKind
from .git import GitError, GitWorkspace
from .persistence import Store
from .recovery import RecoveryRefusal, task_doctor
from .remediation import recorded_findings

REPORT_VERSION = 1
# A GitHub pull-request URL: scheme://host/owner/repo/pull/<digits>. Group 1 is the canonical URL; nothing after the number is captured.
_PR_BASE = r"(https?://[^/\s\"'<>]+/[^/\s\"'<>]+/[^/\s\"'<>]+/pull/\d+)"
# External evidence must be a PR link and nothing else: the canonical URL plus an optional /files, .patch/.diff, trailing slash, query or fragment.
_PR_EVIDENCE_URL = re.compile(_PR_BASE + r"(?:/files)?(?:\.patch|\.diff)?/?(?:[?#]\S*)?", re.ASCII)
# Free text (source-event payloads): the PR number must end at a non-word character, so trailing `)`, `.`, `,` are never captured.
_PR_TEXT_URL = re.compile(_PR_BASE + r"(?!\w)", re.ASCII)
# A gate whose name says it applies a known-failures policy (it fails only on NEW failures) is the recorded baseline-failure signal.
_KNOWN_FAILURE_GATE = re.compile(r"known[-_ ]?failures?|known[-_ ]?baseline", re.IGNORECASE)


def latest_task_id(store: Store) -> str | None:
    """The task with the most recent recorded activity (execution, evidence, or task update)."""
    row = store.conn.execute(
        """
        SELECT id FROM tasks ORDER BY MAX(
            updated_at,
            COALESCE((SELECT MAX(updated_at) FROM executions WHERE executions.task_id = tasks.id), 0),
            COALESCE((SELECT MAX(created_at) FROM evidence WHERE evidence.task_id = tasks.id), 0)
        ) DESC, rowid DESC LIMIT 1
        """
    ).fetchone()
    return str(row["id"]) if row is not None else None


def build_run_report(store: Store, project: Path, task_id: str | None = None) -> dict[str, Any]:
    task_id = task_id or latest_task_id(store)
    if task_id is None:
        raise RecoveryRefusal("no_runs", "no tasks recorded yet; nothing to report")
    doctor = task_doctor(store, project, task_id)  # raises RecoveryRefusal for an unknown task
    candidate = doctor["latest_candidate"]
    sha = str(candidate["sha"]) if candidate else None
    validation = _latest_payload(store, task_id, sha, EvidenceKind.VALIDATION)
    gates = [g for g in validation.get("gates", []) if isinstance(g, dict)]
    passed = sum(1 for g in gates if g.get("status") == "PASSED")
    findings = recorded_findings(store, task_id, sha) if sha else []
    return {
        "report_version": REPORT_VERSION,
        "task_id": task_id,
        "title": doctor["title"],
        "branch": _branch(project, sha),
        "commit_sha": sha,
        "verdict": _verdict(doctor, findings),
        "stage": doctor["stage"],
        "status": doctor["status"],
        "evidence": {key: (doctor[key] or {}).get("status") for key in ("latest_validation", "latest_review", "latest_integration")},
        "files_changed": _files_changed(project, sha, store.task_baseline(task_id), validation),
        "tests_run": [
            {"name": g.get("name"), "command": g.get("command"), "status": g.get("status"), "returncode": g.get("returncode")} for g in gates
        ],
        "pass_fail_counts": {"unit": "validation gates", "total": len(gates), "passed": passed, "failed": len(gates) - passed},
        "known_baseline_failures": [
            {"gate": g.get("name"), "status": g.get("status")} for g in gates if _KNOWN_FAILURE_GATE.search(str(g.get("name", "")))
        ],
        "review_findings": [{k: f[k] for k in ("id", "severity", "message", "status", "source")} for f in findings],
        "next_recommended_action": {"command": doctor["recommended_command"] or None, "reason": doctor["recommendation_reason"]},
        "pr_url": _pr_url(store, doctor, sha),
    }


def _latest_payload(store: Store, task_id: str, sha: str | None, kind: EvidenceKind) -> dict[str, Any]:
    if sha is None:
        return {}
    row = store.conn.execute(
        "SELECT payload FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, sha, kind),
    ).fetchone()
    try:
        value = json.loads(row["payload"]) if row is not None else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _verdict(doctor: dict[str, Any], findings: list[dict[str, object]]) -> str:
    if doctor["status"] == "DONE":
        return "PASSED"
    if doctor["status"] == "BLOCKED":
        return "BLOCKED"
    failed = any((doctor[key] or {}).get("status") == "FAILED" for key in ("latest_validation", "latest_review", "latest_integration"))
    return "FAILED" if failed or any(f["status"] == "OPEN" for f in findings) else "IN_PROGRESS"


def _branch(project: Path, sha: str | None) -> str | None:
    """The branch the candidate is on: the current branch if it contains the commit, else a local branch that does, else None.

    The checkout's branch is never reported for a commit it does not contain; a missing or invalid commit has no branch.
    """
    if not sha:
        return None
    git = GitWorkspace(project)
    try:
        if git.run("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            return None
        if git.run("merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode == 0:
            current = git.run("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
            if current:
                return current
        containing = git.run("for-each-ref", "--contains", sha, "--format=%(refname:short)", "refs/heads", check=False).stdout.split()
        return min(containing) if containing else None
    except (GitError, OSError):
        return None


def _files_changed(project: Path, sha: str | None, baseline: str | None, validation: dict[str, Any]) -> list[str]:
    recorded = validation.get("changed_files")
    if isinstance(recorded, list):
        return [str(p) for p in recorded]
    if sha and baseline:
        try:
            return list(changed_files(project, sha, baseline))
        except (GitError, OSError):
            return []
    return []


def _pr_url(store: Store, doctor: dict[str, Any], sha: str | None) -> str | None:
    """A pull-request URL recorded as external evidence for the candidate or in the task's source events; None when none is recorded."""
    for row in store.external_evidence():
        match = _PR_EVIDENCE_URL.fullmatch(str(row["url"]).strip()) if sha and row["candidate_sha"] == sha else None
        if match:
            return match.group(1)
    task = store.get_task(str(doctor["task_id"]))
    if task is not None and task["source_id"] is not None:
        rows = store.conn.execute(
            "SELECT payload FROM source_events WHERE source=? AND source_id=? ORDER BY created_at DESC, rowid DESC",
            (task["source"], task["source_id"]),
        )
        for row in rows:
            match = _PR_TEXT_URL.search(str(row["payload"]))
            if match:
                return match.group(1)
    return None


def format_run_report(report: dict[str, Any]) -> str:
    counts = report["pass_fail_counts"]
    lines = [
        f"# StageMesh run report: {report['task_id']}",
        "",
        f"- title: {report['title']}",
        f"- verdict: {report['verdict']} ({report['stage']}/{report['status']})",
        f"- branch: {report['branch'] or 'none'}",
        f"- commit SHA: {report['commit_sha'] or 'none'}",
        f"- PR URL: {report['pr_url'] or 'none'}",
        "",
        f"## Files changed ({len(report['files_changed'])})",
        "",
        *[f"- {path}" for path in report["files_changed"]],
        "",
        f"## Tests run ({counts['passed']} passed, {counts['failed']} failed, {counts['total']} {counts['unit']})",
        "",
        *[f"- [{t['status']}] {t['name']}" for t in report["tests_run"]],
        "",
        "## Known baseline failures",
        "",
        *([f"- {b['gate']} ({b['status']})" for b in report["known_baseline_failures"]] or ["- none recorded"]),
        "",
        "## Review findings",
        "",
        *([f"- [{f['severity']}/{f['status']}] {f['message']}" for f in report["review_findings"]] or ["- none"]),
        "",
        "## Next recommended action",
        "",
        f"- {report['next_recommended_action']['command'] or 'none'}: {report['next_recommended_action']['reason']}",
        "",
    ]
    return "\n".join(lines)
