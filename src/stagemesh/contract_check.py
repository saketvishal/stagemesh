"""Pre-flight contract authoring check: parseability, canonical size and planned gates, before a task is labelled ready."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import (
    ChangeContract,
    ContractError,
    _contract_payload,
    canonical_contract_json,
    contract_admission_problem,
    parse_contract,
    task_contract_path,
)
from .contract_feasibility import check_contract_feasibility
from .persistence import MAX_CANONICAL_CONTRACT_CHARS

TOP_SECTIONS = 5


def _section_sizes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical character cost of each top-level section, largest first."""
    sizes = [
        {"section": key, "chars": len(json.dumps(value, sort_keys=True, separators=(",", ":")))} for key, value in payload.items()
    ]
    return sorted(sizes, key=lambda item: (-item["chars"], item["section"]))


def contract_section_sizes(contract: ChangeContract) -> list[dict[str, Any]]:
    """Largest canonical sections of a parsed contract, for actionable oversize feedback."""
    return _section_sizes(_contract_payload(contract))


def check_contract_text(text: str, limit: int = MAX_CANONICAL_CONTRACT_CHARS, *, require_spec: bool = False) -> dict[str, Any]:
    """Fail-closed verdict for contract JSON text. `ok` is True only when it parses and fits the canonical limit."""
    report: dict[str, Any] = {"ok": False, "limit": limit, "size": None, "errors": [], "gates": [], "largest_sections": []}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        report["errors"].append(f"contract is not valid JSON: {exc}")
        return report
    try:
        contract = parse_contract(payload)
    except ContractError as exc:
        report["errors"].append(f"contract is invalid: {exc}")
        return report
    problem = contract_admission_problem(contract)
    if problem:
        report["errors"].append(problem)
    if not contract.explicit:
        report["errors"].append("change contract must be explicit")
    if require_spec and not contract.spec_refs:
        report["errors"].append("strict spec mode requires at least one spec_refs entry")
    size = len(canonical_contract_json(contract))
    report["size"] = size
    report["gates"] = [gate.name for gate in contract.gates]
    report["largest_sections"] = contract_section_sizes(contract)[:TOP_SECTIONS]
    if size > limit:
        top = ", ".join(f"{item['section']} ({item['chars']})" for item in report["largest_sections"][:3])
        report["errors"].append(
            f"contract is {size} characters canonicalized; the limit is {limit} (over by {size - limit}). Shorten the largest sections: {top}"
        )
    if not contract.gates:
        report["warnings"] = ["contract plans no gates (required_tests, lint, typecheck, dependency_checks)"]
    report["ok"] = not report["errors"]
    return report


def check_contract(
    project: Path,
    task_id: str | None = None,
    path: Path | None = None,
    limit: int = MAX_CANONICAL_CONTRACT_CHARS,
    *,
    require_spec: bool = False,
) -> dict[str, Any]:
    if path is None:
        if task_id is None:
            raise ValueError("a task id or a contract file is required")
        path = task_contract_path(project, task_id)
        if path is None:
            return {
                "ok": False,
                "limit": limit,
                "size": None,
                "errors": [f"no contract file found at .stagemesh/contracts/{task_id}.json"],
                "gates": [],
                "largest_sections": [],
                "path": None,
            }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "limit": limit,
            "size": None,
            "errors": [f"cannot read {path}: {exc}"],
            "gates": [],
            "largest_sections": [],
            "path": str(path),
        }
    report = check_contract_text(text, limit=limit, require_spec=require_spec)
    report["path"] = str(path)
    if report["ok"]:
        try:
            parsed = parse_contract(json.loads(text))
        except (ValueError, ContractError):
            parsed = None
        feasibility = None if parsed is None else check_contract_feasibility(project, parsed)
        if feasibility is not None:
            report["feasibility"] = feasibility.to_dict()
            if not feasibility.feasible:
                report["errors"].append(feasibility.message(task_id or path.stem, parsed))
                report["ok"] = False
    if require_spec and report["ok"]:
        try:
            contract = parse_contract(json.loads(text))
        except (ValueError, ContractError):
            return report
        missing = _missing_spec_refs(project, contract)
        if missing:
            report["errors"].extend(f"spec ref is missing or empty: {ref}" for ref in missing)
            report["ok"] = False
    return report


def _missing_spec_refs(project: Path, contract: ChangeContract) -> list[str]:
    root = project.resolve()
    missing: list[str] = []
    for ref in contract.spec_refs:
        try:
            target = (root / ref).resolve()
            if root not in target.parents or not target.is_file() or not target.read_text(encoding="utf-8", errors="replace").strip():
                missing.append(ref)
        except OSError:
            missing.append(ref)
    return missing


def format_report(report: dict[str, Any]) -> list[str]:
    lines = [f"{'OK' if report['ok'] else 'INVALID'}: {report.get('path') or 'contract'}"]
    if report["size"] is not None:
        lines.append(f"  canonical size: {report['size']} / {report['limit']} characters")
        for item in report["largest_sections"]:
            lines.append(f"    {item['section']}: {item['chars']}")
        lines.append(f"  planned gates: {', '.join(report['gates']) if report['gates'] else 'none'}")
    lines.extend(f"  error: {message}" for message in report["errors"])
    lines.extend(f"  warning: {message}" for message in report.get("warnings", []))
    return lines
