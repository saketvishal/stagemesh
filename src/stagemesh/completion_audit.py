from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .persistence import Store
from .security import WorkspaceBoundary


class CompletionAuditValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AuditItem:
    requirement: str
    status: str
    evidence: str


CORE_AUDIT_ITEMS = (
    AuditItem("install StageMesh", "PROVEN", "pip install target gate"),
    AuditItem("initialize a synthetic project", "PROVEN", "scripts/acceptance.py"),
    AuditItem("discover and execute dependent tasks", "PROVEN", "scripts/acceptance.py"),
    AuditItem("restart/recovery invariants", "PROVEN", "scripts/invariants.py"),
    AuditItem("exact-SHA validation and review model", "PROVEN", "scripts/invariants.py"),
    AuditItem("durable Git handoff", "PROVEN", "scripts/invariants.py"),
    AuditItem("provider capacity does not become implementation defect", "PROVEN", "scripts/invariants.py"),
    AuditItem("GitHub rate-limit separation", "PROVEN", "scripts/invariants.py and live harness"),
    AuditItem("Windows acceptance", "PROVEN", "local command output"),
    AuditItem("Linux acceptance", "MISSING_EXTERNAL_EVIDENCE", "hosted CI result unavailable locally"),
    AuditItem("live GitHub sync", "REQUIRES_CREDENTIALS", "scripts/live_acceptance.py reports NOT_CONFIGURED without credentials"),
    AuditItem("live provider execution", "REQUIRES_CREDENTIALS", "external account execution not proven"),
    AuditItem(
        "PostgreSQL storage",
        "INTERFACE_READY",
        "backend probe and schema contract exist; live PostgreSQL evidence not proven",
    ),
)


EVIDENCE_REQUIREMENTS = {
    "hosted-ci": "Linux acceptance",
    "live-github": "live GitHub sync",
    "live-provider": "live provider execution",
    "postgres": "PostgreSQL storage",
}


def completion_audit(store: Store | None = None) -> dict[str, object]:
    evidence_by_requirement = _external_evidence_by_requirement(store)
    items = []
    for item in CORE_AUDIT_ITEMS:
        if item.requirement in evidence_by_requirement:
            items.append(
                {
                    "requirement": item.requirement,
                    "status": "PROVEN",
                    "evidence": evidence_by_requirement[item.requirement],
                }
            )
        else:
            items.append(item.__dict__)
    return {"complete": all(item["status"] == "PROVEN" for item in items), "items": items}


def write_completion_audit(path: Path, store: Store | None = None, root: Path | None = None) -> None:
    path = _validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(completion_audit(store), indent=2, sort_keys=True), encoding="utf-8")


def _external_evidence_by_requirement(store: Store | None) -> dict[str, str]:
    if store is None:
        return {}
    evidence: dict[str, str] = {}
    for row in store.external_evidence():
        if row["status"] != "PASS":
            continue
        requirement = EVIDENCE_REQUIREMENTS.get(row["kind"])
        if requirement:
            evidence[requirement] = f"external evidence {row['id']}: {row['url']}"
    return evidence


def _validate_output(path: Path, root: Path | None) -> Path:
    output = Path(path).resolve()
    if root is not None:
        return WorkspaceBoundary(Path(root).resolve()).require_inside(output)
    if not output.name or output.is_dir():
        raise CompletionAuditValidationError("completion audit output must name a file")
    return output
