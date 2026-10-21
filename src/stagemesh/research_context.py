"""Deterministic, read-only research briefs for implementation prompts."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import (
    CONTRACT_VERSION,
    BoundChangeContract,
    ChangeContract,
    canonical_contract_json,
    scope_classification,
)

MAX_RESEARCH_FILES = 80
MAX_TEST_HINTS = 40
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".stagemesh",
    "__pycache__",
    "dist",
    "node_modules",
}


def build_research_brief(project: Path | None, contract: ChangeContract | None) -> str:
    """Return a scoped, deterministic pre-edit research brief for a provider prompt.

    This is deliberately local and read-only. It gives coding agents a bounded map of
    the files to inspect first, without broadening the change scope.
    """
    if project is None or contract is None:
        return ""
    root = project.resolve()
    allowed = _matching_files(root, contract.allowed_files, limit=MAX_RESEARCH_FILES)
    tests = _test_hints(root, allowed, limit=MAX_TEST_HINTS)
    lines = [
        "Contextual research brief (read-only; complete before editing):",
        "- First inspect the existing code paths below and summarize the local pattern in your own scratch notes.",
        "- Use this brief to narrow context. It does not expand the change contract or authorize edits outside allowed files.",
        "- This brief is a starting map, not a read allowlist. Inspect directly relevant project context as needed, subject to project access rules; an edit outside allowed files still requires an audited scope expansion.",
    ]
    if allowed:
        lines.append("- In-scope existing files to inspect first:")
        lines.extend(f"  - {path}" for path in allowed)
    else:
        lines.append("- No existing files matched the allowed scope; inspect the nearest existing parent directories before editing.")
    if tests:
        lines.append("- Nearby tests or fixtures worth checking before coding:")
        lines.extend(f"  - {path}" for path in tests)
    lines.append("")
    return "\n".join(lines) + "\n"


def _matching_files(root: Path, patterns: tuple[str, ...], *, limit: int) -> list[str]:
    files: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        if any(_matches(rel, pattern) for pattern in patterns):
            files.append(rel)
            if len(files) >= limit:
                break
    return sorted(files)


def _test_hints(root: Path, allowed: list[str], *, limit: int) -> list[str]:
    if not allowed:
        return []
    stems = {Path(item).stem.lower() for item in allowed if Path(item).stem}
    hints: list[str] = []
    for path in _walk_files(root):
        rel = path.relative_to(root).as_posix()
        parts = set(path.parts)
        if "tests" not in parts and not Path(rel).name.startswith("test_"):
            continue
        name = Path(rel).stem.lower().removeprefix("test_")
        if name in stems or any(stem and stem in name for stem in stems):
            hints.append(rel)
            if len(hints) >= limit:
                break
    return sorted(hints)


def _walk_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").strip()
    if not normalized:
        return False
    if normalized == "**":
        return True
    if fnmatch.fnmatch(path, normalized):
        return True
    if normalized.endswith("/**"):
        return path.startswith(normalized[:-3].rstrip("/") + "/")
    return path == normalized


MAX_RESEARCH_EVIDENCE_BYTES = 256 * 1024
MAX_RESEARCH_TEXT = 4096
MAX_RESEARCH_BRIEF = 32 * 1024


@dataclass(frozen=True)
class ResearchFinding:
    path: str
    summary: str


@dataclass(frozen=True)
class ResearchEvidence:
    """Advisory observations, never authorization or independent review evidence.

    Identity is supplied by the caller's trusted binding; this module neither
    resolves Git refs nor attests that an inspector actually read the source.
    """

    baseline_sha: str
    contract_digest: str
    inspected_paths: tuple[str, ...]
    findings: tuple[ResearchFinding, ...]
    unresolved_scope_gaps: tuple[str, ...]
    provenance: tuple[str, ...]
    brief: str
    version: int = 1


def build_research_evidence(
    project: Path,
    bound: BoundChangeContract,
    *,
    inspected_paths: tuple[str, ...],
    findings: tuple[ResearchFinding, ...] = (),
    unresolved_scope_gaps: tuple[str, ...] = (),
    provenance: tuple[str, ...],
) -> ResearchEvidence:
    """Build bounded evidence without reading file contents or changing scope.

    Recorded paths must be in the bound edit scope. This does not restrict the
    broader read-only investigation permitted by the contextual brief. Missing
    context can be described in unresolved_scope_gaps without granting access.
    """
    baseline = _research_binding(bound)
    evidence = ResearchEvidence(
        baseline, bound.digest, inspected_paths, findings,
        unresolved_scope_gaps, provenance, build_research_brief(project, bound.contract),
    )
    _validate_research_evidence(evidence, project, bound)
    return evidence


def write_research_evidence(
    destination: Path, evidence: ResearchEvidence, *, project: Path, bound: BoundChangeContract,
) -> None:
    """Persist to a new caller-owned file outside the source tree; never overwrite.

    Storage selection and lifecycle integration belong to the caller. No default
    location, directory creation, model dispatch, or contract mutation occurs.
    """
    _validate_research_evidence(evidence, project, bound)
    target = _research_storage_path(destination, project)
    with target.open("xb") as stream:
        stream.write(_research_bytes(evidence))


def read_research_evidence(
    source: Path, *, project: Path, bound: BoundChangeContract,
) -> ResearchEvidence:
    """Load bounded JSON and reject stale identity and invalid recorded paths."""
    _research_binding(bound)
    target = _research_storage_path(source, project)
    with target.open("rb") as stream:
        raw = stream.read(MAX_RESEARCH_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ValueError("research evidence exceeds size limit")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "version", "baseline_sha", "contract_digest", "inspected_paths", "findings",
            "unresolved_scope_gaps", "provenance", "brief",
        }:
            raise ValueError("invalid research evidence fields")
        for field in ("inspected_paths", "findings", "unresolved_scope_gaps", "provenance"):
            if not isinstance(payload[field], list) or len(payload[field]) > MAX_RESEARCH_FILES:
                raise ValueError(f"invalid or oversized research {field}")
        payload["findings"] = tuple(ResearchFinding(**item) for item in payload["findings"])
        for field in ("inspected_paths", "unresolved_scope_gaps", "provenance"):
            payload[field] = tuple(payload[field])
        evidence = ResearchEvidence(**payload)
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid research evidence payload") from exc
    _validate_research_evidence(evidence, project, bound)
    return evidence


def _research_binding(bound: BoundChangeContract) -> str:
    if type(bound.version) is not int or bound.version != CONTRACT_VERSION:
        raise ValueError("unsupported research contract version")
    canonical = canonical_contract_json(bound.contract)
    if (canonical != bound.canonical_json
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != bound.digest):
        raise ValueError("research contract digest does not match binding")
    if not isinstance(bound.baseline_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", bound.baseline_sha
    ):
        raise ValueError("research requires a full baseline SHA")
    return bound.baseline_sha


def _research_text(value: str, field: str, limit: int = MAX_RESEARCH_TEXT) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid or oversized research {field}")


def _validate_research_evidence(
    evidence: ResearchEvidence, project: Path, bound: BoundChangeContract,
) -> None:
    baseline = _research_binding(bound)
    if type(evidence.version) is not int or evidence.version != 1:
        raise ValueError("unsupported research evidence version")
    if evidence.baseline_sha != baseline or evidence.contract_digest != bound.digest:
        raise ValueError("stale research identity: baseline or contract digest mismatch")
    for field in ("inspected_paths", "findings", "unresolved_scope_gaps", "provenance"):
        values = getattr(evidence, field)
        if not isinstance(values, tuple) or len(values) > MAX_RESEARCH_FILES:
            raise ValueError(f"invalid or oversized research {field}")
    if not evidence.provenance:
        raise ValueError("research provenance is required")
    for field in ("unresolved_scope_gaps", "provenance"):
        for value in getattr(evidence, field):
            _research_text(value, field)
    _research_text(evidence.brief, "brief", MAX_RESEARCH_BRIEF)
    root = project.resolve()
    for path in evidence.inspected_paths:
        _research_path(root, path, bound.contract)
    if len(set(evidence.inspected_paths)) != len(evidence.inspected_paths):
        raise ValueError("duplicate inspected paths")
    for finding in evidence.findings:
        if not isinstance(finding, ResearchFinding):
            raise TypeError("invalid research finding")
        if finding.path not in evidence.inspected_paths:
            raise ValueError("finding path must be inspected")
        _research_text(finding.summary, "finding summary")
    _research_bytes(evidence)


def _research_path(root: Path, path: str, contract: ChangeContract) -> None:
    _research_text(path, "path")
    if (any(char in path for char in "\\:*?[]")
            or any(ord(char) < 32 for char in path)
            or any(part in ("", ".", "..") or part.rstrip(" .") != part
                   for part in path.split("/"))):
        raise ValueError("research paths must be canonical relative paths")
    try:
        resolved = (root / path).resolve(strict=True)
        actual = resolved.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("research path must exist inside project scope") from exc
    if not resolved.is_file():
        raise ValueError("research path must be a file")
    classified = scope_classification(contract, [path, actual])
    if any(classified[key] for key in ("outside_allowed", "excluded", "forbidden", "protected")):
        raise ValueError("research path is outside contract scope")


def _research_bytes(evidence: ResearchEvidence) -> bytes:
    raw = json.dumps(asdict(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ValueError("research evidence exceeds size limit")
    return raw


def _research_storage_path(path: Path, project: Path) -> Path:
    target = path.resolve()
    if target.is_relative_to(project.resolve()):
        raise ValueError("research evidence storage must be outside the source project")
    return target


MAX_RESEARCH_PROMPT_FINDINGS = 20
MAX_RESEARCH_PROMPT_CHARS = 8000


class ResearchBlocked(Exception):
    """Implementation must not start; `reason` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def research_request(bound: BoundChangeContract) -> str:
    """Prompt for the read-only researcher; it must echo the identity it researched against."""
    return (
        "StageMesh research stage (read-only). Do NOT edit, create, delete or stage any file and do not run git write commands.\n"
        "Inspect the code the change contract needs and reply with ONLY one JSON object with keys: "
        "baseline_sha, contract_digest, inspected_paths (list of existing in-scope file paths), "
        "findings (list of {path, summary}; path must be in inspected_paths), "
        "unresolved_scope_gaps (list of strings; required context that is missing or out of scope), "
        "provenance (non-empty list of strings).\n"
        f"Use baseline_sha={bound.baseline_sha} and contract_digest={bound.digest}.\n\n"
        f"{canonical_contract_json(bound.contract)}\n"
    )


def parse_research_report(raw: str, project: Path, bound: BoundChangeContract) -> ResearchEvidence:
    """Validate a researcher's reply against the bound identity; unresolved scope gaps block."""
    if len(raw.encode("utf-8", "replace")) > MAX_RESEARCH_EVIDENCE_BYTES:
        raise ResearchBlocked("research_invalid", "report exceeds size limit")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        payload = json.loads(raw[start:end + 1]) if 0 <= start < end else None
        if not isinstance(payload, dict) or set(payload) != {
            "baseline_sha", "contract_digest", "inspected_paths", "findings",
            "unresolved_scope_gaps", "provenance",
        }:
            raise ValueError("invalid research report fields")
        for field in ("inspected_paths", "findings", "unresolved_scope_gaps", "provenance"):
            if not isinstance(payload[field], list):
                raise ValueError(f"invalid research {field}")
        evidence = ResearchEvidence(
            payload["baseline_sha"], payload["contract_digest"], tuple(payload["inspected_paths"]),
            tuple(ResearchFinding(**item) for item in payload["findings"]),
            tuple(payload["unresolved_scope_gaps"]), tuple(payload["provenance"]),
            build_research_brief(project, bound.contract),
        )
        _validate_research_evidence(evidence, project, bound)
    except (ValueError, TypeError, RecursionError) as exc:
        reason = "research_stale" if "stale" in str(exc) else "research_invalid"
        raise ResearchBlocked(reason, str(exc)[:200]) from exc
    if evidence.unresolved_scope_gaps:
        raise ResearchBlocked("research_unresolved_scope", "; ".join(evidence.unresolved_scope_gaps)[:200])
    return evidence


def research_findings_prompt(evidence: ResearchEvidence) -> str:
    """Bounded, advisory findings for the implementation prompt."""
    lines = ["Research findings (advisory, from a read-only researcher; they do not expand the change contract):"]
    lines.extend(f"- {f.path}: {f.summary}" for f in evidence.findings[:MAX_RESEARCH_PROMPT_FINDINGS])
    text = "\n".join(lines)
    if len(text) > MAX_RESEARCH_PROMPT_CHARS:
        text = text[:MAX_RESEARCH_PROMPT_CHARS] + "\n[research findings truncated]"
    return text + "\n\n"


def source_snapshot(root: Path) -> str:
    """Digest of HEAD, the status, and the content of every changed or untracked file."""
    import subprocess

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "-z", "-uall")
    digest = hashlib.sha256(head + b"\0" + status)
    for entry in status.split(b"\0"):
        if len(entry) > 3:
            target = root / entry[3:].decode("utf-8", "surrogateescape")
            if target.is_file():
                digest.update(target.read_bytes())
    return digest.hexdigest()
