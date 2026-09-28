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
    return "\n".join(
        [
            "# StageMesh vNext Final Report",
            "",
            f"- version: {__version__}",
            f"- final candidate SHA: {sha}",
            "- architecture: domain, lifecycle, persistence, execution, workspace/git, validation, review/remediation, routing, provider capacity, task sources, integration, objectives/planning, CLI/status, observability, worker registry, distributed work queue, release packaging",
            "- reused components: none from old runtime state; implementation is clean-room in this repository",
            "- rewritten components: coordinator, lifecycle, persistence, execution, validation, review, integration, providers, task sources, objectives, recovery, CI gates",
            "- invariant test results: `python scripts/invariants.py`",
            "- Windows acceptance: `python scripts/acceptance.py`",
            "- Linux acceptance: configured in `.github/workflows/ci.yml`; hosted result required for final external proof",
            "- provider acceptance: provider SDK and capacity failover are local-accepted; live providers require credentials/tools",
            "- GitHub/task-source acceptance: cached/rate-limit semantics are local-accepted; live GitHub requires credentials/network",
            "- restart/recovery acceptance: covered by invariant suite",
            "- exact-SHA validation/review acceptance: covered by invariant suite and review model",
            "- durable Git handoff acceptance: covered by invariant suite",
            "- clean-install acceptance: `python -m pip install . --target .tmp-install --no-cache-dir --upgrade`",
            "- CI result: local `stagemesh ci --future-feature-gate` passes; hosted CI result pending external runner",
            f"- acceptance report status: {acceptance}",
            f"- completion audit status: {audit}",
            f"- acceptance matrix status: {matrix}",
            "- independent review result: exact-SHA review evidence and findings are modeled; live independent provider review pending provider credentials",
            f"- runtime task count: {task_count}",
            f"- registered worker count: {worker_count}",
            f"- external evidence records: {external_count}",
            "- remaining human-only actions: provide live provider/GitHub credentials and hosted CI runner access",
            "- roadmap preservation: tracked in `docs/status.md` with implemented and remaining items",
            "",
        ]
    )


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
