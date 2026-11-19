from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .external_evidence import external_evidence_records
from .persistence import Store
from .release import ReleaseValidationError, validate_candidate_sha
from .security import WorkspaceBoundary


class CompletionAuditValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AuditItem:
    requirement: str
    status: str
    evidence: str


CORE_AUDIT_ITEMS = (
    AuditItem("install StageMesh", "COVERED_NOT_EXECUTED", "development journey step 1"),
    AuditItem("initialize a synthetic project", "COVERED_NOT_EXECUTED", "development journey step 2"),
    AuditItem("discover and execute dependent tasks", "COVERED_NOT_EXECUTED", "development journey steps 3-14"),
    AuditItem("restart/recovery invariants", "COVERED_NOT_EXECUTED", "development journey steps 12-16"),
    AuditItem("exact-SHA validation and review model", "COVERED_NOT_EXECUTED", "development journey steps 10-11"),
    AuditItem("machine-enforced change contracts", "COVERED_NOT_EXECUTED", "development journey steps 9-10"),
    AuditItem("isolated per-task worktree execution", "COVERED_NOT_EXECUTED", "development journey step 9"),
    AuditItem("DONE requires exact-SHA validation, review, and integration evidence", "COVERED_NOT_EXECUTED", "development journey steps 10-14"),
    AuditItem("durable Git handoff", "COVERED_NOT_EXECUTED", "development journey steps 14-17"),
    AuditItem("provider capacity does not become implementation defect", "COVERED_NOT_EXECUTED", "component coverage; executable journey pending"),
    AuditItem("GitHub rate-limit separation", "COVERED_NOT_EXECUTED", "component coverage; executable journey pending"),
    AuditItem("Windows acceptance", "COVERED_NOT_EXECUTED", "current-candidate journey receipt unavailable"),
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


def completion_audit(store: Store | None = None, candidate_sha: str | None = None) -> dict[str, object]:
    evidence_by_requirement = _external_evidence_by_requirement(store, candidate_sha)
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


def write_completion_audit(
    path: Path, store: Store | None = None, root: Path | None = None, candidate_sha: str | None = None
) -> None:
    path = _validate_output(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(completion_audit(store, candidate_sha), indent=2, sort_keys=True), encoding="utf-8")


def _external_evidence_by_requirement(store: Store | None, candidate_sha: str | None) -> dict[str, str]:
    if store is None:
        return {}
    expected_sha = _validate_optional_sha(candidate_sha)
    evidence: dict[str, str] = {}
    for row in external_evidence_records(store):
        if row.status != "PASS":
            continue
        if expected_sha is not None and row.candidate_sha != expected_sha:
            continue
        requirement = EVIDENCE_REQUIREMENTS.get(row.kind)
        if requirement:
            evidence[requirement] = f"external evidence {row.id}: {row.url}"
    return evidence


def _validate_optional_sha(candidate_sha: str | None) -> str | None:
    if candidate_sha is None or candidate_sha == "UNKNOWN":
        return None
    try:
        return validate_candidate_sha(candidate_sha)
    except ReleaseValidationError as exc:
        raise CompletionAuditValidationError(str(exc)) from exc


def _validate_output(path: Path, root: Path | None) -> Path:
    output = Path(path).resolve()
    if root is not None:
        return WorkspaceBoundary(Path(root).resolve()).require_inside(output)
    if not output.name or output.is_dir():
        raise CompletionAuditValidationError("completion audit output must name a file")
    return output
