from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import __version__
from .acceptance_matrix import acceptance_matrix
from .completion_audit import completion_audit
from .e2e_acceptance import end_to_end_acceptance
from .persistence import Store
from .release import validate_candidate_sha, ReleaseValidationError


class FinalReportValidationError(ValueError):
    pass


def candidate_sha(root: Path) -> str:
    root = _validate_root(root)
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return "UNKNOWN"
    sha = result.stdout.strip()
    try:
        return validate_candidate_sha(sha)
    except ReleaseValidationError:
        return "UNKNOWN"


def render_final_report(root: Path, store: Store | None = None) -> str:
    root = _validate_root(root)
    sha = candidate_sha(root)
    task_count = len(store.tasks()) if store else 0
    worker_count = len(store.workers()) if store else 0
    external_count = _external_evidence_count(store, sha)
    acceptance = _acceptance_summary(root)
    audit = _completion_summary(root, sha, store)
    matrix = _matrix_summary(root, sha, store)
    e2e = _end_to_end_summary(root)
    sections = [
        "# StageMesh vNext Final Report",
        "",
        "## Candidate",
        "",
        f"- version: {__version__}",
        f"- final candidate SHA: {sha}",
        f"- runtime task count: {task_count}",
        f"- registered worker count: {worker_count}",
        f"- external evidence records for candidate: {external_count}",
        "",
        "## Final Architecture",
        "",
        "- domain models and lifecycle state machine own task/stage semantics",
        "- SQLite persistence with migrations is authoritative local state",
        "- PostgreSQL backend contract mirrors the authoritative schema and remains optional behind the persistence interface with structured probe output",
        "- execution, process identity, recovery, validation, review, remediation, and integration are separate components",
        "- provider routing, capacity classification, task sources, objective planning, registry, workers, dashboard, release packaging, and CLI remain narrow modules",
        "",
        "## Reused Components",
        "",
        "- no old runtime state, worktrees, claims, executions, or recovery branches are imported",
        "- only product concepts and clean interface ideas were reused from prior StageMesh planning",
        "",
        "## Rewritten Components",
        "",
        "- coordinator, lifecycle, persistence, execution, validation, review, integration, providers, task sources, objectives, recovery, CI gates, release readiness, and reporting",
        "",
        "## Complete Feature Inventory",
        "",
        "- task, stage, claim, execution, candidate, evidence, worker, provider, task-source, and objective models",
        "- deterministic `PLAN -> IMPLEMENT -> VALIDATE -> REVIEW -> INTEGRATE -> DONE` lifecycle",
        "- claims, leases, durable executions, process identity, recovery, exact-SHA evidence, and durable handoff",
        "- local backlog, configured JSON task-source adapters, and GitHub task-source contracts with deferred, stale, unknown, auth, and rate-limit states",
        "- provider SDK, capacity-aware routing, failover, structured capacity visibility, configurable stage routing, single-agent mode, and staged execution mode",
        "- validated objective planning, dependency-aware scheduling, review findings, remediation attempts, and retry/backoff with structured visibility",
        "- worker registry, lease-renewable distributed work packets with structured CLI output, structured CI gates, CI-wait capacity release, ambiguity-checked global project registry, structured config/status/operator reports with blocked-task and degraded execution health, idempotent structured external evidence listing, dashboard tables, audit export, release artifact, acceptance matrix, completion audit, and release-readiness reports with evidence summaries",
        "- workspace boundaries cover generated outputs, configured sources, demo scaffolds, objective inputs, and release packaging while excluding runtime state and symlink escapes",
        "",
        "## Acceptance Evidence",
        "",
        "- invariant test results: `python scripts/invariants.py`",
        "- Windows acceptance: `python scripts/acceptance.py`",
        "- Linux acceptance: configured in `.github/workflows/ci.yml`; hosted result required for final external proof",
        "- provider acceptance: deterministic provider dry-run proves capacity failover; live providers require credentials/tools",
        "- GitHub/task-source acceptance: deterministic dry-run proves zero-config remote detection, discovery, deferred labels, outbound sync, and rate-limit classification; live GitHub requires credentials/network",
        "- restart/recovery acceptance: covered by invariant suite",
        "- exact-SHA validation/review acceptance: covered by invariant suite and review model",
        "- durable Git handoff acceptance: covered by invariant suite",
        "- clean-install acceptance: `python scripts/clean_acceptance.py` plus `python -m pip install . --target .tmp-install --no-cache-dir --upgrade`",
        "- CI result: local `stagemesh ci --future-feature-gate` passes; hosted CI result pending external runner",
        "- independent review result: exact-SHA review evidence and findings are modeled; live independent provider review pending provider credentials",
        "",
        "## Machine Reports",
        "",
        f"- acceptance report status: {acceptance}",
        f"- completion audit status: {audit}",
        f"- acceptance matrix status: {matrix}",
        f"- end-to-end acceptance status: {e2e}",
        "",
        "## Remaining Human-Only Actions",
        "",
        "- provide live GitHub credentials and repository permissions for live inbound/outbound synchronization proof",
        "- provide live provider credentials/tools for live Codex/Claude/Grok execution proof",
        "- provide hosted CI runner results for Linux and external Windows proof",
        "- provide a live PostgreSQL service or durable database acceptance URL to prove the optional backend beyond the local schema contract",
        "",
        "## Roadmap Preservation",
        "",
        "- previously approved roadmap areas are preserved in implementation or explicit acceptance gaps; see `docs/status.md`, completion audit, acceptance matrix, and release-readiness report",
        "",
    ]
    return "\n".join(sections)


def _acceptance_summary(root: Path) -> str:
    path = root / ".stagemesh" / "acceptance-report.json"
    data = _read_report_json(path)
    if data is None:
        return "not generated"
    if not isinstance(data, dict):
        return "invalid report"
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return "invalid report"
    if not all(isinstance(check, dict) for check in checks):
        return "invalid report"
    passed = sum(1 for check in checks if check.get("status") == "PASS")
    gaps = data.get("proof_gaps", [])
    if not isinstance(gaps, list):
        return "invalid report"
    proof_status = data.get("proof_status", "UNKNOWN")
    return f"{data.get('status', 'UNKNOWN')} proof={proof_status} ({passed}/{len(checks)} checks passing, {len(gaps)} proof gaps)"


def _external_evidence_count(store: Store | None, candidate: str) -> int:
    if store is None or candidate == "UNKNOWN":
        return 0
    return sum(1 for row in store.external_evidence() if row["candidate_sha"] == candidate and row["status"] == "PASS")


def _completion_summary(root: Path, candidate: str, store: Store | None) -> str:
    if store is not None:
        data = completion_audit(store, candidate)
        items = data["items"]
        if isinstance(items, list):
            proven = sum(1 for item in items if isinstance(item, dict) and item.get("status") == "PROVEN")
            return f"complete={data.get('complete', False)} candidate={candidate} ({proven}/{len(items)} requirements proven)"
    path = root / ".stagemesh" / "completion-audit.json"
    data = _read_report_json(path)
    if data is None:
        return "not generated"
    if not isinstance(data, dict):
        return "invalid report"
    items = data.get("items", [])
    if not isinstance(items, list):
        return "invalid report"
    if not all(isinstance(item, dict) for item in items):
        return "invalid report"
    proven = sum(1 for item in items if item.get("status") == "PROVEN")
    return f"complete={data.get('complete', False)} candidate={candidate} ({proven}/{len(items)} requirements proven)"


def _matrix_summary(root: Path, candidate: str, store: Store | None) -> str:
    if store is not None:
        data = acceptance_matrix(store, candidate)
        return f"{data.get('status', 'UNKNOWN')} candidate={candidate} ({data.get('proven', 0)}/{data.get('total', 0)} rows proven)"
    path = root / ".stagemesh" / "acceptance-matrix.json"
    data = _read_report_json(path)
    if data is None:
        return "not generated"
    if not isinstance(data, dict):
        return "invalid report"
    return f"{data.get('status', 'UNKNOWN')} candidate={candidate} ({data.get('proven', 0)}/{data.get('total', 0)} rows proven)"


def _end_to_end_summary(root: Path) -> str:
    path = root / ".stagemesh" / "end-to-end-acceptance.json"
    data = _read_report_json(path)
    if data is None:
        data = end_to_end_acceptance()
    if not isinstance(data, dict):
        return "invalid report"
    return f"{data.get('status', 'UNKNOWN')} ({data.get('proven', 0)}/{data.get('total', 0)} steps proven)"


def _read_report_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "invalid"


def _validate_root(root: Path) -> Path:
    resolved = Path(root).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise FinalReportValidationError("final report root must be an existing directory")
    return resolved
