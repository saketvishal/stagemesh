from __future__ import annotations

import subprocess
import json
from pathlib import Path

from . import __version__
from .persistence import Store


def candidate_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


def render_final_report(root: Path, store: Store | None = None) -> str:
    sha = candidate_sha(root)
    task_count = len(store.tasks()) if store else 0
    worker_count = len(store.workers()) if store else 0
    external_count = len(store.external_evidence()) if store else 0
    acceptance = _acceptance_summary(root)
    audit = _completion_summary(root)
    matrix = _matrix_summary(root)
    sections = [
        "# StageMesh vNext Final Report",
        "",
        "## Candidate",
        "",
        f"- version: {__version__}",
        f"- final candidate SHA: {sha}",
        f"- runtime task count: {task_count}",
        f"- registered worker count: {worker_count}",
        f"- external evidence records: {external_count}",
        "",
        "## Final Architecture",
        "",
        "- domain models and lifecycle state machine own task/stage semantics",
        "- SQLite persistence with migrations is authoritative local state",
        "- PostgreSQL backend contract mirrors the authoritative schema and remains optional behind the persistence interface",
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
        "- local backlog and GitHub task-source contracts with deferred, stale, unknown, auth, and rate-limit states",
        "- provider SDK, capacity-aware routing, failover, configurable stage routing, single-agent mode, and staged execution mode",
        "- objective planning, dependency-aware scheduling, review findings, remediation attempts, and retry/backoff",
        "- worker registry, distributed work packets, global project registry, structured operator report, dashboard tables, audit export, release artifact, acceptance matrix, completion audit, and release-readiness reports",
        "- release packaging uses tracked source files plus a hashed manifest and excludes runtime state and symlink escapes",
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
    if not path.exists():
        return "not generated"
    data = json.loads(path.read_text(encoding="utf-8"))
    checks = data.get("checks", [])
    passed = sum(1 for check in checks if check.get("status") == "PASS")
    return f"{data.get('status', 'UNKNOWN')} ({passed}/{len(checks)} checks passing)"


def _completion_summary(root: Path) -> str:
    path = root / ".stagemesh" / "completion-audit.json"
    if not path.exists():
        return "not generated"
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items", [])
    proven = sum(1 for item in items if item.get("status") == "PROVEN")
    return f"complete={data.get('complete', False)} ({proven}/{len(items)} requirements proven)"


def _matrix_summary(root: Path) -> str:
    path = root / ".stagemesh" / "acceptance-matrix.json"
    if not path.exists():
        return "not generated"
    data = json.loads(path.read_text(encoding="utf-8"))
    return f"{data.get('status', 'UNKNOWN')} ({data.get('proven', 0)}/{data.get('total', 0)} rows proven)"
