"""`stagemesh handoff export`: one redacted JSON file with enough state for a reviewer to pick the work up without copy-paste.

Read-only: it reads the store, git and the contract files and writes exactly one new file. It never advances a task, releases a claim,
touches queue admission or calls a provider. Nothing is exported verbatim: every string passes through `sanitize`, which redacts
secret-looking keys and values, credentials in URLs, the project's absolute path and known secret values from the environment, and
bounds the size of free text. Gate environments, gate output, prompts, canonical contract JSON and configuration are never read into
the package at all.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigValidationError, load_config
from .contracts import task_contract_path
from .domain import EvidenceKind, Stage, TaskStatus
from .git import GitError, GitWorkspace
from .observability import health
from .operator_actions import task_details
from .persistence import Store
from .process_identity import classify_process, process_identity
from .queue_run import dirty_paths
from .redaction import (
    SECRET_MARKERS,
    redact_command_secrets,
    redact_mapping,
    redact_url_credentials,
)
from .remediation import latest_candidate_findings
from .security import WorkspaceBoundary
from .timing import execution_timings

HANDOFF_SCHEMA = "stagemesh.handoff/1"
REDACTED = "***REDACTED***"

MAX_TEXT = 1000  # characters of any exported string
MAX_PATHS = 500  # changed / dirty paths listed
MAX_TASKS = 100  # full task entries; finished tasks are compacted first
MAX_ROWS = 50  # packets, retries, workers, audit events, executions

TOP_LEVEL_KEYS = (
    "schema",
    "generated_at",
    "stagemesh_version",
    "project",
    "git",
    "tasks",
    "queue_control",
    "active_executions",
    "latest_run",
    "tests",
    "changed_files",
    "contract_scope",
    "next_action",
    "redaction",
    "warnings",
)

# What a reader may rely on never being in the package.
OMITTED = (
    "gate environment variables",
    "gate stdout and stderr",
    "provider prompts and transcripts",
    "canonical contract JSON (digests only)",
    "stagemesh configuration and tokens",
    "source events and objective payloads",
    "absolute project path",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
    (
        re.compile(r"""(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization)[\w.-]*)(\s*[=:]\s*)(?!\*\*\*REDACTED)("[^"]*"|'[^']*'|\S+)"""),
        rf"\1\2{REDACTED}",
    ),
)


class HandoffError(ValueError):
    pass


def _env_secrets() -> list[str]:
    """Values of environment variables that look like credentials; replaced wherever they appear in exported text."""
    values = {
        value
        for name, value in os.environ.items()
        if len(value) >= 6 and any(marker in name.lower() for marker in SECRET_MARKERS)
    }
    return sorted(values, key=len, reverse=True)


class _Sanitizer:
    def __init__(self, project: Path, secrets: list[str]):
        self.secrets = secrets
        self.paths = [(p, "<project>") for p in sorted({str(project), project.as_posix()}, key=len, reverse=True)]
        home = Path.home()  # a username must not leak through interpreter or cache paths in gate commands
        self.paths += [(p, "~") for p in sorted({str(home), home.as_posix()}, key=len, reverse=True) if len(p) > 3]

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, REDACTED)
        for path, label in self.paths:
            value = value.replace(path, label)
        for pattern, replacement in _PATTERNS:
            value = pattern.sub(replacement, value)
        if len(value) > MAX_TEXT:
            value = value[:MAX_TEXT] + f"...[truncated {len(value) - MAX_TEXT} chars]"
        return value

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(k): self(v) for k, v in redact_mapping(value).items()}
        if isinstance(value, (list, tuple)):
            return [self(item) for item in value]
        return value


def sanitize(value: Any, project: Path, secrets: list[str] | None = None) -> Any:
    """Redact and bound a JSON-able structure the way the exported package is."""
    return _Sanitizer(Path(project).resolve(), _env_secrets() if secrets is None else secrets)(value)


def _loads(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _command_text(command: object) -> str | None:
    if not isinstance(command, (list, tuple)) or not command:
        return None
    return redact_command_secrets(shlex.join(str(part) for part in command))


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds") if epoch is not None else None


# --- git -----------------------------------------------------------------------------------------------------------------


def _git_out(git: GitWorkspace, *args: str) -> str | None:
    result = git.run(*args, check=False)
    text = result.stdout.strip()
    return text if result.returncode == 0 and text else None


def _git_section(project: Path, integration_ref: str | None, warnings: list[str]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    git = GitWorkspace(project)
    if _git_out(git, "rev-parse", "--git-dir") is None:
        warnings.append("project is not a git repository; git state omitted")
        return (
            {"is_repository": False, "branch": None, "sha": None, "subject": None, "remote_origin": None, "upstream": None, "integration": None, "dirty": False},
            {"base_ref": None, "merge_base": None, "committed": [], "uncommitted": [], "truncated": False, "by_task": {}},
            [],
        )
    branch = _git_out(git, "symbolic-ref", "-q", "--short", "HEAD")
    sha = _git_out(git, "rev-parse", "HEAD")
    try:
        dirty = dirty_paths(project)
    except (GitError, OSError) as exc:  # a failed status must not stop the export; the reader is told instead
        warnings.append(f"git status failed: {exc}")
        dirty = []
    ref = integration_ref or _git_out(git, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    integration: dict[str, Any] | None = None
    committed: list[str] = []
    merge_base = None
    if ref and sha and _git_out(git, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"):
        merge_base = _git_out(git, "merge-base", ref, "HEAD")
        counts = _git_out(git, "rev-list", "--left-right", "--count", f"{ref}...HEAD")
        behind, ahead = (int(n) for n in counts.split()) if counts else (None, None)
        integration = {"ref": ref, "sha": _git_out(git, "rev-parse", ref), "merge_base": merge_base, "ahead": ahead, "behind": behind}
        if merge_base:
            names = _git_out(git, "diff", "--name-only", "-M", merge_base, "HEAD")
            committed = sorted(names.splitlines()) if names else []
    elif ref:
        warnings.append(f"integration ref {ref} does not resolve; committed changed files omitted")
    remote = _git_out(git, "config", "--get", "remote.origin.url")
    section = {
        "is_repository": True,
        "branch": branch,
        "sha": sha,
        "subject": _git_out(git, "log", "-1", "--format=%s"),
        "remote_origin": redact_url_credentials(remote),
        "upstream": _git_out(git, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        "integration": integration,
        "dirty": bool(dirty),
    }
    changed = {
        "base_ref": ref,
        "merge_base": merge_base,
        "committed": committed[:MAX_PATHS],
        "uncommitted": dirty[:MAX_PATHS],
        "truncated": len(committed) > MAX_PATHS or len(dirty) > MAX_PATHS,
        "by_task": {},
    }
    return section, changed, dirty


# --- store ---------------------------------------------------------------------------------------------------------------


def _contract_scope(store: Store, project: Path, task_id: str, candidate_sha: str | None) -> dict[str, Any]:
    frozen = store.task_contract(task_id)
    binding = store.contract_binding(task_id, candidate_sha) if candidate_sha else None
    row, origin = (frozen, "frozen") if frozen is not None else (binding, "candidate_binding")
    path = task_contract_path(project, task_id)
    entry: dict[str, Any] = {
        "task_id": task_id,
        "source": origin if row is not None else None,
        "digest": row["digest"] if row is not None else None,
        "version": row["version"] if row is not None else None,
        "baseline_sha": store.task_baseline(task_id),
        "file": str(path.relative_to(project)).replace("\\", "/") if path is not None else None,
    }
    contract = _loads(row["canonical_json"]) if row is not None else {}
    for key in (
        "objective",
        "acceptance_criteria",
        "allowed_files",
        "forbidden_files",
        "protected_files",
        "exclusions",
        "invariants",
        "public_api",
        "exclusive_resources",
        "max_changed_files",
        "max_diff_lines",
        "validation_classification",
        "validation_risk",
    ):
        entry[key] = contract.get(key)
    gates: list[dict[str, Any]] = []
    for group in ("required_tests", "lint", "typecheck", "dependency_checks"):
        for gate in contract.get(group) or []:
            if isinstance(gate, dict):  # name and command only: a gate's env and cwd are not part of a handoff
                gates.append({"group": group, "name": gate.get("name"), "command": _command_text(gate.get("command"))})
    entry["gates"] = gates
    return entry


def _validation_entry(store: Store, task_id: str, candidate_sha: str | None) -> dict[str, Any] | None:
    if not candidate_sha:
        return None
    row = store.conn.execute(
        "SELECT status, payload, created_at FROM evidence WHERE task_id=? AND candidate_sha=? AND kind=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id, candidate_sha, EvidenceKind.VALIDATION),
    ).fetchone()
    if row is None:
        return None
    payload = _loads(row["payload"])
    checks = payload.get("validation_checks") if isinstance(payload.get("validation_checks"), dict) else {}
    findings = [f for f in payload.get("findings", []) if isinstance(f, dict)]
    return {
        "task_id": task_id,
        "candidate_sha": candidate_sha,
        "status": row["status"],
        "recorded_at": _iso(row["created_at"]),
        "contract_hash": payload.get("contract_hash"),
        "gates": [
            {"name": g.get("name"), "status": g.get("status"), "command": _command_text(g.get("command")), "returncode": g.get("returncode")}
            for g in payload.get("gates", [])
            if isinstance(g, dict)
        ],
        "checks": {key: list(checks.get(key) or []) for key in ("planned", "executed", "missing")},
        "findings": [{k: f.get(k) for k in ("code", "severity", "message", "path") if f.get(k) is not None} for f in findings[:MAX_ROWS]],
        "changed_files": [str(p) for p in payload.get("changed_files", [])[:MAX_PATHS]],
    }


def _task_entries(store: Store, project: Path, now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    rows = store.tasks()
    ordered = sorted(rows, key=lambda r: r["status"] == TaskStatus.DONE or r["stage"] == Stage.DONE)  # unfinished tasks first
    entries: list[dict[str, Any]] = []
    scopes: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    for row in ordered[:MAX_TASKS]:
        task_id = str(row["id"])
        details = task_details(store, task_id, now)
        candidate = details["latest_candidate"]
        sha = candidate["sha"] if candidate else None
        entry: dict[str, Any] = {
            "id": task_id,
            "title": row["title"],
            "stage": str(row["stage"]),
            "status": str(row["status"]),
            "source": row["source"],
            "source_id": row["source_id"],
            "depends_on_incomplete": store.incomplete_dependencies(task_id),
            "baseline_sha": store.task_baseline(task_id),
            "latest_candidate": candidate,
            "latest_validation": details["latest_validation"],
            "latest_review": details["latest_review"],
            "latest_integration": details["latest_integration"],
            "active_claim": details["active_claim"],
            "source_reason": details["source_reason"],
            "open_findings": [
                {"severity": f.get("severity"), "message": f.get("message"), "source": f.get("source")}
                for f in latest_candidate_findings(store, task_id)[:MAX_ROWS]
            ],
        }
        entries.append(entry)
        scopes.append(_contract_scope(store, project, task_id, sha))
        validation = _validation_entry(store, task_id, sha)
        if validation is not None:
            tests.append(validation)
    return entries, scopes, tests, len(rows) > MAX_TASKS


def _active_executions(store: Store, now: float) -> list[dict[str, Any]]:
    out = []
    for row in store.running_executions():
        state = classify_process(store.execution_process_identity(row["id"]), process_identity(row["pid"]))
        out.append(
            {
                "id": row["id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "pid": row["pid"],
                "candidate_sha": row["candidate_sha"],
                "claim_id": row["claim_id"],
                "started_at": _iso(row["started_at"]),
                "age_seconds": round(now - row["started_at"], 3),
                "process_state": state,
            }
        )
    return out[:MAX_ROWS]


def _queue_control(store: Store, tasks: list[Any], dirty: list[str], now: float) -> dict[str, Any]:
    report = health(store)
    by_status: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    ready: list[str] = []
    for row in tasks:
        by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + 1
        by_stage[str(row["stage"])] = by_stage.get(str(row["stage"]), 0) + 1
        if row["status"] == TaskStatus.OPEN and row["stage"] != Stage.DONE and not store.incomplete_dependencies(str(row["id"])):
            ready.append(str(row["id"]))
    packets = store.work_packets()
    packet_status: dict[str, int] = {}
    for packet in packets:
        packet_status[str(packet["status"])] = packet_status.get(str(packet["status"]), 0) + 1
    return {
        "backlog_state": report.backlog_state,
        "counts": {"by_status": by_status, "by_stage": by_stage},
        "ready_task_ids": ready[:MAX_ROWS],
        "blocked_task_ids": [str(r["id"]) for r in tasks if r["status"] == TaskStatus.BLOCKED][:MAX_ROWS],
        "claimed_task_ids": [str(r["id"]) for r in tasks if r["status"] == TaskStatus.CLAIMED][:MAX_ROWS],
        "health": {
            "ok": report.ok,
            "current_problems": list(report.current_problems),
            "running": report.running_count,
            "current_failed_executions": report.current_failed_execution_count,
            "unknown_executions": report.unknown_execution_count,
            "stale_executions": report.stale_execution_count,
        },
        "work_packets": {
            "by_status": packet_status,
            "recent": [
                {"id": p["id"], "task_id": p["task_id"], "stage": p["stage"], "status": p["status"], "worker_id": p["worker_id"], "candidate_sha": p["candidate_sha"]}
                for p in packets[:MAX_ROWS]
            ],
        },
        "retry_state": [
            {"key": r["key"], "attempts": r["attempts"], "next_attempt_at": _iso(r["next_attempt_at"]), "reason": r["reason"]}
            for r in store.retry_states()[:MAX_ROWS]
        ],
        "workers": [
            {"id": w["id"], "provider": w["provider"], "heartbeat_at": _iso(w["heartbeat_at"]), "lease_expired": w["lease_expires_at"] < now}
            for w in store.workers()[:MAX_ROWS]
        ],
        "dirty_working_tree": dirty[:MAX_PATHS],
    }


def _latest_run(store: Store, now: float) -> dict[str, Any]:
    executions: list[dict[str, Any]] = []
    for row in store.tasks():
        executions.extend(execution_timings(store, str(row["id"])))
    executions.sort(key=lambda r: (r["finished_at"] or r["started_at"], r["started_at"]), reverse=True)
    recent = [
        {
            **{k: r[k] for k in ("execution_id", "task_id", "stage", "attempt", "actor", "candidate_sha", "status", "result", "reason", "duration_seconds")},
            "started_at": _iso(r["started_at"]),
            "finished_at": _iso(r["finished_at"]),
        }
        for r in executions[:MAX_ROWS // 5]
    ]
    events = []
    for event in store.audit_events(10):
        payload = _loads(event["payload"])
        if len(json.dumps(payload, default=str)) > 2000:
            payload = {"truncated": True, "keys": sorted(payload)}
        events.append({"event_type": event["event_type"], "created_at": _iso(event["created_at"]), "payload": payload})
    failure = health(store).latest_implementation_failure
    return {
        "latest_execution": recent[0] if recent else None,
        "recent_executions": recent,
        "recent_audit_events": events,
        "latest_implementation_failure": failure,
        "external_evidence": [
            {"kind": e["kind"], "status": e["status"], "url": redact_url_credentials(e["url"]), "candidate_sha": e["candidate_sha"], "recorded_at": _iso(e["created_at"])}
            for e in store.external_evidence()[-10:]
        ],
    }


def _next_action(tasks: list[dict[str, Any]], queue: dict[str, Any], active: list[dict[str, Any]], dirty: list[str]) -> dict[str, Any]:
    def action(command: str | None, reason: str) -> dict[str, Any]:
        return {"command": command, "reason": reason, "advisory": True}

    dead = [e for e in active if e["process_state"] == "DEAD"]
    if dead:
        return action(f"stagemesh recover-stale --task {shlex.quote(str(dead[0]['task_id']))}", "a running execution's process is dead; release it before continuing")
    if any(e["process_state"] == "LIVE" for e in active):
        return action(None, "an execution is still running; wait for it to finish before acting")
    if any(e["process_state"] == "UNKNOWN" for e in active):
        return action(None, "a running execution has an unverifiable process; inspect it before releasing (recover-stale --release-unknown)")
    if queue["blocked_task_ids"]:
        return action(f"stagemesh task-doctor --task {shlex.quote(queue['blocked_task_ids'][0])}", "a task is BLOCKED; task-doctor names the cause and the next command")
    if dirty:
        return action(None, f"{len(dirty)} uncommitted path(s) in the project checkout; review or commit them before queueing more work")
    if queue["ready_task_ids"]:
        return action("stagemesh continue", f"{len(queue['ready_task_ids'])} task(s) are ready to run")
    if any(t["status"] == "CLAIMED" for t in tasks):
        return action(None, "a task is claimed but has no live execution; check `stagemesh health`")
    if not tasks:
        return action(None, "the backlog is empty")
    return action(None, "every task is DONE; review the branch diff")


# --- assembly ------------------------------------------------------------------------------------------------------------


def build_handoff(project: Path, *, integration_ref: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Assemble the sanitized handoff document. Read-only."""
    project = Path(project).resolve()
    now = time.time() if now is None else now
    warnings: list[str] = []
    secrets = _env_secrets()
    ref = integration_ref
    try:
        config = load_config(project)
        ref = ref or config.integration_ref
        if config.github.token:
            secrets.insert(0, config.github.token)
    except ConfigValidationError as exc:
        warnings.append(f"configuration unreadable ({exc}); integration ref not taken from config")

    git_section, changed, dirty = _git_section(project, ref, warnings)
    tasks: list[dict[str, Any]] = []
    scopes: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    queue: dict[str, Any] = {"backlog_state": "UNKNOWN", "ready_task_ids": [], "blocked_task_ids": [], "claimed_task_ids": [], "dirty_working_tree": dirty[:MAX_PATHS]}
    run: dict[str, Any] = {"latest_execution": None, "recent_executions": [], "recent_audit_events": [], "latest_implementation_failure": None, "external_evidence": []}
    db = project / ".stagemesh" / "stagemesh.sqlite3"
    if not db.exists():
        warnings.append("no StageMesh store at .stagemesh/stagemesh.sqlite3; task, queue and run state omitted")
    else:
        store = Store(db)  # opened as every read command does; the schema is not migrated by an export
        try:
            tasks, scopes, tests, truncated = _task_entries(store, project, now)
            if truncated:
                warnings.append(f"more than {MAX_TASKS} tasks; finished tasks beyond the first {MAX_TASKS} are omitted")
            active = _active_executions(store, now)
            queue = _queue_control(store, store.tasks(), dirty, now)
            run = _latest_run(store, now)
        except sqlite3.Error as exc:
            warnings.append(f"store could not be read ({exc}); run `stagemesh status` to migrate it, then export again")
        finally:
            store.close()
    changed["by_task"] = {t["task_id"]: t["changed_files"] for t in tests if t["changed_files"]}

    document = {
        "schema": HANDOFF_SCHEMA,
        "generated_at": _iso(now),
        "stagemesh_version": __version__,
        "project": {"name": project.name},
        "git": git_section,
        "tasks": tasks,
        "queue_control": queue,
        "active_executions": active,
        "latest_run": run,
        "tests": tests,
        "changed_files": changed,
        "contract_scope": scopes,
        "next_action": _next_action(tasks, queue, active, dirty),
        "redaction": {"applied": True, "max_text_chars": MAX_TEXT, "omitted": list(OMITTED)},
        "warnings": warnings,
    }
    return sanitize(document, project, secrets)


def default_out(project: Path, now: float | None = None) -> Path:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(project).resolve() / ".stagemesh" / "handoff" / f"{stamp}.json"


def write_handoff(document: dict[str, Any], project: Path, out: Path | None, *, force: bool = False) -> Path:
    """Write the package inside the project, never over an existing file unless forced, and never into .git."""
    project = Path(project).resolve()
    target = default_out(project) if out is None else Path(out)
    if not target.is_absolute():
        target = project / target
    try:
        target = WorkspaceBoundary(project).require_inside(target)
    except ValueError as exc:
        raise HandoffError(f"--out must be inside the project: {exc}") from exc
    if ".git" in target.relative_to(project).parts:
        raise HandoffError("--out must not be inside .git")
    if target.is_dir():
        raise HandoffError(f"--out is a directory: {target.name}")
    if target.exists() and not force:
        raise HandoffError(f"{target.name} already exists; choose another --out or pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target


# --- schema --------------------------------------------------------------------------------------------------------------

_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "project": ("name",),
    "git": ("is_repository", "branch", "sha", "subject", "remote_origin", "upstream", "integration", "dirty"),
    "latest_run": ("latest_execution", "recent_executions", "recent_audit_events", "latest_implementation_failure", "external_evidence"),
    "changed_files": ("base_ref", "merge_base", "committed", "uncommitted", "truncated", "by_task"),
    "next_action": ("command", "reason", "advisory"),
    "redaction": ("applied", "max_text_chars", "omitted"),
}
_QUEUE_KEYS = ("backlog_state", "ready_task_ids", "blocked_task_ids", "claimed_task_ids", "dirty_working_tree")
_TASK_KEYS = (
    "id", "title", "stage", "status", "source", "source_id", "depends_on_incomplete", "baseline_sha", "latest_candidate",
    "latest_validation", "latest_review", "latest_integration", "active_claim", "source_reason", "open_findings",
)  # fmt: skip
_TEST_KEYS = ("task_id", "candidate_sha", "status", "recorded_at", "contract_hash", "gates", "checks", "findings", "changed_files")
_SCOPE_KEYS = ("task_id", "source", "digest", "version", "baseline_sha", "file", "allowed_files", "forbidden_files", "gates")
_LISTS = ("tasks", "active_executions", "tests", "contract_scope", "warnings")


def validate_handoff(document: object) -> list[str]:
    """Structural problems with a handoff document; empty when it matches `HANDOFF_SCHEMA`."""
    if not isinstance(document, dict):
        return ["handoff must be a JSON object"]
    problems: list[str] = []
    if set(document) != set(TOP_LEVEL_KEYS):
        missing, extra = sorted(set(TOP_LEVEL_KEYS) - set(document)), sorted(set(document) - set(TOP_LEVEL_KEYS))
        problems.append(f"top-level keys differ: missing {missing}, unexpected {extra}")
    if document.get("schema") != HANDOFF_SCHEMA:
        problems.append(f"schema must be {HANDOFF_SCHEMA!r}")
    for name in ("generated_at", "stagemesh_version"):
        if not isinstance(document.get(name), str) or not document.get(name):
            problems.append(f"{name} must be a non-empty string")
    for name in _LISTS:
        if not isinstance(document.get(name), list):
            problems.append(f"{name} must be a list")
    for name, keys in {**_SECTION_KEYS, "queue_control": _QUEUE_KEYS}.items():
        section = document.get(name)
        if not isinstance(section, dict):
            problems.append(f"{name} must be an object")
            continue
        absent = [k for k in keys if k not in section]
        if absent:
            problems.append(f"{name} is missing {absent}")
    for name, keys in (("tasks", _TASK_KEYS), ("tests", _TEST_KEYS), ("contract_scope", _SCOPE_KEYS)):
        for index, item in enumerate(document.get(name) or []):
            absent = [k for k in keys if not isinstance(item, dict) or k not in item]
            if absent:
                problems.append(f"{name}[{index}] is missing {absent}")
    return problems
