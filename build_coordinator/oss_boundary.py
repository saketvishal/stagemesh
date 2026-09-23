"""Machine-checkable IP-boundary scanner for the Build Coordinator.

Scans the coordinator source tree for known private-IP patterns (product-specific
env vars, paths, terminology) and fails on any undocumented occurrence.

Scanning is AST-based (string literals, attribute names, getenv keys) with
path-scoped rules so generic words like "coordinator" do not fail the check.

For standalone OSS use: DOCUMENTED_EXCEPTIONS should be empty. Any entry here
represents tracked coupling that has not yet been fully generalized.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


COORDINATOR_ROOT = Path(__file__).resolve().parent

# Temporary, documented exceptions. Each entry is (relative posix path, rule_id).
# Remove an exception only when the coupling is actually extracted.
DOCUMENTED_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()
# No documented exceptions: all private coupling has been extracted from the OSS package.


@dataclass(frozen=True)
class BoundaryFinding:
    path: str
    rule_id: str
    message: str
    line: int
    exception: bool = False


def _rel(path: Path) -> str:
    return path.relative_to(COORDINATOR_ROOT).as_posix()


def _string_literals(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def scan_coordinator_tree(root: Path | None = None) -> list[BoundaryFinding]:
    root = root or COORDINATOR_ROOT
    findings: list[BoundaryFinding] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".py", ".cmd", ".ps1"} and path.name not in {
            "caventra-build",
            "build-coordinator",
        }:
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if "tests" in path.parts:
            continue
        if path.name == "oss_boundary.py":
            continue
        text = path.read_text(encoding="utf-8")
        rel = _rel(path)
        findings.extend(_scan_text(rel, text, path.suffix == ".py"))
    return findings


def _scan_text(rel: str, text: str, is_python: bool) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    if is_python:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        literals = _string_literals(tree) if tree is not None else []
        getenv_keys = _getenv_keys(tree) if tree is not None else []
    else:
        literals = [(i + 1, line) for i, line in enumerate(text.splitlines())]
        getenv_keys = []

    for lineno, value in getenv_keys:
        if value.startswith("CAVENTRA_"):
            findings.append(
                BoundaryFinding(rel, "legacy_caventra_env", f"CAVENTRA_* name {value!r}", lineno)
            )
    for lineno, value in literals:
        if "~/.caventra" in value.replace("\\", "/") or value.endswith(".caventra") or "/.caventra/" in value.replace("\\", "/"):
            findings.append(
                BoundaryFinding(rel, "legacy_caventra_home", f"Caventra home path {value!r}", lineno)
            )
        if value in {"caventra-build", "caventra-test"} or value.endswith("caventra-build"):
            findings.append(
                BoundaryFinding(rel, "caventra_launcher_name", f"launcher name {value!r}", lineno)
            )
        if "LEGAL_POLICY_DECISION_REQUIRED" == value:
            findings.append(
                BoundaryFinding(rel, "legal_policy_gate", "legal-policy gate type", lineno)
            )
        if value in {"Q_RECORD_REQUIRED", "Q_RECORD"} or value.startswith("Q_RECORD"):
            findings.append(
                BoundaryFinding(rel, "caventra_q_record", f"Q-record coupling {value!r}", lineno)
            )
        lowered = value.lower()
        if any(token in lowered for token in ("divorce", "family-law", "family_law")):
            findings.append(
                BoundaryFinding(rel, "legal_domain_taxonomy", f"product-domain string {value!r}", lineno)
            )
        if "legal-intelligence-evaluation-loop" in lowered or "LEGAL_INTELLIGENCE" in value:
            findings.append(
                BoundaryFinding(rel, "legal_intelligence_seed", f"legal-intelligence seed {value!r}", lineno)
            )
        if value.startswith("apps/api/app/") and "legal" in lowered:
            findings.append(
                BoundaryFinding(rel, "caventra_app_path", f"product application path {value!r}", lineno)
            )
    if rel.startswith("bin/caventra-build"):
        findings.append(BoundaryFinding(rel, "caventra_launcher_name", "caventra-build launcher file", 1))
    return findings


def _getenv_keys(tree: ast.AST) -> list[tuple[int, str]]:
    keys: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = ""
        if isinstance(func, ast.Attribute) and func.attr in {"getenv", "get"}:
            name = func.attr
        elif isinstance(func, ast.Name) and func.id == "getenv":
            name = func.id
        if not name:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            keys.append((node.lineno, node.args[0].value))
    return keys


def evaluate_boundary(root: Path | None = None) -> tuple[list[BoundaryFinding], list[BoundaryFinding]]:
    raw = scan_coordinator_tree(root)
    allowed: list[BoundaryFinding] = []
    unexpected: list[BoundaryFinding] = []
    for finding in raw:
        if (finding.path, finding.rule_id) in DOCUMENTED_EXCEPTIONS:
            allowed.append(
                BoundaryFinding(
                    finding.path,
                    finding.rule_id,
                    finding.message,
                    finding.line,
                    exception=True,
                )
            )
        else:
            unexpected.append(finding)
    return allowed, unexpected


def format_failure(unexpected: list[BoundaryFinding]) -> str:
    lines = [
        "Open-source boundary canary failed. New Caventra/private coupling:",
    ]
    for item in unexpected:
        lines.append(f"  {item.path}:{item.line} [{item.rule_id}] {item.message}")
    lines.append(
        "Document a temporary exception in oss_boundary.DOCUMENTED_EXCEPTIONS "
        "only when the coupling is intentional and tracked for extraction."
    )
    return "\n".join(lines)
